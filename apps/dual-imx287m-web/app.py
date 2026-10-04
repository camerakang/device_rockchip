#!/usr/bin/env python3
"""LubanCat4 dual IMX287M test console, stdlib HTTP + Pillow + V4L2 worker."""
import argparse
import collections
import importlib.machinery
import importlib.util
import io
import json
import math
import signal
import socket
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps

BASE = Path(__file__).resolve().parent
HEADER = struct.Struct("<4sIIIQQQddII")
DEFAULT = dict(mode="hardware", format="raw12", fps=319.4, exposure_us=1000,
               preview_fps=20, auto_contrast=True)
LIMITS = dict(raw8=523, raw10=437, raw12=320)


def validate_config(body):
    if not isinstance(body, dict) or set(body) - set(DEFAULT):
        raise ValueError("无效的配置字段")
    config = dict(DEFAULT, **body)
    if not isinstance(config["mode"], str) or not isinstance(config["format"], str) or config["mode"] not in ("hardware", "free") or config["format"] not in LIMITS:
        raise ValueError("请选择有效的采集模式和位深")
    for key in ("fps", "exposure_us", "preview_fps"):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} 必须是有限数值")
    if not isinstance(config["auto_contrast"], bool):
        raise ValueError("auto_contrast 必须是布尔值")
    limit = LIMITS[config["format"]]
    if not 1 <= config["fps"] <= limit:
        raise ValueError(f"当前位深的帧率范围为 1–{limit}")
    if config["format"] == "raw12" and config["mode"] == "hardware" and config["fps"] > 319.4:
        raise ValueError("本机 RAW12 外同步请使用 ≤319.4 Hz；320 Hz 实测会降到160 fps")
    if not 1 <= config["preview_fps"] <= 30:
        raise ValueError("网页预览帧率范围为 1–30")
    camera_fps = min(limit, math.ceil(config["fps"] * (2 if config["mode"] == "hardware" else 1)))
    if int(config["exposure_us"]) != config["exposure_us"] or not 1 <= config["exposure_us"] < 1_000_000 / camera_fps:
        raise ValueError(f"曝光需为正整数，且小于 {1_000_000 / camera_fps:.0f} µs")
    config["exposure_us"] = int(config["exposure_us"])
    config["camera_fps"] = camera_fps
    return config


def load_helper(path):
    loader = importlib.machinery.SourceFileLoader("camera_config", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def read_exact(stream, size):
    parts = []
    remaining = size
    while remaining:
        part = stream.read(remaining)
        if not part:
            raise EOFError("Capture worker closed its output")
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


class Engine:
    def __init__(self, configure_tool, pwm_tool):
        self.configure_tool, self.pwm_tool = str(configure_tool), str(pwm_tool)
        self.operation = threading.RLock()
        self.condition = threading.Condition(threading.RLock())
        self.workers, self.threads = [], []
        self.config = validate_config({})
        self.phase, self.error, self.generation = "stopped", "", 0
        self.logs = collections.deque(maxlen=80)
        self.camera_nodes = {}
        self.frames = [self.empty_frame(), self.empty_frame()]
        self.shutdown_event = threading.Event()
        self.monitor = threading.Thread(target=self.watch, daemon=True)
        self.monitor.start()

    @staticmethod
    def empty_frame():
        return dict(jpeg=None, revision=0, sequence=None, frames=0, dropped=0,
                    timestamp_ns=0, fps=0, mean=0, bits=12, last_seen=0,
                    previews=collections.deque(maxlen=40), trigger_count=0, trigger_lost=0)

    def log(self, message):
        with self.condition:
            self.logs.append(f"{time.strftime('%H:%M:%S')} {message}")

    def command(self, command, timeout=25, log_output=True):
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or str(command))[-1600:].strip())
        if result.stdout and log_output:
            self.log(result.stdout.strip().splitlines()[-1])
        return result.stdout

    def stop(self):
        with self.operation:
            with self.condition:
                self.phase = "stopping"
            pwm_error = None
            try:
                self.command([self.pwm_tool, "stop"], timeout=5)
            except Exception as exc:
                pwm_error = exc
            for worker in self.workers:
                if worker.poll() is None:
                    worker.terminate()
            for worker in self.workers:
                try:
                    worker.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=3)
            for thread in self.threads:
                thread.join(timeout=2)
            self.workers, self.threads = [], []
            with self.condition:
                self.phase = "stopped"
                self.condition.notify_all()
            if pwm_error:
                raise RuntimeError(f"关闭 PWM 失败：{pwm_error}")

    def start(self, body):
        config = validate_config(body)
        with self.operation:
            self.stop()
            with self.condition:
                self.phase, self.error = "starting", ""
                self.config = config
                self.generation += 1
                generation = self.generation
                self.frames = [self.empty_frame(), self.empty_frame()]
            try:
                self.command([self.configure_tool, "--mode", config["mode"], "--format", config["format"],
                              "--fps", str(config["camera_fps"]), "--exposure-us", str(config["exposure_us"]),
                              "--edge", "rising"])
                helper = load_helper(self.configure_tool)
                self.camera_nodes = helper.discover()
                events = []
                for index in (0, 1):
                    camera = self.camera_nodes[index]
                    if config["mode"] == "hardware":
                        self.command(["i2ctransfer", "-f", "-y", str(camera["bus"]), "w6@0x3b",
                                      "0x04", "0x18", "0x00", "0x00", "0x00", "0x01"])
                    worker = subprocess.Popen([str(BASE / "capture-worker"), camera["video"],
                                               str(config["preview_fps"])], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, bufsize=0)
                    self.workers.append(worker)
                    ready = threading.Event()
                    events.append(ready)
                    for target, args in ((self.consume, (index, worker, generation)),
                                         (self.stderr, (index, worker, ready))):
                        thread = threading.Thread(target=target, args=args, daemon=True)
                        thread.start()
                        self.threads.append(thread)
                for index, ready in enumerate(events):
                    if not ready.wait(timeout=6) or self.workers[index].poll() is not None:
                        raise RuntimeError(f"CAM{index} 采集进程无法启动，查看运行日志")
                if config["mode"] == "hardware":
                    self.command([self.pwm_tool, "start", "--fps", str(config["fps"]), "--pulse-us", "100"])
                with self.condition:
                    self.phase = "running"
                self.log(f"两路采集已启动：{config['format'].upper()} / {config['fps']} fps")
            except Exception as exc:
                try:
                    self.stop()
                except Exception as stop_error:
                    self.log(str(stop_error))
                with self.condition:
                    self.phase, self.error = "error", str(exc)
                self.log(str(exc))
                raise

    def stderr(self, index, worker, ready):
        for data in iter(worker.stderr.readline, b""):
            line = data.decode("utf-8", errors="replace").strip()
            self.log(f"CAM{index} {line}")
            if line.startswith("READY "):
                ready.set()
        worker.stderr.close()

    def consume(self, index, worker, generation):
        try:
            while True:
                magic, width, height, sequence, frames, dropped, timestamp, fps, mean, size, bits = HEADER.unpack(
                    read_exact(worker.stdout, HEADER.size))
                if magic != b"FRM1" or (width, height) != (720, 544) or size != width * height or bits not in (8, 10, 12):
                    raise RuntimeError("Invalid preview frame header")
                raw = read_exact(worker.stdout, size)
                image = Image.frombytes("L", (width, height), raw)
                if self.config["auto_contrast"]:
                    image = ImageOps.autocontrast(image, cutoff=1)
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=80)
                with self.condition:
                    if generation != self.generation:
                        continue
                    frame = self.frames[index]
                    frame.update(jpeg=output.getvalue(), sequence=sequence, frames=frames, dropped=dropped,
                                 timestamp_ns=timestamp, fps=fps, mean=mean, bits=bits, last_seen=time.monotonic())
                    frame["revision"] += 1
                    frame["previews"].append(frame["last_seen"])
                    self.condition.notify_all()
        except EOFError:
            pass
        except Exception as exc:
            self.log(f"CAM{index} 预览错误：{exc}")
            if worker.poll() is None:
                worker.terminate()
        finally:
            worker.stdout.close()

    def watch(self):
        next_counters = 0
        while not self.shutdown_event.wait(0.5):
            if not self.operation.acquire(blocking=False):
                continue
            try:
                if self.phase != "running":
                    continue
                for index, worker in enumerate(self.workers):
                    if worker.poll() is not None:
                        self.stop()
                        with self.condition:
                            self.phase, self.error = "error", f"CAM{index} 采集进程退出，已停止共同触发"
                        break
                if self.phase != "running" or self.config["mode"] != "hardware" or time.monotonic() < next_counters:
                    continue
                next_counters = time.monotonic() + 2
                for index, camera in self.camera_nodes.items():
                    try:
                        reply = self.command(["i2ctransfer", "-f", "-y", str(camera["bus"]), "w2@0x3b",
                                              "0x04", "0x18", "r4"], timeout=2, log_output=False)
                        count = int.from_bytes(bytes(int(x, 16) for x in reply.split()), "big")
                        with self.condition:
                            self.frames[index]["trigger_count"] = count & 65535
                            self.frames[index]["trigger_lost"] = count >> 16
                    except Exception as exc:
                        self.log(f"CAM{index} 触发统计读取失败：{exc}")
            finally:
                self.operation.release()

    def status(self):
        with self.condition:
            cameras = []
            for index, frame in enumerate(self.frames):
                data = {key: value for key, value in frame.items() if key not in ("jpeg", "previews", "last_seen")}
                age = time.monotonic() - frame["last_seen"] if frame["last_seen"] else None
                stamps = frame["previews"]
                data["preview_fps"] = (len(stamps) - 1) / (stamps[-1] - stamps[0]) if len(stamps) > 1 and age < 2 else 0
                data["fps"] = frame["fps"] if self.phase == "running" and age is not None and age < 2 else 0
                data.update(index=index, age=age, video=self.camera_nodes.get(index, {}).get("video", ""))
                cameras.append(data)
            paired = self.frames[0]["sequence"] is not None and self.frames[0]["sequence"] == self.frames[1]["sequence"]
            skew = (self.frames[1]["timestamp_ns"] - self.frames[0]["timestamp_ns"]) / 1000 if paired else None
            return dict(phase=self.phase, error=self.error, config=self.config, cameras=cameras,
                        paired_eof_us=skew, logs=list(self.logs), hostname=socket.gethostname())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_body(self, body, content_type, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def json(self, value, status=200):
        self.send_body(json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), "application/json; charset=utf-8", status)

    def do_GET(self):
        path = urlsplit(self.path).path
        engine = self.server.engine
        try:
            if path == "/api/status":
                self.json(engine.status())
            elif path in ("/", "/app.js", "/style.css"):
                name, kind = {"/": ("index.html", "text/html; charset=utf-8"),
                              "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                              "/style.css": ("style.css", "text/css; charset=utf-8")}[path]
                self.send_body((BASE / "static" / name).read_bytes(), kind)
            elif path in ("/snapshot/0.jpg", "/snapshot/1.jpg"):
                index = int(path.split("/")[2][0])
                with engine.condition:
                    jpeg = engine.frames[index]["jpeg"]
                if jpeg is None:
                    self.json(dict(error="尚未收到预览画面"), 503)
                else:
                    extra = {"Content-Disposition": f'attachment; filename="cam{index}-{time.strftime("%Y%m%d-%H%M%S")}.jpg"'} if urlsplit(self.path).query == "download=1" else {}
                    self.send_body(jpeg, "image/jpeg", extra=extra)
            elif path in ("/stream/0.mjpg", "/stream/1.mjpg"):
                self.stream(int(path.split("/")[2][0]))
            else:
                self.json(dict(error="Not found"), 404)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def stream(self, index):
        engine = self.server.engine
        self.connection.settimeout(5)
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        while not engine.shutdown_event.is_set():
            with engine.condition:
                engine.condition.wait_for(lambda: engine.shutdown_event.is_set() or
                                          (engine.frames[index]["jpeg"] is not None and
                                           (engine.generation, engine.frames[index]["revision"]) != last), timeout=2)
                frame = engine.frames[index]
                current = (engine.generation, frame["revision"])
                if frame["jpeg"] is None or current == last:
                    continue
                jpeg = frame["jpeg"]
                last = current
            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                             str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
            self.wfile.flush()

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            self.close_connection = True
            self.json(dict(error="Cross-origin control is disabled"), 403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                self.close_connection = True
                raise ValueError("无效的请求长度")
            body = json.loads(self.rfile.read(size))
            path = urlsplit(self.path).path
            if path == "/api/start":
                self.server.engine.start(body)
            elif path == "/api/stop":
                self.server.engine.stop()
            elif path == "/api/preview":
                if not isinstance(body, dict) or set(body) != {"auto_contrast"} or not isinstance(body["auto_contrast"], bool):
                    raise ValueError("无效的预览设置")
                with self.server.engine.condition:
                    self.server.engine.config["auto_contrast"] = body["auto_contrast"]
            else:
                self.json(dict(error="Not found"), 404)
                return
            self.json(dict(ok=True, status=self.server.engine.status()))
        except (ValueError, json.JSONDecodeError) as exc:
            self.json(dict(error=str(exc)), 400)
        except Exception as exc:
            self.json(dict(error=str(exc)), 500)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--configure-tool", type=Path, default=Path("/usr/local/bin/configure-double-imx287m"))
    parser.add_argument("--pwm-tool", type=Path, default=Path("/usr/local/bin/imx287m-trigger-pwm"))
    parser.add_argument("--no-autostart", action="store_true")
    args = parser.parse_args()
    engine = Engine(args.configure_tool, args.pwm_tool)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.engine = engine
    def shutdown(*unused):
        engine.shutdown_event.set()
        with engine.condition:
            engine.condition.notify_all()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    if not args.no_autostart:
        try:
            engine.start({})
        except Exception as exc:
            print(f"Camera startup failed; use web controls to retry: {exc}", flush=True)
    print(f"Dual IMX287M console listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        engine.shutdown_event.set()
        try:
            engine.stop()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()

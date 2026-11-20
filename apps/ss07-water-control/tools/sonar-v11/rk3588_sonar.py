"""Read V1.1 sonar JSON lines. Usage: python3 rk3588_sonar.py /dev/ttyUSB0"""
import argparse
import json
import sys
import time

import serial


def decode(line):
    if not line.endswith(b"\n"):
        raise ValueError("partial frame")
    f = json.loads(line)
    if not isinstance(f, dict) or f.get("v") != 1 or f.get("ch") not in range(1, 6):
        raise ValueError("frame header")
    if not all(type(f.get(k)) is int for k in ("seq", "ms", "ch", "id")):
        raise ValueError("missing integer")
    if f.get("state") not in ("ok", "no_echo", "no_burst", "no_adc_pin", "spi_error",
                              "wrong_id", "config_error", "adc_error", "adc_init_error",
                              "timing_error", "clock_error"):
        raise ValueError("unknown state")
    if "mm" not in f or (f["state"] == "ok" and
                        (type(f["mm"]) is not int or not 0 <= f["mm"] <= 20000)):
        raise ValueError("distance")
    if f["state"] != "ok" and f["mm"] is not None:
        raise ValueError("unexpected distance")
    return f


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("port")
    p.add_argument("--seconds", type=float, default=0, help="0: run until interrupted")
    a = p.parse_args()
    start = time.monotonic()
    last = dict.fromkeys(range(1, 6), start)
    offline = set()
    dev = serial.Serial(port=None, baudrate=115200, timeout=.1)
    dev.port = a.port
    dev.dtr = dev.rts = False
    retry = start
    try:
        while not a.seconds or time.monotonic() - start < a.seconds:
            now = time.monotonic()
            for ch, stamp in last.items():
                if now - stamp >= 1.5 and ch not in offline:
                    offline.add(ch)
                    print(json.dumps(dict(event="offline", ch=ch, mm=None)), flush=True)
            try:
                if not dev.is_open:
                    if now < retry:
                        time.sleep(.05)
                        continue
                    dev.open()
                data = dev.read_until(b"\n", 256)
            except (serial.SerialException, OSError) as exc:
                dev.close()
                retry = now + 1
                print(str(exc), file=sys.stderr)
                continue
            try:
                frame = decode(data)
            except ValueError:
                continue
            last[frame["ch"]] = time.monotonic()
            offline.discard(frame["ch"])
            print(json.dumps(frame, separators=(",", ":")), flush=True)
    finally:
        dev.close()


if __name__ == "__main__":
    try:
        main()
    except serial.SerialException as error:
        sys.exit(str(error))

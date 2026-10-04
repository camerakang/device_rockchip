#!/usr/bin/env python3
"""Configure two MV-MIPI-IMX287M modules on LubanCat-4 CAM0/CAM1.

Run before starting capture, with the external trigger source stopped.
The tool discovers device nodes; it does not start capture or generate pulses.
Register definitions follow VEYE rk35xx_veye_bsp e4516077e1fe.
"""

import argparse
import glob
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

FORMATS = {
    "raw8": ("Y8_1X8", "GREY"),
    "raw10": ("Y10_1X10", "Y10 "),
    "raw12": ("Y12_1X12", "Y12 "),
}


def run(command):
    return subprocess.check_output(command, text=True).strip()


def entities(topology):
    """Split media-ctl topology into entity names and their device nodes."""
    result = {}
    current = None
    for line in topology.splitlines():
        match = re.match(r"\s*- entity \d+: (.+) \(\d+ pads?, \d+ links?\)", line)
        if match:
            current = match.group(1)
            result[current] = None
        elif current and "device node name " in line:
            result[current] = line.split("device node name ", 1)[1].strip()
    return result


def discover(sysfs=Path("/sys/bus/i2c/devices")):
    cameras = {}
    for media in sorted(glob.glob("/dev/media*")):
        topology = entities(run(["media-ctl", "-d", media, "-p"]))
        for index, bus in enumerate((1, 5)):
            sensor = f"m{index:02d}_b_mvcam {bus}-003b"
            if sensor not in topology:
                continue
            model = (sysfs / f"{bus}-003b/veye_mvcam/camera_model").read_text().strip()
            if model != "MV-MIPI-IMX287M":
                raise RuntimeError(f"CAM{index}: unexpected camera {model!r}")
            subdev = topology[sensor]
            video = topology.get("stream_cif_mipi_id0")
            if not subdev or not video:
                raise RuntimeError(f"CAM{index}: incomplete CIF topology on {media}")
            if index in cameras:
                raise RuntimeError(f"CAM{index}: ambiguous media graph")
            cameras[index] = dict(bus=bus, sensor=sensor, media=media,
                                  subdev=subdev, video=video)
    if set(cameras) != {0, 1}:
        raise RuntimeError("Both CAM0 (I2C1) and CAM1 (I2C5) must be detected")
    if cameras[0]["video"] == cameras[1]["video"]:
        raise RuntimeError("The two cameras cannot share one capture device")
    return cameras


def register_write(bus, address, value, dry_run):
    # VEYE uses a 16-bit register address and a 32-bit big-endian payload.
    data = address.to_bytes(2, "big") + value.to_bytes(4, "big")
    command = ["i2ctransfer", "-f", "-y", str(bus), "w6@0x3b"]
    command += [f"0x{byte:02x}" for byte in data]
    execute(command, dry_run)
    if dry_run:
        return
    time.sleep(0.02)
    reply = run(["i2ctransfer", "-f", "-y", str(bus), "w2@0x3b",
                 f"0x{address >> 8:02x}", f"0x{address & 255:02x}", "r4"])
    actual = int.from_bytes(bytes(int(part, 16) for part in reply.split()), "big")
    if address == 0x0C10:
        # Sensor exposure is quantized; VEYE recommends reading back the
        # effective value instead of assuming the requested value is exact.
        if actual == 0:
            raise RuntimeError(f"I2C{bus}: invalid exposure readback")
        print(f"I2C{bus}: effective exposure {actual} us")
    elif actual != value:
        raise RuntimeError(f"I2C{bus} register 0x{address:04x}: "
                           f"requested {value}, read back {actual}")
    return actual


def execute(command, dry_run):
    print(shlex.join(command), flush=True)
    if not dry_run:
        subprocess.run(command, check=True)


def configure(cameras, args):
    # Configure both cameras while acquisition is stopped; V4L2 owns
    # image format, ROI, frame rate, and trigger mode/source state.
    exposures = []
    mbus, fourcc = FORMATS[args.format]
    for camera in cameras.values():
        execute(["v4l2-ctl", "-d", camera["subdev"],
                 "--set-ctrl", "trigger_mode=0,roi_x=0,roi_y=0"], args.dry_run)
        fmt = f'"{camera["sensor"]}":0[fmt:{mbus}/720x544@1/{args.fps} field:none]'
        execute(["media-ctl", "-d", camera["media"], "--set-v4l2", fmt], args.dry_run)
        # These camera-specific properties are not represented by the
        # vendor V4L2 controls. Do not change format/ROI/FPS with raw I2C.
        registers = [(0x0C04, 0), (0x0C10, args.exposure_us)]  # manual exposure
        if args.mode == "hardware":
            registers += [(0x040C, 1), (0x1000, 0),  # one frame, no delay
                          (0x1004, 0 if args.edge == "rising" else 1),
                          (0x1008, 0), (0x1010, 0)]  # no filter/exposure delay
        for address, value in registers:
            actual = register_write(camera["bus"], address, value, args.dry_run)
            if address == 0x0C10:
                exposures.append(actual)
    if not args.dry_run and len(set(exposures)) != 1:
        raise RuntimeError(f"Effective exposure differs between cameras: {exposures}")
    for index, camera in cameras.items():
        mode = 1 if args.mode == "hardware" else 0
        execute(["v4l2-ctl", "-d", camera["subdev"], "--set-ctrl",
                 f"trigger_src=1,trigger_mode={mode}"], args.dry_run)
        execute(["v4l2-ctl", "-d", camera["video"], "--set-fmt-video",
                 f"width=720,height=544,pixelformat={fourcc}"], args.dry_run)
        capture = ["v4l2-ctl", "-d", camera["video"], "--stream-mmap=4",
                   "--stream-count=100", f"--stream-to=cam{index}.raw"]
        print(f"CAM{index}: media={camera['media']}, video={camera['video']}, "
              f"subdev={camera['subdev']}")
        print("Capture in a separate terminal: " + shlex.join(capture))
    if args.mode == "hardware":
        print("Start both capture processes, then enable the common 3.3 V trigger source.")
        print("--fps sets camera timing; the external pulse rate sets the capture rate.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("free", "hardware"), required=True)
    parser.add_argument("--format", choices=tuple(FORMATS), default="raw8",
                        help="MIPI monochrome bit depth (default: raw8)")
    parser.add_argument("--fps", type=int, default=30,
                        help="camera timing limit; start validation at 30 fps")
    parser.add_argument("--exposure-us", type=int, default=1000)
    parser.add_argument("--edge", choices=("rising", "falling"), default="rising")
    parser.add_argument("--dry-run", action="store_true", help="detect devices and print writes")
    args = parser.parse_args()
    if not 1 <= args.fps <= 530:
        parser.error("--fps must be in [1, 530]; actual limits depend on firmware")
    if not 1 <= args.exposure_us < 1_000_000 / args.fps:
        parser.error("exposure must be positive and shorter than the requested frame period")
    for program in ("media-ctl", "v4l2-ctl", "i2ctransfer"):
        if not shutil.which(program):
            parser.error(f"{program} is missing; install v4l-utils and i2c-tools")
    try:
        configure(discover(), args)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Configuration failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Read or update one V1.1 field parameter on a single channel."""
import argparse
import json
import sys
import time

import serial

KEYS = ("thr_mv", "blank_us", "range_mm", "sound_mps", "offset_mm", "width", "gain_x10")


def request(dev, command, match):
    dev.write((command + "\n").encode("ascii"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            msg = json.loads(dev.read_until(b"\n", 256))
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("ack") is False:
            raise ValueError("board rejected command; check firmware, limits and NVS")
        if match(msg):
            return msg
    raise TimeoutError("no reply; check port, firmware version and serial ownership")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("port")
    p.add_argument("channel", type=int, choices=range(1, 6))
    p.add_argument("key", nargs="?", choices=KEYS)
    p.add_argument("value", nargs="?", type=int)
    a = p.parse_args()
    if (a.key is None) != (a.value is None):
        p.error("provide both key and integer value, or neither")
    with serial.Serial(port=None, baudrate=115200, timeout=.15, write_timeout=1) as dev:
        dev.port = a.port
        dev.dtr = dev.rts = False
        dev.open()
        dev.reset_input_buffer()

        def readback():
            return request(dev, f"get {a.channel}", lambda m: m.get("params") == a.channel
                           and all(type(m.get(k)) is int for k in KEYS))

        result = readback()
        if a.key is not None and result[a.key] != a.value:
            request(dev, f"set {a.channel} {a.key} {a.value}", lambda m: m.get("ack") is True)
            result = readback()
            if result[a.key] != a.value:
                raise ValueError("readback mismatch")
        print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, TimeoutError, serial.SerialException) as exc:
        sys.exit(str(exc))

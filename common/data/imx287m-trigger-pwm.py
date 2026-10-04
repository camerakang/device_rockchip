#!/usr/bin/env python3
"""Control LubanCat4 J9 physical pin 12 (PWM14_M2) camera trigger pulses.

Start both camera capture processes before invoking start. No automatic output
is enabled at boot. The two ADP-MV1 J3-3 inputs must share this output and GND.
"""

import argparse
import math
import sys
import time
from pathlib import Path


def find_chip(root=Path("/sys/class/pwm")):
    matches = []
    for chip in root.glob("pwmchip*"):
        node = chip / "device/of_node/name"
        if node.exists() and node.read_bytes().rstrip(b"\0") == b"pwm":
            if (chip / "device/of_node").resolve().name == "pwm@febf0020":
                matches.append(chip)
        elif (chip / "device").resolve().name == "febf0020.pwm":
            matches.append(chip)
    if len(matches) != 1:
        raise RuntimeError("PWM14 not found uniquely; enable the PWM14_M2 overlay")
    return matches[0]


def timing(fps, pulse_us):
    if not math.isfinite(fps) or not 1 <= fps <= 100:
        raise ValueError("--fps must be between 1 and 100 for initial bring-up")
    if not math.isfinite(pulse_us) or pulse_us <= 0:
        raise ValueError("--pulse-us must be positive")
    period = round(1_000_000_000 / fps)
    duty = round(pulse_us * 1000)
    if not 0 < duty < period:
        raise ValueError("Pulse width must be shorter than the trigger period")
    return period, duty


def control(chip, action, period, duty):
    pwm = chip / "pwm0"
    if action == "stop":
        if pwm.exists() and (pwm / "enable").read_text().strip() == "1":
            (pwm / "enable").write_text("0")
        return
    if not pwm.exists():
        (chip / "export").write_text("0")
        for _ in range(100):
            if pwm.exists():
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("Timed out waiting for PWM14 export")
    if (pwm / "enable").read_text().strip() == "1":
        (pwm / "enable").write_text("0")
    try:
        # Linux rejects any PWM apply with a zero period, even disabling an
        # already-disabled channel or setting duty to zero. Initialize period
        # first on a freshly exported channel; existing channels must clear
        # their duty before shortening the period.
        if int((pwm / "period").read_text()) == 0:
            (pwm / "period").write_text(str(period))
        (pwm / "duty_cycle").write_text("0")
        (pwm / "period").write_text(str(period))
        (pwm / "polarity").write_text("normal")
        (pwm / "duty_cycle").write_text(str(duty))
        (pwm / "enable").write_text("1")
    except Exception:
        if (pwm / "enable").read_text().strip() == "1":
            (pwm / "enable").write_text("0")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--fps", type=float, default=30)
    parser.add_argument("--pulse-us", type=float, default=100)
    args = parser.parse_args()
    try:
        period, duty = timing(args.fps, args.pulse_us)
        chip = find_chip()
        control(chip, args.action, period, duty)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"PWM trigger error: {exc}", file=sys.stderr)
        return 1
    print(f"J9 pin 12: {args.action}; PWM={chip.name}; "
          f"frequency={1_000_000_000 / period:.6f} Hz; pulse={duty / 1000:g} us")
    return 0


if __name__ == "__main__":
    sys.exit(main())

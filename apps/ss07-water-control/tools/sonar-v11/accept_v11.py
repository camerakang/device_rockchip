"""Bounded serial acceptance. Without --target it verifies acquisition, not range."""
import argparse
from collections import Counter
import json
import time

import serial
from rk3588_sonar import decode


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("port")
    p.add_argument("--seconds", type=int, default=60)
    p.add_argument("--log", required=True, help="raw serial capture")
    p.add_argument("--gpio4-rework", action="store_true")
    p.add_argument("--target", action="append", default=[], metavar="CH:MM")
    p.add_argument("--tolerance-mm", type=int, default=100)
    a = p.parse_args()
    target = dict(tuple(map(int, item.split(":"))) for item in a.target)
    counts = {ch: Counter() for ch in range(1, 6)}
    misses = Counter()
    bad = gaps = 0
    prev = ch_prev = None
    with serial.Serial(port=None, baudrate=115200, timeout=.4) as s, open(a.log, "xb") as log:
        s.dtr = s.rts = False
        s.port = a.port
        s.open()
        s.reset_input_buffer()
        s.readline()  # first line may be partial
        end = time.monotonic() + a.seconds
        while time.monotonic() < end:
            line = s.read_until(b"\n", 256)
            if not line:
                continue
            log.write(line)
            try:
                f = decode(line)
            except ValueError:
                bad += 1
                continue
            ch, seq = f["ch"], f["seq"]
            if prev is not None and (seq != (prev+1) % 2**32 or ch != ch_prev % 5 + 1):
                gaps += 1
            prev, ch_prev = seq, ch
            counts[ch][f["state"]] += 1
            allowed = {"no_adc_pin"} if ch == 1 and not a.gpio4_rework else {"ok", "no_echo"}
            if f["id"] != 185 or f["state"] not in allowed:
                bad += 1
            if ch in target and (f["mm"] is None or abs(f["mm"]-target[ch]) > a.tolerance_mm):
                misses[ch] += 1
    # Allow a partial rotation at the start/end; reject silence and low throughput.
    minimum = max(1, int(a.seconds*1.8)-1)
    passed = not (bad or gaps or misses) and all(sum(s.values()) >= minimum for s in counts.values())
    print(json.dumps(dict(counts=counts, bad=bad, gaps=gaps, target_misses=misses,
                          stream_pass=passed), indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Bounded completion notifications; this helper never opens a recording file."""
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path


def main():
    destination = os.environ['SS07_RECORD_SOCKET']
    event = dict(schema_version=1, session_id=os.environ['SS07_RECORD_SESSION'],
                 event=sys.argv[1], channel=os.environ['MTX_PATH'], path=os.environ['MTX_SEGMENT_PATH'],
                 duration=os.environ.get('MTX_SEGMENT_DURATION'),
                 observed_monotonic_us=str(time.monotonic_ns() // 1000))
    raw = json.dumps(event, separators=(',', ':')).encode()
    try:
        if len(raw) > 2048 or event['event'] not in ('create', 'complete'):
            raise ValueError('invalid notification')
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.sendto(raw, destination)
        return 0
    except (OSError, ValueError):
        # Fixed-size failure marker makes lost metadata observable without an
        # unbounded spool. It causes recording to stop; monitoring is independent.
        directory = Path(destination).parent
        try:
            fd, temporary = tempfile.mkstemp(prefix='.failure-', dir=str(directory))
            with os.fdopen(fd, 'w') as stream:
                json.dump({'reason': 'record_notification_lost',
                           'observed_monotonic_us': event['observed_monotonic_us']}, stream)
            os.replace(temporary, str(directory / 'hook-failure.json'))
        except OSError:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())

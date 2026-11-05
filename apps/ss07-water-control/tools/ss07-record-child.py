#!/usr/bin/env python3
"""Set Linux parent-death signalling before exec, without Python preexec_fn."""
import ctypes
import os
import signal
import sys


parent = int(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
    raise OSError(ctypes.get_errno(), 'parent-death signal failed')
if os.getppid() != parent:
    sys.exit(1)
os.execv(sys.argv[2], sys.argv[2:])

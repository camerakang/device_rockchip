"""Normative, dependency-free v0x22 datagram codec.

One UDP datagram carries exactly one frame; there is no byte-stream framing.
"""
from dataclasses import dataclass
from enum import IntEnum
import struct

VERSION = 0x22
MAX_PAYLOAD = 240
HEADER_SIZE = 37
MAX_FRAME = HEADER_SIZE + MAX_PAYLOAD + 2
# dst_uid=0 addresses all nodes through the fixed multicast group.
ALL_NODES = 0
HEADER = struct.Struct('<2sBBBQQIHQH')

class Kind(IntEnum):
    REQUEST = 1
    RESPONSE = 2
    ACK = 3
    ERROR = 4

class Command(IntEnum):
    HELLO = 1
    DISCOVER = 2
    INFO = 4
    CAPS = 5
    SESSION_OPEN = 6
    SET_SOURCE = 0x10
    HEARTBEAT = 0x11
    WRITE_TARGETS = 0x20
    EXECUTE_ACTION = 0x21
    ACTION_STATUS = 0x22
    STATUS = 0x30
    STOP = 0x40
    ESTOP = 0x41
    RESET_FAULT = 0x42
    READ_CONFIG = 0x50
    WRITE_CONFIG = 0x51

class Error(IntEnum):
    OK = 0
    MALFORMED = 1
    UNSUPPORTED = 2
    STATE = 3
    BOOT = 4
    GENERATION = 5
    OWNER = 6
    SEQUENCE = 7
    CONFLICT = 8
    RANGE = 9
    GROUP = 10
    BUSY = 11
    SPACE = 12
    SAFETY = 13
    NOT_FOUND = 14
    ADDRESS = 15
    CAPABILITY = 16
    BANDWIDTH = 17
    TIMEOUT = 18
    DRIVER = 19
    EPOCH = 20
    SESSION = 21

def crc16(data: bytes) -> int:
    c = 0xffff
    for b in data:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ (0x1021 if c & 0x8000 else 0)) & 0xffff
    return c

@dataclass(frozen=True)
class Frame:
    kind: int
    src_uid: int
    dst_uid: int
    sequence: int
    command: int
    session: int
    payload: bytes = b''

def encode(f: Frame) -> bytes:
    if f.kind not in range(1, 5) or not 0 < f.sequence <= 0xffffffff or not f.session:
        raise ValueError('invalid header')
    if not 0 <= f.src_uid <= 0xffffffffffffffff or not 0 <= f.dst_uid <= 0xffffffffffffffff:
        raise ValueError('invalid uid')
    if len(f.payload) > MAX_PAYLOAD:
        raise ValueError('payload too long')
    raw = HEADER.pack(b'\xd5\x7d', VERSION, f.kind, 0, f.src_uid, f.dst_uid,
                      f.sequence, f.command, f.session, len(f.payload)) + f.payload
    return raw + struct.pack('<H', crc16(raw[2:]))

def decode(raw: bytes) -> Frame:
    """Decode one complete datagram; trailing or missing bytes are invalid."""
    if len(raw) < HEADER_SIZE + 2:
        raise ValueError('short frame')
    sof, version, kind, flags, src, dst, seq, cmd, session, size = HEADER.unpack_from(raw)
    if (sof != b'\xd5\x7d' or version != VERSION or flags or kind not in range(1, 5)
            or not seq or not session or size > MAX_PAYLOAD or len(raw) != HEADER_SIZE + 2 + size
            or crc16(raw[2:-2]) != struct.unpack_from('<H', raw, len(raw)-2)[0]):
        raise ValueError('invalid frame')
    return Frame(kind, src, dst, seq, cmd, session, raw[HEADER_SIZE:-2])

def guard(boot: int, generation: int, epoch: int) -> bytes:
    return struct.pack('<QIQ', boot, generation, epoch)

def targets(boot: int, generation: int, values: list[tuple[int,int,int]], epoch: int) -> bytes:
    return guard(boot, generation, epoch) + bytes([len(values)]) + b''.join(struct.pack('<HHi', *v) for v in values)

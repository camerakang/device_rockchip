"""Receive-only 0x12 decoder, checked against device_protocol/timecode/reference.py.

Reference SHA256: 53bf59b40db120516808c8f6ed141f62a1c6ebd487fa10292ab47b764d5ad52c.
No encoder, ACK, Control protocol or HostTimeline playback behavior is included.
"""
import binascii
import struct
from dataclasses import dataclass

LOSS_US = 30000
PERIOD_US = 10000
MAX_READ = 1024


@dataclass(frozen=True)
class Packet:
    epoch: int
    sequence: int
    time_us: int


def decode(raw):
    if len(raw) != 26:
        raise ValueError('timecode_length')
    sof, version, flags, epoch, seq, source = struct.unpack_from('<2sBBQIQ', raw)
    if (sof != b'\xd5\x7c' or version != 0x12 or flags != 0 or epoch == 0
            or binascii.crc_hqx(raw[2:24], 0xffff) != struct.unpack_from('<H', raw, 24)[0]):
        raise ValueError('timecode_frame')
    return Packet(epoch, seq, source)


class Decoder:
    """Bounded stream assembly; timestamps are USB reads, never invented SOF times."""
    def __init__(self):
        self.buffer = bytearray()
        self.arrivals = []
        self.last_read_us = None
        self.frames = self.invalid_frames = self.discarded_bytes = self.partial_timeouts = 0

    def feed(self, raw, received_us):
        if (not isinstance(raw, bytes) or len(raw) > MAX_READ or type(received_us) is not int
                or not 0 <= received_us <= 2**64 - 1):
            raise ValueError('invalid_timecode_read')
        if self.last_read_us is not None and received_us < self.last_read_us:
            raise ValueError('local_clock_regressed')
        self.last_read_us = received_us
        if self.arrivals and received_us - self.arrivals[0] >= LOSS_US:
            self.partial_timeouts += 1
            self.discarded_bytes += len(self.buffer)
            self.buffer.clear(); self.arrivals.clear()
        self.buffer.extend(raw)
        self.arrivals.extend([received_us] * len(raw))
        result = []
        while self.buffer:
            index = self.buffer.find(b'\xd5\x7c')
            if index < 0:
                keep = 1 if self.buffer[-1:] == b'\xd5' else 0
                count = len(self.buffer) - keep
                self._discard(count)
                break
            if index:
                self._discard(index)
            if len(self.buffer) >= 4 and self.buffer[2:4] != b'\x12\x00':
                self.invalid_frames += 1
                self._discard(1)
                continue
            if len(self.buffer) < 26:
                break
            try:
                packet = decode(bytes(self.buffer[:26]))
            except ValueError:
                self.invalid_frames += 1
                self._discard(1)
                continue
            result.append((packet, self.arrivals[0]))
            del self.buffer[:26]; del self.arrivals[:26]
            self.frames += 1
        return result

    def _discard(self, count):
        self.discarded_bytes += count
        del self.buffer[:count]; del self.arrivals[:count]

    def counters(self):
        return dict(frames=str(self.frames), invalid_frames=str(self.invalid_frames),
                    discarded_bytes=str(self.discarded_bytes), partial_timeouts=str(self.partial_timeouts),
                    buffered_bytes=len(self.buffer))

"""Schema 4: board-local focus ownership; source and normalized wired commands; no persistence."""
from dataclasses import dataclass
from enum import IntEnum
import struct
from .configuration import ConfigurationSnapshot

FOCUS_CONTROL_SCHEMA = 4

class FocusControlSource(IntEnum):
    WIRELESS = 0
    WIRED = 1
    STOPPED = 2
    FAULT = 3

def focus_source(source):
    if not isinstance(source, int) or source not in (0, 1, 2):
        raise ValueError('focus source must be Wireless/Wired/Stopped')
    return bytes((source,))

@dataclass(frozen=True)
class FocusControlStatus:
    source: FocusControlSource
    output_idle: bool
    radio_ready: bool
    bus_quiet: bool
    button_remaining_ms: int = 0
    wired_commands: bool = False

    def encode(self):
        if self.source not in range(4) or (self.output_idle and self.button_remaining_ms):
            raise ValueError('focus status')
        flags = int(self.output_idle) | int(self.radio_ready) << 1 | int(self.bus_quiet) << 2 | int(self.wired_commands) << 3
        return struct.pack('<BBHI', self.source, flags, 0, self.button_remaining_ms)

    @classmethod
    def decode(cls, data):
        if len(data) != 8:
            raise ValueError('focus status length')
        source, flags, reserved, remaining = struct.unpack('<BBHI', data)
        if source > 3 or flags & ~15 or reserved or (flags & 1 and remaining):
            raise ValueError('focus status layout')
        return cls(FocusControlSource(source), bool(flags & 1), bool(flags & 2), bool(flags & 4), remaining, bool(flags & 8))

def focus_control(snapshot: ConfigurationSnapshot):
    if snapshot.schema != FOCUS_CONTROL_SCHEMA:
        raise ValueError('focus schema')
    return FocusControlStatus.decode(snapshot.values)

def focus_positions(mask, positions):
    if not isinstance(mask, int) or not 1 <= mask <= 7 or len(positions) != 3:
        raise ValueError('focus position mask')
    if any(not isinstance(v, int) or not 0 <= v <= 65535 or (not mask & (1 << i) and v) for i, v in enumerate(positions)):
        raise ValueError('focus normalized position')
    return struct.pack('<BBHHH', 0x10, mask, *positions)

def focus_button(action):
    if not isinstance(action, int) or action not in (0, 1):
        raise ValueError('focus button action')
    return bytes((0x11, action))

def decode_focus_command(data):
    if len(data) == 8 and data[0] == 0x10:
        positions = struct.unpack_from('<HHH', data, 2)
        focus_positions(data[1], positions)
        return data[0], data[1], positions
    if len(data) == 2 and data[0] == 0x11:
        focus_button(data[1])
        return data[0], data[1], ()
    raise ValueError('focus command layout')

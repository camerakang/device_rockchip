"""Optional configuration codec. Persistence and hardware application belong to adapters."""
from dataclasses import dataclass
from enum import IntEnum
import struct
from .wire import guard

DUAL_RADIO_SCHEMA = 1
HEADER = struct.Struct('<HIBHQIH')

class ConfigurationState(IntEnum):
    IDLE = 0
    PENDING = 1
    APPLIED = 2
    FAILED = 3

@dataclass(frozen=True)
class ConfigurationSnapshot:
    schema: int
    revision: int = 0
    state: int = ConfigurationState.IDLE
    error: int = 0
    request_session: int = 0
    request_sequence: int = 0
    values: bytes = b''

    def encode(self):
        if len(self.values) > 217 or self.state not in range(4):
            raise ValueError('configuration layout')
        return HEADER.pack(self.schema, self.revision, self.state, self.error,
                           self.request_session, self.request_sequence, len(self.values)) + self.values

    @classmethod
    def decode(cls, data):
        if len(data) < HEADER.size:
            raise ValueError('configuration header')
        fields = HEADER.unpack_from(data)
        if fields[2] not in range(4) or fields[-1] > 217 or len(data) != HEADER.size + fields[-1]:
            raise ValueError('configuration layout')
        return cls(*fields[:-1], data[HEADER.size:])

def configuration_write(uid, boot, generation, epoch, schema, revision, values):
    if len(values) > 206:
        raise ValueError('configuration values')
    return struct.pack('<Q', uid) + guard(boot, generation, epoch) + struct.pack('<HI', schema, revision) + values

def radio_channels(focus, wheel):
    if not all(isinstance(v, int) and 0 <= v <= 15 for v in (focus, wheel)):
        raise ValueError('logical channel must be 0..15')
    return bytes((focus, wheel))

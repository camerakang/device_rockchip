"""Reviewed manufacturer/adapter profiles only; no generic or simulated driver.

A profile must validate UART parameters and canonicalize all five addresses
before opening anything. Its transport owns a nonblocking exclusive USB tty,
checks stable identity and the separate timecode adapter, and implements bounded
read/write/close/fileno. Direction control is specific to the verified adapter.

The codec has encode(Request)->bytes, feed(bytes)->list[Reply] and reset(). It
must reject CRC/framing errors, bound its buffer and never emit invalid frames as
measurements. Reply time means the host read containing the complete frame.
"""
from dataclasses import dataclass
from types import MappingProxyType

from .config import ConfigError
from .sonar_core import Timing
from .snapshots import DIRECTIONS


@dataclass(frozen=True)
class Profile:
    protocol_id: str
    timing: Timing
    prepare: object
    codec: object
    open_transport: object

    def __post_init__(self):
        if (not isinstance(self.protocol_id, str) or not 1 <= len(self.protocol_id) <= 256
                or not isinstance(self.timing, Timing)
                or any(not callable(fn) for fn in (self.prepare, self.codec, self.open_transport))):
            raise ValueError('invalid_sonar_driver_profile')


# Populated in source only after reviewing a real protocol and adapter. Config
# strings are identifiers, never module names, shell commands or import paths.
PROFILES = MappingProxyType({})


def resolve(config):
    item = config['sonar']
    identity = item['vendor_protocol']
    if identity is None: raise ConfigError('sonar_not_configured')
    profile = PROFILES.get(identity)
    if profile is None: raise ConfigError('sonar_protocol_unavailable')
    if (not item['binding'] or any(v is None for v in item['uart'].values())
            or not any(node['address'] is not None for node in item['nodes'].values())):
        raise ConfigError('sonar_not_configured')
    addresses = profile.prepare(config)
    if not isinstance(addresses, dict) or set(addresses) != set(DIRECTIONS):
        raise ConfigError('invalid_sonar_driver_addresses')
    configured = [a for a in addresses.values() if a is not None]
    if (any(not isinstance(a, str) or not 1 <= len(a) <= 256 for a in configured)
            or len(set(configured)) != len(configured)
            or any((addresses[d] is None) != (item['nodes'][d]['address'] is None) for d in DIRECTIONS)):
        raise ConfigError('invalid_sonar_driver_addresses')
    return profile, dict(addresses)

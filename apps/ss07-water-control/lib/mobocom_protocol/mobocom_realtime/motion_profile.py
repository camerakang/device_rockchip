"""Schema 2: volatile Mobocom motion planner limits; units come from CAPS."""
from dataclasses import dataclass
import struct
from .configuration import ConfigurationSnapshot

MOTION_PROFILE_SCHEMA = 2
LIVE_MOTION_PROFILE_SCHEMA = 3
MAX_MOTION_PROFILE_RESOURCES = 7

@dataclass(frozen=True)
class MotionLimit:
    endpoint: int
    resource: int
    vmax: int
    amax: int

@dataclass(frozen=True)
class MotionLimitRange(MotionLimit):
    min_vmax: int
    max_vmax: int
    min_amax: int
    max_amax: int

def _validate(values, ranges=False):
    if not 1 <= len(values) <= MAX_MOTION_PROFILE_RESOURCES:
        raise ValueError('motion profile count')
    seen = set()
    for v in values:
        key = (v.endpoint, v.resource)
        if key in seen or any(type(x) is not int or not 0 <= x <= 65535 for x in key):
            raise ValueError('motion profile resources')
        seen.add(key)
        numbers = (v.vmax, v.amax)
        if ranges:
            numbers += (v.min_vmax, v.max_vmax, v.min_amax, v.max_amax)
        if any(type(x) is not int or not 1 <= x <= 0x7fffffff for x in numbers):
            raise ValueError('motion profile positive int32')
        if ranges and not (v.min_vmax <= v.vmax <= v.max_vmax and v.min_amax <= v.amax <= v.max_amax):
            raise ValueError('motion profile bounds')

def motion_limits(values):
    _validate(values)
    return bytes((2, 0, len(values))) + b''.join(struct.pack('<HHii', v.endpoint, v.resource, v.vmax, v.amax) for v in values)

def decode_motion_limits(data):
    if len(data) < 3 or data[:2] != b'\x02\0' or len(data) != 3 + 12 * data[2]:
        raise ValueError('motion limits layout')
    values = [MotionLimit(*struct.unpack_from('<HHii', data, 3 + 12*i)) for i in range(data[2])]
    _validate(values)
    return values

def encode_motion_profile(values):
    _validate(values, True)
    return bytes((2, 0, len(values))) + b''.join(struct.pack('<HHiiiiii', v.endpoint, v.resource, v.vmax, v.amax, v.min_vmax, v.max_vmax, v.min_amax, v.max_amax) for v in values)

def motion_profile(snapshot: ConfigurationSnapshot):
    data = snapshot.values
    if snapshot.schema not in (MOTION_PROFILE_SCHEMA, LIVE_MOTION_PROFILE_SCHEMA) or len(data) < 3 or data[:2] != b'\x02\0' or len(data) != 3 + 28 * data[2]:
        raise ValueError('motion profile layout')
    values = [MotionLimitRange(*struct.unpack_from('<HHiiiiii', data, 3 + 28*i)) for i in range(data[2])]
    _validate(values, True)
    return values

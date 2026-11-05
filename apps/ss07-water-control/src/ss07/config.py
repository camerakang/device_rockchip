"""Strict validation of the shipped JSON Schema subset plus binding invariants."""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
MAX_CONFIG_BYTES = 65536


class ConfigError(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_json(raw, max_bytes=MAX_CONFIG_BYTES):
    if len(raw) > max_bytes:
        raise ConfigError("JSON exceeds byte limit")
    try:
        return json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ConfigError(value)))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ConfigError(f"invalid JSON: {exc}") from exc


def read_json(path):
    with open(path, "rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    return decode_json(raw)


def check_schema(value, schema, path="$", root=None):
    """Only keywords used by our schema; NOT a general JSON Schema engine."""
    root = schema if root is None else root
    supported = {'$schema', '$id', '$defs', '$ref', 'type', 'enum', 'properties',
                 'required', 'additionalProperties', 'minLength', 'maxLength',
                 'pattern', 'minimum', 'maximum'}
    if schema.keys() - supported:
        raise ConfigError(f"unsupported schema keywords: {sorted(schema.keys() - supported)}")
    if "$ref" in schema:
        return check_schema(value, root["$defs"][schema["$ref"].split("/")[-1]], path, root)
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "boolean": bool, "null": type(None)}
    allowed = schema.get("type", list(types))
    allowed = [allowed] if isinstance(allowed, str) else allowed
    if not any(type(value) is types[t] for t in allowed):
        raise ConfigError(f"{path}: expected {allowed}")
    if "enum" in schema and value not in schema["enum"]:
        raise ConfigError(f"{path}: not an allowed value")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - value.keys()
        extra = value.keys() - properties.keys()
        if missing or (extra and schema.get("additionalProperties") is False):
            raise ConfigError(f"{path}: missing={sorted(missing)}, unknown={sorted(extra)}")
        for key in value.keys() & properties.keys():
            check_schema(value[key], properties[key], f"{path}.{key}", root)
    if isinstance(value, str):
        if any(ord(char) < 32 for char in value):
            raise ConfigError(f"{path}: control characters are not allowed")
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 4096):
            raise ConfigError(f"{path}: invalid length")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ConfigError(f"{path}: invalid syntax or unstable device identity")
    if type(value) is int:
        if not schema.get("minimum", value) <= value <= schema.get("maximum", value):
            raise ConfigError(f"{path}: out of range")


def validate(config):
    check_schema(config, read_json(ROOT / "config/system.schema.json"))
    if config['timecode']['uart']['data_bits'] not in (None, 8):
        raise ConfigError('timecode 0x12 requires eight data bits; unknown stays null')
    storage = config['storage']
    if (storage.get('recording_enabled', False) or storage.get('sonar_recording_enabled', False)) and not storage.get('filesystem_uuid'):
        raise ConfigError('recording requires an explicit filesystem UUID')
    reserve = int(storage.get('reserve_bytes', '50000000000'))
    if not 268435456 <= reserve <= 2**63 - 1:
        raise ConfigError('recording reserve must be at least 256 MiB and fit int64')
    from .media import settings
    media = settings(config)
    tcp_ports = [config['api']['port']] + [media[k] for k in ('rtsp_port', 'webrtc_port', 'api_port')]
    udp_ports = [media['ice_port']] + [media['rtp_base_port'] + i * 2 for i in range(5)]
    if len(set(tcp_ports)) != len(tcp_ports) or len(set(udp_ports)) != len(udp_ports):
        raise ConfigError('conflicting media/API ports')
    if max(udp_ports) > 65535:
        raise ConfigError('RTP port range exceeds 65535')
    bindings = [c["binding"] for c in config["cameras"].values()]
    bindings += [config[k]["binding"] for k in ("sonar", "timecode")]
    bound = [p for p in bindings if p is not None]
    if any(Path(p).name in ('', '.', '..') or p.endswith('/.') for p in bound):
        raise ConfigError("invalid device binding basename")
    if len(set(bound)) != len(bound):
        raise ConfigError("duplicate device binding")
    # Detect aliases to the same currently connected device. Recheck at open in future workers.
    resolved = [str(Path(p).resolve()) for p in bound if Path(p).exists()]
    if len(set(resolved)) != len(resolved):
        raise ConfigError("bindings resolve to the same device")
    addresses = [n["address"] for n in config["sonar"]["nodes"].values()
                 if n["address"] is not None]
    if len(set(addresses)) != len(addresses):
        raise ConfigError("duplicate sonar address")
    url = config['sdi']['rtsp_url']
    if url is not None:
        try:
            parsed = urlsplit(url)
            if not parsed.hostname or parsed.username or parsed.password or parsed.port == 0:
                raise ValueError('host, port or credentials')
        except ValueError as exc:
            raise ConfigError('invalid RTSP URL') from exc
    return config


def load(path):
    return validate(read_json(path))


def revision(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()

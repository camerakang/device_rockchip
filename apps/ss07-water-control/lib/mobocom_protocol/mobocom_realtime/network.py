"""Fixed v0x22 UDP network binding constants."""

CONTROL_MULTICAST = '239.255.33.21'
CONTROL_PORT = 47821
DISCOVERY_WINDOW_US = 100_000
DEFAULT_TRANSACTION_TIMEOUT_MS = 30


def validate_endpoint(uid: int, address: str, port: int) -> tuple[int, str, int]:
    if not 0 < uid <= 0xffffffffffffffff:
        raise ValueError('UID范围')
    if not address or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('网络endpoint无效')
    return uid, address, port

"""Sonar Drive V1.1: U10 unsolicited JSONL measurements, never host polls."""
from dataclasses import dataclass
import re

from .config import ConfigError, decode_json
from .serial_receive import open_port
from .sonar_core import Poller, Reply, Request, event

PROTOCOL = 'sonar_drive_v1_1'
UART = dict(baud=115200, data_bits=8, parity='none', stop_bits=1)
STATES = frozenset('ok no_echo no_burst no_adc_pin spi_error wrong_id config_error adc_error adc_init_error timing_error clock_error'.split())
MASK = 2**32 - 1


@dataclass(frozen=True)
class StreamTiming:
    stale_us: int = 1500000


def prepare(config):
    item = config['sonar']
    addresses = {d: n['address'] for d, n in item['nodes'].items()}
    if (item['uart'] != UART or not isinstance(item['binding'], str)
            or not re.fullmatch(r'/dev/serial/by-path/[^/\s]+', item['binding'])
            or set(addresses.values()) != set('12345')):
        raise ConfigError('invalid_sonar_v11_configuration')
    return addresses


def open_u10(config):
    prepare(config)
    return open_port(config, role='sonar', usb_id=('1a86', '7523'))


class Decoder:
    def __init__(self):
        self.reset()

    def reset(self):
        self.buf = bytearray()
        self.discard = False

    def feed(self, raw):
        result = []
        for byte in raw:
            if byte == 10:
                if not self.discard:
                    try:
                        f = decode_json(bytes(self.buf), 256)
                        if type(f) is dict and ('ack' in f or 'params' in f):
                            callback = getattr(self, 'on_control', None)
                            if callback: callback(f)
                        if (type(f) is dict and all(type(f.get(k)) is int for k in
                            ('v', 'ch', 'seq', 'ms', 'id', 'thr_mv')) and f['v'] == 1
                            and 1 <= f['ch'] <= 5 and 0 <= f['seq'] <= MASK and 0 <= f['ms'] <= MASK
                            and 200 <= f['thr_mv'] <= 3200 and isinstance(f.get('state'), str)
                            and f['state'] in STATES and 'mm' in f and
                            ((f['state'] == 'ok' and type(f['mm']) is int and 0 <= f['mm'] <= 20000)
                             or (f['state'] != 'ok' and f['mm'] is None))):
                            result.append(f)
                    except ConfigError:
                        pass
                self.reset()
            elif len(self.buf) >= 255:
                self.discard = True
            elif not self.discard:
                self.buf.append(byte)
        return result


class Stream(Poller):
    """Share the status projection and event sinks; no pending requests."""
    def __init__(self, addresses, timing, boot, instance, submit, evidence_kind):
        self.addresses, self.timing, self.boot, self.instance = addresses, timing, boot, instance
        self.submit, self.evidence = submit, evidence_kind
        self.directions = {a: d for d, a in addresses.items()}
        self.latest, self.last_received, self.invalidated = {}, {}, set()
        self.previous = self.last_clock = None
        self.seq = 0
        self.counters = dict(measurements=0, invalid_measurements=0, missing_frames=0, duplicates=0, sequence_resets=0)

    def tick(self, now):
        self.clock(now)

    def connection(self, connected, now):
        self.clock(now)
        self.previous = None
        self.invalidated.update(self.addresses)
        self.seq += 1
        self.submit(event(self.boot, self.instance, self.seq,
                          'BUS_CONNECTED' if connected else 'BUS_DISCONNECTED', now, self.evidence))

    def receive(self, frame, now):
        self.clock(now)
        seq, ms = frame['seq'], frame['ms']
        if self.previous is not None:
            delta = (seq - self.previous[0]) & MASK
            if self.previous == (seq, ms):
                self.counters['duplicates'] += 1
                return
            if not delta or delta > MASK // 2 or ((ms - self.previous[1]) & MASK) > MASK // 2:
                self.counters['sequence_resets'] += 1
                self.invalidated.update(self.addresses)
            else:
                self.counters['missing_frames'] += delta - 1
        self.previous = (seq, ms)
        address = str(frame['ch'])
        direction = self.directions[address]
        reason = frame['state']
        if reason == 'ok' and frame['id'] != 185:
            reason = 'wrong_id'
        valid = reason == 'ok'
        reply = Reply(address, None, valid, frame['mm'] if valid else None, None if valid else reason, None)
        self.seq += 1
        sample = event(self.boot, self.instance, self.seq, 'MEASUREMENT', now, self.evidence,
                       Request(direction, address, None), reply, 'device_stream')
        self.latest[direction], self.last_received[direction] = sample, now
        self.invalidated.discard(direction)
        self.counters['measurements'] += 1
        self.counters['invalid_measurements'] += int(not valid)
        self.submit(sample)

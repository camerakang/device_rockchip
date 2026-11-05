import binascii
import json
import struct
import unittest

from ss07.config import ROOT
from ss07.timecode_model import HOLDOVER_US, Mapper, validate_tag
from ss07.timecode_wire import Decoder, LOSS_US, Packet, decode


def wire(epoch=1, sequence=0, time_us=0):
    # Test-only encoder, independently checked against the pinned official vectors.
    raw = struct.pack('<2sBBQIQ', b'\xd5\x7c', 0x12, 0, epoch, sequence, time_us)
    return raw + struct.pack('<H', binascii.crc_hqx(raw[2:], 0xffff))


class WireTests(unittest.TestCase):
    def test_official_golden_vectors_and_fragment_boundaries(self):
        fixture = json.loads((ROOT / 'tests/fixtures/timecode-golden.json').read_text())
        self.assertEqual(binascii.crc_hqx(b'123456789', 0xffff), 0x29b1)
        for item in fixture['vectors']:
            raw = bytes.fromhex(item['hex'])
            expected = Packet(int(item['epoch']), int(item['sequence']), int(item['time_us']))
            self.assertEqual(wire(expected.epoch, expected.sequence, expected.time_us), raw)
            self.assertEqual(decode(raw), expected)
            for split in range(1, 26):
                decoder = Decoder()
                self.assertEqual(decoder.feed(raw[:split], 100), [])
                self.assertEqual(decoder.feed(raw[split:], 110), [(expected, 100)])

    def test_noise_bad_crc_embedded_magic_and_partial_timeout(self):
        good = wire(0x7cd5, 1, 10000)
        bad = bytearray(good); bad[-1] ^= 1
        decoder = Decoder()
        result = decoder.feed(b'\x00\xd5bad' + bytes(bad) + good + good, 100)
        self.assertEqual([packet.epoch for packet, _ in result], [0x7cd5, 0x7cd5])
        self.assertGreater(decoder.invalid_frames, 0)
        self.assertEqual(decoder.buffer, b'')
        decoder.feed(good[:10], 1000)
        self.assertEqual(decoder.feed(good[10:], 1000 + LOSS_US), [])
        self.assertEqual(decoder.partial_timeouts, 1)
        for _ in range(100):
            decoder.feed(b'x' * 1024, 1000 + LOSS_US)
            self.assertLessEqual(len(decoder.buffer), 25)
        self.assertEqual(decoder.feed(good, 40000)[0][0], decode(good))

    def test_header_epoch_flags_and_read_bounds(self):
        for raw in (wire(epoch=0), wire()[:-1], wire() + b'x'):
            with self.assertRaises(ValueError): decode(raw)
        for offset, value in ((0, 0), (2, 0x21), (3, 1)):
            raw = bytearray(wire()); raw[offset] = value
            raw[-2:] = struct.pack('<H', binascii.crc_hqx(raw[2:24], 0xffff))
            with self.assertRaises(ValueError): decode(raw)
        with self.assertRaises(ValueError): Decoder().feed(b'x' * 1025, 0)


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.mapper = Mapper('test-boot', 'test-instance', 'synthetic_test', self.events.append)

    def observe(self, seq, receive, source=None, epoch=1):
        return self.mapper.observe(Packet(epoch, seq, seq * 10000 if source is None else source), receive, receive)

    def test_usb_receipt_is_estimated_not_sof_or_exposure(self):
        self.assertEqual(self.mapper.tag(0, 0)['quality'], 'UNAVAILABLE')
        self.assertTrue(self.observe(0, 1000))
        tag = self.mapper.tag(1500, 1500, 'segment_create_notification')
        self.assertEqual(tag['quality'], 'ESTIMATED')
        self.assertEqual(tag['estimated_time_us'], '500')
        self.assertTrue(tag['source_valid'])
        self.assertIsNone(tag['origin_monotonic_us']); self.assertIsNone(tag['uncertainty_us'])
        self.assertEqual(tag['evidence_kind'], 'synthetic_test')
        tag['quality'] = 'VALID'
        with self.assertRaises(ValueError): validate_tag(tag)

    def test_duplicates_bad_step_old_samples_do_not_extend_age(self):
        self.observe(1, 10000)
        for packet in (Packet(1, 1, 10000), Packet(1, 0, 0), Packet(1, 2, 20051)):
            self.assertFalse(self.mapper.observe(packet, 20000, 20000))
        tag = self.mapper.tag(40000, 40000)
        self.assertFalse(tag['source_valid']); self.assertEqual(tag['source_age_us'], '30000')
        self.assertEqual(tag['reason'], 'source_lost_holdover')
        self.assertEqual(self.mapper.tag(10000 + HOLDOVER_US, 10000 + HOLDOVER_US)['quality'], 'UNAVAILABLE')

    def test_sequence_wrap_and_both_step_tolerance_boundaries(self):
        self.observe(0xffffffff, 1000, source=100000)
        self.assertTrue(self.observe(0, 11000, source=109950))
        self.assertTrue(self.observe(1, 21000, source=120000))
        self.assertFalse(self.observe(2, 22000, source=130051))
        self.assertFalse(self.observe(1 + 0x80000000, 23000, source=120000 + 10000 * 0x80000000))

    def test_loss_recovery_epoch_and_old_queued_event_are_separate(self):
        self.observe(0, 1000)
        first = self.mapper.tag(1500, 1500)
        self.mapper.tick(31000)
        self.assertTrue(self.observe(4, 41000))
        second = self.mapper.tag(41500, 41500)
        self.assertNotEqual(first['mapping_id'], second['mapping_id'])
        old = self.mapper.tag(1500, 41500)
        self.assertEqual(old, first)
        gap = self.mapper.tag(32000, 41500)
        self.assertEqual(gap['mapping_id'], first['mapping_id']); self.assertFalse(gap['source_valid'])
        self.assertTrue(self.observe(0, 42000, source=0, epoch=2))
        new = self.mapper.tag(42500, 42500)
        self.assertNotEqual(new['mapping_id'], second['mapping_id'])
        self.assertEqual(new['source_epoch'], '2')
        self.assertFalse(self.observe(5, 43000, epoch=1))
        queued = self.mapper.tag(41500, 43000)
        self.assertEqual(queued['mapping_id'], second['mapping_id'])
        self.assertEqual(queued['source_epoch'], '1')
        self.assertEqual(self.mapper.tag(41500, 43000, boot_id='old-boot')['quality'], 'UNAVAILABLE')

    def test_stale_partial_future_and_regressing_clock_fail_closed(self):
        self.assertFalse(self.mapper.observe(Packet(1, 1, 10000), 0, LOSS_US))
        self.observe(1, 40000)
        self.assertEqual(self.mapper.tag(41000, 40000)['reason'], 'future_event')
        self.mapper.tick(39999)
        self.assertEqual(self.mapper.tag(40000, 41000)['reason'], 'local_clock_regressed')
        self.assertFalse(self.observe(2, 50000))

    def test_history_is_bounded_and_restart_does_not_reinterpret_old_events(self):
        for seq in range(7000): self.observe(seq, seq * 10000)
        self.assertLessEqual(len(self.mapper.history), 6500)
        self.assertEqual(self.mapper.tag(0, 70000000)['reason'], 'history_expired')
        restarted = Mapper('test-boot', 'new-instance', 'synthetic_test')
        restarted.observe(Packet(2, 0, 0), 70000000, 70000000)
        self.assertEqual(restarted.tag(69999999, 70000000)['reason'], 'no_prior_sample')


if __name__ == '__main__': unittest.main()

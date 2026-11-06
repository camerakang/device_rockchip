import unittest
from .wire import HEADER_SIZE, MAX_FRAME, VERSION, ALL_NODES, Command, Kind, Frame, encode, decode


class NetworkV22WireTests(unittest.TestCase):
    def test_uid_header_and_round_trip(self):
        frame = Frame(Kind.REQUEST, 0, 0x9006000000000001, 7,
                      Command.DISCOVER, 123, b'')
        raw = encode(frame)
        self.assertEqual(VERSION, 0x22)
        self.assertEqual(len(raw), HEADER_SIZE + 2)
        self.assertEqual(decode(raw), frame)

    def test_max_frame(self):
        frame = Frame(Kind.REQUEST, 0, 1, 1, Command.WRITE_TARGETS, 1,
                      bytes(240))
        self.assertEqual(len(encode(frame)), MAX_FRAME)

    def test_datagram_must_be_exactly_one_frame(self):
        raw = encode(Frame(Kind.REQUEST, 0, 2, 1, Command.INFO, 9, b''))
        for bad in (raw + b'\x00', raw[:-1], raw + raw):
            with self.assertRaises(ValueError):
                decode(bad)

    def test_uid_above_2_53_round_trips(self):
        uid = 0xfedcba9876543211
        frame = Frame(Kind.RESPONSE, uid, ALL_NODES, 3, Command.INFO, 5, b'x')
        self.assertEqual(decode(encode(frame)).src_uid, uid)
        with self.assertRaises(ValueError):
            encode(Frame(Kind.REQUEST, 0, 1 << 64, 1, Command.INFO, 1))

    def test_zero_destination_is_multicast(self):
        self.assertEqual(ALL_NODES, 0)


if __name__ == '__main__':
    unittest.main()

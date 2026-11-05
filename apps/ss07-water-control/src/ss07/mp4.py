"""Bounded structural check for MediaMTX fMP4 files, without decoding video."""
import os
import struct


class InvalidMP4(ValueError):
    pass


def inspect(fd):
    size = os.fstat(fd).st_size
    offset, boxes, fragments = 0, 0, 0
    init, movie, pending_fragment = False, False, False
    while offset < size:
        boxes += 1
        if boxes > 100000 or size - offset < 8:
            raise InvalidMP4('truncated_or_excessive_mp4_boxes')
        raw = os.pread(fd, 16, offset)
        length, kind = struct.unpack('>I4s', raw[:8])
        header = 8
        if length == 1:
            if len(raw) < 16:
                raise InvalidMP4('truncated_extended_box')
            length, header = struct.unpack('>Q', raw[8:16])[0], 16
        if length < header or length > size - offset:
            raise InvalidMP4('unfinished_mp4_box')
        if kind == b'ftyp':
            if offset != 0 or length < 16:
                raise InvalidMP4('invalid_initialization')
            init = True
        elif kind == b'moov':
            if not init or movie or length <= header:
                raise InvalidMP4('invalid_movie_box')
            movie = True
        elif kind == b'moof':
            if not movie or pending_fragment or length <= header:
                raise InvalidMP4('invalid_fragment')
            pending_fragment = True
        elif kind == b'mdat':
            if not pending_fragment or length <= header:
                raise InvalidMP4('missing_fragment_header_or_data')
            pending_fragment = False
            fragments += 1
        offset += length
    if not init or not movie or pending_fragment or fragments == 0:
        raise InvalidMP4('incomplete_fragmented_mp4')
    return {'size_bytes': str(size), 'fragments': fragments,
            'verification': 'container_structure_only'}

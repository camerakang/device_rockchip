"""Read-only catalog and byte-range access, always by opaque recording ID."""
import json
import os
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .catalog import CatalogError, RecordStore, valid_id
from .config import decode_json
from .volume import MountLease, VolumeError


def byte_range(value, size):
    if value is None:
        return 0, size - 1, False
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError('invalid_range')
    match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', value)
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError('invalid_range')
    if not match[1]:
        count = int(match[2])
        if count == 0:
            raise ValueError('invalid_range')
        return max(0, size - count), size - 1, True
    start = int(match[1])
    end = min(int(match[2]), size - 1) if match[2] else size - 1
    if start >= size or end < start:
        raise ValueError('range_not_satisfiable')
    return start, end, True


def error(handler, status, code):
    handler.reply(status, {'error': {'code': code, 'message': code.lower(), 'details': []}})


def handle(handler, head=False):
    try:
        parsed = urlsplit(handler.path)
    except ValueError:
        error(handler, 400, 'BAD_REQUEST')
        return True
    if parsed.path != '/api/v1/recordings' and not parsed.path.startswith('/api/v1/recordings/'):
        return False
    catalog = handler.server.state.catalog
    if catalog is None:
        error(handler, 503, 'CATALOG_UNAVAILABLE')
        return True
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8) if parsed.query else {}
        if any(len(values) != 1 for values in query.values()):
            raise ValueError('duplicate query')
        query = {key: values[0] for key, values in query.items()}
        if parsed.path == '/api/v1/recordings' and not head:
            if set(query) - {'channel', 'start', 'end', 'limit', 'cursor', 'epoch', 'time_start_us', 'time_end_us'}:
                raise ValueError('unknown query')
            result = catalog.list(channel=query.get('channel'), start=query.get('start'), end=query.get('end'),
                                  limit=int(query.get('limit', '50')), cursor=query.get('cursor'),
                                  epoch=query.get('epoch'), time_start_us=query.get('time_start_us'),
                                  time_end_us=query.get('time_end_us'))
            result['events'] = catalog.events(20)
            result['disk_online'] = False
            try:
                with MountLease.acquire(handler.server.state.config['storage']) as volume:
                    store = RecordStore(catalog, volume)
                    result['disk_online'] = True
                    for item in result['items']:
                        if item['complete']:
                            try:
                                fd, _ = store.open_recording(item['recording_id'])
                                os.close(fd)
                                item['availability'] = 'available'
                            except (OSError, ValueError):
                                item['availability'] = 'file_unavailable'
            except (OSError, ValueError):
                for item in result['items']:
                    if item['complete']:
                        item['availability'] = 'disk_offline'
            handler.reply(200, {'data': result})
            return True
        match = re.fullmatch(r'/api/v1/recordings/([a-f0-9-]{36})/content', parsed.path)
        if not match or set(query) - {'download'} or query.get('download', '0') not in ('0', '1'):
            error(handler, 404, 'NOT_FOUND')
            return True
        record_id = valid_id(match[1])
        # A missing catalog entry is 404 regardless of whether a disk is attached.
        catalog.get(record_id)
        with MountLease.acquire(handler.server.state.config['storage']) as volume:
            fd, meta = RecordStore(catalog, volume).open_recording(record_id)
            with os.fdopen(fd, 'rb') as stream:
                size = int(meta['size_bytes'])
                etag = '"' + record_id + '-' + str(size) + '"'
                try:
                    ranges = handler.headers.get_all('Range', [])
                    if len(ranges) > 1:
                        raise ValueError('multiple_ranges')
                    requested = handler.headers.get('Range')
                    if handler.headers.get('If-Range') not in (None, etag):
                        requested = None
                    start, end, partial = byte_range(requested, size)
                except ValueError:
                    handler.send_response(416)
                    handler.send_header('Content-Range', 'bytes */' + str(size))
                    handler.send_header('Content-Length', '0')
                    handler.end_headers()
                    return True
                length = end - start + 1
                handler.send_response(206 if partial else 200)
                handler.send_header('Content-Type', 'video/mp4')
                handler.send_header('Content-Length', str(length))
                handler.send_header('Accept-Ranges', 'bytes')
                handler.send_header('ETag', etag)
                handler.send_header('Cache-Control', 'no-store')
                handler.send_header('X-Content-Type-Options', 'nosniff')
                if partial:
                    handler.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
                if query.get('download') == '1':
                    handler.send_header('Content-Disposition', 'attachment; filename="ss07-' + meta['channel_id'] + '-' + record_id + '.mp4"')
                handler.end_headers()
                if not head:
                    try:
                        sent = 0
                        while sent < length and not handler.server.stopping.is_set():
                            if not handler.stream_allowed('recordings.read'): break
                            count = handler.connection.sendfile(stream, offset=start + sent, count=min(1048576, length - sent))
                            if not count: break
                            sent += count
                        if sent != length: handler.close_connection = True
                    except OSError:
                        handler.close_connection = True
                return True
    except KeyError:
        error(handler, 404, 'RECORDING_NOT_FOUND')
    except FileNotFoundError:
        error(handler, 503, 'RECORDING_STORAGE_UNAVAILABLE')
    except VolumeError:
        error(handler, 409, 'RECORDING_UNAVAILABLE')
    except (OSError, sqlite3.Error):
        error(handler, 503, 'CATALOG_OR_STORAGE_UNAVAILABLE')
    except (ValueError, CatalogError, TypeError):
        error(handler, 422, 'INVALID_RECORDING_REQUEST')
    return True

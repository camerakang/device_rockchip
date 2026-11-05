"""Bounded read-only audit history; this is not an unbounded source sample archive."""
import sqlite3
from urllib.parse import parse_qs, urlsplit
from .snapshots import u64


def history(handler):
    parsed = urlsplit(handler.path)
    if parsed.path != '/api/v1/timecode/history': return False
    try:
        query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True, max_num_fields=2) if parsed.query else {}
        if set(query) - {'limit', 'cursor'} or any(len(values) != 1 for values in query.values()):
            raise ValueError('invalid_history_query')
        limit, cursor = int(query.get('limit', ['50'])[0]), query.get('cursor', [None])[0]
        if not 1 <= limit <= 200 or (cursor is not None and not 1 <= u64(cursor) <= 2**63-1):
            raise ValueError('invalid_history_range')
    except ValueError:
        handler.reply(422, {'error': {'code': 'INVALID_TIMECODE_HISTORY_REQUEST', 'message': 'Invalid history request', 'details': []}})
        return True
    try:
        journal = handler.server.state.timecode_journal
        if journal is None: raise OSError('timecode_history_unconfigured')
        data = journal.history(limit=limit, cursor=cursor)
        handler.reply(200, {'data': data})
    except (OSError, sqlite3.Error, ValueError):
        handler.reply(503, {'error': {'code': 'TIMECODE_HISTORY_UNAVAILABLE', 'message': 'Timecode history unavailable', 'details': []}})
    return True

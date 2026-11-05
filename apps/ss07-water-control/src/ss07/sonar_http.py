"""Bounded read-only diagnostic history. External times always require an epoch."""
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .snapshots import DIRECTIONS, u64


def history(handler):
    parsed = urlsplit(handler.path)
    if parsed.path != '/api/v1/sonars/history': return False
    try:
        query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True, max_num_fields=6) if parsed.query else {}
        allowed = {'limit', 'cursor', 'direction', 'source_epoch', 'start_us', 'end_us'}
        if set(query) - allowed or any(len(v) != 1 for v in query.values()): raise ValueError('invalid_query')
        args = {k: v[0] for k, v in query.items()}
        args['limit'] = int(args.get('limit', '50'))
        if not 1 <= args['limit'] <= 200: raise ValueError('invalid_limit')
        if 'cursor' in args and not 1 <= u64(args['cursor']) <= 2**63-1: raise ValueError('invalid_cursor')
        if 'direction' in args and args['direction'] not in DIRECTIONS: raise ValueError('invalid_direction')
        if 'source_epoch' in args and not u64(args['source_epoch']): raise ValueError('invalid_epoch')
        for key in ('start_us', 'end_us'):
            if key in args:
                u64(args[key])
                if 'source_epoch' not in args: raise ValueError('epoch_required')
        if all(k in args for k in ('start_us', 'end_us')) and u64(args['start_us']) > u64(args['end_us']):
            raise ValueError('reversed_time_range')
    except (ValueError, TypeError):
        handler.reply(422, {'error': {'code': 'INVALID_SONAR_HISTORY_REQUEST', 'message': 'Invalid sonar history request', 'details': []}})
        return True
    try:
        journal = handler.server.state.sonar_journal
        if journal is None: raise OSError('sonar_history_unconfigured')
        handler.reply(200, {'data': journal.history(**args)})
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
        handler.reply(503, {'error': {'code': 'SONAR_HISTORY_UNAVAILABLE', 'message': 'Sonar diagnostic history unavailable', 'details': []}})
    return True

"""Read-only native mirror evidence; never return addresses or error contents."""
from datetime import datetime, timedelta, timezone


def evaluate_status(configured, last_success, last_error, now):
    if type(configured) is not bool or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('mirror status requires explicit configuration and an aware time')
    if last_success is not None and (last_success.tzinfo is None or last_success.utcoffset() is None
                                     or last_success > now):
        raise ValueError('mirror success time is invalid')
    if not configured:
        return 'unconfigured'
    if last_error:
        return 'failed'
    if last_success is None:
        return 'pending'
    return 'stale' if now - last_success > timedelta(hours=36) else 'healthy'


def summarize(rows, now):
    summaries = []
    for row in rows:
        if (row['sync_on_commit'] is not False or row['interval_seconds'] != 28800
                or row['https'] is not True):
            raise ValueError('native mirror must use HTTPS, eight-hour eligibility and no sync on push')
        success = (datetime.fromtimestamp(row['last_attempt'], timezone.utc)
                   if row['last_attempt'] and not row['failed'] else None)
        summaries.append({'id': row['id'], 'last_attempt': row['last_attempt'],
            'status': evaluate_status(True, success, 'failed' if row['failed'] else None, now)})
    return summaries


QUERY = """SELECT COALESCE(json_agg(json_build_object(
 'id', id, 'last_attempt', last_update, 'failed', COALESCE(last_error,'') <> '',
 'sync_on_commit', sync_on_commit, 'interval_seconds', interval / 1000000000,
 'https', remote_address ~ '^https://[^/@[:space:]]+/')), '[]') FROM push_mirror;"""

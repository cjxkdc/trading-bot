"""Rotating diagnostic logs under runs/portal/logs.

Only exception types, code locations and request paths are written. Exception
messages are deliberately omitted because vendor SDK errors can carry account
identifiers or credentials; the incident id links a user-facing message to the
traceback in the file.
"""
from datetime import datetime, timezone
import logging
import logging.handlers
from pathlib import Path
import re
import traceback
from uuid import uuid4

LOGGER = 'trading_bot'
LOG_NAMES = ('portal', 'backend')
LOG_FOLDER = 'runs/portal/logs'
RECORD_START = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')
TAIL_BYTES = 512_000


def configure_logging(root, name):
    """Attach one rotating file per process role (portal, backend, worker)."""
    folder = Path(root) / 'runs/portal/logs'
    folder.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER)
    logger.setLevel(logging.INFO)
    marker = f'{LOGGER}:{name}'
    if any(getattr(handler, 'trading_bot_role', None) == marker for handler in logger.handlers):
        return logger
    handler = logging.handlers.RotatingFileHandler(folder / f'{name}.log', maxBytes=2_000_000,
                                                   backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    handler.trading_bot_role = marker
    logger.addHandler(handler)
    uvicorn_logger = logging.getLogger('uvicorn.error')
    uvicorn_logger.setLevel(logging.WARNING)
    uvicorn_logger.addHandler(handler)
    return logger


def incident_id():
    return uuid4().hex[:10]


def safe_traceback(exc):
    """Frames and exception type only; never the exception text."""
    frames = ''.join(traceback.format_tb(exc.__traceback__)) if exc.__traceback__ else ''
    return f'{type(exc).__module__}.{type(exc).__name__}\n{frames}'.rstrip()


def log_exception(logger, where, exc, ident):
    logger.error('%s incident=%s\n%s', where, ident, safe_traceback(exc))


def log_files(root):
    """Sizes of the current log files; rotated backups are not listed."""
    files = []
    for name in LOG_NAMES:
        path = Path(root) / LOG_FOLDER / f'{name}.log'
        stat = path.stat() if path.is_file() else None
        files.append({'name': name, 'size': stat.st_size if stat else 0,
                      'modified': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat() if stat else None})
    return files


def read_log(root, name, *, limit=200, query=''):
    """The newest records of one log, newest first, optionally filtered.

    A record is a header line (timestamp, level, logger, message) followed by
    the traceback lines that belong to it. Only the tail of a large file is
    read, so the oldest record of a long file may be cut.
    """
    if name not in LOG_NAMES:
        raise ValueError('unknown log')
    limit = max(1, min(int(limit), 1000))
    query = str(query or '')[:80].strip().lower()
    path = Path(root) / LOG_FOLDER / f'{name}.log'
    text, truncated = '', False
    if path.is_file():
        size = path.stat().st_size
        with path.open('rb') as handle:
            if size > TAIL_BYTES:
                handle.seek(size - TAIL_BYTES)
                truncated = True
            text = handle.read().decode('utf-8', 'replace')
        if truncated:
            text = text.split('\n', 1)[1] if '\n' in text else ''
    records = []
    for line in text.splitlines():
        if RECORD_START.match(line) or not records:
            records.append({'header': line, 'lines': []})
        else:
            records[-1]['lines'].append(line)
    if query:
        records = [record for record in records
                   if query in record['header'].lower() or any(query in line.lower() for line in record['lines'])]
    records = records[-limit:]
    records.reverse()
    return {'name': name, 'folder': LOG_FOLDER, 'files': log_files(root), 'records': records,
            'truncated': truncated, 'query': query}

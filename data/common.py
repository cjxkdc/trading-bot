"""Provider-independent data errors, atomic files and collection locks."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json, math, os
from pathlib import Path
from uuid import uuid4


class DataIssue(ValueError):
    """Public, bounded data-quality reason; never raw SDK error output."""



def utcnow():
    return datetime.now(timezone.utc)



def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)



def finite(value):
    if isinstance(value, bool):
        raise DataIssue('invalid_numeric_value')
    number = float(value)
    if not math.isfinite(number):
        raise DataIssue('missing_or_nonfinite_value')
    return number



@contextmanager
def collection_lock(directory):
    """One collector across CLI and GUI processes; OS releases it after a crash."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'collector.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DataIssue('已有数据采集任务正在运行，请等待完成后再更新。') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

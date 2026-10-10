"""Atomic file replacement that tolerates transient Windows file locks."""
import time
from pathlib import Path

# Antivirus, indexing and sync tools briefly open freshly written files on
# Windows; os.replace then fails with PermissionError although the rename
# itself is atomic. A short retry keeps a job finalization or a state save
# from failing on a lock that clears within a second.
RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def replace_file(temporary, path):
    """Move ``temporary`` over ``path`` atomically, retrying short-lived locks."""
    temporary, path = Path(temporary), Path(path)
    for delay in RETRY_DELAYS:
        try:
            temporary.replace(path)
            return
        except PermissionError:
            time.sleep(delay)
    temporary.replace(path)

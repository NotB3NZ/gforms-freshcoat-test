"""One host only. All local processes must use the same runtime directory."""
import fcntl
import os
import threading
from contextlib import contextmanager
from .config import private_directory

_locks = {}
_guard = threading.Lock()


@contextmanager
def file_lock(path, blocking=True):
    private_directory(path.parent)
    with _guard:
        lock = _locks.setdefault(str(path.resolve()), threading.RLock())
    acquired = lock.acquire(blocking=blocking)
    if not acquired:
        raise RuntimeError("Another local process is already running this component.")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(fd, flags)
            except BlockingIOError:
                raise RuntimeError("Another local process is already running this component.") from None
            yield
        finally:
            os.close(fd)
    finally:
        lock.release()

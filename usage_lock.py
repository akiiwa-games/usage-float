"""Small cross-process locks for telemetry (never lock or write CLI credentials)."""
from contextlib import contextmanager
from pathlib import Path
import os
import threading
import time

_guard = threading.Lock()
_locks = {}


@contextmanager
def file_lock(path: Path, timeout: float = 5.0):
    key = str(path.resolve())
    with _guard:
        local = _locks.setdefault(key, threading.Lock())
    deadline = time.monotonic() + timeout
    if not local.acquire(timeout=timeout):
        raise TimeoutError('Telemetry is already being updated')
    stream = None
    acquired = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open('a+b')
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'\0')
            stream.flush()
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Telemetry is already being updated') from None
                time.sleep(0.025)
        yield
    finally:
        if stream is not None:
            if acquired:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        local.release()

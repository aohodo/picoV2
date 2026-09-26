"""Small cross-process file lease used by runtime persistence boundaries."""

import os
from pathlib import Path


class FileLock:
    """Own one byte of a lock file until release or process termination."""

    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self, blocking=True):
        if self.handle is not None:
            raise RuntimeError("file lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(handle.fileno(), mode, 1)
            else:
                import fcntl

                mode = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), mode)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        handle = self.handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self.handle = None

    def __enter__(self):
        if not self.acquire(blocking=True):
            raise RuntimeError(f"could not acquire file lock: {self.path}")
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.release()

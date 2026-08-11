from __future__ import annotations

import os
import time
from pathlib import Path


class InterProcessLock:
    """Small cross-platform advisory lock with bounded waiting."""

    def __init__(self, path: Path, timeout: float = 3600.0):
        self.path = path
        self.timeout = timeout
        self._stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("xb") as initializer:
                initializer.write(b"0")
        except FileExistsError:
            pass
        self._stream = self.path.open("r+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    self._stream.close()
                    raise TimeoutError(f"Timed out waiting for lock: {self.path}")
                time.sleep(0.2)

    def __exit__(self, exc_type, exc, tb):
        assert self._stream is not None
        self._stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()

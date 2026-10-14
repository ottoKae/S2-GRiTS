import tempfile
import threading
import time
from pathlib import Path

from s2grits.locking import InterProcessLock


def test_lock_serializes_writers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "shared.lock"
        active = 0
        maximum = 0
        guard = threading.Lock()

        def writer() -> None:
            nonlocal active, maximum
            with InterProcessLock(lock_path, timeout=5):
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.05)
                with guard:
                    active -= 1

        threads = [threading.Thread(target=writer) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert maximum == 1

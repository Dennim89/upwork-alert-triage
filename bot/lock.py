"""Single-instance lock: an OS byte-range lock (msvcrt / fcntl) held for the process lifetime.

The OS drops it when the process dies, so it never goes stale and no PID is ever probed or killed.
"""
from __future__ import annotations

import os
from pathlib import Path


class SingleInstance:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.fh.close()
            self.fh = None

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise RuntimeError(f"locked: {self.path}")
        return self

    def __exit__(self, *exc) -> None:
        self.release()

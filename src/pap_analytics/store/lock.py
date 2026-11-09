"""Single-writer lock: ``flock`` on ``<store_dir>/store.lock`` (non-blocking)."""

from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO

from pap_analytics.store.types import StoreLockedError


class WriterLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[bytes] | None = None

    def acquire(self) -> None:
        fh = self.path.open("ab")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise StoreLockedError(f"another writer holds {self.path}") from None
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

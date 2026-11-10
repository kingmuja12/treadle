"""The single-instance lock on the state directory (section 8, "Single-instance lock")."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Final

from treadle.errors import LockHeldError
from treadle.types import LockHolder

_LOCK_FILE: Final = "treadle.lock"
_HOLDER_FILE: Final = "holder.json"
# Reported while a holder has the lock but hasn't written holder.json yet.
_UNKNOWN_HOLDER: Final = LockHolder(pid=0, kind="maintenance", started_at="", version="")


class InstanceLock:
    """An exclusive `flock` on `state/treadle.lock`, with the holder recorded in `holder.json`.

    The operating system releases the lock when the process dies. The state directory must
    be on a local filesystem, because `flock` isn't reliable on network filesystems.
    """

    def __init__(self, state_dir: Path, holder: LockHolder) -> None:
        self._state_dir = state_dir
        self._holder = holder
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take the lock and write `holder.json`. Raises LockHeldError with the current holder.

        Creates the state directory (mode 0700) and the lock file (mode 0600) when missing.
        """
        # Design note: the daemon creates the state directory before it locks (section 8),
        # but `treadle run` and `treadle cancel` lock first, so acquire creates it as well.
        self._state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self._state_dir / _LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if isinstance(error, BlockingIOError):
                raise LockHeldError(_read_holder(self._state_dir)) from None
            raise
        self._fd = fd
        try:
            self.update(self._holder)
        except BaseException:
            self.release()
            raise

    def update(self, holder: LockHolder) -> None:
        """Rewrite `holder.json` atomically, for example with the bound port.

        The record goes to a temporary file first and then replaces `holder.json`, so a
        reader never sees a partial file. Raises RuntimeError unless the lock is held.
        """
        if self._fd is None:
            raise RuntimeError("the instance lock isn't held")
        self._holder = holder
        fd, temporary = tempfile.mkstemp(prefix=".holder-", dir=self._state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(holder.to_json(), file)
            os.replace(temporary, self._state_dir / _HOLDER_FILE)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise

    def release(self) -> None:
        """Remove `holder.json`, unlock, and close the lock file. Does nothing when not held."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            # Removed before unlocking, so it can't delete the next holder's record.
            (self._state_dir / _HOLDER_FILE).unlink(missing_ok=True)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def probe(state_dir: Path) -> LockHolder | None:
        """Return the current holder, or None when no process holds the lock.

        A holder whose record isn't readable yet is reported with pid 0 and kind
        "maintenance".
        """
        try:
            fd = os.open(state_dir / _LOCK_FILE, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read_holder(state_dir) or _UNKNOWN_HOLDER
        finally:
            os.close(fd)
        return None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _read_holder(state_dir: Path) -> LockHolder | None:
    """Read `holder.json`, or return None when it's missing or malformed."""
    try:
        text = (state_dir / _HOLDER_FILE).read_text(encoding="utf-8")
        return LockHolder.from_json(json.loads(text))
    except (OSError, ValueError):
        return None

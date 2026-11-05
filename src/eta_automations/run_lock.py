"""Single-instance OS file lock (03-foundations §14).

Two concurrent runs could double-send e-mails and race the QuickBooks refresh-token rotation (04 §5.3),
so every command that writes holds <ETA_STATE_DIR>/run.lock. The OS drops the lock automatically when
the process dies, so there is no stale-lock handling and NO PID probing (on Windows os.kill(pid, 0)
would terminate the process).
"""

from __future__ import annotations

import contextlib
import errno
import os
import time
from collections.abc import Iterator
from pathlib import Path

from .errors import RunLockedError

__all__ = ["LOCK_FILE_NAME", "run_lock"]

LOCK_FILE_NAME = "run.lock"

_WINDOWS = os.name == "nt"
# errno values meaning "somebody else holds the lock": flock → EWOULDBLOCK/EAGAIN; msvcrt.locking with
# LK_NBLCK → EACCES (EDEADLOCK = 36 on Windows only; 36 is ENAMETOOLONG on Linux).
_CONTENTION_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES} | ({36} if _WINDOWS else set()))


def _info_bytes() -> bytes:
    started = time.strftime("%Y-%m-%dT%H:%M:%S")  # human annotation only
    return f"pid={os.getpid()} started={started}\n".encode("ascii")


def _is_contention(exc: OSError) -> bool:
    return isinstance(exc, (BlockingIOError, PermissionError)) or exc.errno in _CONTENTION_ERRNOS


def _lock_posix(fd: int) -> None:
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_posix(fd: int) -> None:
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _write_info_posix(fd: int) -> None:
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, _info_bytes())


def _lock_windows(fd: int) -> None:
    import msvcrt

    # msvcrt.locking locks bytes from the current position: lock byte 0, so make sure it exists.
    if os.fstat(fd).st_size == 0:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"\n")
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _unlock_windows(fd: int) -> None:
    import msvcrt

    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _write_info_windows(fd: int) -> None:
    # Byte 0 is the locked placeholder; the human-readable line goes after it.
    os.lseek(fd, 1, os.SEEK_SET)
    data = _info_bytes()
    os.write(fd, data)
    os.ftruncate(fd, 1 + len(data))


@contextlib.contextmanager
def run_lock(state_dir: Path) -> Iterator[None]:
    """Open/create <state_dir>/run.lock and take an exclusive, NON-blocking OS lock on it for the
    lifetime of the `with` block: fcntl.flock(fd, LOCK_EX | LOCK_NB) on POSIX,
    msvcrt.locking(fd, LK_NBLCK, 1) on Windows (lock byte 0; a placeholder byte is written first if the
    file is empty). On contention raise RunLockedError("Another ETAutomationsAPI run is in progress
    (lock: <path>)."). Writes "pid=<pid> started=<iso time>" into the file for humans; releases and
    closes on exit."""
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / LOCK_FILE_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        try:
            (_lock_windows if _WINDOWS else _lock_posix)(fd)
        except OSError as exc:
            if _is_contention(exc):
                raise RunLockedError(f"Another ETAutomationsAPI run is in progress (lock: {path}).") from exc
            raise
        try:
            with contextlib.suppress(OSError):  # the annotation is informative only
                (_write_info_windows if _WINDOWS else _write_info_posix)(fd)
            yield
        finally:
            with contextlib.suppress(OSError):  # closing the descriptor releases the lock anyway
                (_unlock_windows if _WINDOWS else _unlock_posix)(fd)
    finally:
        os.close(fd)

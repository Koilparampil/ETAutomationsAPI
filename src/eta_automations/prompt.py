"""Interruptible console input for coroutines (10 §4 Phase 3 step 7 confirmation, 04 §5.4 paste-back; review fix
RUL-04).

`asyncio.to_thread(input, …)` ran input() in the event loop's default executor. Its worker thread is joined when
asyncio.run() ends and again at interpreter exit, so after Ctrl+C the process stayed alive until ENTER: a second
Ctrl+C only reached the CLI's handler and a third ended it with an 'Exception ignored on threading shutdown'
traceback. A daemon thread does not help on its own: on a terminal, input() blocks in the C library's fgets(stdin)
holding the stdin FILE lock, and glibc's exit() waits for that lock to flush the streams.

read_line() therefore reads a terminal on POSIX without any thread: the event loop watches stdin (add_reader) and
the line is read with os.read(), so a cancelled wait (Ctrl+C) just stops watching and nothing is left blocked.
Elsewhere (the Windows console, whose input() uses ReadConsoleW and no C stdio lock; a non-terminal stdin; an
injected input function) input_fn runs in a daemon thread whose answer is handed over through a future.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import os
import sys
import threading
from collections.abc import Callable

__all__ = ["pending_reader", "read_line"]

_last_reader: threading.Thread | None = None    # the daemon thread of the latest threaded read
_last_answered = threading.Event()              # set once that thread got its line (or EOF / an error)


async def read_line(input_fn: Callable[[str], str], prompt: str) -> str:
    """input_fn(prompt) without blocking the event loop, interruptible by cancelling the awaiting task. The answer
    is returned; EOFError (and any other exception of input_fn) is raised here."""
    loop = asyncio.get_running_loop()
    if input_fn is builtins.input:
        fd = _terminal_stdin()
        if fd is not None:
            with contextlib.suppress(_NotWatchable):
                return await _read_terminal(loop, fd, prompt)
    return await _read_in_thread(loop, input_fn, prompt)


def pending_reader() -> threading.Thread | None:
    """The daemon thread of the latest threaded read while it still waits for its line (its caller stopped waiting,
    e.g. on Ctrl+C), else None. The double-click pause joins it: that thread already waits for the ENTER."""
    thread = _last_reader
    if thread is None or _last_answered.is_set() or not thread.is_alive():
        return None
    return thread


# ── POSIX terminal: the event loop watches stdin ──
class _NotWatchable(Exception):
    """This loop cannot watch stdin (e.g. the Windows proactor loop): use a thread instead."""


def _terminal_stdin() -> int | None:
    if os.name != "posix":
        return None
    stream = sys.stdin
    try:
        fd = stream.fileno() if stream is not None else -1
        return fd if fd >= 0 and os.isatty(fd) else None
    except (AttributeError, OSError, ValueError):
        return None


async def _read_terminal(loop: asyncio.AbstractEventLoop, fd: int, prompt: str) -> str:
    """One line from the terminal on `fd` (canonical mode: the terminal edits and echoes it)."""
    future: asyncio.Future[str] = loop.create_future()
    pending = bytearray()
    encoding = getattr(sys.stdin, "encoding", None) or "utf-8"

    def readable() -> None:
        if future.done():
            return
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            future.set_exception(exc)
            return
        if not chunk:                                       # Ctrl+D / end of input
            future.set_exception(EOFError())
            return
        pending.extend(chunk)
        if b"\n" in pending:
            line = bytes(pending).split(b"\n", 1)[0]
            future.set_result(line.decode(encoding, "replace").rstrip("\r"))

    try:
        loop.add_reader(fd, readable)
    except (NotImplementedError, OSError, ValueError) as exc:
        raise _NotWatchable from exc
    try:
        print(prompt, end="", flush=True)
        return await future
    finally:
        loop.remove_reader(fd)


# ── anything else: a daemon thread ──
async def _read_in_thread(loop: asyncio.AbstractEventLoop, input_fn: Callable[[str], str], prompt: str) -> str:
    global _last_reader
    future: asyncio.Future[str] = loop.create_future()

    def deliver(answer: str | None, error: BaseException | None) -> None:
        if future.done():                                   # the wait was cancelled meanwhile
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(answer if answer is not None else "")

    def reader() -> None:
        answer: str | None = None
        error: BaseException | None = None
        try:
            answer = input_fn(prompt)
        except BaseException as exc:  # noqa: BLE001 - handed to the waiting coroutine
            error = exc
        if _last_reader is threading.current_thread():
            _last_answered.set()
        with contextlib.suppress(RuntimeError):            # the loop is closed: nobody waits for the answer
            loop.call_soon_threadsafe(deliver, answer, error)

    thread = threading.Thread(target=reader, name="eta-input", daemon=True)
    _last_answered.clear()
    _last_reader = thread
    thread.start()
    return await future

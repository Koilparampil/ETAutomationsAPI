"""prompt.read_line(): interruptible console input for the asyncio parts (review fix RUL-04).

`asyncio.to_thread(input, …)` ran input() in the loop's default executor, whose worker is joined when asyncio.run()
ends and again at interpreter exit: after one Ctrl+C at the `[y/N]` prompt the process stayed alive, a second only
reached the CLI's handler, and a third ended it with an 'Exception ignored on threading shutdown' traceback.
"""

from __future__ import annotations

import asyncio
import os
import select
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from eta_automations import prompt
from eta_automations.prompt import pending_reader, read_line

TESTS_DIR = Path(__file__).resolve().parent


async def test_read_line_returns_the_answer_and_passes_the_prompt() -> None:
    seen: list[tuple[str, str]] = []

    def input_fn(text: str) -> str:
        seen.append((text, threading.current_thread().name))
        return " y "

    assert await read_line(input_fn, "Apply? [y/N] ") == " y "
    assert seen == [("Apply? [y/N] ", "eta-input")]
    assert pending_reader() is None


async def test_read_line_reraises_what_input_raised() -> None:
    def eof(text: str) -> str:
        raise EOFError

    with pytest.raises(EOFError):
        await read_line(eof, "> ")


async def test_a_cancelled_read_returns_at_once_and_leaves_only_a_daemon_thread() -> None:
    entered, release = threading.Event(), threading.Event()

    def blocking(text: str) -> str:
        entered.set()
        release.wait(10)
        return "late answer"

    task = asyncio.create_task(read_line(blocking, "Apply? [y/N] "))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    started = time.perf_counter()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.perf_counter() - started < 1.0
    reader = pending_reader()
    assert reader is not None and reader.daemon and reader.is_alive()   # never joined at interpreter exit
    await asyncio.wait_for(asyncio.get_running_loop().shutdown_default_executor(), 1)
    release.set()
    reader.join(5)
    assert pending_reader() is None and not reader.is_alive()


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "openpty"), reason="needs a POSIX pseudo-terminal")
def test_one_ctrl_c_at_the_confirmation_prompt_ends_eta_run(tmp_path: Path) -> None:
    """The real CLI path (`eta run` with the fakes) on a pseudo-terminal: one SIGINT at the [y/N] prompt ends the
    process with exit code 3, without a hang and without an 'Exception ignored' traceback."""
    env_file = tmp_path / "test.env"
    env_file.write_text(
        f"ETA_OUTPUT_DIR={tmp_path / 'output'}\nETA_STATE_DIR={tmp_path / 'state'}\nQB_CLIENT_ID=id\n"
        "QB_CLIENT_SECRET=secret\nQB_REALM_ID=1234567890\nQB_REFRESH_TOKEN=rt\nVSHIP_USER_NAME=ops@example.com\n"
        "VSHIP_PASSWORD=pw\n", encoding="utf-8")
    script = tmp_path / "eta_prompt.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        from datetime import datetime
        sys.path.insert(0, {str(TESTS_DIR)!r})
        from fakes import FakeWorld, fake_build_services
        from eta_automations import cli, pipeline
        from eta_automations.clock import FixedClock

        world = FakeWorld(FixedClock(datetime(2026, 11, 5, 9, 30)))
        booking = world.add("EBKG10000001", "2450.00", eta=datetime(2026, 11, 3))
        csv_path = {str(tmp_path / 'input.csv')!r}
        open(csv_path, "w", encoding="utf-8").write(booking.line + "\\n")
        pipeline.build_services = fake_build_services(world)
        sys.exit(cli.main(["run", csv_path, "--env-file", {str(env_file)!r}]))
    """), encoding="utf-8")
    master, slave = os.openpty()
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    proc = subprocess.Popen([sys.executable, str(script)], stdin=slave, stdout=slave, stderr=slave, cwd=tmp_path,
                            env=env)
    os.close(slave)
    output = b""

    def read_available(timeout: float) -> bool:
        nonlocal output
        ready, _, _ = select.select([master], [], [], timeout)
        if not ready:
            return True
        try:
            chunk = os.read(master, 4096)
        except OSError:                         # EIO: the child closed the terminal (it exited)
            return False
        output += chunk
        return bool(chunk)

    try:
        deadline = time.monotonic() + 30
        while b"[y/N]" not in output:           # input() writes the prompt to the terminal (stderr)
            assert time.monotonic() < deadline, f"no prompt; output so far: {output!r}"
            assert read_available(0.5), f"exited early: {output!r}"
        time.sleep(0.3)                         # the reader is now blocked in input()
        proc.send_signal(signal.SIGINT)         # one Ctrl+C
        code = proc.wait(timeout=10)
        while read_available(0.2):
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
    text = output.decode("utf-8", "replace")
    assert code == 3, text
    assert "Interrupted (Ctrl+C)." in text
    for bad in ("Exception ignored", "Fatal Python error", "Traceback"):
        assert bad not in text


def test_pause_after_an_abandoned_prompt_waits_for_the_same_enter(monkeypatch: pytest.MonkeyPatch,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    """Double-click mode: after a Ctrl+C at the [y/N] prompt its reader thread still waits for ENTER, and the window's
    'Press ENTER to close' pause uses that same ENTER instead of asking for a second one."""
    from eta_automations import cli

    release = threading.Event()
    reader = threading.Thread(target=release.wait, args=(10,), daemon=True)
    reader.start()
    monkeypatch.setattr(prompt, "_last_reader", reader)
    monkeypatch.setattr(prompt, "_last_answered", threading.Event())   # it has not got its line yet
    asked: list[str] = []
    monkeypatch.setattr("builtins.input", lambda text="": asked.append(text) or "")
    threading.Timer(0.2, release.set).start()
    cli._pause_before_exit()
    assert asked == [] and not reader.is_alive()
    assert cli.PAUSE_PROMPT in capsys.readouterr().out
    cli._pause_before_exit()                        # no abandoned reader: the normal input() pause
    assert asked == [cli.PAUSE_PROMPT]


class _TerminalStdin:
    """sys.stdin stand-in on a pseudo-terminal's slave end."""

    encoding = "utf-8"

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """A pseudo-terminal as stdin; yields the master end to type into."""
    if sys.platform == "win32" or not hasattr(os, "openpty"):
        pytest.skip("needs a POSIX pseudo-terminal")
    master, slave = os.openpty()
    monkeypatch.setattr(sys, "stdin", _TerminalStdin(slave))
    try:
        yield master
    finally:
        os.close(master)
        os.close(slave)


async def test_terminal_line_is_read_by_the_event_loop_without_a_thread(
        terminal: int, capsys: pytest.CaptureFixture[str]) -> None:
    threads = threading.active_count()
    task = asyncio.create_task(read_line(input, "Apply? [y/N] "))
    await asyncio.sleep(0.05)
    assert threading.active_count() == threads            # no reader thread on a POSIX terminal
    os.write(terminal, "yes ✓\n".encode())
    assert await asyncio.wait_for(task, 5) == "yes ✓"
    assert capsys.readouterr().out == "Apply? [y/N] "


async def test_terminal_end_of_input_is_eof(terminal: int) -> None:
    task = asyncio.create_task(read_line(input, "> "))
    await asyncio.sleep(0.05)
    os.write(terminal, b"\x04")                            # Ctrl+D at the start of a line
    with pytest.raises(EOFError):
        await asyncio.wait_for(task, 5)


async def test_a_cancelled_terminal_read_leaves_nothing_waiting(terminal: int) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(read_line(input, "> "))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    watched = {key.fd for key in loop._selector.get_map().values()}   # type: ignore[attr-defined]
    assert sys.stdin.fileno() not in watched                          # stdin is no longer watched
    assert pending_reader() is None

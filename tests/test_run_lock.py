"""run_lock.py: single-instance OS lock (03-foundations §14; pipeline test P38 relies on it)."""

from __future__ import annotations

import ast
import errno
import os
import re
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from eta_automations import run_lock as run_lock_module
from eta_automations.errors import RunLockedError
from eta_automations.run_lock import LOCK_FILE_NAME, run_lock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def _expected_message(state_dir: Path) -> str:
    return f"Another ETAutomationsAPI run is in progress (lock: {state_dir / LOCK_FILE_NAME})."


def test_lock_writes_pid_and_start_time(tmp_path: Path) -> None:
    with run_lock(tmp_path):
        content = (tmp_path / LOCK_FILE_NAME).read_text(encoding="ascii")
    assert re.fullmatch(rf"pid={os.getpid()} started=\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\d\n", content)


def test_second_lock_in_same_process_is_refused(tmp_path: Path) -> None:
    with run_lock(tmp_path):
        with pytest.raises(RunLockedError) as info:
            with run_lock(tmp_path):
                pytest.fail("must not enter")  # pragma: no cover
    assert str(info.value) == _expected_message(tmp_path)
    assert isinstance(info.value.__cause__, OSError)


def test_lock_is_released_on_exit_and_on_exception(tmp_path: Path) -> None:
    with run_lock(tmp_path):
        pass
    with pytest.raises(ValueError):
        with run_lock(tmp_path):
            raise ValueError("boom")
    with run_lock(tmp_path):  # can be taken again
        pass


def test_missing_state_dir_is_created(tmp_path: Path) -> None:
    state = tmp_path / "a" / "b"
    with run_lock(state):
        assert (state / LOCK_FILE_NAME).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX flock semantics")
def test_lock_held_by_another_process_blocks_until_it_exits(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from eta_automations.run_lock import run_lock
        with run_lock(Path(sys.argv[1])):
            print("locked", flush=True)
            sys.stdin.readline()
        """
    )
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
    with subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True, env=env) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            assert proc.stdout.readline().strip() == "locked"
            with pytest.raises(RunLockedError, match="Another ETAutomationsAPI run is in progress"):
                with run_lock(tmp_path):
                    pass  # pragma: no cover
        finally:
            try:
                proc.stdin.write("\n")
                proc.stdin.flush()
            except BrokenPipeError:  # pragma: no cover - child already gone
                pass
            proc.wait(timeout=30)
    with run_lock(tmp_path):  # the OS released it when the other process ended
        pass


def test_non_contention_os_errors_are_not_reported_as_locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_lock(fd: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(run_lock_module, "_WINDOWS", False)
    monkeypatch.setattr(run_lock_module, "_lock_posix", broken_lock)
    with pytest.raises(OSError) as info:
        with run_lock(tmp_path):
            pass  # pragma: no cover
    assert not isinstance(info.value, RunLockedError)


# ── Windows branch, exercised with a fake msvcrt module ──────────────────────────────────────────────
class FakeMsvcrt(types.ModuleType):
    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2

    def __init__(self, fail_with: int | None = None) -> None:
        super().__init__("msvcrt")
        self.calls: list[tuple[int, int, int]] = []  # (mode, position, nbytes)
        self.fail_with = fail_with

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        position = os.lseek(fd, 0, os.SEEK_CUR)
        self.calls.append((mode, position, nbytes))
        if mode == self.LK_NBLCK and self.fail_with is not None:
            raise OSError(self.fail_with, "Permission denied")


def test_windows_branch_writes_placeholder_locks_byte_zero_and_unlocks(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(run_lock_module, "_WINDOWS", True)
    with run_lock(tmp_path):
        content = (tmp_path / LOCK_FILE_NAME).read_bytes()
    assert content[:1] == b"\n"  # placeholder byte 0 written because the file was empty
    assert re.fullmatch(rb"\npid=\d+ started=\S+\n", content)
    assert fake.calls == [(FakeMsvcrt.LK_NBLCK, 0, 1), (FakeMsvcrt.LK_UNLCK, 0, 1)]


def test_windows_branch_contention_raises_run_locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / LOCK_FILE_NAME).write_bytes(b"\npid=1 started=x\n")
    fake = FakeMsvcrt(fail_with=errno.EACCES)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(run_lock_module, "_WINDOWS", True)
    with pytest.raises(RunLockedError) as info:
        with run_lock(tmp_path):
            pass  # pragma: no cover
    assert str(info.value) == _expected_message(tmp_path)
    assert fake.calls == [(FakeMsvcrt.LK_NBLCK, 0, 1)]  # existing file: no placeholder rewrite, no unlock
    assert (tmp_path / LOCK_FILE_NAME).read_bytes() == b"\npid=1 started=x\n"  # other instance's info kept


def test_no_pid_probing_in_module_source() -> None:
    """03 §14: no PID probing (on Windows os.kill(pid, 0) would terminate the process)."""
    tree = ast.parse(Path(run_lock_module.__file__).read_text(encoding="utf-8"))
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert not {"os.kill", "kill", "os.getpgid", "psutil.pid_exists"} & called

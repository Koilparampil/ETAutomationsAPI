"""Safety statements the user documentation must keep (11 §8 README safety notes; 04b §10).

Plain text checks of README.md and .env.example for review findings whose fix is (partly) documentation.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")


def _env_comment(name: str) -> str:
    """The comment block right above `# NAME=` / `NAME=` in .env.example."""
    lines = ENV_EXAMPLE.splitlines()
    index = next(i for i, line in enumerate(lines) if line.lstrip("# ").startswith(f"{name}="))
    block: list[str] = []
    for line in reversed(lines[:index]):
        if not line.startswith("#"):
            break
        block.insert(0, line)
    return " ".join(block)


def test_shadow_mode_docs_say_quickbooks_and_vship_changes_still_happen() -> None:
    """CLI-01: QB_EMAIL_REDIRECT_TO changes only the recipients (04b §10 step 4)."""
    row = next(line for line in README.splitlines() if line.startswith("| `QB_EMAIL_REDIRECT_TO`"))
    for text in (row, _env_comment("QB_EMAIL_REDIRECT_TO")):
        assert "QuickBooks" in text and "VShip" in text and "still" in text
        assert "QB_SANDBOX=true" in text
    safety = README.split("## Safety notes", 1)[1].split("\n## ", 1)[0]
    assert "QB_EMAIL_REDIRECT_TO" in safety


def test_write_probe_docs_do_not_promise_that_prefs_roundtrip_can_undo_everything() -> None:
    """CLI-03: --prefs-roundtrip writes back the whole Preferences object and cannot undo a lossy write-back."""
    assert "always restore what they change" not in README
    probes = README.split("### Probes", 1)[1].split("\n### ", 1)[0]
    assert "--prefs-roundtrip" in probes and "cannot undo" in probes and "QB_SANDBOX=true" in probes
    e_mail_steps = README.split("**6. E-mail verification**", 1)[1].split("\n**7.", 1)[0]
    step = e_mail_steps.split("1. `eta probe qbo --prefs-roundtrip`", 1)[1].split("\n2. ", 1)[0]
    assert "QB_SANDBOX=true" in step and "--yes" in step                 # sandbox first, then --yes on production


def test_ctrl_c_safety_note_is_about_one_ctrl_c_and_says_what_a_second_one_does() -> None:
    """NEW-01: the safety note promised that Ctrl+C never splits a notice. That holds for the program's own stop: one
    Ctrl+C, after which a further Ctrl+C only repeats the stop message. Ending the process otherwise still can."""
    safety = README.split("## Safety notes", 1)[1].split("\n## ", 1)[0]
    note = next(item for item in safety.split("\n- ") if "Ctrl+C" in item)
    assert "never left with the `A` suffix" not in note
    assert "do not press Ctrl+C again" in note and "only repeats" in note
    assert "closing the window" in note.lower() and "half-recorded" in note
    notes = (REPO_ROOT / "IMPLEMENTATION_NOTES.md").read_text(encoding="utf-8")
    assert "Ctrl+C never splits a notice" not in notes


def test_ctrl_c_docs_cover_quick_presses_piped_output_and_both_stop_messages() -> None:
    """R2-01 / R2-02: the safety note says what piped output does (the run still finishes, exit code 3) and that a
    further Ctrl+C only repeats the message however quickly it comes; the troubleshooting row lists both forms of the
    stop message. IMPLEMENTATION_NOTES no longer says the handler only comes with the first Ctrl+C."""
    safety = README.split("## Safety notes", 1)[1].split("\n## ", 1)[0]
    note = " ".join(next(item for item in safety.split("\n- ") if "Ctrl+C" in item).split())
    assert "| tee run.log" in note and "the run still finishes as described and ends with exit code 3" in note
    assert "however quickly" in note and "log file" in note
    row = next(line for line in README.splitlines() if line.startswith("| `Stopping (Ctrl+C):"))
    assert "finishing the changes already started for <n> invoice(s)." in row and "finishing up." in row
    notes = " ".join((REPO_ROOT / "IMPLEMENTATION_NOTES.md").read_text(encoding="utf-8").split())
    assert "first Ctrl+C during apply until" not in notes


def test_recovery_file_docs_tell_another_company_s_file_from_a_damaged_one() -> None:
    """R2-03: only another company's recovery file is harmless for runs without template_swap e-mails (exit code 0); a
    damaged file or this company's without an original subject is reported with what to do (exit code 4)."""
    rows = [line for line in README.splitlines() if "recovery file" in line and line.startswith("| `")]
    other = next(line for line in rows if line.startswith("| `recovery file belongs to another QuickBooks company"))
    damaged = next(line for line in rows if "names no QuickBooks company" in line)
    assert "exit code 0" in other and "cannot be read" not in other and "no original subject" not in other
    assert "exit code 4" in damaged and "has no original subject" in damaged and "then delete the file" in damaged

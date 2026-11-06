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

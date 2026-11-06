"""token_store.py: atomic writes, 0600, corrupt file, is_fresh skew, JWT exp (03-foundations §7; 12-testing §3)."""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import threading
from pathlib import Path

import pytest

from eta_automations import token_store as token_store_module
from eta_automations.token_store import TokenStore, is_fresh, jwt_exp

# 05 §9 tests/fixtures/vship/login_ok.json token: payload decodes to {"exp":1794000000,"sub":"test"}
VSHIP_SAMPLE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3OTQwMDAwMDAsInN1YiI6InRlc3QifQ.c2lnbmF0dXJl"


def _b64url(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _jwt(payload: dict[str, object], header: dict[str, object] | None = None, signature: str = "sig") -> str:
    return f"{_b64url(header or {'alg': 'none'})}.{_b64url(payload)}.{signature}"


# ── TokenStore ─────────────────────────────────────────────────────────────────────────────────────
def test_put_then_get_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "state" / "tokens.json"
    TokenStore(path).put("vship", {"access_token": "AT-1", "expires_at": 1794000000.0})
    TokenStore(path).put("qbo", {"access_token": "AT-2", "expires_at": 1.0, "refresh_token": "RT"})
    fresh = TokenStore(path)
    assert fresh.get("vship") == {"access_token": "AT-1", "expires_at": 1794000000.0}
    assert fresh.get("qbo")["refresh_token"] == "RT"  # type: ignore[index]
    assert fresh.get("msc") is None
    assert json.loads(path.read_text(encoding="utf-8")).keys() == {"vship", "qbo"}


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")
def test_file_mode_is_0600_on_posix(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    store.put("maersk", {"access_token": "x", "expires_at": 2.0})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    store.put("maersk", {"access_token": "y", "expires_at": 3.0})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")
def test_missing_state_dir_is_created_user_only(tmp_path: Path) -> None:
    path = tmp_path / "new-state" / "tokens.json"
    TokenStore(path).put("cma", {"access_token": "x", "expires_at": 1.0})
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_write_is_atomic_via_os_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    store.put("qbo", {"access_token": "old", "expires_at": 1.0})
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy_replace(src: str, dst: str) -> None:
        calls.append((str(src), str(dst)))
        assert Path(src).parent == path.parent  # temp file lives in the same directory
        real_replace(src, dst)

    monkeypatch.setattr(token_store_module.os, "replace", spy_replace)
    store.put("qbo", {"access_token": "new", "expires_at": 2.0})
    assert len(calls) == 1 and calls[0][1] == str(path)
    assert store.get("qbo") == {"access_token": "new", "expires_at": 2.0}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tokens.json"]  # no temp files left


def test_failed_write_keeps_previous_file_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    store.put("qbo", {"access_token": "old", "expires_at": 1.0})

    def failing_replace(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(token_store_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        store.put("qbo", {"access_token": "new", "expires_at": 2.0})
    monkeypatch.undo()
    assert TokenStore(path).get("qbo") == {"access_token": "old", "expires_at": 1.0}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tokens.json"]


def test_non_serializable_value_does_not_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    store.put("qbo", {"access_token": "old", "expires_at": 1.0})
    with pytest.raises(TypeError):
        store.put("bad", {"access_token": object(), "expires_at": 1.0})
    assert store.get("qbo") == {"access_token": "old", "expires_at": 1.0}


@pytest.mark.parametrize("content", ["{not json", "[1, 2, 3]", "\x00\x01garbage"])
def test_corrupt_file_warns_reads_empty_and_is_overwritten(tmp_path: Path, caplog: pytest.LogCaptureFixture,
                                                          content: str) -> None:
    path = tmp_path / "tokens.json"
    path.write_text(content, encoding="utf-8")
    store = TokenStore(path)
    with caplog.at_level(logging.WARNING, logger="eta"):
        assert store.get("qbo") is None
        assert store.get("vship") is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "tokens.json" in r.getMessage()]
    assert len(warnings) == 1  # warned once, not on every read
    store.put("qbo", {"access_token": "x", "expires_at": 1.0})
    assert json.loads(path.read_text(encoding="utf-8")) == {"qbo": {"access_token": "x", "expires_at": 1.0}}


def test_empty_file_is_empty_store(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text("", encoding="utf-8")
    assert TokenStore(path).get("qbo") is None


def test_delete_and_get_returns_copy(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    store.put("vship", {"access_token": "a", "expires_at": 5.0})
    store.put("msc", {"access_token": "b", "expires_at": 6.0})
    entry = store.get("vship")
    assert entry is not None
    entry["access_token"] = "mutated"
    assert store.get("vship") == {"access_token": "a", "expires_at": 5.0}
    store.delete("vship")
    store.delete("never-existed")
    assert store.get("vship") is None
    assert store.get("msc") == {"access_token": "b", "expires_at": 6.0}


def test_concurrent_puts_from_threads_keep_every_key(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")

    def worker(i: int) -> None:
        store.put(f"svc{i}", {"access_token": f"t{i}", "expires_at": float(i)})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    reread = TokenStore(tmp_path / "tokens.json")
    assert all(reread.get(f"svc{i}") == {"access_token": f"t{i}", "expires_at": float(i)} for i in range(16))


# ── is_fresh ───────────────────────────────────────────────────────────────────────────────────────
NOW = 1_800_000_000.0


@pytest.mark.parametrize("entry,expected", [
    (None, False),
    ({}, False),
    ({"expires_at": NOW + 3600}, False),                                  # no access_token
    ({"access_token": "", "expires_at": NOW + 3600}, False),
    ({"access_token": "t"}, False),                                       # no expires_at
    ({"access_token": "t", "expires_at": "soon"}, False),
    ({"access_token": "t", "expires_at": True}, False),
    ({"access_token": "t", "expires_at": float("nan")}, False),
    ({"access_token": "t", "expires_at": NOW + 120}, False),              # exactly at the skew → stale
    ({"access_token": "t", "expires_at": NOW + 60}, False),               # within the 120 s skew
    ({"access_token": "t", "expires_at": NOW + 121}, True),
    ({"access_token": "t", "expires_at": str(NOW + 3600)}, True),
])
def test_is_fresh_with_default_skew(entry: dict[str, object] | None, expected: bool) -> None:
    assert is_fresh(entry, now=NOW) is expected


def test_is_fresh_custom_skew_and_real_clock() -> None:
    assert is_fresh({"access_token": "t", "expires_at": NOW + 61}, skew_seconds=60, now=NOW)
    assert not is_fresh({"access_token": "t", "expires_at": NOW + 59}, skew_seconds=60, now=NOW)
    import time

    assert is_fresh({"access_token": "t", "expires_at": time.time() + 3600})
    assert not is_fresh({"access_token": "t", "expires_at": time.time() + 100})


# ── jwt_exp ────────────────────────────────────────────────────────────────────────────────────────
def test_jwt_exp_from_vship_sample_token() -> None:
    assert jwt_exp(VSHIP_SAMPLE_JWT) == 1794000000.0


def test_jwt_exp_alg_none_token_with_empty_signature() -> None:
    token = _jwt({"exp": 1793860000, "iss": "x"}, signature="")
    assert token.endswith(".")
    assert jwt_exp(token) == 1793860000.0


@pytest.mark.parametrize("payload_extra", ["", "a", "ab", "abc"])
def test_jwt_exp_handles_any_padding(payload_extra: str) -> None:
    token = _jwt({"exp": 1700000000.5, "pad": payload_extra})
    assert jwt_exp(token) == 1700000000.5


@pytest.mark.parametrize("token", [
    "not-a-jwt",
    "only.two",
    "a.b.c.d",
    ".eyJleHAiOjF9.sig",
    "eyJhbGciOiJub25lIn0.!!!notbase64!!!.sig",
    _jwt({"sub": "no-exp"}),
    _jwt({"exp": "1794000000"}),     # not numeric
    _jwt({"exp": True}),
    "eyJhbGciOiJub25lIn0." + base64.urlsafe_b64encode(b"[1,2]").decode() + ".sig",   # payload not an object
    "eyJhbGciOiJub25lIn0." + base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode() + ".sig",  # not UTF-8 JSON
    "",
])
def test_jwt_exp_returns_none_for_non_jwts(token: str) -> None:
    assert jwt_exp(token) is None


def test_jwt_exp_rejects_non_string() -> None:
    assert jwt_exp(None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", [b'{"exp": 1' + b"0" * 400 + b"}", b'{"exp": 1e400}', b'{"exp": -1e400}'])
def test_jwt_exp_returns_none_for_an_exp_too_large_for_a_float(payload: bytes) -> None:
    """03 §7: None on any decoding error — an integer `exp` of 400 digits overflows float() (FND-03)."""
    token = "eyJhbGciOiJub25lIn0." + base64.urlsafe_b64encode(payload).decode().rstrip("=") + ".sig"
    assert jwt_exp(token) is None

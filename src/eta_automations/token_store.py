"""Token cache in one JSON file, e.g. <ETA_STATE_DIR>/tokens.json (03-foundations §7).

Top-level keys are service names ("qbo", "vship", "msc", "maersk", "cma", …); each value has at least
`access_token` and `expires_at` (epoch seconds). Writes are atomic (temp file in the same directory,
flush + fsync, os.replace) and the file is chmod 0600 on POSIX. A corrupt/unreadable file is logged
once as a WARNING, treated as empty, and overwritten on the next put().
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import math
import os
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["TokenStore", "is_fresh", "jwt_exp"]

log = logging.getLogger("eta")

_POSIX = os.name == "posix"


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._warned_unreadable = False

    # ── public API ──
    def get(self, key: str) -> dict[str, Any] | None:
        """A copy of the entry for `key`, or None."""
        with self._lock:
            value = self._read().get(key)
        return dict(value) if isinstance(value, dict) else None

    def put(self, key: str, value: dict[str, Any]) -> None:
        """Insert/replace one entry (atomic write, 0600 on POSIX)."""
        with self._lock:
            data = self._read()
            data[key] = dict(value)
            self._write(data)

    def delete(self, key: str) -> None:
        """Remove one entry (no-op when absent)."""
        with self._lock:
            data = self._read()
            if key in data:
                del data[key]
                self._write(data)

    # ── internals (call with self._lock held) ──
    def _warn_once(self, reason: str) -> None:
        if not self._warned_unreadable:
            log.warning("Token store %s %s — treating it as empty; it will be rewritten on the next save",
                        self.path, reason)
            self._warned_unreadable = True

    def _read(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            self._warn_once(f"could not be read ({type(exc).__name__})")
            return {}
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            self._warn_once("is not valid JSON")
            return {}
        if not isinstance(data, dict):
            self._warn_once("does not contain a JSON object")
            return {}
        return data

    def _write(self, data: Mapping[str, Any]) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(data, indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=directory)
        try:
            if _POSIX:
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        if _POSIX:
            os.chmod(self.path, 0o600)
            _fsync_directory(directory)
        self._warned_unreadable = False


def _fsync_directory(directory: Path) -> None:
    """Persist the rename itself (best effort, POSIX only)."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def is_fresh(entry: Mapping[str, Any] | None, *, skew_seconds: int = 120, now: float | None = None) -> bool:
    """True if entry has 'access_token' and 'expires_at' (epoch seconds) > now + skew."""
    if not entry:
        return False
    token = entry.get("access_token")
    if not isinstance(token, str) or not token:
        return False
    expires_at = entry.get("expires_at")
    if isinstance(expires_at, bool):
        return False
    try:
        expires = float(expires_at)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if not math.isfinite(expires):
        return False
    current = time.time() if now is None else now
    return expires > current + skew_seconds


def jwt_exp(token: str) -> float | None:
    """If `token` looks like a JWT (three dot-separated segments), base64url-decode the payload segment
    (padding added as needed, NO signature verification) and return its numeric `exp` claim as epoch
    seconds; otherwise, or on any decoding error, return None. Shared by every auth provider."""
    if not isinstance(token, str):
        return None
    parts = token.strip().split(".")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return None
    segment = parts[1]
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        claims = json.loads(raw)
    except (ValueError, binascii.Error):  # includes JSONDecodeError and UnicodeDecodeError
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    value = float(exp)
    return value if math.isfinite(value) else None

"""Logging configuration and secret redaction (03-foundations §11).

`redact()` is used for log lines, exception body excerpts, probe output and reports. It must never let a
secret through; masking a little too much (e.g. a non-secret value under a key that contains "token")
is acceptable. This module imports nothing from the package (errors.py imports it).
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DATE_FORMAT",
    "LOGGER_NAMES",
    "LOG_FORMAT",
    "MASK",
    "REDACT_INPUT_MAX",
    "SECRET_KEY_RE",
    "RedactingFormatter",
    "configure_logging",
    "is_secret_key",
    "log_file_path",
    "redact",
    "redact_excerpt",
    "reset_logging",
]

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"
ROOT_LOGGER = "eta"
# 03 §11 logger names (children of "eta" inherit its handlers).
LOGGER_NAMES = (
    "eta",
    "eta.http",
    "eta.pipeline",
    "eta.qbo",
    "eta.vship",
    "eta.carrier.msc",
    "eta.carrier.maersk",
    "eta.carrier.cma",
    "eta.carrier.dcsa",
    "eta.carrier.aggregator",
)

MASK = "***"

# 03 §11 key pattern, searched case-insensitively anywhere in the key. Addition: `subscription[-_]?key`
# so MSC's `Ocp-Apim-Subscription-Key` header (06 §6.6 "never log the subscription key") is masked too.
SECRET_KEY_RE = re.compile(
    r"(?i)(authorization|token|secret|password|passwd|cookie|consumer-key|keyid|api[-_]?key"
    r"|client[-_]?secret|refresh|subscription[-_]?key)"
)

# "key": "value" / 'key': 'value' pairs (JSON bodies, Python reprs). An unterminated value at the end of
# a truncated excerpt is masked as well.
_QUOTED_PAIR_RE = re.compile(
    r"""(?P<q1>["'])(?P<key>[^"'\\\r\n]{1,128})(?P=q1)"""
    r"""(?P<sep>\s*[:=]\s*)"""
    r"""(?P<q2>["'])(?P<val>(?:\\[\s\S]?|(?!(?P=q2))[^\\])*)(?P<end>(?P=q2)|\Z)"""
)
# Every pattern below runs in linear time (FND-02): a whitespace run next to another run that can also match
# whitespace is possessive (`*+`: the next token can never be whitespace, so the matches are unchanged), and the
# URL scheme is bounded (an unbounded one rescanned a long "a.a.a…" run from every word start).
# scheme://user:password@host
_URL_USERINFO_RE = re.compile(
    r"(?i)\b(?P<scheme>[a-z][a-z0-9+.\-]{0,31}://)(?P<user>[^/\s:@]+):(?P<pw>[^/\s@]+)@")
# Authorization: Basic xxx / authorization=Bearer xxx (any scheme word)
_AUTH_SCHEME_RE = re.compile(
    r"""(?i)(?P<pre>\b(?:proxy-)?authorization["']?\s*+[:=]\s*+["']?\s*+)"""
    r"""(?P<scheme>basic|bearer|digest|negotiate|ntlm|token)\s+(?P<cred>[^\s"',;]+)"""
)
# 03 §11: "Bearer <x>" → "Bearer ***" anywhere in a string — except a URL path segment such as QuickBooks'
# token endpoint ".../oauth2/v1/tokens/bearer", whose following word is log text, not a credential.
_BEARER_RE = re.compile(r"(?i)(?<!/)\b(?P<scheme>bearer)\s+(?P<tok>[A-Za-z0-9\-._~+/]+=*)")
# URL query parameters / form fields / env-style assignments / HTML bodies: key=value, where the key is
# not preceded by another key character.
_KEY_VALUE_RE = re.compile(
    r"""(?<![A-Za-z0-9_.\-\[\]%])(?P<key>[A-Za-z0-9_.\-\[\]]{1,128})=(?P<val>"[^"]*"|'[^']*'|[^&#\s"',;)}<]*)"""
)
# Header-style lines: "Cookie: a=b; c=d"
_HEADER_LINE_RE = re.compile(r"(?m)^(?P<key>[A-Za-z][A-Za-z0-9_-]{0,63})[ \t]*+:[ \t]*+(?P<val>[^\r\n]*[^\s])")

# redact_excerpt(): only this many characters of a response body are redacted for an error excerpt.
REDACT_INPUT_MAX = 4096
_EXCERPT_CUT_WINDOW = 512   # the cut moves back to the last whitespace within this many characters


def is_secret_key(key: object) -> bool:
    """True if a mapping key / parameter name names a secret (03 §11 pattern)."""
    return bool(SECRET_KEY_RE.search(str(key)))


def _mask_value(value: Any) -> Any:
    # None and booleans cannot carry a secret; keeping them makes diagnostics ("has_token": True) readable.
    if value is None or isinstance(value, bool):
        return value
    return MASK


def _sub_pair(m: re.Match[str]) -> str:
    if not is_secret_key(m.group("key")) or m.group("val") == MASK:
        return m.group(0)
    return f"{m['q1']}{m['key']}{m['q1']}{m['sep']}{m['q2']}{MASK}{m['end']}"


def _sub_userinfo(m: re.Match[str]) -> str:
    if m.group("pw") == MASK:
        return m.group(0)
    return f"{m['scheme']}{m['user']}:{MASK}@"


def _sub_auth_scheme(m: re.Match[str]) -> str:
    if m.group("cred") == MASK:
        return m.group(0)
    return f"{m['pre']}{m['scheme']} {MASK}"


def _sub_key_value(m: re.Match[str]) -> str:
    val = m.group("val")
    if not is_secret_key(m.group("key")) or val.strip("\"'") == MASK:
        return m.group(0)
    return f"{m['key']}={MASK}"


def _sub_header_line(m: re.Match[str]) -> str:
    if not is_secret_key(m.group("key")) or m.group("val") == MASK:
        return m.group(0)
    return f"{m['key']}: {MASK}"


def _redact_text(text: str) -> str:
    text = _QUOTED_PAIR_RE.sub(_sub_pair, text)
    text = _URL_USERINFO_RE.sub(_sub_userinfo, text)
    text = _AUTH_SCHEME_RE.sub(_sub_auth_scheme, text)
    text = _BEARER_RE.sub(lambda m: f"{m['scheme']} {MASK}", text)
    text = _KEY_VALUE_RE.sub(_sub_key_value, text)
    return _HEADER_LINE_RE.sub(_sub_header_line, text)


def redact(value: Any) -> Any:
    """Return a redacted copy of `value` (03 §11).

    - Mapping → dict: the value of every key matching SECRET_KEY_RE becomes "***" (None/bool kept);
      other values are redacted recursively.
    - list / tuple → same type, items redacted recursively.
    - str → "Bearer <x>" → "Bearer ***"; secret URL query parameters, form fields, quoted JSON/repr pairs,
      header lines and URL passwords → "***".
    - anything else is returned unchanged.
    """
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        return {k: (_mask_value(v) if is_secret_key(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    return value


def redact_excerpt(text: str, limit: int = 500) -> str:
    """`redact(text)[:limit]` for a response body of any size (03 §6 body_excerpt): only the first
    REDACT_INPUT_MAX characters are redacted, so a huge or hostile body costs bounded work in the event-loop
    thread (FND-02). The cut moves back to the last whitespace in its final 512 characters, so a token such as
    `https://user:password@host` is never split; a value cut at the boundary is masked like any unterminated
    value."""
    if len(text) > REDACT_INPUT_MAX:
        cut = text[:REDACT_INPUT_MAX]
        space = max(cut.rfind(" "), cut.rfind("\t"), cut.rfind("\n"), cut.rfind("\r"))
        text = cut[:space] if space >= REDACT_INPUT_MAX - _EXCERPT_CUT_WINDOW else cut
    return _redact_text(text)[:limit]


class RedactingFormatter(logging.Formatter):
    """Formatter that passes the whole formatted record (message, traceback) through redact()."""

    def format(self, record: logging.LogRecord) -> str:
        return _redact_text(super().format(record))


def log_file_path(output_dir: Path, when: datetime | None = None) -> Path:
    """<ETA_OUTPUT_DIR>/logs/eta_<YYYYmmdd_HHMMSS>.log (03 §11)."""
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")  # log timestamp (allowed by 03 §3)
    return Path(output_dir) / "logs" / f"eta_{stamp}.log"


def _parse_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    mapping = logging.getLevelNamesMapping()
    name = str(level).strip().upper()
    if name not in mapping:
        raise ValueError(f"unknown log level {level!r} (use DEBUG, INFO, WARNING, ERROR or CRITICAL)")
    return mapping[name]


_OWNED_ATTR = "_eta_owned_handler"


def reset_logging() -> None:
    """Remove (and close) the handlers installed by configure_logging(); reset the 'eta' logger level."""
    logger = logging.getLogger(ROOT_LOGGER)
    for handler in list(logger.handlers):
        if getattr(handler, _OWNED_ATTR, False):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(logging.NOTSET)


def configure_logging(level: str | int = "INFO", log_file: Path | None = None) -> None:
    """Console handler at `level` (+ optional DEBUG file handler), on the 'eta' logger (03 §11).

    Idempotent: calling it again replaces the handlers it installed earlier (so the CLI may configure a
    preliminary console before load_settings() and reconfigure with ETA_LOG_LEVEL afterwards).
    Raises ValueError for an unknown level name.
    """
    console_level = _parse_level(level)
    reset_logging()
    logger = logging.getLogger(ROOT_LOGGER)
    formatter = RedactingFormatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(formatter)
    setattr(console, _OWNED_ATTR, True)
    logger.addHandler(console)
    effective = console_level

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        setattr(file_handler, _OWNED_ATTR, True)
        logger.addHandler(file_handler)
        effective = logging.DEBUG

    logger.setLevel(min(effective, console_level))

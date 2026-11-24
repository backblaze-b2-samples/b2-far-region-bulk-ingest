"""The demo's pure helpers: no network, so `make test` covers them offline."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterable, Mapping

KEY_ROOT = "sample-lite"
_NONCE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def resolve_nonce(value: str | None) -> str:
    """LITE_NONCE when set (validated), otherwise a fresh random nonce."""
    if not value:
        return secrets.token_hex(8)
    if not _NONCE.match(value):
        raise ValueError('LITE_NONCE may only contain letters, digits, "_" and "-"')
    return value


def object_key(repo: str, nonce: str, name: str) -> str:
    """Every object the demo writes lives under sample-lite/<repo>/<nonce>/."""
    return "/".join((KEY_ROOT, repo, nonce, name))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def roundtrip_line(key: str, data: bytes) -> str:
    """The one machine-checked line `make demo` prints."""
    return f"B2_ROUNDTRIP ok key={key} bytes={len(data)} sha256={sha256_hex(data)}"


def require_env(env: Mapping[str, str], names: Iterable[str]) -> dict[str, str]:
    """Reads the named variables; the error names missing variables, never values."""
    names = list(names)
    missing = [n for n in names if not env.get(n)]
    if missing:
        raise RuntimeError("missing environment variable(s): " + ", ".join(missing))
    return {n: env[n] for n in names}


def redact(text: str, values: Iterable[str | None]) -> str:
    """Replaces every non-empty secret value in text with <redacted>."""
    for v in values:
        if v:
            text = text.replace(v, "<redacted>")
    return text

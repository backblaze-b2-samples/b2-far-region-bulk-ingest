"""The demo's pure helpers: no network, so `make test` covers them offline."""

from __future__ import annotations

import hashlib
import re
import secrets

_NONCE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def resolve_nonce(value: str | None) -> str:
    """LITE_NONCE when set (validated), otherwise a fresh random nonce."""
    if not value:
        return secrets.token_hex(8)
    if not _NONCE.match(value):
        raise ValueError('LITE_NONCE may only contain letters, digits, "_" and "-"')
    return value


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def roundtrip_line(key: str, data: bytes) -> str:
    """The one machine-checked line `make demo` prints."""
    return f"B2_ROUNDTRIP ok key={key} bytes={len(data)} sha256={sha256_hex(data)}"

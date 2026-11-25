"""Redaction for anything the wrapper prints, logs or stores from a subprocess."""

from __future__ import annotations

import re
from collections.abc import Iterable

_PATTERNS = [
    (re.compile(r"(?i)\b4_z[0-9a-f]+(?:_[a-z0-9]+)*"), "<redacted>"),
    (re.compile(r"(?i)(x-amz-(?:signature|credential|security-token)=)[^&\s\"']+"), r"\1<redacted>"),
    (re.compile(r"(?i)(authorization:\s*)[^\r\n]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(access_key_id|secret_access_key)(\s*=\s*)\S+"), r"\1\2<redacted>"),
]


def redact(text: str, values: Iterable[str | None]) -> str:
    """Replaces known secrets, B2 upload/file IDs and signed-URL parameter values."""
    for v in sorted((v for v in values if v), key=len, reverse=True):
        text = text.replace(v, "<redacted>")
    for rx, sub in _PATTERNS:
        text = rx.sub(sub, text)
    return text

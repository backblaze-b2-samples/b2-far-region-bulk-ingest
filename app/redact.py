"""Redaction for anything the wrapper prints, logs or stores from a subprocess."""

from __future__ import annotations

import re
from collections.abc import Iterable

_PATTERNS = [
    (re.compile(r"(?i)(x-amz-(?:signature|credential|security-token)=)[^&\s\"']+"), r"\1<redacted>"),
    (re.compile(r"(?i)(authorization:\s*)[^\r\n]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(access_key_id|secret_access_key)(\s*=\s*)\S+"), r"\1\2<redacted>"),
]


def redact(text: str, values: Iterable[str | None]) -> str:
    """Replaces every secret value (credentials, bucket name) and signed-URL parameter."""
    for v in sorted((v for v in values if v), key=len, reverse=True):
        text = text.replace(v, "<redacted>")
    for rx, sub in _PATTERNS:
        text = rx.sub(sub, text)
    return text

"""Offline tests: `make test` runs these with no B2 variables set."""

import re

import pytest

from app.roundtrip import object_key, redact, require_env, resolve_nonce, roundtrip_line

LINE = re.compile(r"^B2_ROUNDTRIP ok key=sample-lite/[^ ]+ bytes=[0-9]+ sha256=[0-9a-f]{64}$")


def test_object_key_uses_sample_prefix():
    assert object_key("my-sample", "n1", "a.txt") == "sample-lite/my-sample/n1/a.txt"


def test_roundtrip_line_format():
    line = roundtrip_line(object_key("my-sample", "n1", "a.txt"), b"abc")
    assert LINE.match(line)
    assert line.endswith("bytes=3 sha256=ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


def test_resolve_nonce():
    assert resolve_nonce("p3") == "p3"
    assert re.fullmatch(r"[0-9a-f]{16}", resolve_nonce(None))
    with pytest.raises(ValueError):
        resolve_nonce("../x")


def test_require_env_names_only_missing_variables():
    with pytest.raises(RuntimeError) as exc:
        require_env({"A": "value-a"}, ["A", "B"])
    assert str(exc.value).endswith(": B")
    assert "value-a" not in str(exc.value)
    assert require_env({"A": "x"}, ["A"]) == {"A": "x"}


def test_redact_hides_every_secret():
    assert redact("id=abc key=def", ["abc", None, "def"]) == "id=<redacted> key=<redacted>"

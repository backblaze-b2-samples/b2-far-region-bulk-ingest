"""Demo scale precedence through an isolated Makefile and a shell-only uv stand-in."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture
def demo_makefile(tmp_path):
    shutil.copyfile(Path(__file__).resolve().parents[1] / "Makefile", tmp_path / "Makefile")
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf \'SCALE=%s\\n\' "${SCALE-}"\n')
    uv.chmod(0o700)
    env = {"PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    env.pop("SCALE", None)  # No caller scale, MAKEFLAGS or credentials enter these subprocesses.
    return tmp_path, env


@pytest.mark.parametrize("dotenv,caller,argument,expected", [
    ("large", None, None, "large"),
    ("large", None, "default", "default"),
    (None, None, None, ""),
    (None, "large", None, "large"),
    ("large", "default", None, "large"),
    ("", "large", None, ""),
    (None, "large", "default", "default"),
    ("large", "large", "", ""),
])
def test_demo_scale_precedence(demo_makefile, dotenv, caller, argument, expected):
    root, env = demo_makefile
    if dotenv is not None:
        (root / ".env").write_text(f"SCALE={dotenv}\n")
    if caller is not None:
        env["SCALE"] = caller
    args = ["make", "demo"] + ([] if argument is None else [f"SCALE={argument}"])
    result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"SCALE={expected}\n"


def test_command_line_scale_is_shell_quoted(demo_makefile):
    root, env = demo_makefile
    (root / ".env").write_text("SCALE=large\n")
    value = '''bad ' " `touch injected` $${SCALE} $$(touch injected) ; , value'''
    result = subprocess.run(["make", "demo", f"SCALE={value}"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    # Make consumes one dollar from $$; the recipe must preserve the resulting shell metacharacters.
    assert result.stdout == "SCALE=" + value.replace("$$", "$") + "\n"
    assert not (root / "injected").exists()

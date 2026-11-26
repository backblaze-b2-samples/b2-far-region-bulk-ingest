"""rclone, run only for the documented copy and check steps against the destination.

- The binary is the pinned, checksum-verified copy `make setup` puts in `.local/bin/`.
- Each run writes its own rclone config (mode 600) in a temporary directory, filled from
  the B2_* settings, and passes it with `--config`. The operator's global rclone config
  is never read or changed, and credentials never appear on a command line.
- Every invocation goes through `RcloneSession.command`, which always adds
  `--user-agent b2ai-far-region-bulk-ingest`.
- Output is captured and redacted before it is logged or reported.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.redact import redact
from app.store import Settings

RCLONE_VERSION = "v1.75.2"
USER_AGENT = "b2ai-far-region-bulk-ingest"
REMOTE = "dest"
REPO_ROOT = Path(__file__).resolve().parent.parent
# B2's minimum part size is 5 MB; files above the cutoff upload as multipart, and rclone
# then stores the file's MD5 in the X-Amz-Meta-Md5chksum object metadata.
CHUNK = "5Mi"
ALLOWED_SUBCOMMANDS = ("copy", "check")


def binary() -> Path:
    path = REPO_ROOT / ".local" / "bin" / "rclone"
    if not path.is_file():
        raise RuntimeError("pinned rclone not found at .local/bin/rclone: run `make setup`")
    return path


@dataclass
class Result:
    returncode: int
    output: str


class RcloneSession:
    """An isolated rclone configuration for one wrapper run."""

    def __init__(self, settings: Settings, proxy: str | None = None):
        self.settings = settings
        self.proxy = proxy
        self.dir = Path(tempfile.mkdtemp(prefix="ingest-rclone-"))
        os.chmod(self.dir, 0o700)
        self.config = self.dir / "rclone.conf"
        fd = os.open(self.config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(
                f"[{REMOTE}]\n"
                "type = s3\n"
                "provider = Other\n"
                f"access_key_id = {settings.key_id}\n"
                f"secret_access_key = {settings.key}\n"
                f"endpoint = {settings.endpoint_url}\n"
                f"region = {settings.region}\n"
                "force_path_style = true\n"
                "no_check_bucket = true\n"
            )

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self) -> "RcloneSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def remote(self, key_prefix: str) -> str:
        return f"{REMOTE}:{self.settings.bucket}/{key_prefix.rstrip('/')}"

    def command(self, subcommand: str, *args: str) -> list[str]:
        if subcommand not in ALLOWED_SUBCOMMANDS:
            raise ValueError("only the copy and check steps may run rclone")
        return [str(binary()), subcommand, *args,
                "--config", str(self.config),
                "--user-agent", USER_AGENT,
                "--s3-upload-cutoff", CHUNK, "--s3-chunk-size", CHUNK]

    def env(self) -> dict[str, str]:
        keep = ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env["RCLONE_CONFIG"] = str(self.config)
        if self.proxy:
            env["HTTPS_PROXY"] = env["HTTP_PROXY"] = self.proxy
        return env

    def clean(self, text: str) -> str:
        text = redact(text, self.settings.secrets() + [str(self.dir)])
        return text.replace(str(REPO_ROOT), "<repo>")

    def copy_args(self, source: Path, landing_prefix: str) -> list[str]:
        return self.command(
            "copy", str(source), self.remote(landing_prefix),
            "--checksum", "--transfers", "2", "--order-by", "size,ascending", "--check-first",
            "--retries", "3", "--low-level-retries", "10", "--stats", "0", "-vv",
        )

    def popen_copy(self, source: Path, landing_prefix: str) -> subprocess.Popen:
        return subprocess.Popen(self.copy_args(source, landing_prefix), env=self.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, errors="replace", bufsize=1)

    def check(self, source: Path, landing_prefix: str) -> tuple[Result, list[str]]:
        combined = self.dir / "combined.txt"
        args = self.command("check", str(source), self.remote(landing_prefix),
                            "--combined", str(combined), "--low-level-retries", "10")
        proc = subprocess.run(args, env=self.env(), capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=1800)
        lines = combined.read_text().splitlines() if combined.exists() else []
        return Result(proc.returncode, self.clean(proc.stdout + proc.stderr)), lines


_ATTEMPT = re.compile(r"Attempt (\d+)/\d+ failed")
_LOW_LEVEL = re.compile(r"low level retry \d+/\d+")
_UNCHECKED = re.compile(r"(\d+) hashes could not be checked")


def count_retries(log_text: str) -> dict[str, int]:
    """Retries rclone itself logged in a copy log: whole-pass attempts and low-level retries.
    Retries the S3 SDK inside rclone performs silently are not visible here."""
    return {"attempts_failed": len(_ATTEMPT.findall(log_text)),
            "low_level_retries": len(_LOW_LEVEL.findall(log_text))}


def parse_check(returncode: int, output: str, combined: list[str]) -> dict:
    """rclone check --combined: '=' match, '+' only in the source (missing at the destination),
    '-' only at the destination (extra), '*' differs, '!' error. A hash rclone could not
    compare counts as a failure."""
    out = {"matched": [], "missing": [], "extra": [], "differ": [], "errors": []}
    marks = {"=": "matched", "+": "missing", "-": "extra", "*": "differ", "!": "errors"}
    for line in combined:
        if len(line) > 2 and line[0] in marks and line[1] == " ":
            out[marks[line[0]]].append(line[2:])
    m = _UNCHECKED.search(output)
    unchecked = int(m.group(1)) if m else 0
    ok = (returncode == 0 and unchecked == 0 and not any(out[k] for k in ("missing", "extra", "differ", "errors")))
    return {"ok": ok, "exit": returncode, "unchecked_hashes": unchecked,
            "matched": len(out["matched"]), "missing": out["missing"], "extra": out["extra"],
            "differ": out["differ"], "errors": out["errors"]}

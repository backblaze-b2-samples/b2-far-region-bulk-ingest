"""rclone invocation rules: user agent on every call, isolated config, parsing, redaction."""

import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest

from app import rclone
from app.ingest import Ingest, Layout
from app.redact import redact
from app.store import Settings
from tests.conftest import wait_for

S = Settings("kid-1234567890", "appkey-abcdefghijkl", "my-private-bucket", "test-region-000",
             "https://s3.test-region-000.backblazeb2.com")


@pytest.fixture()
def session(monkeypatch, tmp_path):
    fake = tmp_path / "rclone"
    fake.write_text("")
    monkeypatch.setattr(rclone, "binary", lambda: fake)
    with rclone.RcloneSession(S, proxy="http://127.0.0.1:9") as s:
        yield s


def test_every_invocation_sets_the_user_agent(session, tmp_path):
    for args in (session.copy_args(tmp_path, "ingest/landing/s/b/"), session.command("check", "a", "b")):
        i = args.index("--user-agent")
        assert args[i + 1] == "b2ai-far-region-bulk-ingest"


def test_only_copy_and_check_are_authorized(session):
    for sub in ("sync", "move", "delete", "purge", "config"):
        with pytest.raises(ValueError):
            session.command(sub)


def test_copy_never_deletes_or_moves_source(session, tmp_path):
    args = session.copy_args(tmp_path, "ingest/landing/s/b/")
    assert args[1] == "copy" and "--checksum" in args
    assert not any(a.startswith(("--delete", "--remove", "--move")) for a in args)


def test_config_is_isolated_and_private(session):
    assert session.config.parent == session.dir
    assert stat.S_IMODE(os.stat(session.config).st_mode) == 0o600
    text = session.config.read_text()
    assert "endpoint = https://s3.test-region-000.backblazeb2.com" in text and "no_check_bucket = true" in text
    child = session.env()
    assert child["RCLONE_CONFIG"] == str(session.config) and child["HTTPS_PROXY"] == "http://127.0.0.1:9"
    # Credentials live only in the private config file, never on the command line.
    args = " ".join(session.copy_args(session.dir, "p/"))
    assert S.key_id not in args and S.key not in args


def test_config_is_removed_after_the_run(monkeypatch, tmp_path):
    monkeypatch.setattr(rclone, "binary", lambda: tmp_path / "rclone")
    with rclone.RcloneSession(S) as s:
        d = s.dir
    assert not d.exists()


def test_parse_check_requires_every_hash_compared():
    lines = ["= a.bin", "= b.bin"]
    assert rclone.parse_check(0, "", lines)["ok"]
    assert not rclone.parse_check(0, "NOTICE: 1 hashes could not be checked", lines)["ok"]
    out = rclone.parse_check(1, "", ["= a.bin", "+ b.bin", "- c.bin", "* d.bin"])
    assert not out["ok"] and out["missing"] == ["b.bin"] and out["extra"] == ["c.bin"] and out["differ"] == ["d.bin"]


def test_count_retries():
    log = ("ERROR : Attempt 1/3 failed with 1 errors and: connection reset\n"
           "DEBUG : pacer: low level retry 1/10 (error x)\nDEBUG : pacer: low level retry 2/10 (error x)\n")
    assert rclone.count_retries(log) == {"attempts_failed": 1, "low_level_retries": 2}


def test_redaction_hides_credentials_bucket_and_signatures(session):
    signature = "X-Amz-" + "Signature"
    credential = "X-Amz-" + "Credential"
    text = ("dest:my-private-bucket/x kid-1234567890 appkey-abcdefghijkl "
            f"https://h/o?{signature}=abc123&{credential}=zzz Authorization: AWS4 x")
    out = session.clean(text)
    for secret in ("my-private-bucket", "kid-1234567890", "appkey-abcdefghijkl", "abc123", "zzz", "AWS4"):
        assert secret not in out
    assert redact("plain", [None, ""]) == "plain"


def test_redaction_hides_b2_upload_and_file_ids(session):
    bucket_id = "0" * 24
    upload_id = f"4_z{bucket_id}_f{'1' * 16}"
    file_id = upload_id + "_d" + "2" * 8
    text = f'DEBUG : upload id="{upload_id}"\nDEBUG : file id={file_id}\n'
    assert session.clean(text) == 'DEBUG : upload id="<redacted>"\nDEBUG : file id=<redacted>\n'
    assert redact(f"bucket id={bucket_id}", [bucket_id]) == "bucket id=<redacted>"
    checksum = "a" * 64
    assert session.clean(f"sha256={checksum}") == f"sha256={checksum}"


def test_copy_log_is_redacted_while_rclone_is_still_running(session, tmp_path, monkeypatch):
    upload_id = "4_z" + "0" * 24 + "_f" + "1" * 16
    fake = rclone.binary()
    fake.write_text(
        f"#!{sys.executable}\nimport sys, time\n"
        f"print('bucket {S.bucket}', flush=True)\n"
        f"print('upload {upload_id}', file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n")
    fake.chmod(0o700)
    source = tmp_path / "source"
    source.mkdir()
    job = Ingest(Layout("site-a", "batch-0001"), source, Mock(), session, tmp_path / "logs")
    started = Event()
    processes = []
    popen = session.popen_copy

    def start(*args):
        proc = popen(*args)
        processes.append(proc)
        started.set()
        return proc

    monkeypatch.setattr(session, "popen_copy", start)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(job.copy, {})
        try:
            assert started.wait(5)
            log = wait_for(lambda: next(job.log_dir.glob("copy-*.log"), None), timeout=5)
            wait_for(lambda: len(log.read_text().splitlines()) >= 2, timeout=5)
            assert processes[0].poll() is None
            assert log.read_text() == "bucket <redacted>\nupload <redacted>\n"
        finally:
            if processes:
                processes[0].kill()
            future.result(timeout=5)

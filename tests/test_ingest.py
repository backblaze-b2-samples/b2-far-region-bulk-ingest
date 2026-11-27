"""Batch marker lifecycle with an in-process S3 mock and simulated rclone steps."""

import io
import shlex
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from botocore.config import Config
from moto import mock_aws

from app import manifest as mf
from app.ingest import Ingest, Layout
from app.rclone import RcloneSession
from app.store import USER_AGENT, Store


@pytest.mark.parametrize("location", ["relative", "absolute_inside", "outside"])
def test_copy_announces_existing_log_before_start_and_after_interruption(
        location, tmp_path, monkeypatch, capsys):
    cwd = tmp_path / "working"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    log_dir = Path("logs with 'quotes'")
    if location == "absolute_inside":
        log_dir = cwd / log_dir
    elif location == "outside":
        log_dir = tmp_path / log_dir
    rclone = Mock(spec=RcloneSession)
    rclone.clean.return_value = "redacted copy output\n"
    job = Ingest(Layout("site-a", "batch-0001"), cwd / "source", Mock(), rclone, log_dir)
    ticks = iter([1, 2])
    monkeypatch.setattr("app.ingest.time.time", lambda: next(ticks))
    announced_logs = []

    def start(*args):
        captured = capsys.readouterr()
        assert captured.out == ""
        lines = captured.err.splitlines()
        assert lines[0] == "copy started for site-a/batch-0001; follow its log with:"
        assert len(lines) == 2
        command = shlex.split(lines[1])
        assert command[:2] == ["tail", "-f"]
        assert len(command) == 3
        path = Path(command[2])
        assert path.is_absolute() == (location == "outside")
        assert path.exists()
        assert path.resolve().parent == log_dir.resolve()
        assert path.name.startswith("copy-") and path.suffix == ".log"
        assert lines[1] == f"  tail -f {shlex.quote(str(path))}"
        announced_logs.append(path.resolve())
        proc = Mock(stdout=io.StringIO("raw copy output\n"), returncode=0)
        if len(announced_logs) == 1:
            proc.returncode = 130
            proc.wait.side_effect = [KeyboardInterrupt, 130]
        else:
            proc.wait.return_value = 0
        return proc

    rclone.popen_copy.side_effect = start
    with pytest.raises(KeyboardInterrupt):
        job.copy({})
    assert job.copy({}) == 0
    assert len(set(announced_logs)) == 2
    assert set(announced_logs) == {p.resolve() for p in log_dir.glob("copy-*.log")}
    assert all(p.read_text() == "redacted copy output\n" for p in announced_logs)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("status", ["copy_failed", "verification_failed", "verified", "killed"])
@mock_aws
def test_verified_batch_rerun_marker_lifecycle(status, tmp_path, monkeypatch):
    s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y",
                      config=Config(user_agent_extra=USER_AGENT))
    s3.create_bucket(Bucket="emulator-bucket")
    store = Store(s3, "emulator-bucket")
    delete = Mock(wraps=store.delete)
    monkeypatch.setattr(store, "delete", delete)
    layout = Layout("site-a", "batch-0001", "tests/run")
    source = tmp_path / "source"
    source.mkdir()
    (source / "capture.bin").write_bytes(b"capture")
    before = mf.scan_source(source)
    job = Ingest(layout, source, store, Mock(spec=RcloneSession), tmp_path / "logs")

    def first_copy(attempt):
        s3.put_object(Bucket=store.bucket, Key=layout.landing_prefix + "capture.bin", Body=b"capture")
        attempt["result"] = "copied"
        return 0

    monkeypatch.setattr(job, "copy", first_copy)
    monkeypatch.setattr(job, "check", lambda: {"ok": True})
    code, verified_report = job.run()
    assert code == 0
    assert verified_report["counts"] == {"expected": 1, "verified": 1, "missing": 0, "duplicates": 0}
    assert "capture_to_verification_seconds" not in verified_report
    assert verified_report["captured_at"] <= verified_report["verified_at"]
    delete.assert_not_called()
    assert store.head(layout.marker_key) is not None
    if status == "verification_failed":
        s3.put_object(Bucket=store.bucket, Key=layout.landing_prefix + "extra.bin", Body=b"extra")

    marker_during_copy = []

    def rerun_copy(attempt):
        running = store.get_json(layout.report_key)
        assert running["status"] == "running" and running["complete"] is False
        assert running["attempts"] == verified_report["attempts"] + [attempt]
        assert running["attempts"][-1]["result"] == "running"
        marker_during_copy.append(store.head(layout.marker_key))
        assert marker_during_copy[-1] is None
        if status == "killed":
            raise SystemExit(137)
        attempt["result"] = "copy_failed" if status == "copy_failed" else "copied"
        return 1 if status == "copy_failed" else 0

    monkeypatch.setattr(job, "copy", rerun_copy)
    if status == "killed":
        with pytest.raises(SystemExit) as exc:
            job.run()
        assert exc.value.code == 137
        assert store.get_json(layout.report_key)["status"] == "running"
        assert store.head(layout.marker_key) is None
        assert mf.scan_source(source) == before
        return
    code, report = job.run()
    delete.assert_called_once_with(layout.marker_key)
    assert code == (0 if status == "verified" else 1)
    assert report["status"] == status
    assert store.get_json(layout.report_key) == report
    assert (store.head(layout.marker_key) is not None) == (status == "verified")
    assert marker_during_copy == [None]
    assert mf.scan_source(source) == before

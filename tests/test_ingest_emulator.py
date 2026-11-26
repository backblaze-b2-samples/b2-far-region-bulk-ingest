"""The whole wrapper, offline: real rclone against a local S3 emulator behind the link shaper.

Covers interruption and rerun, manifest agreement, report coverage, no missing or duplicate
destination files, rejection of mismatches and refusal to write the verified marker after
failed or incomplete verification.
"""

from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

from app import manifest as mf
from app.fixtures import MiB, make_batch
from app.ingest import Ingest, Layout
from app.rclone import RcloneSession, binary
from app.store import USER_AGENT, Settings, Store
from tests.conftest import BUCKET, FAKE_ENV, run_cli, start_cli, wait_for

LAYOUT = Layout("site-a", "batch-0001", "tests/run")
ARGS = ("ingest", "--site", "site-a", "--batch", "batch-0001", "--dest-prefix", "tests/run")


@pytest.fixture(scope="module", autouse=True)
def _rclone_installed():
    binary()  # fails with "run make setup" when the pinned rclone is missing


@pytest.fixture()
def batch(tmp_path):
    return make_batch(tmp_path / "site", multipart_bytes=11 * MiB)


def _keys(s3, prefix):
    return {o["Key"]: o for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", [])}


def _json(s3, key):
    return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())


def _has(s3, key):
    return key in _keys(s3, key)


def _snapshot(path: Path):
    return {p.relative_to(path).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in path.rglob("*") if p.is_file()}


def test_verified_batch_through_a_dropping_link(bucket, shaped, batch, tmp_path):
    sh = shaped(latency_ms=10, drop_after_bytes=3 * MiB, max_drops=1)
    before = _snapshot(batch)
    out = run_cli(*ARGS, "--source", str(batch), "--log-dir", str(tmp_path / "logs"),
                  endpoint=f"http://127.0.0.1:{sh.port}")
    assert out.returncode == 0, out.stderr
    assert sh.stats.drops == 1  # rclone recovered from an injected connection drop

    report = _json(bucket, LAYOUT.report_key)
    manifest = _json(bucket, LAYOUT.manifest_key)
    assert report["complete"] and report["status"] == "verified"
    assert report["counts"] == {"expected": len(manifest["files"]), "verified": len(manifest["files"]),
                                "missing": 0, "duplicates": 0}
    assert "capture_to_verification_seconds" not in report
    assert report["rclone_check"]["ok"] and report["manifest_comparison"]["ok"]
    assert sorted(f["path"] for f in report["files"]) == sorted(f["path"] for f in manifest["files"])
    assert all(f["verified"] for f in report["files"])
    assert {f["upload"] for f in report["files"]} == {"single-part", "multipart"}
    assert _has(bucket, LAYOUT.marker_key)
    landed = _keys(bucket, LAYOUT.landing_prefix)
    assert sorted(landed) == sorted(LAYOUT.landing_prefix + f["path"] for f in manifest["files"])
    assert _snapshot(batch) == before  # nothing at the site changed or was removed
    # Every request (rclone and boto3) carried the custom user agent.
    assert sh.stats.user_agents and all(USER_AGENT in ua for ua in sh.stats.user_agents)
    assert USER_AGENT in sh.stats.user_agents  # rclone sends exactly --user-agent
    assert any(ua.startswith("Boto3/") for ua in sh.stats.user_agents)


def test_interrupted_batch_is_not_verified_and_a_rerun_completes_it(bucket, shaped, batch, tmp_path):
    sh = shaped(bandwidth_bps=3 * MiB)
    endpoint = f"http://127.0.0.1:{sh.port}"
    proc = start_cli(*ARGS, "--source", str(batch), "--log-dir", str(tmp_path / "logs"), endpoint=endpoint)
    # Interrupt once the first file has landed and the large one is still in flight.
    first = wait_for(lambda: _keys(bucket, LAYOUT.landing_prefix))
    proc.send_signal(signal.SIGINT)
    _, err = proc.communicate(timeout=120)
    assert proc.returncode == 130, err
    landed_before = _keys(bucket, LAYOUT.landing_prefix)
    assert 0 < len(landed_before) < 3 and first
    report = _json(bucket, LAYOUT.report_key)
    assert report["status"] == "interrupted" and not report["complete"]
    assert not _has(bucket, LAYOUT.marker_key)

    out = run_cli(*ARGS, "--source", str(batch), "--log-dir", str(tmp_path / "logs"), endpoint=endpoint)
    assert out.returncode == 0, out.stderr
    report = _json(bucket, LAYOUT.report_key)
    assert report["complete"] and len(report["attempts"]) == 2
    assert [a["result"] for a in report["attempts"]] == ["interrupted", "copied"]
    assert report["retries"]["reruns"] == 1
    landed = _keys(bucket, LAYOUT.landing_prefix)
    assert len(landed) == 3  # every file once, nothing extra
    for key, obj in landed_before.items():  # files that had landed were not uploaded again
        assert landed[key]["ETag"] == obj["ETag"] and landed[key]["LastModified"] == obj["LastModified"]
    assert _has(bucket, LAYOUT.marker_key)


def test_source_changed_after_manifest_is_refused(bucket, shaped, batch, tmp_path):
    sh = shaped()
    endpoint = f"http://127.0.0.1:{sh.port}"
    bucket.put_object(Bucket=BUCKET, Key=LAYOUT.manifest_key, Body=json.dumps(
        mf.build_manifest("site-a", "batch-0001", mf.scan_source(batch))).encode())
    (batch / "telemetry" / "imu-0000.csv").write_text("edited after capture\n")
    out = run_cli(*ARGS, "--source", str(batch), "--log-dir", str(tmp_path / "logs"), endpoint=endpoint)
    assert out.returncode == 3 and "refused" in out.stderr
    assert not _has(bucket, LAYOUT.marker_key)
    assert not _keys(bucket, LAYOUT.landing_prefix)


def test_extra_destination_file_blocks_the_marker(bucket, shaped, batch, tmp_path):
    sh = shaped()
    bucket.put_object(Bucket=BUCKET, Key=LAYOUT.landing_prefix + "camera/clip-0000 (1).bin", Body=b"dup")
    out = run_cli(*ARGS, "--source", str(batch), "--log-dir", str(tmp_path / "logs"),
                  endpoint=f"http://127.0.0.1:{sh.port}")
    assert out.returncode == 1
    report = _json(bucket, LAYOUT.report_key)
    assert report["status"] == "verification_failed"
    assert report["manifest_comparison"]["extra"] == ["camera/clip-0000 (1).bin"]
    assert report["rclone_check"]["extra"] == ["camera/clip-0000 (1).bin"]
    assert not _has(bucket, LAYOUT.marker_key)


@pytest.mark.parametrize("extra_file", [False, True])
def test_verified_batch_rerun(bucket, shaped, batch, tmp_path, extra_file):
    sh = shaped()
    endpoint = f"http://127.0.0.1:{sh.port}"
    before = _snapshot(batch)
    out = run_cli(*ARGS, "--source", str(batch), "--log-dir", str(tmp_path / "logs"), endpoint=endpoint)
    assert out.returncode == 0, out.stderr
    assert _json(bucket, LAYOUT.report_key)["status"] == "verified"
    assert _has(bucket, LAYOUT.marker_key)
    landed_before = _keys(bucket, LAYOUT.landing_prefix)
    if extra_file:
        bucket.put_object(Bucket=BUCKET, Key=LAYOUT.landing_prefix + "extra.bin", Body=b"extra")

    out = run_cli(*ARGS, "--source", str(batch), "--log-dir", str(tmp_path / "logs"), endpoint=endpoint)
    assert out.returncode == (1 if extra_file else 0), out.stderr
    report = _json(bucket, LAYOUT.report_key)
    assert report["status"] == ("verification_failed" if extra_file else "verified")
    assert report["complete"] is not extra_file
    assert report["retries"]["reruns"] == 1
    assert _has(bucket, LAYOUT.marker_key) is not extra_file
    landed = _keys(bucket, LAYOUT.landing_prefix)
    assert set(landed) == set(landed_before) | ({LAYOUT.landing_prefix + "extra.bin"} if extra_file else set())
    for key, obj in landed_before.items():
        assert landed[key]["ETag"] == obj["ETag"] and landed[key]["LastModified"] == obj["LastModified"]
    assert _snapshot(batch) == before


def _in_process(emulator, batch, tmp_path):
    settings = Settings.from_env(FAKE_ENV, endpoint_url=f"http://{emulator[0]}:{emulator[1]}")
    from app.store import make_client
    store = Store(make_client(settings), settings.bucket)
    session = RcloneSession(settings)
    return Ingest(LAYOUT, batch, store, session, tmp_path / "logs"), session


def test_checksum_mismatch_blocks_the_marker_even_when_rclone_check_passes(
        bucket, emulator, batch, tmp_path, monkeypatch):
    job, session = _in_process(emulator, batch, tmp_path)

    def fake_copy(attempt):  # lands every file, one with the right size but wrong bytes
        for f in mf.scan_source(batch):
            data = (batch / f["path"]).read_bytes()
            if f["path"].startswith("camera/"):
                data = b"\0" * len(data)
            bucket.put_object(Bucket=BUCKET, Key=LAYOUT.landing_prefix + f["path"], Body=data)
        attempt["result"] = "copied"
        return 0

    monkeypatch.setattr(job, "copy", fake_copy)
    monkeypatch.setattr(job, "check", lambda: {"ok": True, "matched": 3, "missing": [], "extra": [],
                                                "differ": [], "errors": [], "unchecked_hashes": 0, "exit": 0})
    code, report = job.run()
    session.close()
    assert code == 1 and report["status"] == "verification_failed"
    assert report["manifest_comparison"]["mismatched"] == ["camera/clip-0000.bin"]
    assert not _has(bucket, LAYOUT.marker_key)


def test_failed_rclone_check_blocks_the_marker(bucket, emulator, batch, tmp_path, monkeypatch):
    job, session = _in_process(emulator, batch, tmp_path)
    monkeypatch.setattr(job, "check", lambda: {"ok": False, "matched": 2, "missing": [], "extra": [],
                                                "differ": [], "errors": [], "unchecked_hashes": 1, "exit": 0})
    code, report = job.run()
    session.close()
    assert code == 1 and not report["complete"]
    assert report["manifest_comparison"]["ok"]  # the destination matched, rclone check did not
    assert not _has(bucket, LAYOUT.marker_key)


"""Batch marker lifecycle with an in-process S3 mock and simulated rclone steps."""

from unittest.mock import Mock

import boto3
import pytest
from botocore.config import Config
from moto import mock_aws

from app import manifest as mf
from app.ingest import Ingest, Layout
from app.rclone import RcloneSession
from app.store import USER_AGENT, Store


@pytest.mark.parametrize("status", ["copy_failed", "verification_failed", "verified"])
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
    assert job.run()[0] == 0
    delete.assert_not_called()
    assert store.head(layout.marker_key) is not None
    if status == "verification_failed":
        s3.put_object(Bucket=store.bucket, Key=layout.landing_prefix + "extra.bin", Body=b"extra")

    marker_during_copy = []

    def rerun_copy(attempt):
        marker_during_copy.append(store.head(layout.marker_key))
        attempt["result"] = "copy_failed" if status == "copy_failed" else "copied"
        return 1 if status == "copy_failed" else 0

    monkeypatch.setattr(job, "copy", rerun_copy)
    code, report = job.run()
    delete.assert_called_once_with(layout.marker_key)
    assert code == (0 if status == "verified" else 1)
    assert report["status"] == status
    assert store.get_json(layout.report_key) == report
    assert (store.head(layout.marker_key) is not None) == (status == "verified")
    assert marker_during_copy == [None]
    assert mf.scan_source(source) == before

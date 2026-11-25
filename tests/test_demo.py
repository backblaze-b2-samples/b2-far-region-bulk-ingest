"""Demo report formatting without running the B2 demo."""

from unittest.mock import Mock, call

import pytest

from app import demo
from app.demo import print_report
from app.store import Settings


@pytest.mark.parametrize("entry", ["Versions", "DeleteMarkers", "Uploads"])
def test_occupied_nonce_prefix_is_refused_before_any_writes(entry, monkeypatch):
    settings = Settings("x", "y", "emulator-bucket", "us-east-1", "https://example.invalid")
    prefix = f"sample-lite/{demo.REPO_NAME}/occupied/"
    client = Mock()
    client.list_object_versions.return_value = (
        {} if entry == "Uploads" else {entry: [{"Key": prefix + "existing", "VersionId": "prior"}]})
    client.list_multipart_uploads.return_value = {
        "Uploads": [{"Key": prefix + "existing", "UploadId": "prior-upload"}]}
    generate = Mock(side_effect=AssertionError("source generation attempted for occupied prefix"))
    monkeypatch.setattr(demo.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(demo, "resolve_nonce", lambda value: "occupied")
    monkeypatch.setattr(demo, "make_client", lambda settings: client)
    monkeypatch.setattr(demo, "make_batch", generate)

    with pytest.raises(demo.DemoError, match="nonce prefix is occupied"):
        demo.run(keep=False)

    generate.assert_not_called()
    client.delete_object.assert_not_called()
    expected = [call.list_object_versions(Bucket=settings.bucket, Prefix=prefix, MaxKeys=1)]
    if entry == "Uploads":
        expected.append(call.list_multipart_uploads(Bucket=settings.bucket, Prefix=prefix, MaxUploads=1))
    assert client.mock_calls == expected


def test_upload_cleanup_aborts_only_written_keys():
    prefix = f"sample-lite/{demo.REPO_NAME}/cleanup"
    written = [prefix + "/ingest/landing/owned.bin"]
    client = Mock()
    client.list_multipart_uploads.return_value = {"Uploads": [
        {"Key": written[0], "UploadId": "owned-upload"},
        {"Key": prefix + "/ingest/landing/unowned.bin", "UploadId": "unowned-upload"},
    ]}

    assert demo.abort_written_uploads(client, "emulator-bucket", prefix, written) == 1

    client.list_multipart_uploads.assert_called_once_with(Bucket="emulator-bucket", Prefix=prefix + "/")
    client.abort_multipart_upload.assert_called_once_with(
        Bucket="emulator-bucket", Key=written[0], UploadId="owned-upload")


@pytest.mark.parametrize("failed,low_level,drops", [(0, 0, 2), (1, 3, 0)])
def test_report_distinguishes_logged_retries_from_injected_drops(capsys, failed, low_level, drops):
    report = {
        "site": "site-a", "batch": "batch-0001", "status": "verified", "complete": True,
        "attempts": [{"result": "interrupted"}, {"result": "copied"}],
        "rclone_check": {"ok": True, "matched": 3, "unchecked_hashes": 0},
        "manifest_comparison": {"ok": True, "missing": [], "extra": []},
        "files": [{"path": "capture.bin", "size": 7, "sha256": "a" * 64,
                   "verified": True, "upload": "single-part"}],
        "marker_key": "ingest/reports/site-a/batch-0001.verified",
    }
    evidence = {
        "shaper": {"drops": drops},
        "rerun": {"retries": {
            "reruns": 1, "rclone_attempts_failed": failed, "rclone_low_level_retries": low_level,
            "link_shaper_drops_injected": drops,
        }},
    }
    print_report(report, evidence)
    output = capsys.readouterr().out
    assert f"rclone-logged retries  {failed} failed attempt(s), {low_level} low-level retries" in output
    assert f"{drops} connection drop(s) injected" in output
    assert "recovered" not in output
    assert "recovery shown by passing rclone check and manifest comparison" in output
    assert "source SHA-256 " + "a" * 16 in output
    assert "destination verification  md5+size via rclone check and manifest comparison" in output

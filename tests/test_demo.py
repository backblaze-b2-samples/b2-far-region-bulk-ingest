"""Demo report formatting without running the B2 demo."""

import json
import base64
import hashlib
import re
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app import demo
from app.demo import print_report
from app.store import Settings
from app.scenario import resolve_scale


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
    monkeypatch.setattr(demo, "make_scaled_batch", generate)

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
    scale = resolve_scale("default")
    evidence = demo.new_evidence("test", "sample-lite/test", "test-region", scale, 123)
    evidence["batches"] = evidence["batches"][:1]
    batch = evidence["batches"][0]
    batch.update(counts={"expected": 34, "verified": 34, "missing": 0, "duplicates": 0},
                 attempts=["interrupted", "copied"],
                 retries={"reruns": 1, "rclone_attempts_failed": failed, "rclone_low_level_retries": low_level},
                 cut={"seed": 123, "fraction": .4, "target_bytes": 100,
                      "shaper_bytes_at_cut": 101, "files_landed_at_cut": 2},
                 files_landed_before_rerun=["telemetry/imu-0000.csv"],
                 already_landed_not_reuploaded=1)
    evidence["shaper"] = {"drops": drops}
    demo.print_header(scale, evidence["prefix"], evidence["region"])
    demo.print_interrupt(batch)
    demo.print_batch(batch)
    print_report(evidence)
    output = capsys.readouterr().out
    assert f"rclone-logged retries: {failed} failed attempts, {low_level} low-level" in output
    assert f"link shaper drops injected: {drops}" in output
    assert all(name in output for name in ("expected", "verified", "missing", "duplicates", "attempts", "Totals"))
    assert "Transfer report" in output and "site-a/batch-0001" in output
    assert not any(bad in output for bad in ("/s", "capture_to_verification", str(scale.shaping["bandwidth_bps"])))
    assert all(len(line) <= 120 for line in output.splitlines())
    assert "recovered" not in output
    assert "recovery shown by passing rclone check and manifest comparison" in output
    assert "destination verification  md5+size via rclone check and manifest comparison" in output


def test_invalid_scale_precedes_credentials_and_storage(monkeypatch, capsys):
    monkeypatch.setenv("SCALE", "bogus")
    for name in list(demo.os.environ):
        if name.startswith("B2_"):
            monkeypatch.delenv(name)
    access = Mock(side_effect=AssertionError("B2 access attempted"))
    monkeypatch.setattr(demo.Settings, "from_env", access)
    monkeypatch.setattr(demo, "make_client", access)
    assert demo.main() == 2
    assert capsys.readouterr().err == 'SCALE must be "default" or "large" (got "bogus")\n'
    access.assert_not_called()


def test_transfer_table_columns_align(capsys):
    evidence = demo.new_evidence("test", "sample-lite/test", "test-region", resolve_scale("default"), 123)
    evidence["batches"] = evidence["batches"][:2]
    for batch, counts, retries in zip(evidence["batches"], [(34, 34, 0, 0), (524, 500, 24, 2)], [(1, 23), (12, 3)]):
        batch["counts"] = dict(zip(("expected", "verified", "missing", "duplicates"), counts))
        batch["attempts"] = ["interrupted", "copied"]
        batch["retries"] = {"rclone_attempts_failed": retries[0], "rclone_low_level_retries": retries[1]}
    print_report(evidence)
    lines = capsys.readouterr().out.splitlines()
    start = lines.index("Transfer report") + 1
    table = lines[start:start + 3]
    assert all(len(line) < 100 for line in table)
    assert len(set(map(len, table))) == 1
    # Check fixed column boundaries independently of the production formatter.
    expected = [
        ["site/batch", "expected", "verified", "missing", "duplicates", "attempts", "failed", "low-level"],
        ["site-a/batch-0001", "34", "34", "0", "0", "2", "1", "23"],
        ["site-a/batch-0002", "524", "500", "24", "2", "2", "12", "3"],
    ]
    for line, cells in zip(table, expected):
        widths = (20, 8, 8, 7, 10, 8, 6, 9)
        offset = 0
        for index, (width, cell) in enumerate(zip(widths, cells)):
            part = line[offset:offset + width]
            assert part.strip() == cell
            assert part == (cell.ljust(width) if index == 0 else cell.rjust(width))
            offset += width
            separator = ", " if index == 6 else " "
            if index < len(cells) - 1:
                assert line[offset:offset + len(separator)] == separator
                offset += len(separator)


def test_evidence_shaping_labels_and_cut_record():
    for name, label in (("default", "standard"), ("large", "raised")):
        scale = resolve_scale(name)
        evidence = demo.new_evidence("test", "sample-lite/test", "test-region", scale, 123)
        cut = {"seed": 123, "fraction": .3, "target_bytes": 50,
               "shaper_bytes_at_cut": 51, "files_landed_at_cut": 0}
        evidence["batches"][0]["cut"] = cut
        encoded = json.dumps(evidence)
        assert evidence["batches"][0]["cut"] == cut
        assert evidence["shaping"]["bandwidth_cap"] == label
        assert "bandwidth_bps" not in encoded
        assert str(scale.shaping["bandwidth_bps"]) not in encoded


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_failed_run_still_saves_evidence_and_preserves_unowned_keys(tmp_path, monkeypatch, cleanup_failure):
    settings = Settings("x", "y", "emulator-bucket", "us-east-1", "https://example.invalid")
    client = Mock()
    unowned = {"Versions": [{"Key": "sample-lite/b2-far-region-bulk-ingest/test/unowned", "VersionId": "v"}]}
    client.list_object_versions.side_effect = [{}, unowned, unowned] if cleanup_failure else [{}, {}, {}]
    client.list_multipart_uploads.return_value = {}
    shaper = Mock(stats=SimpleNamespace(drops=0, connections=0, refused=0))
    shaper.start.side_effect = demo.DemoError("shaper unavailable")
    monkeypatch.setattr(demo, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(demo.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(demo, "make_client", lambda settings: client)
    monkeypatch.setattr(demo, "resolve_nonce", lambda value: "test")
    monkeypatch.setattr(demo, "LinkShaper", Mock(return_value=shaper))
    message = "remain under the run prefix" if cleanup_failure else "shaper unavailable"
    with pytest.raises(demo.DemoError, match=message):
        demo.run(keep=False, scale=resolve_scale("default"))
    evidence = json.loads((tmp_path / ".local/verify/demo-evidence.json").read_text())
    assert evidence["scale"] == "default"
    assert evidence["totals"]["expected"] == 185
    assert evidence["totals"]["verified"] == 0
    assert evidence["unfinished_uploads_aborted"] == evidence["cleanup_versions_deleted"] == 0
    client.delete_object.assert_not_called()
    client.delete_objects.assert_not_called()
    shaper.stop.assert_called_once()


@pytest.mark.parametrize("unowned", [False, True])
def test_cleanup_versions_exact_keys_paginated_with_delete_markers(unowned):
    client = Mock()
    prefix = "sample-lite/test/cleanup"
    owned = prefix + "/ingest/landing/owned.bin"
    other = prefix + "/ingest/landing/unowned.bin"
    client.list_object_versions.side_effect = [
        {"Versions": [{"Key": owned, "VersionId": "old"}], "IsTruncated": True,
         "NextKeyMarker": owned, "NextVersionIdMarker": "old"},
        {"Versions": [{"Key": owned, "VersionId": "new"}] + (
            [{"Key": other, "VersionId": "unowned"}] if unowned else []),
         "DeleteMarkers": [{"Key": owned, "VersionId": "marker"}], "IsTruncated": False},
        {"Versions": [{"Key": other, "VersionId": "unowned"}]} if unowned else {},
    ]
    if unowned:
        with pytest.raises(demo.DemoError, match="remain under the run prefix"):
            demo.cleanup_versions(client, "b", prefix, {owned})
    else:
        assert demo.cleanup_versions(client, "b", prefix, {owned}) == 3
    assert set(tuple(sorted(c.kwargs.items())) for c in client.delete_object.call_args_list) == {
        tuple(sorted({"Bucket": "b", "Key": owned, "VersionId": v}.items())) for v in ("old", "new", "marker")}
    client.delete_objects.assert_not_called()
    client.abort_multipart_upload.assert_not_called()
    assert client.list_object_versions.call_args_list == [
        call(Bucket="b", Prefix=prefix + "/"),
        call(Bucket="b", Prefix=prefix + "/", KeyMarker=owned, VersionIdMarker="old"),
        call(Bucket="b", Prefix=prefix + "/")]


def test_duplicate_stored_versions_fail():
    client = Mock()
    client.list_object_versions.return_value = {"Versions": [
        {"Key": "run/landing/a", "VersionId": "one"}, {"Key": "run/landing/a", "VersionId": "two"}]}
    with pytest.raises(demo.DemoError, match="exactly one stored version"):
        demo.check_landing_versions(client, "b", "run/landing/", ["run/landing/a"])


def test_interrupt_uses_shaper_delta_and_poll_interval(monkeypatch):
    shaper = SimpleNamespace(stats=SimpleNamespace(bytes_up=500))
    proc = Mock(returncode=130)
    proc.poll.return_value = None
    proc.communicate.return_value = ("", "")
    proc.send_signal.side_effect = lambda sig: setattr(proc.poll, "return_value", 130)
    monkeypatch.setattr(demo.subprocess, "Popen", lambda *a, **kw: proc)
    sleeps = []

    def advance(interval):
        sleeps.append(interval)
        shaper.stats.bytes_up += 60

    monkeypatch.setattr(demo.time, "sleep", advance)
    assert demo.interrupt_wrapper(["wrapper"], shaper, 100) == (130, 120)
    assert sleeps == [.05, .05]
    proc.send_signal.assert_called_once_with(demo.signal.SIGINT)


def test_finishing_before_cut_fails(monkeypatch):
    proc = Mock(returncode=0)
    proc.poll.return_value = 0
    monkeypatch.setattr(demo.subprocess, "Popen", lambda *a, **kw: proc)
    shaper = SimpleNamespace(stats=SimpleNamespace(bytes_up=500))
    with pytest.raises(demo.DemoError, match="finished before.*cut"):
        demo.interrupt_wrapper(["wrapper"], shaper, 100)
    proc.send_signal.assert_not_called()


def test_upload_cleanup_pages_without_aborting_unowned_uploads():
    client = Mock()
    client.list_multipart_uploads.side_effect = [
        {"Uploads": [{"Key": "run/owned", "UploadId": "one"}], "IsTruncated": True,
         "NextKeyMarker": "run/owned", "NextUploadIdMarker": "one"},
        {"Uploads": [{"Key": "run/unowned", "UploadId": "two"}, {"Key": "run/owned", "UploadId": "three"}]},
    ]
    assert demo.abort_written_uploads(client, "b", "run", {"run/owned"}) == 2
    assert client.abort_multipart_upload.call_args_list == [
        call(Bucket="b", Key="run/owned", UploadId="one"),
        call(Bucket="b", Key="run/owned", UploadId="three")]


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("landed_before_cut", [0, 1])
def test_multibatch_demo_with_in_memory_storage_and_simulated_wrapper(
        tmp_path, monkeypatch, capsys, keep, landed_before_cut):
    """Exercise orchestration, report checks, readback, evidence and cleanup without sockets or rclone."""
    import boto3
    from botocore.config import Config
    from moto import mock_aws

    from app.fixtures import MiB
    from app.ingest import Ingest, Layout
    from app.scenario import BatchPlan, Scale
    from app.store import USER_AGENT, Store
    from tests.conftest import BUCKET, FAKE_ENV

    scale = Scale("large", tuple(BatchPlan(s, "batch-0001", 2, (size * MiB,))
                                 for s, size in (("site-c", 6), ("site-b", 7), ("site-a", 7))),
                  resolve_scale("large").shaping, 2)
    settings = Settings.from_env(FAKE_ENV)
    monkeypatch.setattr(demo, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(demo.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(demo, "resolve_nonce", lambda value: "unit-test")
    rng = demo.new_rng()
    rng.seed(123)
    rng.run_seed = 123
    monkeypatch.setattr(demo, "new_rng", lambda: rng)
    shaper = Mock(stats=SimpleNamespace(drops=4, connections=8, refused=0, bytes_up=0))
    shaper.start.return_value = 9
    monkeypatch.setattr(demo, "LinkShaper", Mock(return_value=shaper))
    commands = []

    with mock_aws():
        client = boto3.client("s3", region_name=settings.region, aws_access_key_id=settings.key_id,
                              aws_secret_access_key=settings.key, config=Config(user_agent_extra=USER_AGENT))
        client.create_bucket(Bucket=settings.bucket)
        client.put_bucket_versioning(Bucket=settings.bucket, VersioningConfiguration={"Status": "Enabled"})
        monkeypatch.setattr(demo, "make_client", lambda settings: client)
        store = Store(client, settings.bucket)

        def invoke(command, interrupted):
            commands.append((command, interrupted))
            arg = lambda flag: command[command.index(flag) + 1]
            layout = Layout(arg("--site"), arg("--batch"), arg("--dest-prefix"))
            source = demo.Path(arg("--source"))
            job = Ingest(layout, source, store, Mock(), tmp_path / "logs")

            def copy(attempt):
                if interrupted and landed_before_cut == 0:
                    attempt["result"] = "interrupted"
                    raise KeyboardInterrupt
                for path in sorted(source.rglob("*")):
                    if not path.is_file():
                        continue
                    key = layout.landing_prefix + path.relative_to(source).as_posix()
                    if store.head(key) is not None:
                        continue
                    data = path.read_bytes()
                    if len(data) > 5 * MiB:
                        upload = client.create_multipart_upload(
                            Bucket=BUCKET, Key=key,
                            Metadata={"md5chksum": base64.b64encode(hashlib.md5(data).digest()).decode()})
                        parts = []
                        for number, offset in enumerate(range(0, len(data), 5 * MiB), 1):
                            part = client.upload_part(Bucket=BUCKET, Key=key, UploadId=upload["UploadId"],
                                                      PartNumber=number, Body=data[offset:offset + 5 * MiB])
                            parts.append({"PartNumber": number, "ETag": part["ETag"]})
                        client.complete_multipart_upload(Bucket=BUCKET, Key=key, UploadId=upload["UploadId"],
                                                         MultipartUpload={"Parts": parts})
                    else:
                        client.put_object(Bucket=BUCKET, Key=key, Body=data)
                    if interrupted:
                        attempt["result"] = "interrupted"
                        raise KeyboardInterrupt
                attempt["result"] = "copied"
                return 0

            job.copy = copy
            job.check = lambda: {"ok": True, "matched": 3, "unchecked_hashes": 0}
            try:
                code, _ = job.run()
                return code
            except KeyboardInterrupt:
                return 130

        monkeypatch.setattr(demo, "interrupt_wrapper", lambda command, shaper, target: (invoke(command, True), target + 1))
        monkeypatch.setattr(demo.subprocess, "run", lambda command, **kwargs: SimpleNamespace(
            returncode=invoke(command, False)))
        assert demo.run(keep=keep, scale=scale) == 0
        evidence = json.loads((tmp_path / ".local/verify/demo-evidence-large.json").read_text())
        assert sum(b["interrupted"] for b in evidence["batches"]) == 2
        assert len(commands) == 5
        for command, _ in commands:
            source = demo.Path(command[command.index("--source") + 1])
            assert source.parent.parent == tmp_path / ".local/demo/unit-test/large"
        for b in evidence["batches"]:
            assert b["counts"] == {"expected": 3, "verified": 3, "missing": 0, "duplicates": 0}
            assert b["exit_codes"] == ([130, 0] if b["interrupted"] else [0])
            assert b["attempts"] == (["interrupted", "copied"] if b["interrupted"] else ["copied"])
            assert b["capture_to_verification_seconds"] >= 0
            if b["interrupted"]:
                assert b["marker_present_after_interrupt"] is False
                assert b["cut"]["seed"] == 123
                assert b["cut"]["shaper_bytes_at_cut"] == b["cut"]["target_bytes"] + 1
                assert b["cut"]["files_landed_at_cut"] == b["already_landed_not_reuploaded"] == landed_before_cut
                first = next(c for c, cut in commands if cut and b["site"] in c)
                assert next(c for c, cut in commands if not cut and b["site"] in c) == first
        assert evidence["totals"]["expected"] == evidence["totals"]["verified"] == 9
        assert evidence["totals"]["reruns"] == 2
        assert evidence["totals"]["link_shaper_drops_injected"] == 4
        assert evidence["totals"]["capture_to_verification_seconds"]["sum"] == round(sum(
            b["capture_to_verification_seconds"] for b in evidence["batches"]), 3)
        assert evidence["readback"]["key"].endswith("site-a/batch-0001/lidar/sweep-0000.bin")
        assert evidence["readback"]["bytes"] == 7 * MiB
        assert len(evidence["destination"]) == 9
        assert evidence["cleanup_versions_deleted"] > 0 if not keep else evidence["cleanup_versions_deleted"] == 0
        assert bool(demo.list_versions(client, BUCKET, evidence["prefix"] + "/")) == keep
        shaper.stop.assert_called_once()
    output = capsys.readouterr().out
    assert output.count("B2_ROUNDTRIP ok ") == 1
    assert output.splitlines()[-1].startswith("B2_ROUNDTRIP ok ")
    assert evidence["readback"]["key"] in output.splitlines()[-1]
    assert not any(token in output for token in ("capture_to_verification", str(scale.shaping["bandwidth_bps"])))
    assert not re.search(r"\b(?:bytes|[KMGT]?i?B)/s\b", output)
    assert all(len(line) <= 120 for line in output.splitlines()
               if not line.startswith(("B2_ROUNDTRIP", "destination ", "kept:")))

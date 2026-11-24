"""Destination comparison with mocked boto3 responses (botocore Stubber)."""

import base64
import hashlib

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from app import manifest as mf
from app.store import USER_AGENT, Store

PREFIX = "ingest/landing/site-a/b1/"


def _client():
    return boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y",
                        config=Config(user_agent_extra=USER_AGENT))


def _entry(path, data):
    return {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "md5": hashlib.md5(data).hexdigest()}


def _run(files, listing, heads):
    client = _client()
    stub = Stubber(client)
    stub.add_response("list_objects_v2", {"Contents": [{"Key": k, "Size": s} for k, s in listing],
                                          "IsTruncated": False}, {"Bucket": "b", "Prefix": PREFIX})
    for key, head in heads:
        stub.add_response("head_object", head, {"Bucket": "b", "Key": key})
    with stub:
        out = mf.compare({"files": files}, PREFIX, Store(client, "b"))
        stub.assert_no_pending_responses()
    return out


def test_single_part_etag_is_the_md5():
    f = _entry("a.bin", b"a" * 100)
    out = _run([f], [(PREFIX + "a.bin", 100)],
               [(PREFIX + "a.bin", {"ETag": '"%s"' % f["md5"], "ContentLength": 100})])
    assert out["ok"] and out["files"][0]["upload"] == "single-part"


def test_multipart_uses_md5chksum_metadata_not_the_etag():
    data = b"b" * 100
    f = _entry("b.bin", data)
    b64 = base64.b64encode(hashlib.md5(data).digest()).decode()
    out = _run([f], [(PREFIX + "b.bin", 100)],
               [(PREFIX + "b.bin", {"ETag": '"0123456789abcdef0123456789abcdef-3"', "ContentLength": 100,
                                    "Metadata": {"md5chksum": b64}})])
    assert out["ok"] and out["files"][0]["md5_source"] == "md5chksum metadata (multipart)"


def test_multipart_without_metadata_is_never_verified_from_its_etag():
    f = _entry("c.bin", b"c" * 100)
    # Even an ETag whose hex part equals the MD5 is not accepted for a multipart object.
    out = _run([f], [(PREFIX + "c.bin", 100)],
               [(PREFIX + "c.bin", {"ETag": '"%s-2"' % f["md5"], "ContentLength": 100})])
    assert not out["ok"] and out["mismatched"] == ["c.bin"]


def test_checksum_mismatch_is_rejected():
    f = _entry("d.bin", b"d" * 10)
    out = _run([f], [(PREFIX + "d.bin", 10)],
               [(PREFIX + "d.bin", {"ETag": '"%s"' % hashlib.md5(b"x" * 10).hexdigest(), "ContentLength": 10})])
    assert not out["ok"] and out["files"][0]["problem"] == "checksum differs"


def test_missing_and_extra_destination_files_are_rejected():
    f1, f2 = _entry("e.bin", b"e"), _entry("f.bin", b"f")
    out = _run([f1, f2], [(PREFIX + "e.bin", 1), (PREFIX + "e copy.bin", 1)],
               [(PREFIX + "e.bin", {"ETag": '"%s"' % f1["md5"], "ContentLength": 1})])
    assert not out["ok"]
    assert out["missing"] == ["f.bin"] and out["extra"] == ["e copy.bin"]


def test_scan_source_is_read_only_and_complete(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "1.bin").write_bytes(b"12345")
    (tmp_path / "2.txt").write_text("hi")
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    files = mf.scan_source(tmp_path)
    assert [f["path"] for f in files] == ["2.txt", "x/1.bin"]
    assert before == {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}


def test_empty_batch_is_refused(tmp_path):
    with pytest.raises(RuntimeError):
        mf.scan_source(tmp_path)

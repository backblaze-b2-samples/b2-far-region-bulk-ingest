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


def test_counts_include_duplicate_raw_listing_entries_across_pages():
    client = _client()
    stub = Stubber(client)
    f = _entry("a.bin", b"a")
    obj = {"Key": PREFIX + "a.bin", "Size": 1}
    stub.add_response("list_objects_v2", {"Contents": [obj], "IsTruncated": True,
                                         "NextContinuationToken": "next"}, {"Bucket": "b", "Prefix": PREFIX})
    stub.add_response("list_objects_v2", {"Contents": [obj], "IsTruncated": False},
                      {"Bucket": "b", "Prefix": PREFIX, "ContinuationToken": "next"})
    stub.add_response("head_object", {"ContentLength": 1, "ETag": f['md5']},
                      {"Bucket": "b", "Key": obj["Key"]})
    with stub:
        out = mf.compare({"files": [f, _entry("missing.bin", b"m")]}, PREFIX, Store(client, "b"))
        stub.assert_no_pending_responses()
    assert out["counts"] == {"expected": 2, "verified": 1, "missing": 1, "duplicates": 1}
    assert not out["ok"]


def test_parallel_comparison_preserves_sorted_results_and_counts():
    files = [_entry("z.bin", b"z"), _entry("a.bin", b"a"), _entry("missing.bin", b"m")]

    class FakeStore:
        def list_entries(self, prefix):
            return [{"Key": prefix + p, "Size": 1} for p in ("z.bin", "a.bin", "a.bin", "extra.bin")]

        def head(self, key):
            return {"ContentLength": 1, "ETag": next(f["md5"] for f in files if key.endswith(f["path"]))}

    sequential = mf.compare({"files": files}, PREFIX, FakeStore())
    parallel = mf.compare({"files": files}, PREFIX, FakeStore(), workers=8)
    assert parallel == sequential
    assert [f["path"] for f in parallel["files"]] == ["a.bin", "missing.bin", "z.bin"]
    assert parallel["counts"] == {"expected": 3, "verified": 2, "missing": 1, "duplicates": 1}
    assert parallel["extra"] == ["extra.bin"]


def test_seconds_is_public():
    assert mf.seconds("2026-01-01T00:00:00.000000Z", "2026-01-01T00:01:02.500000Z") == 62.5

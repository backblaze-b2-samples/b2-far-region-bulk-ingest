"""Checksum manifests and the destination comparison.

How a destination object's MD5 is established (rclone S3 backend behaviour):

- single-part upload: the S3 ETag is the hex MD5 of the object;
- multipart upload: the ETag is NOT the file's MD5 (it ends in `-<parts>`); rclone stores
  the file's MD5 in the `X-Amz-Meta-Md5chksum` metadata (base64), and that is what we use.
  A multipart object without that metadata fails verification.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def hash_file(path: Path) -> tuple[int, str, str]:
    sha, md5, size = hashlib.sha256(), hashlib.md5(), 0  # noqa: S324 - MD5 is S3's checksum
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return size, sha.hexdigest(), md5.hexdigest()


def scan_source(source: Path) -> list[dict]:
    """Every regular file under source, sorted, with size, SHA-256 and MD5. Read-only."""
    if not source.is_dir():
        raise RuntimeError("source is not a directory")
    files = []
    for p in sorted(source.rglob("*")):
        if p.is_symlink():
            raise RuntimeError(f"symlinks are not supported in a batch: {p.relative_to(source)}")
        if p.is_file():
            size, sha, md5 = hash_file(p)
            files.append({"path": p.relative_to(source).as_posix(), "size": size, "sha256": sha, "md5": md5})
    if not files:
        raise RuntimeError("source batch has no files")
    return files


def build_manifest(site: str, batch: str, files: list[dict]) -> dict:
    return {"site": site, "batch": batch, "created_at": now(), "algorithm": ["sha256", "md5"], "files": files}


def same_files(a: list[dict], b: list[dict]) -> bool:
    key = lambda f: (f["path"], f["size"], f["sha256"], f["md5"])  # noqa: E731
    return sorted(map(key, a)) == sorted(map(key, b))


def destination_md5(head: dict) -> tuple[str | None, str]:
    """(md5 hex or None, how it was established) for one head_object response."""
    etag = head.get("ETag", "").strip('"')
    if "-" in etag:
        meta = head.get("Metadata", {}) or {}
        b64 = meta.get("md5chksum")
        if not b64:
            return None, "multipart without md5chksum metadata"
        try:
            return base64.b64decode(b64, validate=True).hex(), "md5chksum metadata (multipart)"
        except ValueError:
            return None, "unreadable md5chksum metadata"
    if len(etag) == 32:
        return etag.lower(), "etag (single-part)"
    return None, "unrecognised etag"


def compare(manifest: dict, landing_prefix: str, store) -> dict:
    """Compares the manifest with what is at the destination: every file present once,
    no extra keys, sizes and MD5s equal."""
    listed = store.list_keys(landing_prefix)
    expected = {landing_prefix + f["path"]: f for f in manifest["files"]}
    missing = sorted(k[len(landing_prefix):] for k in expected if k not in listed)
    extra = sorted(k[len(landing_prefix):] for k in listed if k not in expected)
    results = []
    for key, f in sorted(expected.items()):
        row = {"path": f["path"], "size": f["size"], "sha256": f["sha256"], "verified": False}
        if key in listed:
            head = store.head(key)
            md5, how = destination_md5(head or {})
            row["md5_source"] = how
            row["upload"] = "multipart" if "multipart" in how else "single-part"
            if head is None:
                row["problem"] = "not readable at destination"
            elif head.get("ContentLength") != f["size"]:
                row["problem"] = "size differs"
            elif md5 is None:
                row["problem"] = how
            elif md5 != f["md5"]:
                row["problem"] = "checksum differs"
            else:
                row["verified"] = True
        else:
            row["problem"] = "missing at destination"
        results.append(row)
    ok = not missing and not extra and all(r["verified"] for r in results)
    return {"ok": ok, "missing": missing, "extra": extra,
            "mismatched": [r["path"] for r in results if r.get("problem") and r["path"] not in missing],
            "files": results}

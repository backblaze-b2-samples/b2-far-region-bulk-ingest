"""`make demo`: the real ingest workflow against B2 through the local link shaper.

1. Generates a capture batch (two single-part files and one multipart file).
2. Starts the link shaper (latency, a bandwidth cap and two injected connection drops).
3. Runs the wrapper, interrupts it once the first file has landed, and checks that the
   batch is reported as interrupted and has no verified marker.
4. Reruns the wrapper and checks completion: every file exactly once, files that had
   already landed not uploaded again, rclone check and manifest comparison passed,
   the report lists every file as verified and the marker exists.
5. Reads the multipart payload back with boto3 and compares bytes and SHA-256.
6. Deletes exactly the object versions this run wrote, prints the transfer report and
   one B2_ROUNDTRIP line.

Everything lives under sample-lite/b2-far-region-bulk-ingest/<nonce>/ in the bucket.
Pass --keep to leave the objects in place for inspection.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from app import manifest as mf
from app.fixtures import MiB, make_batch
from app.ingest import Layout, tools
from app.linkshaper import LinkShaper
from app.rclone import REPO_ROOT
from app.redact import redact
from app.roundtrip import resolve_nonce, roundtrip_line, sha256_hex
from app.store import SECRET_NAMES, Settings, Store, make_client

REPO_NAME = "b2-far-region-bulk-ingest"
SITE, BATCH = "site-a", "batch-0001"
SHAPING = {"latency_ms": 120, "bandwidth_bps": int(1.5 * MiB), "drop_after_bytes": 4 * MiB, "max_drops": 2}


class DemoError(RuntimeError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise DemoError(message)


def wrapper(layout: Layout, source: Path, proxy: str, log_dir: Path) -> list[str]:
    return [sys.executable, "-m", "app.cli", "ingest", "--source", str(source), "--site", layout.site,
            "--batch", layout.batch, "--dest-prefix", layout.prefix, "--proxy", proxy, "--log-dir", str(log_dir)]


def delete_all_versions(s3, bucket: str, key: str) -> int:
    """Deletes every version of one exact key, newest first, by VersionId."""
    deleted = 0
    for _ in range(20):
        head = Store(s3, bucket).head(key)
        if head is None:
            return deleted
        version = head.get("VersionId")
        if version:
            s3.delete_object(Bucket=bucket, Key=key, VersionId=version)
        else:
            s3.delete_object(Bucket=bucket, Key=key)
        deleted += 1
    raise DemoError(f"could not remove every version of {key}")


def abort_written_uploads(s3, bucket: str, prefix: str, written: list[str]) -> int:
    """Abort only unfinished uploads for exact keys written by this invocation."""
    uploads = s3.list_multipart_uploads(Bucket=bucket, Prefix=prefix + "/").get("Uploads", [])
    owned = [u for u in uploads if u["Key"] in written]
    for u in owned:
        s3.abort_multipart_upload(Bucket=bucket, Key=u["Key"], UploadId=u["UploadId"])
    return len(owned)


def print_report(report: dict, evidence: dict) -> None:
    retries = evidence["rerun"]["retries"]
    print("\nTransfer report  " + report["site"] + "/" + report["batch"])
    print(f"  status        {report['status']}  (complete={str(report['complete']).lower()})")
    print(f"  attempts      {len(report['attempts'])}: " + ", ".join(a["result"] for a in report["attempts"]))
    print(f"  rclone-logged retries  {retries['rclone_attempts_failed']} failed attempt(s),"
          f" {retries['rclone_low_level_retries']} low-level retries")
    print(f"  link shaper   {retries['link_shaper_drops_injected']} connection drop(s) injected")
    print("  recovery shown by passing rclone check and manifest comparison")
    print(f"  rclone check  {'ok' if report['rclone_check']['ok'] else 'FAILED'}"
          f" ({report['rclone_check']['matched']} matching, {report['rclone_check']['unchecked_hashes']} unchecked)")
    print(f"  manifest      {'ok' if report['manifest_comparison']['ok'] else 'FAILED'}"
          f" (missing {len(report['manifest_comparison']['missing'])},"
          f" extra {len(report['manifest_comparison']['extra'])})")
    print("  destination verification  md5+size via rclone check and manifest comparison")
    for f in report["files"]:
        mark = "verified" if f["verified"] else "NOT VERIFIED"
        print(f"  {mark:<12}  {f['path']:<24} {f['size']:>9} B  {f['upload']:<11}"
              f"  source SHA-256 {f['sha256'][:16]}...")
    print(f"  marker        {report['marker_key']}")


def run(keep: bool) -> int:
    settings = Settings.from_env()
    nonce = resolve_nonce(os.environ.get("LITE_NONCE"))
    prefix = f"sample-lite/{REPO_NAME}/{nonce}"
    layout = Layout(SITE, BATCH, prefix)
    s3 = make_client(settings)
    existing = s3.list_object_versions(Bucket=settings.bucket, Prefix=prefix + "/", MaxKeys=1)
    check(not existing.get("Versions") and not existing.get("DeleteMarkers"),
          "demo nonce prefix is occupied; choose a new LITE_NONCE")
    uploads = s3.list_multipart_uploads(Bucket=settings.bucket, Prefix=prefix + "/", MaxUploads=1)
    check(not uploads.get("Uploads"), "demo nonce prefix is occupied; choose a new LITE_NONCE")
    work = REPO_ROOT / ".local" / "demo" / nonce
    source = make_batch(work / SITE / BATCH)
    log_dir = work / "logs"
    store = Store(s3, settings.bucket)
    written = [layout.manifest_key, layout.report_key, layout.marker_key] + [
        layout.landing_prefix + f["path"] for f in mf.scan_source(source)]
    evidence: dict = {"nonce": nonce, "prefix": prefix, "region": settings.region,
                      "tools": tools(), "shaping": SHAPING}
    site_before = {p: p.stat().st_mtime_ns for p in source.rglob("*")}
    shaper = LinkShaper("connect", allowed_hosts=(urlparse(settings.endpoint_url).hostname,), **SHAPING)
    proxy = f"http://127.0.0.1:{shaper.start()}"
    print(f"batch {SITE}/{BATCH}: 3 files, destination {prefix}/ingest/ (region {settings.region})")
    print(f"link shaper: {SHAPING['latency_ms']} ms latency, {SHAPING['bandwidth_bps'] // 1024} KiB/s cap,"
          f" {SHAPING['max_drops']} connection drops after {SHAPING['drop_after_bytes'] // MiB} MiB")
    try:
        # -- 1. interrupted run ---------------------------------------------------------
        print("\n[1/3] ingest, interrupted once the first file lands")
        started = time.monotonic()
        proc = subprocess.Popen(wrapper(layout, source, proxy, log_dir), cwd=REPO_ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        landed: dict = {}
        while proc.poll() is None and not landed:
            time.sleep(0.25)
            landed = store.list_keys(layout.landing_prefix)
        check(proc.poll() is None, "the batch finished before it could be interrupted")
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=180)
        check(proc.returncode == 130, f"interrupted wrapper exited {proc.returncode}, expected 130")
        print("  " + err.strip().splitlines()[-1])
        report = store.get_json(layout.report_key)
        before = {k: store.head(k) for k in store.list_keys(layout.landing_prefix)}
        check(report and report["status"] == "interrupted" and not report["complete"], "report is not 'interrupted'")
        check(store.head(layout.marker_key) is None, "a verified marker exists after an interrupted run")
        check(0 < len(before) < 3, f"{len(before)} of 3 files landed before the interruption")
        print(f"  {len(before)} of 3 files had landed; report status 'interrupted'; no verified marker")
        evidence["interrupted"] = {"files_landed": sorted(k[len(layout.landing_prefix):] for k in before),
                                   "seconds_to_interrupt": round(time.monotonic() - started, 2),
                                   "marker_present": False, "exit": proc.returncode}

        # -- 2. rerun -----------------------------------------------------------------------
        print("\n[2/3] rerun the same command")
        rerun = subprocess.run(wrapper(layout, source, proxy, log_dir), cwd=REPO_ROOT,
                               capture_output=True, text=True, timeout=1800)
        check(rerun.returncode == 0, "rerun failed: " + rerun.stderr.strip()[-300:])
        print("  " + rerun.stdout.strip().splitlines()[-1])
        report = store.get_json(layout.report_key)
        manifest = store.get_json(layout.manifest_key)
        expected = sorted(layout.landing_prefix + f["path"] for f in manifest["files"])
        listed = store.list_keys(layout.landing_prefix)
        check(sorted(listed) == expected, "destination files differ from the manifest (missing or extra)")
        check(report["complete"] and report["status"] == "verified", "report is not 'verified'")
        check(report["rclone_check"]["ok"] and report["manifest_comparison"]["ok"], "verification did not pass")
        check(sorted(f["path"] for f in report["files"]) == sorted(f["path"] for f in manifest["files"])
              and all(f["verified"] for f in report["files"]), "report does not list every file as verified")
        marker = store.head(layout.marker_key)
        check(marker is not None, "verified marker missing after verification")
        check(marker["LastModified"] >= store.head(layout.report_key)["LastModified"],
              "marker is older than the final report")
        for key, head in before.items():
            now_head = store.head(key)
            check(now_head.get("ETag") == head.get("ETag") and now_head.get("VersionId") == head.get("VersionId"),
                  f"{key[len(layout.landing_prefix):]} was uploaded again on rerun")
        print(f"  {len(listed)} files at the destination, each once; {len(before)} already-landed file(s)"
              " not uploaded again")
        evidence["rerun"] = {"exit": 0, "status": report["status"], "attempts": [a["result"] for a in report["attempts"]],
                             "retries": {**report["retries"], "link_shaper_drops_injected": shaper.stats.drops},
                             "rclone_check": report["rclone_check"],
                             "manifest_comparison": report["manifest_comparison"],
                             "files_at_destination": len(listed),
                             "already_landed_not_reuploaded": len(before),
                             "capture_to_verification_seconds": report["capture_to_verification_seconds"]}

        # -- 3. readback -------------------------------------------------------------------
        print("\n[3/3] read back the multipart payload with boto3")
        entry = max(manifest["files"], key=lambda f: f["size"])
        key = layout.landing_prefix + entry["path"]
        head = store.head(key)
        check("-" in head["ETag"] and head.get("Metadata", {}).get("md5chksum"),
              "multipart object without md5chksum metadata")
        data = store.get_bytes(key)
        local = (source / entry["path"]).read_bytes()
        check(data == local, "downloaded bytes differ from the source file")
        check(sha256_hex(data) == entry["sha256"], "downloaded SHA-256 differs from the manifest")
        print(f"  {entry['path']}: {len(data)} bytes, SHA-256 matches source and manifest"
              f" (ETag has {head['ETag'].strip(chr(34)).split('-')[1]} parts; MD5 from md5chksum metadata)")
        check({p: p.stat().st_mtime_ns for p in source.rglob("*")} == site_before, "source files changed")
        evidence["readback"] = {"key": key, "bytes": len(data), "sha256": sha256_hex(data),
                                "matches_source": True, "matches_manifest": True,
                                "multipart_parts": int(head["ETag"].strip('"').split("-")[1])}
        evidence["destination"] = [{"key": k, "bytes": listed[k]} for k in expected]
        evidence["files"] = report["files"]
        evidence["site_files_unchanged"] = True
    finally:
        shaper.stop()
        evidence["shaper"] = {"drops": shaper.stats.drops, "connections": shaper.stats.connections,
                              "refused": shaper.stats.refused}
        evidence["unfinished_uploads_aborted"] = abort_written_uploads(s3, settings.bucket, prefix, written)
        if not keep:
            evidence["cleanup_versions_deleted"] = sum(delete_all_versions(s3, settings.bucket, k) for k in written)
            check(not store.list_keys(prefix + "/"), "objects remain under the run prefix after cleanup")
        out_dir = REPO_ROOT / ".local" / "verify"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "demo-evidence.json").write_text(json.dumps(evidence, indent=2, default=str) + "\n")

    print_report(report, evidence)
    if keep:
        print("\nkept: objects remain under " + prefix + "/")
    else:
        print(f"\ncleanup: deleted the {evidence['cleanup_versions_deleted']} object versions this run wrote")
    print(roundtrip_line(key, data))
    return 0


def main() -> int:
    try:
        return run(keep="--keep" in sys.argv[1:])
    except Exception as exc:  # noqa: BLE001 - one redacted line for any failure
        message = redact(f"{type(exc).__name__}: {exc}", [os.environ.get(n) for n in SECRET_NAMES])
        print(f"B2_ROUNDTRIP fail {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Real multi-site ingest, interrupted at random traffic cuts, verified and cleaned up."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from app import manifest as mf
from app.fixtures import MiB, make_scaled_batch, scaled_files
from app.ingest import Layout, tools
from app.linkshaper import LinkShaper
from app.rclone import REPO_ROOT
from app.redact import redact
from app.roundtrip import resolve_nonce, roundtrip_line, sha256_hex
from app.scenario import DATA_SEED, Scale, choose_cut, new_rng, resolve_scale
from app.store import SECRET_NAMES, Settings, Store, make_client

REPO_NAME = "b2-far-region-bulk-ingest"


class DemoError(RuntimeError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise DemoError(message)


def wrapper(layout: Layout, source: Path, proxy: str, log_dir: Path) -> list[str]:
    return [sys.executable, "-m", "app.cli", "ingest", "--source", str(source), "--site", layout.site,
            "--batch", layout.batch, "--dest-prefix", layout.prefix, "--proxy", proxy, "--log-dir", str(log_dir)]


def list_versions(s3, bucket: str, prefix: str) -> list[dict]:
    """One paginated snapshot including versions and delete markers. Never emit IDs."""
    entries = []
    markers = {}
    while True:
        page = s3.list_object_versions(Bucket=bucket, Prefix=prefix, **markers)
        entries.extend(page.get("Versions", []))
        entries.extend(page.get("DeleteMarkers", []))
        if not page.get("IsTruncated"):
            return entries
        markers = {"KeyMarker": page["NextKeyMarker"]}
        if page.get("NextVersionIdMarker"):
            markers["VersionIdMarker"] = page["NextVersionIdMarker"]


def cleanup_versions(s3, bucket: str, prefix: str, written: set[str]) -> int:
    """Delete only exact owned keys by version, then verify the whole run prefix."""
    scoped = prefix.rstrip("/") + "/"
    owned = [v for v in list_versions(s3, bucket, scoped) if v["Key"] in written]

    def delete(v):
        s3.delete_object(Bucket=bucket, Key=v["Key"], VersionId=v["VersionId"])

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(delete, owned))
    check(not list_versions(s3, bucket, scoped), "object versions remain under the run prefix after cleanup")
    return len(owned)


def check_landing_versions(s3, bucket: str, prefix: str, expected: list[str]) -> None:
    counts = Counter(v["Key"] for v in list_versions(s3, bucket, prefix))
    check(set(counts) == set(expected) and all(counts[k] == 1 for k in expected),
          "each expected landing key must have exactly one stored version")


def abort_written_uploads(s3, bucket: str, prefix: str, written) -> int:
    """Abort only unfinished uploads for exact keys written by this invocation."""
    owned = set(written)
    uploads = []
    markers = {}
    while True:
        page = s3.list_multipart_uploads(Bucket=bucket, Prefix=prefix + "/", **markers)
        uploads.extend(u for u in page.get("Uploads", []) if u["Key"] in owned)
        if not page.get("IsTruncated"):
            break
        markers = {"KeyMarker": page["NextKeyMarker"], "UploadIdMarker": page["NextUploadIdMarker"]}
    for u in uploads:
        s3.abort_multipart_upload(Bucket=bucket, Key=u["Key"], UploadId=u["UploadId"])
    return len(uploads)


def new_evidence(nonce: str, prefix: str, region: str, scale: Scale, seed: int) -> dict:
    """Evidence schema deliberately excludes the numeric bandwidth setting."""
    shaping = {k: scale.shaping[k] for k in ("latency_ms", "drop_after_bytes", "max_drops")}
    shaping["bandwidth_cap"] = "raised" if scale.name == "large" else "standard"
    return {
        "nonce": nonce, "prefix": prefix, "region": region, "tools": tools(), "scale": scale.name,
        "seed": seed, "data_seed": DATA_SEED, "shaping": shaping,
        "batches": [
            {"site": p.site, "batch": p.batch, "files": p.files, "bytes": p.bytes,
             "interrupted": False, "cut": None, "marker_present_after_interrupt": None,
             "exit_codes": [], "attempts": [], "retries": {}, "rclone_check": None,
             "manifest_comparison": None, "counts": {"expected": p.files, "verified": 0,
                                                    "missing": p.files, "duplicates": 0},
             "capture_to_verification_seconds": None, "files_landed_before_rerun": [],
             "already_landed_not_reuploaded": 0}
            for p in scale.batches],
        "destination": [], "readback": None, "unfinished_uploads_aborted": 0, "cleanup_versions_deleted": 0,
    }


def totals(evidence: dict) -> dict:
    batches = evidence["batches"]
    result = {name: sum(b["counts"][name] for b in batches)
              for name in ("expected", "verified", "missing", "duplicates")}
    result.update({name: sum(b["retries"].get(name, 0) for b in batches)
                   for name in ("reruns", "rclone_attempts_failed", "rclone_low_level_retries")})
    result["link_shaper_drops_injected"] = evidence.get("shaper", {}).get("drops", 0)
    durations = [b["capture_to_verification_seconds"] for b in batches
                 if b["capture_to_verification_seconds"] is not None]
    result["capture_to_verification_seconds"] = {"sum": round(sum(durations), 3), "max": max(durations, default=0)}
    return result


def print_header(scale: Scale, prefix: str, region: str) -> None:
    print(f"scale {scale.name}: {len({p.site for p in scale.batches})} sites, {len(scale.batches)} batches,"
          f" {sum(p.files for p in scale.batches)} files, {sum(p.bytes for p in scale.batches) / MiB:.1f} MiB")
    print(f"destination {prefix}/ingest/ (region {region})")
    s = scale.shaping
    print(f"link shaper: {s['latency_ms']} ms latency, bandwidth cap,"
          f" up to {s['max_drops']} connection drops after {s['drop_after_bytes'] // MiB} MiB")


def print_interrupt(batch: dict) -> None:
    cut = batch["cut"]
    print(f"{batch['site']}/{batch['batch']}: cut at {cut['fraction']:.0%} of the batch bytes;"
          f" {cut['files_landed_at_cut']} of {batch['files']} files had landed; report 'interrupted';"
          " no verified marker")


def print_batch(batch: dict) -> None:
    print(f"batch {batch['site']}/{batch['batch']}: verified"
          f" ({batch['counts']['verified']}/{batch['counts']['expected']} files verified);"
          f" {batch['already_landed_not_reuploaded']} not re-uploaded")


def print_report(evidence: dict) -> None:
    print("\nTransfer report")
    row = "{:<20} {:>8} {:>8} {:>7} {:>10} {:>8} {:>6}, {:>9}"
    print(row.format("site/batch", "expected", "verified", "missing", "duplicates", "attempts", "failed", "low-level"))
    for b in evidence["batches"]:
        c, r = b["counts"], b["retries"]
        print(row.format(f"{b['site']}/{b['batch']}", c["expected"], c["verified"], c["missing"],
                         c["duplicates"], len(b["attempts"]), r.get("rclone_attempts_failed", 0),
                         r.get("rclone_low_level_retries", 0)))
    t = totals(evidence)
    print(f"Totals: expected {t['expected']}, verified {t['verified']}, missing {t['missing']},"
          f" duplicates {t['duplicates']}, reruns {t['reruns']}")
    print(f"rclone-logged retries: {t['rclone_attempts_failed']} failed attempts,"
          f" {t['rclone_low_level_retries']} low-level; link shaper drops injected: {t['link_shaper_drops_injected']}")
    print("recovery shown by passing rclone check and manifest comparison")
    print("destination verification  md5+size via rclone check and manifest comparison")


def interrupt_wrapper(command: list[str], shaper, target: int) -> tuple[int, int]:
    """Poll traffic since this wrapper started, without listing destination objects."""
    start_bytes = shaper.stats.bytes_up
    proc = subprocess.Popen(command, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 7200
    try:
        while shaper.stats.bytes_up - start_bytes < target:
            check(proc.poll() is None, "the batch finished before the chosen traffic cut was reached")
            check(time.monotonic() < deadline, "timed out waiting for the chosen traffic cut")
            time.sleep(0.05)
        observed = shaper.stats.bytes_up - start_bytes
        check(proc.poll() is None, "the batch finished before the chosen traffic cut could interrupt it")
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=180)
        check(proc.returncode == 130, f"interrupted wrapper exited {proc.returncode}, expected 130")
        return proc.returncode, observed
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()


def verify_batch(store: Store, layout: Layout, before: dict) -> tuple[dict, dict, dict]:
    report = store.get_json(layout.report_key)
    manifest = store.get_json(layout.manifest_key)
    check(report is not None and manifest is not None, "batch report or manifest missing")
    expected = sorted(layout.landing_prefix + f["path"] for f in manifest["files"])
    listed = store.list_keys(layout.landing_prefix)
    check(sorted(listed) == expected, "destination files differ from the manifest (missing or extra)")
    check(report["complete"] and report["status"] == "verified", "report is not 'verified'")
    check(report["rclone_check"]["ok"] and report["manifest_comparison"]["ok"], "verification did not pass")
    check(report["counts"] == {"expected": len(expected), "verified": len(expected), "missing": 0, "duplicates": 0},
          "report counts do not show every file verified exactly once")
    check(sorted(f["path"] for f in report["files"]) == sorted(f["path"] for f in manifest["files"])
          and all(f["verified"] for f in report["files"]), "report does not list every file as verified")
    marker = store.head(layout.marker_key)
    check(marker is not None, "verified marker missing after verification")
    check(marker["LastModified"] >= store.head(layout.report_key)["LastModified"],
          "marker is older than the final report")
    check_landing_versions(store.s3, store.bucket, layout.landing_prefix, expected)
    for key, head in before.items():
        current = store.head(key)
        check(current is not None and current.get("ETag") == head.get("ETag")
              and current.get("VersionId") == head.get("VersionId"), "a landed file was uploaded again on rerun")
    return report, manifest, listed


def run(keep: bool, scale: Scale | None = None) -> int:
    scale = scale or resolve_scale(os.environ.get("SCALE"))
    settings = Settings.from_env()
    nonce = resolve_nonce(os.environ.get("LITE_NONCE"))
    prefix = f"sample-lite/{REPO_NAME}/{nonce}"
    s3 = make_client(settings)
    existing = s3.list_object_versions(Bucket=settings.bucket, Prefix=prefix + "/", MaxKeys=1)
    check(not existing.get("Versions") and not existing.get("DeleteMarkers"),
          "demo nonce prefix is occupied; choose a new LITE_NONCE")
    uploads = s3.list_multipart_uploads(Bucket=settings.bucket, Prefix=prefix + "/", MaxUploads=1)
    check(not uploads.get("Uploads"), "demo nonce prefix is occupied; choose a new LITE_NONCE")

    # No source generation or local writes until the remote prefix is known to be empty.
    rng = new_rng()
    evidence = new_evidence(nonce, prefix, settings.region, scale, rng.run_seed)
    interrupted = set(rng.sample(range(len(scale.batches)), scale.interrupt_batches))
    work = REPO_ROOT / ".local" / "demo" / nonce / scale.name
    store = Store(s3, settings.bucket)
    written = set()
    for p in scale.batches:
        layout = Layout(p.site, p.batch, prefix)
        written.update((layout.manifest_key, layout.report_key, layout.marker_key))
        written.update(layout.landing_prefix + name for name, _ in scaled_files(p.small_files, p.multipart_sizes))
    shaper = LinkShaper("connect", allowed_hosts=(urlparse(settings.endpoint_url).hostname,), **scale.shaping)
    candidates, sources = [], []
    print_header(scale, prefix, settings.region)
    try:
        proxy = f"http://127.0.0.1:{shaper.start()}"
        for index, (p, batch) in enumerate(zip(scale.batches, evidence["batches"])):
            layout = Layout(p.site, p.batch, prefix)
            source = make_scaled_batch(work / p.site / p.batch, p.small_files, p.multipart_sizes, DATA_SEED + index)
            snapshot = {source / name: (source / name).stat().st_mtime_ns
                        for name, _ in scaled_files(p.small_files, p.multipart_sizes)}
            sources.append(snapshot)
            command = wrapper(layout, source, proxy, work / "logs" / p.site / p.batch)
            before = {}
            if index in interrupted:
                target = choose_cut(rng, p.bytes)
                batch["interrupted"] = True
                batch["cut"] = {"seed": rng.run_seed, "fraction": target / p.bytes, "target_bytes": target,
                                "shaper_bytes_at_cut": None, "files_landed_at_cut": None}
                code, observed = interrupt_wrapper(command, shaper, target)
                batch["exit_codes"].append(code)
                batch["cut"]["shaper_bytes_at_cut"] = observed
                report = store.get_json(layout.report_key)
                batch["marker_present_after_interrupt"] = store.head(layout.marker_key) is not None
                check(report and report["status"] == "interrupted" and not report["complete"],
                      "report is not 'interrupted'")
                batch.update(attempts=[a["result"] for a in report["attempts"]], retries=report["retries"])
                check(not batch["marker_present_after_interrupt"], "a verified marker exists after an interrupted run")
                before = {k: store.head(k) for k in store.list_keys(layout.landing_prefix)}
                batch["files_landed_before_rerun"] = sorted(k[len(layout.landing_prefix):] for k in before)
                batch["cut"]["files_landed_at_cut"] = len(before)
                check(len(before) < p.files, "all files landed before the interruption")
                print_interrupt(batch)

            completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=7200)
            batch["exit_codes"].append(completed.returncode)
            check(completed.returncode == 0, f"batch wrapper exited {completed.returncode}; expected verified completion")
            report, manifest, listed = verify_batch(store, layout, before)
            batch.update(attempts=[a["result"] for a in report["attempts"]], retries=report["retries"],
                         rclone_check=report["rclone_check"], manifest_comparison=report["manifest_comparison"],
                         counts=report["counts"], already_landed_not_reuploaded=len(before),
                         capture_to_verification_seconds=mf.seconds(report["captured_at"], report["verified_at"]))
            evidence["destination"].extend({"key": k, "bytes": listed[k]} for k in sorted(listed))
            candidates.extend((f, layout, source) for f in manifest["files"])
            print_batch(batch)

        entry, layout, source = min(candidates, key=lambda c: (-c[0]["size"], c[1].site, c[1].batch, c[0]["path"]))
        key = layout.landing_prefix + entry["path"]
        head = store.head(key)
        check(head is not None and "-" in head["ETag"] and head.get("Metadata", {}).get("md5chksum"),
              "multipart object without md5chksum metadata")
        data = store.get_bytes(key)
        check(data == (source / entry["path"]).read_bytes(), "downloaded bytes differ from the source file")
        check(sha256_hex(data) == entry["sha256"], "downloaded SHA-256 differs from the manifest")
        check(all(path.exists() and path.stat().st_mtime_ns == mtime
                  for snapshot in sources for path, mtime in snapshot.items()), "source files changed")
        evidence["readback"] = {"key": key, "bytes": len(data), "sha256": sha256_hex(data),
                                "matches_source": True, "matches_manifest": True,
                                "multipart_parts": int(head["ETag"].strip('"').split("-")[1])}
        evidence["site_files_unchanged"] = True
    finally:
        shaper.stop()
        evidence["shaper"] = {"drops": shaper.stats.drops, "connections": shaper.stats.connections,
                              "refused": shaper.stats.refused}
        try:
            try:
                evidence["unfinished_uploads_aborted"] = abort_written_uploads(s3, settings.bucket, prefix, written)
            finally:
                if not keep:
                    evidence["cleanup_versions_deleted"] = cleanup_versions(s3, settings.bucket, prefix, written)
        finally:
            evidence["totals"] = totals(evidence)
            out_dir = REPO_ROOT / ".local" / "verify"
            out_dir.mkdir(parents=True, exist_ok=True)
            filename = "demo-evidence-large.json" if scale.name == "large" else "demo-evidence.json"
            (out_dir / filename).write_text(json.dumps(evidence, indent=2, default=str) + "\n")

    print_report(evidence)
    print(f"readback: {layout.site}/{layout.batch}/{entry['path']}: {len(data)} bytes;"
          " bytes and SHA-256 match source and manifest")
    print("multipart readback: ETag confirms parts; MD5 from md5chksum metadata")
    print(f"cleanup: deleted {evidence['cleanup_versions_deleted']} object versions;"
          f" aborted {evidence['unfinished_uploads_aborted']} unfinished uploads")
    if keep:
        print("kept: objects remain under " + prefix + "/")
    print(roundtrip_line(key, data))
    return 0


def main() -> int:
    try:
        scale = resolve_scale(os.environ.get("SCALE"))
    except ValueError as exc:
        print(redact(str(exc), [os.environ.get(n) for n in SECRET_NAMES]), file=sys.stderr)
        return 2
    try:
        return run(keep="--keep" in sys.argv[1:], scale=scale)
    except Exception as exc:  # noqa: BLE001 - one redacted line for any failure
        message = redact(f"{type(exc).__name__}: {exc}", [os.environ.get(n) for n in SECRET_NAMES])
        print(f"B2_ROUNDTRIP fail {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

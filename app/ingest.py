"""The per-batch ingest: manifest, rclone copy, rclone check, manifest comparison, report,
verified marker. Source files are only read: nothing at the site is moved or deleted.

Destination layout under the configured prefix:

    ingest/manifests/<site>/<batch>/manifest.json
    ingest/landing/<site>/<batch>/<files...>
    ingest/reports/<site>/<batch>.json
    ingest/reports/<site>/<batch>.verified      (written last, only after verification)
"""

from __future__ import annotations

import platform
import re
import subprocess
import time
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from app import manifest as mf
from app.rclone import RCLONE_VERSION, RcloneSession, count_retries, parse_check
from app.store import Store

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

EXIT_VERIFIED, EXIT_FAILED, EXIT_SOURCE_CHANGED, EXIT_INTERRUPTED = 0, 1, 3, 130


class SourceChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class Layout:
    site: str
    batch: str
    prefix: str = ""

    def __post_init__(self):
        for label, v in (("site", self.site), ("batch", self.batch)):
            if not _NAME.match(v):
                raise ValueError(f"{label} must match {_NAME.pattern}")
        if self.prefix and (self.prefix.startswith("/") or ".." in self.prefix.split("/")):
            raise ValueError("destination prefix must be relative")

    @property
    def root(self) -> str:
        p = self.prefix.strip("/")
        return f"{p}/ingest/" if p else "ingest/"

    @property
    def manifest_key(self) -> str:
        return f"{self.root}manifests/{self.site}/{self.batch}/manifest.json"

    @property
    def landing_prefix(self) -> str:
        return f"{self.root}landing/{self.site}/{self.batch}/"

    @property
    def report_key(self) -> str:
        return f"{self.root}reports/{self.site}/{self.batch}.json"

    @property
    def marker_key(self) -> str:
        return f"{self.root}reports/{self.site}/{self.batch}.verified"


def tools() -> dict:
    return {"rclone": RCLONE_VERSION, "boto3": version("boto3"), "python": platform.python_version()}


class Ingest:
    def __init__(self, layout: Layout, source: Path, store: Store, rclone: RcloneSession, log_dir: Path):
        self.layout = layout
        self.source = source
        self.store = store
        self.rclone = rclone
        self.log_dir = log_dir
        log_dir.mkdir(parents=True, exist_ok=True)

    # -- steps -------------------------------------------------------------------

    def ensure_manifest(self) -> dict:
        """Step 1: write the manifest once; a rerun must see the same source files."""
        files = mf.scan_source(self.source)
        existing = self.store.get_json(self.layout.manifest_key)
        if existing is not None:
            if not mf.same_files(existing["files"], files):
                raise SourceChanged("source files differ from the batch manifest already at the destination")
            return existing
        manifest = mf.build_manifest(self.layout.site, self.layout.batch, files)
        self.store.put_json(self.layout.manifest_key, manifest)
        return manifest

    def copy(self, attempt: dict) -> int:
        """Step 2: rclone copy --checksum. A rerun only sends what is missing or different."""
        log_path = self.log_dir / f"copy-{int(time.time() * 1000)}.log"
        with log_path.open("w", buffering=1) as log:
            proc = self.rclone.popen_copy(self.source, self.layout.landing_prefix)
            try:
                for line in proc.stdout:
                    log.write(self.rclone.clean(line).replace(str(self.source), "<source>"))
                code = proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                attempt["result"] = "interrupted"
                raise
            finally:
                proc.stdout.close()
                log.flush()
                attempt["retries"] = count_retries(log_path.read_text())
                attempt["rclone_exit"] = proc.returncode
        attempt["result"] = "copied" if code == 0 else "copy_failed"
        return code

    def check(self) -> dict:
        """Step 3: rclone check against the destination."""
        result, combined = self.rclone.check(self.source, self.layout.landing_prefix)
        output = result.output.replace(str(self.source), "<source>")
        (self.log_dir / f"check-{int(time.time() * 1000)}.log").write_text(output)
        return parse_check(result.returncode, output, combined)

    # -- the whole batch -----------------------------------------------------------

    def run(self) -> tuple[int, dict]:
        manifest = self.ensure_manifest()
        previous = self.store.get_json(self.layout.report_key) or {}
        attempts = list(previous.get("attempts", []))
        attempt = {"started_at": mf.now(), "result": "running"}
        attempts.append(attempt)
        report = self._report(manifest, attempts)
        self.store.put_json(self.layout.report_key, report)
        if self.store.head(self.layout.marker_key) is not None:
            self.store.delete(self.layout.marker_key)
        try:
            code = self.copy(attempt)
        except KeyboardInterrupt:
            attempt["ended_at"] = mf.now()
            report.update(status="interrupted", complete=False)
            self.store.put_json(self.layout.report_key, self._finish(report, attempts))
            raise
        if code != 0:
            attempt["ended_at"] = mf.now()
            report.update(status="copy_failed", complete=False)
            self.store.put_json(self.layout.report_key, self._finish(report, attempts))
            return EXIT_FAILED, report
        check = self.check()
        comparison = mf.compare(manifest, self.layout.landing_prefix, self.store, workers=8)
        attempt["ended_at"] = mf.now()
        report["rclone_check"] = check
        report["manifest_comparison"] = {k: comparison[k] for k in ("ok", "missing", "extra", "mismatched")}
        report["files"] = comparison["files"]
        report["counts"] = comparison["counts"]
        verified = check["ok"] and comparison["ok"]
        if not verified:
            report.update(status="verification_failed", complete=False)
            self.store.put_json(self.layout.report_key, self._finish(report, attempts))
            return EXIT_FAILED, report
        report.update(status="verified", complete=True, verified_at=mf.now(),
                      marker_key=self.layout.marker_key)
        self.store.put_json(self.layout.report_key, self._finish(report, attempts))
        # Step 5: the marker is written last, only after check and comparison both passed.
        self.store.put_json(self.layout.marker_key, {
            "site": self.layout.site, "batch": self.layout.batch, "verified_at": report["verified_at"],
            "files": len(manifest["files"]), "report_key": self.layout.report_key,
            "manifest_key": self.layout.manifest_key})
        return EXIT_VERIFIED, report

    def _report(self, manifest: dict, attempts: list) -> dict:
        """captured_at is when the site writes the batch manifest, handing the batch to ingest."""
        return {
            "site": self.layout.site, "batch": self.layout.batch,
            "manifest_key": self.layout.manifest_key, "landing_prefix": self.layout.landing_prefix,
            "captured_at": manifest["created_at"], "complete": False, "status": "running",
            "marker_key": None, "files": [
                {"path": f["path"], "size": f["size"], "sha256": f["sha256"], "verified": False}
                for f in manifest["files"]],
            "tools": tools(), "attempts": attempts,
        }

    @staticmethod
    def _finish(report: dict, attempts: list) -> dict:
        report["attempts"] = attempts
        report["retries"] = {
            "reruns": max(len(attempts) - 1, 0),
            "rclone_attempts_failed": sum(a.get("retries", {}).get("attempts_failed", 0) for a in attempts),
            "rclone_low_level_retries": sum(a.get("retries", {}).get("low_level_retries", 0) for a in attempts),
        }
        return report

"""`python -m app.cli`: the site-side ingest wrapper around rclone.

    ingest  --source DIR --site SITE --batch BATCH [--dest-prefix P] [--proxy URL]
    report  --site SITE --batch BATCH [--dest-prefix P]

Exit codes: 0 verified, 1 copy or verification failed, 2 usage, 3 source changed since
the manifest, 130 interrupted (rerun the same command to resume).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.ingest import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_SOURCE_CHANGED, Ingest, Layout, SourceChanged
from app.rclone import REPO_ROOT, RcloneSession
from app.redact import redact
from app.store import SECRET_NAMES, Settings, Store, make_client


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="Verifiable batch ingest to B2 with rclone.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("ingest", "report"):
        s = sub.add_parser(name)
        s.add_argument("--site", required=True)
        s.add_argument("--batch", required=True)
        s.add_argument("--dest-prefix", default="", help="key prefix above ingest/ (default: bucket root)")
        s.add_argument("--endpoint-url", default=None, help=argparse.SUPPRESS)  # local emulator in tests
        if name == "ingest":
            s.add_argument("--source", required=True, type=Path, help="the batch directory at the site")
            s.add_argument("--proxy", default=None, help="HTTP proxy for rclone, e.g. the local link shaper")
            s.add_argument("--log-dir", type=Path, default=REPO_ROOT / ".local" / "logs")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    secrets = [os.environ.get(n) for n in SECRET_NAMES]
    try:
        layout = Layout(args.site, args.batch, args.dest_prefix)
        settings = Settings.from_env(endpoint_url=args.endpoint_url)
        store = Store(make_client(settings), settings.bucket)
        if args.cmd == "report":
            report = store.get_json(layout.report_key)
            if report is None:
                print("no report for this batch yet", file=sys.stderr)
                return EXIT_FAILED
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        with RcloneSession(settings, proxy=args.proxy) as rclone:
            job = Ingest(layout, args.source, store, rclone, args.log_dir)
            code, report = job.run()
        print(f"batch {layout.site}/{layout.batch}: {report['status']}"
              f" ({sum(f['verified'] for f in report['files'])}/{len(report['files'])} files verified)")
        if code:
            print("not marked verified; see " + layout.report_key, file=sys.stderr)
        return code
    except KeyboardInterrupt:
        print("interrupted: batch is not verified; rerun the same command to resume", file=sys.stderr)
        return EXIT_INTERRUPTED
    except SourceChanged as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_SOURCE_CHANGED
    except ValueError as exc:
        print(f"usage: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - one redacted line for any failure
        print(redact(f"error: {type(exc).__name__}: {exc}", secrets), file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())

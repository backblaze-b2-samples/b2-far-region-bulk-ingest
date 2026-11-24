# b2-far-region-bulk-ingest

A Python CLI that wraps [rclone](https://rclone.org) to move capture batches (video, lidar, telemetry) from many sites into [Backblaze B2 Cloud Storage](https://www.backblaze.com/sign-up/ai-cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=b2ai-far-region-bulk-ingest) over long or unstable links, with proof of what arrived. Every batch gets a checksum manifest, a rerunnable `rclone copy --checksum`, an `rclone check`, an independent manifest comparison through `boto3`, a transfer report and a verified marker that is written only after both checks pass. Source files at the site are never moved or deleted.

B2 holds the batches and their verification records through the S3-compatible API, so downstream training jobs can wait for a batch's verified marker instead of trusting that a transfer "probably finished".

## Quick start

Prerequisites: Python 3.11+, [uv](https://docs.astral.sh/uv/), macOS or Linux (arm64 or x86_64), a B2 bucket and an application key scoped to it.

```bash
make setup             # Python deps + pinned rclone v1.75.2 into .local/bin (checksum-verified)
make test              # offline: local S3 emulator behind the link shaper, no B2 credentials
cp .env.example .env   # then fill in your B2 values
make demo              # the real workflow against your bucket, through the local link shaper
```

Ingest your own batch:

```bash
set -a; . ./.env; set +a
uv run python -m app.cli ingest --source /data/site-a/batch-0042 --site site-a --batch batch-0042
uv run python -m app.cli report --site site-a --batch batch-0042
```

Rerun the same `ingest` command after an interruption: rclone sends only what is missing, and the batch is marked verified only when everything checks out.

![make demo output](docs/images/demo.png)

## How it works

For a batch `<batch>` from site `<site>`, under an optional `--dest-prefix`:

1. **Manifest.** The wrapper hashes every source file (size, SHA-256, MD5) and writes `ingest/manifests/<site>/<batch>/manifest.json` with `put_object`. On a rerun it reads the manifest back and refuses to continue (exit 3) if the source files no longer match it.
2. **Copy.** `rclone copy --checksum` uploads to `ingest/landing/<site>/<batch>/`. Files that already match are skipped, so a rerun completes an interrupted batch without uploading anything twice.
3. **Check.** `rclone check` compares the source with the destination (`--combined` output: matched, missing, extra, differing). A hash rclone could not compare counts as a failure.
4. **Manifest comparison.** Independently of rclone, `list_objects_v2` and `head_object` confirm that every manifest file is at the destination exactly once, with nothing extra, and that size and MD5 match.
5. **Report and marker.** `ingest/reports/<site>/<batch>.json` lists every file and whether it was verified, every attempt (interrupted, copied, failed), the retries rclone logged and the capture-to-verification time. Only when steps 3 and 4 both pass is `ingest/reports/<site>/<batch>.verified` written, after the final report.

### Checksum behavior

- Files below 5 MiB upload in one request; their S3 ETag is the MD5 of the file.
- Larger files upload as multipart (5 MiB parts). Their ETag is **not** the file's MD5 (it ends in `-<parts>`); rclone stores the file's MD5 in the `X-Amz-Meta-Md5chksum` metadata, and the wrapper verifies against that. A multipart object without that metadata fails verification.

### Isolation and safety

- rclone runs only for `copy` and `check`. Each run writes its own rclone config (mode 600) in a temporary directory from the `B2_*` variables and deletes it afterwards; your global rclone config is never read or changed, and credentials never appear on a command line.
- Every boto3 client sets `Config(user_agent_extra="b2ai-far-region-bulk-ingest")`; every rclone call passes `--user-agent b2ai-far-region-bulk-ingest`.
- rclone output is captured and redacted (credentials, bucket name, signed-URL parameters, local paths) before it is logged or reported.

### The demo and the link shaper

`make demo` generates a three-file batch (two single-part files, one 12 MiB multipart file) and starts `app/linkshaper.py`, a local HTTP CONNECT proxy that adds latency, caps bandwidth and drops connections. rclone reaches B2 through it with TLS intact. The demo interrupts the wrapper once the first file lands, checks that the batch is reported as interrupted with no marker, reruns it, checks completion (every file once, already-landed files not re-uploaded, both checks passed, marker after report), reads the multipart file back with `boto3` and compares bytes and SHA-256 with the source, then deletes exactly the object versions it wrote and prints:

```
B2_ROUNDTRIP ok key=sample-lite/b2-far-region-bulk-ingest/<nonce>/ingest/landing/site-a/batch-0001/lidar/sweep-0000.bin bytes=12582912 sha256=<hex>
```

Everything the demo writes stays under `sample-lite/b2-far-region-bulk-ingest/<nonce>/` (`LITE_NONCE` sets the nonce). `uv run python -m app.demo --keep` leaves the objects in place for inspection; delete them afterwards with your usual tooling, scoped to that prefix. The demo's observations are saved to `.local/verify/demo-evidence.json`.

The evidence records the destination region and tool versions, verified batches, retries and reruns, injected link-shaper drops, and the time from capture to verification. These are observations of that run, with no speed claim.

## When to use

- Many capture sites push batches into one training corpus, each under its own `--site` prefix.
- Links are slow, far from the bucket's region or drop out, and transfers must survive reruns.
- Downstream jobs need evidence of what arrived (manifest, report, marker) before processing.
- Your team already uses rclone and wants to keep it as the transfer engine.

## When not to use

- You need continuous directory mirroring: `rclone sync` or `b2 sync` fit better (ingest never deletes destination batch files).
- You need to delete source data after upload, replicate across regions or migrate from another provider: out of scope.
- You want transfer tuning or throughput numbers: the sample makes no speed claims.

## FAQ

**Does `make test` need B2 credentials?**
No. It runs with every `B2_*` variable unset, against a local S3 emulator (moto) behind the link shaper, using the pinned rclone from `make setup`. It covers interruption and rerun, manifest agreement, report coverage, missing or extra destination files, checksum mismatches and refusal to write the marker after failed or incomplete verification.

**What makes a batch "verified"?**
Both `rclone check` and the boto3 manifest comparison pass on the same run. Any existing marker is removed when a new attempt starts, before copying, and written again only after both checks pass and the final report is saved. A reader that sees the marker can trust that the latest report is verified.

**What happens to my source files?**
Nothing: they are only read. The wrapper never calls `rclone sync`, `move` or `delete`.

**Why boto3 in addition to rclone?**
It gives an independent check of the destination and handles manifests, reports, markers and readback through the S3-compatible API.

**What does a rerun count as?**
Each `ingest` invocation is an attempt in the report. rclone's own retries are counted from its log; retries the S3 SDK inside rclone makes silently are not visible there.

## License

MIT, see [LICENSE](LICENSE). rclone (downloaded by `make setup`, not redistributed here) is MIT; boto3 and moto (tests only) are Apache-2.0. Test batches are generated; no third-party data is used.

# b2-far-region-bulk-ingest

A Python CLI that wraps [rclone](https://rclone.org) to move capture batches (video, lidar, telemetry) from many sites into [Backblaze B2 Cloud Storage](https://www.backblaze.com/sign-up/ai-cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=b2ai-far-region-bulk-ingest) over long or unstable links, with proof of what arrived. Every batch gets a checksum manifest, a rerunnable `rclone copy --checksum`, an `rclone check`, an independent manifest comparison through `boto3`, a transfer report and a verified marker that is written only after both checks pass. Source files at the site are never moved or deleted.

B2 holds the batches and their verification records through the S3-compatible API, so downstream training jobs can wait for a batch's verified marker instead of trusting that a transfer "probably finished".

## Quick start

Prerequisites: Python 3.11+, [uv](https://docs.astral.sh/uv/), macOS or Linux (arm64 or x86_64), a B2 bucket and an application key scoped to it.

```bash
make setup             # Python deps + pinned rclone v1.75.2 into .local/bin (checksum-verified)
make test              # offline: local S3 emulator behind the link shaper, no B2 credentials
cp .env.example .env   # then fill in your B2 values
make demo              # the real workflow through the local link shaper, using configured SCALE
make demo SCALE=large  # thousands of files and a few GB generated locally and uploaded
```

The default scale is `default`. `large` needs several GB of free disk space and takes much longer.
Command-line `SCALE` wins over `.env`, then the caller's environment; unset or empty uses `default`.
Any other `SCALE` value is an error, checked before B2 configuration or access.

Ingest your own batch:

```bash
set -a; . ./.env; set +a
uv run python -m app.cli ingest --source /data/site-a/batch-0042 --site site-a --batch batch-0042
uv run python -m app.cli report --site site-a --batch batch-0042
```

Rerun the same `ingest` command after an interruption: rclone sends only what is missing, and the batch is marked verified only when everything checks out.
Each copy prints its log path and a `tail -f` command to follow it; CLI logs are under `.local/logs/` by default (`--log-dir` changes it).

![make demo output](docs/images/demo.png)

## How it works

For a batch `<batch>` from site `<site>`, under an optional `--dest-prefix`:

1. **Manifest.** The wrapper hashes every source file (size, SHA-256, MD5) and writes `ingest/manifests/<site>/<batch>/manifest.json` with `put_object`. On a rerun it reads the manifest back and refuses to continue (exit 3) if the source files no longer match it.
2. **Copy.** `rclone copy --checksum` uploads to `ingest/landing/<site>/<batch>/`. Files that already match are skipped, so a rerun completes an interrupted batch without uploading anything twice.
3. **Check.** `rclone check` compares the source with the destination (`--combined` output: matched, missing, extra, differing). A hash rclone could not compare counts as a failure.
4. **Manifest comparison.** Independently of rclone, `list_objects_v2` and `head_object` confirm that every manifest file is at the destination exactly once, with nothing extra, and that size and MD5 match.
5. **Report and marker.** `ingest/reports/<site>/<batch>.json` lists every file and whether it was verified, counts (`expected`, `verified`, `missing`, `duplicates`), every attempt (interrupted, copied, failed), retries, `captured_at` and `verified_at`. Counts come from the manifest comparison; duplicate destination listing entries are counted before keys are deduplicated, while unexpected keys remain listed as `extra`. Only when steps 3 and 4 both pass is `ingest/reports/<site>/<batch>.verified` written, after the final report.

The report's `captured_at` is when the batch manifest is first written at the site, when the batch is handed to ingest. `verified_at` records successful verification. The demo computes `capture_to_verification_seconds` from these timestamps and saves it only in demo evidence, including interruptions and reruns. These timestamps do not measure when the source data was originally captured.

### Checksum behavior

- Per-file `sha256` values in reports and evidence are the source SHA-256 from the manifest. A file marked `verified` passed destination MD5 and size checks via rclone check and the manifest comparison. Only the demo's multipart readback checks destination bytes with SHA-256; other destination files are not read back for SHA-256 verification.
- Files below 5 MiB upload in one request; their S3 ETag is the MD5 of the file.
- Larger files upload as multipart (5 MiB parts). Their ETag is **not** the file's MD5 (it ends in `-<parts>`); rclone stores the file's MD5 in the `X-Amz-Meta-Md5chksum` metadata, and the wrapper verifies against that. A multipart object without that metadata fails verification.

### Isolation and safety

- rclone runs only for `copy` and `check`. Each run writes its own rclone config (mode 600) in a temporary directory from the `B2_*` variables and deletes it afterwards; your global rclone config is never read or changed, and credentials never appear on a command line.
- Every boto3 client sets `Config(user_agent_extra="b2ai-far-region-bulk-ingest")`; every rclone call passes `--user-agent b2ai-far-region-bulk-ingest`.
- rclone copy output is streamed line by line through redaction (credentials, bucket name, B2 upload/file IDs, signed-URL parameters, local paths) before any log write. A killed wrapper can leave an incomplete log, but raw subprocess output is never written to it.

### The demo and the link shaper

Both scales generate deterministic telemetry CSV and lidar payloads across three sites. The interruption point is random per run:

| Scale | Sites and batches | Files | Multipart payloads | Interrupted batches |
|---|---|---|---|---|
| `default` | site-a: 2 batches; site-b and site-c: 1 each | 185 total; 32, 48, 40, 56 telemetry files per batch | 2, 2, 2, 3 files of 6 MiB | All 4 |
| `large` | 2 batches per site, 6 total | 3,144 total; 520 telemetry files per batch | 4 files of 100 MiB per batch | 2 randomly chosen |

Default generates roughly 60 MiB; large generates a few GB. Telemetry files range from a few KiB to about 100 KiB. Generated files remain locally under `.local/demo/<nonce>/<scale>/` after remote cleanup. Separate scale directories prevent source files from a previous large run entering a default run when reusing an emptied nonce prefix.

The demo starts one `app/linkshaper.py` HTTP CONNECT proxy for the whole run. It is configured with latency, a bandwidth cap and connection drops: default uses 120 ms latency and up to 2 drops after 4 MiB; large uses lower latency, a raised cap and up to 4 drops after 4 MiB. rclone reaches B2 through it with TLS intact. Batches run sequentially.

For each selected batch, the demo polls uploaded traffic through the shaper and interrupts the wrapper at a uniformly chosen byte count within 20% to 70% of that batch's payload. It checks for an `interrupted`, incomplete report, fewer landed files than the manifest (possibly zero), and no verified marker. It then reruns the same command. Other batches run once. Every batch must finish verified: all files present exactly once, both checks passed, marker after the report, and already-landed files retaining their ETag and VersionId. A paginated version listing also checks that each landing key has exactly one stored version.

Expected transfer report counts for default (rclone-logged retries shown as `failed, low-level`):

```text
site/batch           expected verified missing duplicates attempts failed, low-level
site-a/batch-0001          34       34       0          0        2      0,         0
site-a/batch-0002          50       50       0          0        2      0,         0
site-b/batch-0001          42       42       0          0        2      0,         0
site-c/batch-0001          59       59       0          0        2      0,         0
Totals: expected 185, verified 185, missing 0, duplicates 0, reruns 4
```

Retry counts vary by run. The demo reads back the largest multipart file with `boto3` (ties ordered by site, batch, path), compares its bytes and SHA-256 with the source and manifest, deletes exactly the object versions it wrote, and ends with exactly one round-trip line:

```
B2_ROUNDTRIP ok key=sample-lite/b2-far-region-bulk-ingest/<nonce>/ingest/landing/<site>/<batch>/lidar/sweep-NNNN.bin bytes=<n> sha256=<64 lowercase hex>
```

Everything the demo writes stays under `sample-lite/b2-far-region-bulk-ingest/<nonce>/` (`LITE_NONCE` sets the nonce). Before generating files or uploading, the demo refuses to run if any object version, delete marker or unfinished multipart upload exists under that prefix. Cleanup pages versions and delete markers under that prefix, deletes only exact written keys by VersionId using individual `delete_object` calls, and verifies the prefix is empty. Unfinished uploads are aborted only for exact written keys. Choose a fresh nonce for each invocation. `uv run python -m app.demo --keep` leaves the objects in place for inspection; delete them afterwards with your usual tooling, scoped to that prefix.

Evidence is saved in `.local/verify/demo-evidence.json` for default and `.local/verify/demo-evidence-large.json` for large, including failures after prefix validation. It records the region, tools, scale, shaping configuration (bandwidth label only), random seed and per-batch cut (fraction, target bytes, observed shaper bytes and landed files), counts, attempts, retries, both checks, and files preserved on rerun. It also records `capture_to_verification_seconds` per batch and its sum/max, destination keys and sizes, multipart readback, shaper drops/connections/refusals, aborted uploads and deleted versions. Recovery is shown by passing rclone check and manifest comparison. The timing starts at manifest handoff to ingest; it is an observation of that run with no speed claim, and appears only in evidence, never stdout or transfer reports.

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
Both `rclone check` and the boto3 manifest comparison pass on the same run. Any existing marker is removed when a new attempt starts, before copying, and written again only after both checks pass and the final report is saved. A new attempt saves a running, incomplete report before removing the old marker, so readers must confirm both marker presence and a verified report.

**What happens to my source files?**
Nothing: they are only read. The wrapper never calls `rclone sync`, `move` or `delete`.

**Why boto3 in addition to rclone?**
It gives an independent check of the destination and handles manifests, reports, markers and readback through the S3-compatible API.

**What does a rerun count as?**
Each `ingest` invocation is an attempt in the report; `retries.reruns` counts invocations after the first. Default interrupts and reruns all four batches; large interrupts two randomly chosen batches and runs the other four once. The source content stays deterministic while the cut point changes per run. `rclone_attempts_failed` and `rclone_low_level_retries` count only rclone-logged retries; retries the S3 SDK inside rclone makes silently are not visible there, so these counts can be zero despite injected drops. Demo evidence records retries per batch and `link_shaper_drops_injected` in totals. Recovery is shown by rclone check passing and manifest comparison succeeding, with already-landed files retaining their ETag and VersionId.

## License

MIT, see [LICENSE](LICENSE). rclone (downloaded by `make setup`, not redistributed here) is MIT; boto3 and moto (tests only) are Apache-2.0. Test batches are generated; no third-party data is used.

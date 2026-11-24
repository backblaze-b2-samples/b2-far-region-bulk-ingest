# AGENTS.md

Instructions for AI coding agents and humans working on `b2-far-region-bulk-ingest`. `CLAUDE.md` only imports this file.

## What this repository is

A Python sample (cli) for Backblaze B2 Cloud Storage. It talks to B2 through `boto3`, and its facts live in `sample.json` (`language`, `variant`, `interface`, `required_sdk`, `user_agent_extra`, `main_flow`, `extra_clients`). Keep `sample.json` in agreement with the documented scope in `README.md`.

## The make contract

| Target | What it does | Needs B2 credentials |
|---|---|---|
| `make setup` | Installs dependencies. | No |
| `make test` | Runs the offline tests. | No, and it must pass with no `B2_*` variable set |
| `make demo` | Runs the main flow against a real bucket and prints exactly one round-trip line. | Yes, read from `.env` or the environment |

The round-trip line has this exact format, on its own line:

```
B2_ROUNDTRIP ok key=sample-lite/b2-far-region-bulk-ingest/<nonce>/<name> bytes=<n> sha256=<64 lowercase hex>
```

`<nonce>` comes from the `LITE_NONCE` environment variable (a random one is generated when it is unset). `bytes` and `sha256` describe the bytes read back from B2, after checking they equal the bytes written.

## This sample

- `app/cli.py` is the site-side wrapper (`ingest`, `report`); `app/ingest.py` holds the batch steps (manifest, copy, check, comparison, report, marker); `app/rclone.py` is the only place rclone is invoked; `app/manifest.py` hashes sources and compares the destination; `app/linkshaper.py` is the local link shaper; `app/demo.py` is `make demo`.
- The round-trip key is the multipart payload the ingest actually landed: `sample-lite/b2-far-region-bulk-ingest/<nonce>/ingest/landing/site-a/batch-0001/lidar/sweep-0000.bin`.
- rclone is the extra client declared in `sample.json`: pinned to v1.75.2 in `scripts/install_rclone.py` (SHA-256 checked) and installed to `.local/bin/rclone`. Run it only through `RcloneSession.command`, which always adds `--user-agent b2ai-far-region-bulk-ingest` and allows only `copy` and `check`. `tests/test_rclone.py` and `tests/test_ingest_emulator.py` fail if the user agent goes missing.
- Never delete or move source files, never write the verified marker before both `rclone check` and the manifest comparison pass, and never treat a multipart ETag as an MD5.
- The demo's cleanup deletes every version of exactly the keys it wrote and aborts unfinished multipart uploads under its own prefix.

## B2 rules

1. Read configuration only through the five standard names: `B2_APPLICATION_KEY_ID`, `B2_APPLICATION_KEY`, `B2_BUCKET_NAME`, `B2_REGION`, `B2_PUBLIC_URL_BASE`. Never use other names or aliases for them.
2. Every B2 client sets the custom user agent `b2ai-far-region-bulk-ingest` at the place it is constructed (the snippet for this language is below). Use a string literal or one constant bound once to that literal; never build it at runtime or pass options through a spread.
3. Use only the SDK named in `sample.json`. Any other storage client (another SDK, a raw HTTP call to B2, a CLI subprocess) is allowed only when `sample.json` names it under `extra_clients`, and then it also sets the user agent.
4. Every object the demo writes lives under `sample-lite/b2-far-region-bulk-ingest/<nonce>/`. Any listing is scoped to that prefix, and cleanup deletes only the exact keys the demo wrote. Never create, update or delete buckets or keys, and never call another management operation outside the documented scope in `README.md`.
5. The demo performs the operations declared in `sample.json` under `main_flow`. The round trip under the prefix above is always allowed in addition.
6. Never print, log or commit a credential, the bucket name, the bucket id, the account id, an authorization token or a signed URL. Errors are printed with those values redacted.

## Evidence and images

- One PNG in `docs/images/`: for a CLI, library, service or worker, a terminal capture of `make demo` (`docs/images/demo.png`); for a web app, a capture of the page (`docs/images/app.png`). The pipeline renders it; do not hand-draw it.
- Local run state goes under `.local/`, which is gitignored. Never commit `.env` or anything under `.local/`.

## Language and SDK notes

### Python

- Dependencies are managed with `uv` and pinned in `pyproject.toml`; commit `uv.lock` after `make setup`. `make test` runs `pytest` offline.
- The demo entry is `app/demo.py` (run as `python -m app.demo`); keep pure logic in modules the tests import.
- Never shell out to the `b2` or `aws` command-line tools.

#### Variant: boto3 over the S3-compatible API

- B2 calls go through `boto3` against `https://s3.<B2_REGION>.backblazeb2.com`. Do not add `b2sdk`, `aioboto3`, `s3fs`, `minio` or `obstore`.
- Every boto3 S3 client sets the user agent where it is constructed:

  ```python
  USER_AGENT = "b2ai-far-region-bulk-ingest"
  s3 = boto3.client("s3", endpoint_url=..., config=Config(user_agent_extra=USER_AGENT))
  ```

- Use only S3 API calls; never call the native `b2_*` API. Never create, delete or reconfigure buckets (`create_bucket`, `delete_bucket`, `put_bucket_*`) or batch-delete (`delete_objects`) unless explicitly documented in `README.md`.

# AGENTS.md

Instructions for AI coding agents and humans working on `b2-far-region-bulk-ingest`. `CLAUDE.md` only imports this file.

## What this repository is

A Python sample (cli) for Backblaze B2 Cloud Storage. It talks to B2 through `boto3`, and its facts live in `sample.json` (`language`, `variant`, `interface`, `required_sdk`, `user_agent_extra`, `main_flow`, `extra_clients`). Keep `sample.json` in agreement with the card the sample was built from.

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

## B2 rules

1. Read configuration only through the five standard names: `B2_APPLICATION_KEY_ID`, `B2_APPLICATION_KEY`, `B2_BUCKET_NAME`, `B2_REGION`, `B2_PUBLIC_URL_BASE`. Never use other names or aliases for them.
2. Every B2 client sets the custom user agent `b2ai-far-region-bulk-ingest` at the place it is constructed (the snippet for this language is below). Use a string literal or one constant bound once to that literal; never build it at runtime or pass options through a spread.
3. Use only the SDK named in `sample.json`. Any other storage client (another SDK, a raw HTTP call to B2, a CLI subprocess) is allowed only when the card names it under `extra:`, and then it also sets the user agent.
4. Every object the demo writes lives under `sample-lite/b2-far-region-bulk-ingest/<nonce>/`. Any listing is scoped to that prefix, and cleanup deletes only the exact keys the demo wrote. Never create, update or delete buckets or keys, and never call another management operation the card does not name.
5. The demo performs the card's operations (`main_flow` in `sample.json`). The round trip under the prefix above is always allowed in addition.
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

- Use only S3 API calls; never call the native `b2_*` API. Never create, delete or reconfigure buckets (`create_bucket`, `delete_bucket`, `put_bucket_*`) or batch-delete (`delete_objects`) unless the card names it.

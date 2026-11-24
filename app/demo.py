"""`make demo`: the main flow against a real bucket. Prints one B2_ROUNDTRIP line."""

from __future__ import annotations

import os
import sys

import boto3
from botocore.config import Config

from app.roundtrip import object_key, redact, require_env, resolve_nonce, roundtrip_line

REPO_NAME = "b2-far-region-bulk-ingest"
USER_AGENT = "b2ai-far-region-bulk-ingest"
SECRET_NAMES = ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET_NAME")


def run() -> None:
    env = require_env(os.environ, SECRET_NAMES + ("B2_REGION",))
    key = object_key(REPO_NAME, resolve_nonce(os.environ.get("LITE_NONCE")), "roundtrip.txt")
    payload = f"hello from {REPO_NAME}\n".encode()
    region = env["B2_REGION"]
    bucket = env["B2_BUCKET_NAME"]

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://s3.{region}.backblazeb2.com",
        region_name=region,
        aws_access_key_id=env["B2_APPLICATION_KEY_ID"],
        aws_secret_access_key=env["B2_APPLICATION_KEY"],
        config=Config(user_agent_extra=USER_AGENT),
    )
    put = s3.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="text/plain")
    try:
        read_back = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        if read_back != payload:
            raise RuntimeError("downloaded bytes differ from uploaded bytes")
        print(roundtrip_line(key, read_back))
    finally:
        # Exact-key cleanup: only the version this run wrote.
        version = put.get("VersionId")
        if version:
            s3.delete_object(Bucket=bucket, Key=key, VersionId=version)
        else:
            s3.delete_object(Bucket=bucket, Key=key)


def main() -> int:
    try:
        run()
    except Exception as exc:  # noqa: BLE001 - one redacted line for any failure
        message = redact(f"{type(exc).__name__}: {exc}", [os.environ.get(n) for n in SECRET_NAMES])
        print(f"B2_ROUNDTRIP fail {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

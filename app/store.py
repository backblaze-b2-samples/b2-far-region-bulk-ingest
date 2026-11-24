"""B2 settings and the boto3 S3 client (S3-compatible API, custom user agent)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

USER_AGENT = "b2ai-far-region-bulk-ingest"
SECRET_NAMES = ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET_NAME")


@dataclass(frozen=True)
class Settings:
    key_id: str
    key: str
    bucket: str
    region: str
    endpoint_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ, endpoint_url: str | None = None) -> "Settings":
        names = SECRET_NAMES + ("B2_REGION",)
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise RuntimeError("missing environment variable(s): " + ", ".join(missing))
        region = env["B2_REGION"]
        return cls(
            key_id=env["B2_APPLICATION_KEY_ID"],
            key=env["B2_APPLICATION_KEY"],
            bucket=env["B2_BUCKET_NAME"],
            region=region,
            endpoint_url=endpoint_url or f"https://s3.{region}.backblazeb2.com",
        )

    def secrets(self) -> list[str]:
        """Values that must never appear in output."""
        return [self.key_id, self.key, self.bucket]


def make_client(settings: Settings):
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        region_name=settings.region,
        aws_access_key_id=settings.key_id,
        aws_secret_access_key=settings.key,
        config=Config(user_agent_extra=USER_AGENT, retries={"max_attempts": 5, "mode": "standard"}),
    )


def is_missing(err: ClientError) -> bool:
    code = err.response.get("Error", {}).get("Code", "")
    return code in ("404", "NoSuchKey", "NotFound")


class Store:
    """The few S3 operations the wrapper needs, all scoped by the caller's keys."""

    def __init__(self, client, bucket: str):
        self.s3 = client
        self.bucket = bucket

    def get_json(self, key: str):
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as err:
            if is_missing(err):
                return None
            raise
        return json.loads(body)

    def put_json(self, key: str, data) -> None:
        body = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json")

    def head(self, key: str):
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as err:
            if is_missing(err):
                return None
            raise

    def list_keys(self, prefix: str) -> dict[str, int]:
        """Every key under prefix with its size (paginated list_objects_v2)."""
        out: dict[str, int] = {}
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            page = self.s3.list_objects_v2(**kw)
            for obj in page.get("Contents", []):
                out[obj["Key"]] = obj["Size"]
            if not page.get("IsTruncated"):
                return out
            token = page["NextContinuationToken"]

    def get_bytes(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

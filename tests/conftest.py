"""Offline fixtures: a local S3 emulator (moto server) behind the link shaper.

No B2 credentials are used: the emulator accepts the placeholder values below.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from moto.server import ThreadedMotoServer

from app.linkshaper import LinkShaper
from app.store import USER_AGENT

ROOT = Path(__file__).resolve().parent.parent
BUCKET = "emulator-bucket"
FAKE_ENV = {
    "B2_APPLICATION_KEY_ID": "emulator-key-id",
    "B2_APPLICATION_KEY": "emulator-application-key",
    "B2_BUCKET_NAME": BUCKET,
    "B2_REGION": "us-east-1",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session", autouse=True)
def _no_b2_variables():
    leaked = [k for k in os.environ if k.startswith("B2_")]
    assert not leaked, f"make test must run with no B2_* variables, found: {leaked}"


@pytest.fixture(scope="session")
def emulator():
    port = _free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield ("127.0.0.1", port)
    server.stop()


@pytest.fixture()
def bucket(emulator):
    """A fresh, empty emulator bucket for each test."""
    s3 = boto3.client("s3", endpoint_url=f"http://{emulator[0]}:{emulator[1]}", region_name="us-east-1",
                      aws_access_key_id=FAKE_ENV["B2_APPLICATION_KEY_ID"],
                      aws_secret_access_key=FAKE_ENV["B2_APPLICATION_KEY"],
                      config=Config(user_agent_extra=USER_AGENT))
    for b in s3.list_buckets().get("Buckets", []):
        for obj in s3.list_objects_v2(Bucket=b["Name"]).get("Contents", []):
            s3.delete_object(Bucket=b["Name"], Key=obj["Key"])
        s3.delete_bucket(Bucket=b["Name"])
    s3.create_bucket(Bucket=BUCKET)
    return s3


@pytest.fixture()
def shaped(emulator):
    """Factory: a forward-mode link shaper in front of the emulator."""
    shapers = []

    def make(**kw):
        sh = LinkShaper("forward", upstream=emulator, record_user_agents=True, **kw)
        sh.start()
        shapers.append(sh)
        return sh

    yield make
    for sh in shapers:
        sh.stop()


def run_cli(*args: str, endpoint: str, timeout: int = 300) -> subprocess.CompletedProcess:
    env = dict(os.environ, **FAKE_ENV)
    return subprocess.run([sys.executable, "-m", "app.cli", *args, "--endpoint-url", endpoint],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)


def start_cli(*args: str, endpoint: str) -> subprocess.Popen:
    env = dict(os.environ, **FAKE_ENV)
    return subprocess.Popen([sys.executable, "-m", "app.cli", *args, "--endpoint-url", endpoint],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for(predicate, timeout: float = 60, every: float = 0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(every)
    raise AssertionError("condition not reached in time")

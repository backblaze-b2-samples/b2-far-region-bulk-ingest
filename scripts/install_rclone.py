"""Installs the pinned rclone into .local/bin/rclone (sample-local, nothing global).

The archive comes from the official rclone GitHub release and must match the SHA-256
published in that release's SHA256SUMS before anything is extracted.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = "v1.75.2"
SHA256 = {
    "osx-arm64": "610ab7d48615135dfc60b763858b11a426a0d3d1160c1bb76fb0ae6011b02edd",
    "osx-amd64": "574e1f31cf1c464e5fb0325cb66fcbdeaf1efa1802bd9b17f13c256fd74b6561",
    "linux-arm64": "7e1e8d69654941b7b7df84ee74c5f7fb09cce7d5496947fb7bf16b55ff427d10",
    "linux-amd64": "349ac8fba6ff65d6247043f1750cdcb518ec5d500ef91463a10d37c0ccdf3702",
}
ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / ".local" / "bin" / "rclone"


def platform_id() -> str:
    system = {"Darwin": "osx", "Linux": "linux"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}.get(platform.machine())
    if not system or not arch:
        sys.exit(f"unsupported platform {platform.system()} {platform.machine()}: install rclone {VERSION} "
                 "into .local/bin/rclone yourself")
    return f"{system}-{arch}"


def installed() -> bool:
    if not TARGET.is_file():
        return False
    out = subprocess.run([str(TARGET), "version"], capture_output=True, text=True).stdout
    return out.startswith(f"rclone {VERSION}\n")


def main() -> int:
    if installed():
        print(f"rclone {VERSION} already at .local/bin/rclone")
        return 0
    pid = platform_id()
    name = f"rclone-{VERSION}-{pid}"
    url = f"https://github.com/rclone/rclone/releases/download/{VERSION}/{name}.zip"
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https URL
        data = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256[pid]:
        sys.exit(f"checksum mismatch for {name}.zip: refusing to install")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        binary = zf.read(f"{name}/rclone")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    tmp = TARGET.with_suffix(".tmp")
    tmp.write_bytes(binary)
    os.chmod(tmp, 0o755)
    os.replace(tmp, TARGET)
    print(f"installed rclone {VERSION} ({pid}, sha256 {digest[:12]}...) at .local/bin/rclone")
    return 0 if installed() else 1


if __name__ == "__main__":
    sys.exit(main())

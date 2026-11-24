"""Generated capture batches (no third-party data): small single-part files and one file
large enough to upload as multipart."""

from __future__ import annotations

import random
from pathlib import Path

MiB = 1024 * 1024


def make_batch(root: Path, *, seed: int = 1979, multipart_bytes: int = 12 * MiB) -> Path:
    """Writes a deterministic batch: telemetry CSV, a video-like clip and a lidar sweep."""
    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    (root / "telemetry").mkdir(exist_ok=True)
    (root / "camera").mkdir(exist_ok=True)
    (root / "lidar").mkdir(exist_ok=True)
    rows = ["t_ms,ax,ay,az,gx,gy,gz"] + [
        ",".join([str(i * 10)] + [f"{rng.uniform(-9.81, 9.81):.4f}" for _ in range(6)]) for i in range(2000)]
    (root / "telemetry" / "imu-0000.csv").write_text("\n".join(rows) + "\n")
    (root / "camera" / "clip-0000.bin").write_bytes(rng.randbytes(768 * 1024))
    (root / "lidar" / "sweep-0000.bin").write_bytes(rng.randbytes(multipart_bytes))
    return root

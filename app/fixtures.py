"""Generated capture batches (no third-party data): small files and multipart payloads."""

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


_CSV_HEADER = b"t_ms,ax,ay,az,gx,gy,gz\n"


def scaled_files(small_files: int, multipart_sizes: tuple[int, ...]) -> list[tuple[str, int]]:
    """Names and exact sizes, without scanning or hashing generated sources."""
    return [(f"telemetry/imu-{i:04d}.csv", len(_CSV_HEADER) + (64 + i * 137 % 1900) * 51)
            for i in range(small_files)] + [
                (f"lidar/sweep-{i:04d}.bin", size) for i, size in enumerate(multipart_sizes)]


def make_scaled_batch(root: Path, small_files: int, multipart_sizes: tuple[int, ...], seed: int) -> Path:
    """Deterministic CSV captures and multipart payloads, written in bounded chunks."""
    rng = random.Random(seed)
    for name, size in scaled_files(small_files, multipart_sizes):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as out:
            if name.endswith(".csv"):
                out.write(_CSV_HEADER)
                for row in range((size - len(_CSV_HEADER)) // 51):
                    values = [f"{row * 10:08d}"] + [f"{rng.randint(-9999, 9999):+06d}" for _ in range(6)]
                    out.write((",".join(values) + "\n").encode("ascii"))
            else:
                remaining = size
                while remaining:
                    chunk = min(remaining, MiB)
                    out.write(rng.randbytes(chunk))
                    remaining -= chunk
    return root

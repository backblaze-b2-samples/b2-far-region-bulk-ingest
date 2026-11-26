"""Pure demo plans and random interruption choices. No storage or network access."""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass

from app.fixtures import MiB, scaled_files

DATA_SEED = 1979


@dataclass(frozen=True)
class BatchPlan:
    site: str
    batch: str
    small_files: int
    multipart_sizes: tuple[int, ...]

    @property
    def files(self) -> int:
        return self.small_files + len(self.multipart_sizes)

    @property
    def bytes(self) -> int:
        return sum(size for _, size in scaled_files(self.small_files, self.multipart_sizes))


@dataclass(frozen=True)
class Scale:
    name: str
    batches: tuple[BatchPlan, ...]
    shaping: dict[str, int]
    interrupt_batches: int


DEFAULT = Scale("default", (
    BatchPlan("site-a", "batch-0001", 32, (6 * MiB,) * 2),
    BatchPlan("site-a", "batch-0002", 48, (6 * MiB,) * 2),
    BatchPlan("site-b", "batch-0001", 40, (6 * MiB,) * 2),
    BatchPlan("site-c", "batch-0001", 56, (6 * MiB,) * 3),
), {"latency_ms": 120, "bandwidth_bps": int(1.5 * MiB), "drop_after_bytes": 4 * MiB, "max_drops": 2}, 4)

LARGE = Scale("large", tuple(
    BatchPlan(site, batch, 520, (100 * MiB,) * 4)
    for site in ("site-a", "site-b", "site-c") for batch in ("batch-0001", "batch-0002")
), {"latency_ms": 10, "bandwidth_bps": 64 * MiB, "drop_after_bytes": 4 * MiB, "max_drops": 4}, 2)


def resolve_scale(value: str | None) -> Scale:
    if not value or value == "default":
        return DEFAULT
    if value == "large":
        return LARGE
    raise ValueError(f'SCALE must be "default" or "large" (got "{value}")')


def new_rng() -> random.Random:
    seed = secrets.randbits(64)
    rng = random.Random(seed)
    rng.run_seed = seed
    return rng


def choose_cut(rng: random.Random, batch_bytes: int) -> int:
    """Uniform integer cut in [20%, 70%], clamped inside tiny transfers.

    A one-byte transfer has no strictly interior integer cut and is rejected.
    """
    if batch_bytes < 2:
        raise ValueError("batch payload must contain at least 2 bytes")
    low = max(1, (batch_bytes + 4) // 5)
    high = max(low, min(batch_bytes - 1, batch_bytes * 7 // 10))
    return rng.randint(low, high)

"""Pure scale profiles, interruption choices and generated fixtures."""

import random

import pytest

from app import scenario
from app.fixtures import MiB, make_scaled_batch, scaled_files


def test_profiles():
    default = scenario.resolve_scale("default")
    large = scenario.resolve_scale("large")
    assert all(scenario.resolve_scale(v) == default for v in (None, "", "default"))
    assert [(p.site, p.batch) for p in default.batches] == [
        ("site-a", "batch-0001"), ("site-a", "batch-0002"),
        ("site-b", "batch-0001"), ("site-c", "batch-0001")]
    assert default.interrupt_batches == len(default.batches)
    for p in default.batches:
        assert 30 <= p.small_files <= 60
        assert 2 <= len(p.multipart_sizes) <= 3
        assert all(n > 5 * MiB for n in p.multipart_sizes)
    assert sum(p.files for p in large.batches) >= 3000
    assert 2e9 <= sum(p.bytes for p in large.batches) <= 3.2e9
    assert large.shaping["bandwidth_bps"] > default.shaping["bandwidth_bps"]
    assert large.shaping["latency_ms"] < default.shaping["latency_ms"]
    assert large.interrupt_batches == 2
    assert default.shaping["max_drops"] == 2 and large.shaping["max_drops"] == 4
    assert all(s.shaping["drop_after_bytes"] == 4 * MiB for s in (default, large))
    with pytest.raises(ValueError) as exc:
        scenario.resolve_scale("bogus")
    assert str(exc.value) == 'SCALE must be "default" or "large" (got "bogus")'


def test_cut_bounds_and_randomness(monkeypatch):
    for total in (2, 3, 10, 101, 50 * MiB):
        for seed in range(100):
            cut = scenario.choose_cut(random.Random(seed), total)
            assert 1 <= cut < total
            if total >= 10:
                assert .2 * total <= cut <= .7 * total
    assert scenario.choose_cut(random.Random(1), 10**6) != scenario.choose_cut(random.Random(2), 10**6)
    with pytest.raises(ValueError, match="at least 2"):
        scenario.choose_cut(random.Random(0), 1)
    seeds = iter((123, 456))
    monkeypatch.setattr(scenario.secrets, "randbits", lambda bits: next(seeds) if bits == 64 else None)
    a, b = scenario.new_rng(), scenario.new_rng()
    assert (a.run_seed, b.run_seed) == (123, 456)
    assert a.random() == random.Random(123).random()
    assert b.random() == random.Random(456).random()


def test_scaled_fixture_counts_sizes_and_seed(tmp_path):
    a = make_scaled_batch(tmp_path / "a", 3, (6 * MiB, 6 * MiB + 7), 123)
    b = make_scaled_batch(tmp_path / "b", 3, (6 * MiB, 6 * MiB + 7), 123)
    c = make_scaled_batch(tmp_path / "c", 3, (17,), 456)
    planned = dict(scaled_files(3, (6 * MiB, 6 * MiB + 7)))
    assert {p.relative_to(a).as_posix(): p.stat().st_size for p in a.rglob("*") if p.is_file()} == planned
    assert len(planned) == 5
    for name in planned:
        assert (a / name).read_bytes() == (b / name).read_bytes()
    assert (a / "telemetry/imu-0000.csv").read_text().startswith("t_ms,ax,ay,az,gx,gy,gz\n")
    assert (a / "telemetry/imu-0000.csv").read_bytes() != (c / "telemetry/imu-0000.csv").read_bytes()

"""Demo report formatting without running the B2 demo."""

import pytest

from app.demo import print_report


@pytest.mark.parametrize("failed,low_level,drops", [(0, 0, 2), (1, 3, 0)])
def test_report_distinguishes_logged_retries_from_link_recoveries(capsys, failed, low_level, drops):
    report = {
        "site": "site-a", "batch": "batch-0001", "status": "verified", "complete": True,
        "attempts": [{"result": "interrupted"}, {"result": "copied"}],
        "rclone_check": {"ok": True, "matched": 3, "unchecked_hashes": 0},
        "manifest_comparison": {"ok": True, "missing": [], "extra": []},
        "files": [], "marker_key": "ingest/reports/site-a/batch-0001.verified",
    }
    evidence = {
        "shaper": {"drops": drops},
        "rerun": {"retries": {
            "reruns": 1, "rclone_attempts_failed": failed, "rclone_low_level_retries": low_level,
            "link_shaper_drops_injected": drops, "link_shaper_drops_recovered": drops,
        }},
    }
    print_report(report, evidence)
    output = capsys.readouterr().out
    assert f"rclone-logged retries  {failed} failed attempt(s), {low_level} low-level retries" in output
    assert f"{drops} connection drop(s) injected, {drops} recovered" in output

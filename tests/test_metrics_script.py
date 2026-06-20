"""loop-metrics.sh event-derived frontier metrics."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "loop-metrics.sh"
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@pytest.fixture
def metrics_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".loop" / "messages" / "processed").mkdir(parents=True)
    (root / ".loop" / "sessions" / "demo" / "engine").mkdir(parents=True)
    (root / "ops-wiki").mkdir()
    (root / "ops-wiki" / "checkpoint.md").write_text(
        "# checkpoint\n<!-- coord-decisions -->\n", encoding="utf-8"
    )
    (root / "ops-wiki" / "index.md").write_text("# index\n", encoding="utf-8")
    return root


def _ts(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).strftime(TS_FORMAT)


def _day(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%d")


def _mailbox_stamp(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y%m%d-%H%M%S")


def _write_events(project: Path, events: list[dict]) -> None:
    path = project / ".loop" / "sessions" / "demo" / "engine" / "events.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for seq, event in enumerate(events, start=1):
            record = {"ts": event.pop("ts", _ts()), "seq": seq, **event}
            fh.write(json.dumps(record) + "\n")
        fh.write("{not json}\n")


def _write_mail(project: Path, subdir: str, name: str, subject: str) -> None:
    directory = project / ".loop" / "messages" / subdir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(f"---\nsubject: {subject}\n---\n\nbody\n", encoding="utf-8")


def _run_metrics(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "--project-root", str(project), "--session", "demo", *args],
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )


def test_loop_metrics_counts_events_jsonl_and_mailbox_steers(metrics_project: Path):
    today = _day()
    old = _day(timedelta(days=-9))
    (metrics_project / "ops-wiki" / "log.md").write_text(
        f"## [{today}] ingest | mail\n"
        f"## [{today}] task | T0001 done\n"
        f"## [{today}] task | T0002 done\n"
        f"## [{old}] task | T0000 done\n",
        encoding="utf-8",
    )
    _write_events(
        metrics_project,
        [
            {"event": "decision-approved", "decided_by": "engine"},
            {"event": "decision-approved", "decided_by": "engine"},
            {"event": "decision-approved"},  # missing decided_by => human
            {"event": "decision-rejected", "reason": "no"},
            {"event": "escalate", "summary": "needs operator"},
            {"event": "action", "kind": "stop"},
            {"event": "action", "kind": "dispatch", "lane": "web"},
            {"event": "action", "kind": "dispatch", "lane": "web"},
            {"event": "action", "kind": "verify", "lane": "validate"},
            {"event": "action", "kind": "dispatch", "lane": ""},
            {"event": "action", "kind": "dispatch", "lane": "my lane"},  # space -> sanitized
            {"event": "action", "kind": "dispatch", "lane": "old", "ts": _ts(timedelta(days=-9))},
            {"event": "ingest-timeout"},
            {"event": "brain-call"},
            {"event": "escalate", "ts": _ts(timedelta(days=-9))},
        ],
    )
    restarts = metrics_project / ".loop" / "sessions" / "demo" / "lane-restarts.jsonl"
    restarts.write_text(
        json.dumps({"timestamp": _ts(), "lane": "web"})
        + "\n"
        + json.dumps({"timestamp": _ts(timedelta(days=-2)), "lane": "ops"})
        + "\n"
        + json.dumps({"timestamp": _ts(), "event": "giving-up", "lane": "web"})
        + "\n"
        + json.dumps({"timestamp": _ts(timedelta(days=-9)), "lane": "old"})
        + "\n",
        encoding="utf-8",
    )
    stamp = _mailbox_stamp()
    duplicate = f"{stamp}-andrew-to-coord.md"
    _write_mail(metrics_project, "", duplicate, "ship the next unit")
    _write_mail(metrics_project, "processed", duplicate, "ship the next unit")
    _write_mail(metrics_project, "processed", f"{stamp}-web-to-coord.md", "prove the gate")
    _write_mail(metrics_project, "processed", f"{stamp}-qa-to-coord.md", "re : d-1-0")
    _write_mail(metrics_project, "processed", f"{stamp}-coord-to-web.md", "go validate")

    result = _run_metrics(metrics_project, "--log")

    assert result.returncode == 0, result.stderr
    assert "autonomy_ratio:                  0.50 (2/4)" in result.stdout
    assert "interventions_per_shipped_unit: 2.00 (4 interventions / 2 shipped)" in result.stdout
    assert "escalations_7d:                  1" in result.stdout
    assert "rejects_7d:                      1" in result.stdout
    assert "stops_7d:                        1" in result.stdout
    assert "ingest_timeouts_7d:              1" in result.stdout
    assert "lane_restarts_7d:                2" in result.stdout
    assert "unsolicited_steers_7d:           2" in result.stdout
    assert "brain_calls_7d:                  1" in result.stdout
    assert 'dispatches_per_lane_7d:          {"my_lane":1,"validate":1,"web":2}' in result.stdout
    assert "distinct_lanes_used_7d:          3" in result.stdout
    log = (metrics_project / "ops-wiki" / "log.md").read_text(encoding="utf-8")
    assert "autonomy=0.50(2/4)" in log
    assert "interventions_per_shipped=2.00(4/2)" in log
    assert "escalations7d=1" in log
    assert 'dispatches_per_lane7d={"my_lane":1,"validate":1,"web":2}' in log
    assert "distinct_lanes_used7d=3" in log


def test_loop_metrics_missing_events_jsonl_degrades_to_zero(metrics_project: Path):
    (metrics_project / "ops-wiki" / "log.md").write_text("", encoding="utf-8")

    result = _run_metrics(metrics_project)

    assert result.returncode == 0, result.stderr
    assert "autonomy_ratio:                  n/a (0/0)" in result.stdout
    assert "interventions_per_shipped_unit: n/a (0 interventions / 0 shipped)" in result.stdout
    assert "escalations_7d:                  0" in result.stdout
    assert "rejects_7d:                      0" in result.stdout
    assert "stops_7d:                        0" in result.stdout
    assert "ingest_timeouts_7d:              0" in result.stdout
    assert "lane_restarts_7d:                0" in result.stdout
    assert "unsolicited_steers_7d:           0" in result.stdout
    assert "brain_calls_7d:                  0" in result.stdout
    assert "dispatches_per_lane_7d:          {}" in result.stdout
    assert "distinct_lanes_used_7d:          0" in result.stdout
    assert "no events.jsonl for session 'demo'" in result.stdout

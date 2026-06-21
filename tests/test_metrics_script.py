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


def _write_events(project: Path, events: list[dict], session: str = "demo") -> None:
    path = project / ".loop" / "sessions" / session / "engine" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for seq, event in enumerate(events, start=1):
            record = {
                "ts": event.get("ts", _ts()),
                "seq": seq,
                **{k: v for k, v in event.items() if k != "ts"},
            }
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


def _run_metrics_all(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "--project-root", str(project), "--all", *args],
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )


def _write_snapshot(
    project: Path, session: str, *, checkpoint_tokens: int = 0, lanes: dict[str, str] | None = None
) -> None:
    engine = project / ".loop" / "sessions" / session / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "checkpoint_tokens": checkpoint_tokens,
        "lanes": {name: {"status": status} for name, status in (lanes or {}).items()},
    }
    (engine / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")


def _write_task(project: Path, task_id: str, loop: str, status: str = "open") -> None:
    tasks = project / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / f"{task_id}-x.md").write_text(
        f"---\nid: {task_id}\ntitle: x\nstatus: {status}\nloop: {loop}\n"
        "depends_on: []\nscope: test\n---\n",
        encoding="utf-8",
    )


def test_loop_metrics_counts_events_jsonl_and_mailbox_steers(metrics_project: Path):
    today = _day()
    old = _day(timedelta(days=-9))
    stamp = _mailbox_stamp()
    duplicate = f"{stamp}-andrew-to-coord.md"
    peer = f"{stamp}-web-to-coord.md"
    reply = f"{stamp}-qa-to-coord.md"
    coord = f"{stamp}-coord-to-web.md"
    (metrics_project / "ops-wiki" / "log.md").write_text(
        f"## [{today}] ingest | mail\n"
        f"## [{today}] checkpoint | cycle\n"
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
            {"event": "ingest-done"},
            {"event": "lint-dispatch", "ok": True},
            {"event": "cycle-trigger"},
            {"event": "mailbox-new", "file": duplicate},
            {"event": "mailbox-new", "file": duplicate},  # dedupe by file
            {"event": "mailbox-new", "file": peer},
            {"event": "mailbox-new", "file": reply},
            {"event": "mailbox-new", "file": coord},
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
    _write_mail(metrics_project, "", duplicate, "ship the next unit")
    _write_mail(metrics_project, "processed", duplicate, "ship the next unit")
    _write_mail(metrics_project, "processed", peer, "prove the gate")
    _write_mail(metrics_project, "processed", reply, "re : d-1-0")
    _write_mail(metrics_project, "processed", coord, "go validate")

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
    assert "ingests_7d:        1" in result.stdout
    assert "lints_7d:          1" in result.stdout
    assert "checkpoints_7d:    1" in result.stdout
    assert "events.jsonl for session 'demo' skipped 1 corrupt line(s)" in result.stdout
    log = (metrics_project / "ops-wiki" / "log.md").read_text(encoding="utf-8")
    assert "autonomy=0.50(2/4)" in log
    assert "interventions_per_shipped=2.00(4/2)" in log
    assert "escalations7d=1" in log
    assert 'dispatches_per_lane7d={"my_lane":1,"validate":1,"web":2}' in log
    assert "distinct_lanes_used7d=3" in log
    assert "ingests7d=1" in log
    assert "lints7d=1" in log
    assert "source=session-events-v2" in log


def test_loop_metrics_computes_brain_cost_from_usage_events(metrics_project: Path):
    today = _day()
    (metrics_project / "ops-wiki" / "log.md").write_text(
        f"## [{today}] task | T0001 done\n## [{today}] task | T0002 done\n",
        encoding="utf-8",
    )
    _write_events(
        metrics_project,
        [
            {"event": "brain-call"},
            {
                "event": "brain-usage",
                "model": "claude-fable-5",
                "usage_source": "stream-json",
                "cost_source": "provider",
                "input_tokens": 50,
                "output_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 20,
                "total_tokens": 100,
                "cost_usd": 0.5,
            },
            {"event": "brain-call"},
            {
                "event": "brain-usage",
                "model": "claude-fable-5",
                "usage_source": "stream-json",
                "cost_source": "provider",
                "input_tokens": 25,
                "output_tokens": 5,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 10,
                "total_tokens": 50,
                "cost_usd": 0.25,
            },
        ],
    )

    result = _run_metrics(metrics_project, "--log")

    assert result.returncode == 0, result.stderr
    assert "brain_calls_7d:                  2" in result.stdout
    assert "brain_tokens_7d:                 150" in result.stdout
    assert "cost_usd_7d:                     0.750000" in result.stdout
    assert "cost_per_shipped_unit:           0.375000" in result.stdout
    assert "cost_per_decision:               0.375000" in result.stdout
    log = (metrics_project / "ops-wiki" / "log.md").read_text(encoding="utf-8")
    assert "brain_tokens7d=150" in log
    assert "cost_usd7d=0.750000" in log
    assert "brain_cost_per_shipped=0.375000" in log
    assert "cost_per_decision=0.375000" in log


def test_loop_metrics_unpriced_usage_does_not_undercount_cost(metrics_project: Path):
    (metrics_project / "ops-wiki" / "log.md").write_text("", encoding="utf-8")
    _write_events(
        metrics_project,
        [
            {"event": "brain-call"},
            {
                "event": "brain-usage",
                "model": "future-model",
                "usage_source": "stream-json",
                "cost_source": "unpriced",
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "cost_usd": None,
            },
        ],
    )

    result = _run_metrics(metrics_project)

    assert result.returncode == 0, result.stderr
    assert "brain_tokens_7d:                 15" in result.stdout
    assert "cost_usd_7d:                     n/a" in result.stdout
    assert "cost_per_decision:               n/a" in result.stdout
    assert "1 brain-usage event(s) unpriced; cost metrics n/a" in result.stdout


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


def test_loop_metrics_empty_events_do_not_bleed_repo_level_counts(metrics_project: Path):
    today = _day()
    stamp = _mailbox_stamp()
    (metrics_project / "ops-wiki" / "log.md").write_text(
        f"## [{today}] ingest | old shared ingest\n## [{today}] lint | old shared lint\n",
        encoding="utf-8",
    )
    (metrics_project / "ops-wiki" / "checkpoint.md").write_text("x" * 4000, encoding="utf-8")
    _write_mail(
        metrics_project,
        "processed",
        f"{stamp}-andrew-to-coord.md",
        "repo-level steer that the fresh session never observed",
    )
    _write_events(metrics_project, [])

    result = _run_metrics(metrics_project)

    assert result.returncode == 0, result.stderr
    assert "checkpoint_tokens: 0" in result.stdout
    assert "ingests_7d:        0" in result.stdout
    assert "lints_7d:          0" in result.stdout
    assert "unsolicited_steers_7d:           0" in result.stdout


def test_loop_metrics_checkpoint_falls_back_to_engine_checkpoint(metrics_project: Path):
    checkpoint = metrics_project / ".loop" / "sessions" / "demo" / "engine" / "checkpoint.md"
    checkpoint.write_text("é" * 400, encoding="utf-8")
    _write_events(metrics_project, [])

    result = _run_metrics(metrics_project)

    assert result.returncode == 0, result.stderr
    assert "checkpoint_tokens: 100 (400 chars / 4)" in result.stdout


def test_loop_metrics_checkpoint_falls_back_to_latest_brain_prompt(metrics_project: Path):
    brain = metrics_project / ".loop" / "sessions" / "demo" / "engine" / "brain"
    brain.mkdir()
    (brain / "older.prompt.md").write_text("x" * 80, encoding="utf-8")
    latest = brain / "latest.prompt.md"
    latest.write_text("x" * 800, encoding="utf-8")
    os.utime(latest, None)
    _write_events(metrics_project, [])

    result = _run_metrics(metrics_project)

    assert result.returncode == 0, result.stderr
    assert "checkpoint_tokens: 200 (800 chars / 4)" in result.stdout


def test_loop_metrics_corrupt_snapshot_is_reported(metrics_project: Path):
    snapshot = metrics_project / ".loop" / "sessions" / "demo" / "engine" / "snapshot.json"
    snapshot.write_text("{not json}", encoding="utf-8")
    _write_events(metrics_project, [])

    result = _run_metrics(metrics_project)

    assert result.returncode == 0, result.stderr
    assert "checkpoint_tokens: 0" in result.stdout
    assert "snapshot.json for session 'demo' unparseable" in result.stdout


def test_loop_metrics_invalid_snapshot_tokens_are_rejected(metrics_project: Path):
    snapshot = metrics_project / ".loop" / "sessions" / "demo" / "engine" / "snapshot.json"
    snapshot.write_text(json.dumps({"checkpoint_tokens": True}), encoding="utf-8")
    _write_events(metrics_project, [])

    result = _run_metrics(metrics_project)

    assert result.returncode == 0, result.stderr
    assert "checkpoint_tokens: 0" in result.stdout
    assert "invalid checkpoint_tokens" in result.stdout


def test_loop_metrics_cycle_trigger_does_not_change_checkpoint_count(metrics_project: Path):
    (metrics_project / "ops-wiki" / "log.md").write_text("", encoding="utf-8")
    _write_events(metrics_project, [{"event": "cycle-trigger"}])

    result = _run_metrics(metrics_project)

    assert result.returncode == 0, result.stderr
    assert "checkpoints_7d:    0" in result.stdout


def test_loop_metrics_missing_mailbox_file_does_not_count_as_steer(metrics_project: Path):
    missing = f"{_mailbox_stamp()}-andrew-to-coord.md"
    _write_events(
        metrics_project,
        [{"event": "mailbox-new", "file": missing}, {"event": "mailbox-new", "file": missing}],
    )

    result = _run_metrics(metrics_project)

    assert result.returncode == 0, result.stderr
    assert "unsolicited_steers_7d:           0" in result.stdout
    assert f"mailbox-new file '{missing}' not found" in result.stdout
    assert result.stdout.count(f"mailbox-new file '{missing}' not found") == 1


def test_loop_metrics_lint_dispatch_requires_boolean_ok(metrics_project: Path):
    _write_events(
        metrics_project,
        [
            {"event": "lint-dispatch", "ok": True},
            {"event": "lint-dispatch"},
            {"event": "lint-dispatch", "ok": False},
            {"event": "lint-dispatch", "ok": 0},
            {"event": "lint-dispatch", "ok": "false"},
        ],
    )

    result = _run_metrics(metrics_project)

    assert result.returncode == 0, result.stderr
    assert "lints_7d:          2" in result.stdout
    assert "lint-dispatch with non-boolean ok skipped" in result.stdout


def test_loop_metrics_all_prints_session_rows_and_fleet_aggregate(tmp_path: Path):
    metrics_project = tmp_path / "proj"
    (metrics_project / ".loop" / "messages" / "processed").mkdir(parents=True)
    (metrics_project / "ops-wiki").mkdir()
    (metrics_project / "ops-wiki" / "log.md").write_text("", encoding="utf-8")
    (metrics_project / ".loop" / "sessions" / "alpha" / "engine").mkdir(parents=True)
    (metrics_project / ".loop" / "sessions" / "beta" / "engine").mkdir(parents=True)
    _write_snapshot(metrics_project, "alpha", checkpoint_tokens=20, lanes={"web": "idle"})
    _write_snapshot(metrics_project, "beta", checkpoint_tokens=40, lanes={"ops": "idle"})
    _write_task(metrics_project, "T9001", "alpha")
    _write_task(metrics_project, "T9002", "beta")
    _write_events(
        metrics_project,
        [
            {"event": "decision-approved", "decided_by": "engine"},
            {"event": "brain-call"},
            {"event": "ingest-done"},
            {"event": "lint-dispatch", "ok": True},
            {"event": "cycle-trigger"},
        ],
        session="alpha",
    )
    _write_events(
        metrics_project,
        [
            {"event": "decision-approved"},
            {"event": "decision-rejected"},
            {"event": "escalate"},
            {"event": "brain-call"},
        ],
        session="beta",
    )

    result = _run_metrics_all(metrics_project)

    assert result.returncode == 0, result.stderr
    assert "SESSION" in result.stdout
    assert "alpha" in result.stdout
    assert "beta" in result.stdout
    assert "fleet aggregate:" in result.stdout
    assert "sessions:                    2" in result.stdout
    assert "checkpoint_tokens:           60" in result.stdout
    assert "autonomy_ratio:              0.33 (1/3)" in result.stdout
    assert "brain_calls_7d:              2" in result.stdout
    assert "ingests_7d:                  1" in result.stdout
    assert "lints_7d:                    1" in result.stdout
    assert "lanes_idle_with_backlog:     2" in result.stdout


def test_loop_metrics_all_surfaces_degradation_notes(metrics_project: Path):
    result = _run_metrics_all(metrics_project)

    assert result.returncode == 0, result.stderr
    assert "notes:" in result.stdout
    assert "no events.jsonl for session 'demo'" in result.stdout


def test_loop_metrics_all_log_is_rejected(metrics_project: Path):
    log_file = metrics_project / "ops-wiki" / "log.md"
    log_file.write_text("before\n", encoding="utf-8")
    before = log_file.read_text(encoding="utf-8") if log_file.exists() else ""

    result = _run_metrics_all(metrics_project, "--log")

    assert result.returncode == 2
    assert "--all and --log cannot be combined" in result.stderr
    assert log_file.read_text(encoding="utf-8") == before


def test_loop_metrics_all_session_is_rejected(metrics_project: Path):
    result = _run_metrics_all(metrics_project, "--session", "demo")

    assert result.returncode == 2
    assert "--all and --session cannot be combined" in result.stderr

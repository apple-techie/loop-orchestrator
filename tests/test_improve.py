"""Self-harness improve loop: mining, proposal parsing, and human-gated apply.

The checkpoint-header apply test mutates the real packaged header (that IS the
behavior under test — the engine reads it via importlib.resources) and
restores it via a finalizer.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

import pytest
import yaml

from loop_orchestrator.engine import cli, improve
from loop_orchestrator.engine.config import EngineConfig
from loop_orchestrator.engine.events import utc_now
from loop_orchestrator.engine.loop import run_once
from loop_orchestrator.engine.wiki import MARKER
from loop_orchestrator.paths import SessionPaths

FAKES_BIN = Path(__file__).resolve().parent / "fakes" / "bin"
COMPILED = "# Checkpoint\n\ncompiled state, docs-owned\n\n" + MARKER + "\n"

AGENTS_STUB = """# AGENTS.md

### Ingest protocol
Move each processed file to processed/ and append to log.md.

### Experiment protocol
Every change is an experiment.
"""

PROPOSALS_REPLY = """Looked at the evidence; two safe edits.

```proposals
version: 1
proposals:
  - surface: agents-md-append
    title: nudge docs lane harder
    signature: ingest:timeout
    rationale: headless ingests keep timing out
    edit: |
      #### experiment: nudge docs lane harder

      Raise ingest.timeout_s before nudging again.
    expected_effect: ingest-timeout count drops
  - surface: engine-config
    title: raise ingest timeout
    signature: ingest:timeout
    rationale: 600s is not enough for big mailboxes
    edit: "engine:\\n  ingest:\\n    timeout_s: 1200"
    expected_effect: fewer ingest-timeout events
```
"""


@pytest.fixture
def project(tmp_path: Path, fakes_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".loop" / "messages" / "processed").mkdir(parents=True)
    (root / "ops-wiki").mkdir()
    (root / "ops-wiki" / "checkpoint.md").write_text(COMPILED, encoding="utf-8")
    (root / "AGENTS.md").write_text(AGENTS_STUB, encoding="utf-8")
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(FAKES_BIN / "fake-brain"))
    return root


def _paths(project: Path) -> SessionPaths:
    paths = SessionPaths(project, "demo")
    paths.ensure()
    return paths


def _events(project: Path) -> list[dict]:
    path = SessionPaths(project, "demo").events_path
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _write_events(paths: SessionPaths, events: list[dict]) -> None:
    with open(paths.events_path, "w", encoding="utf-8") as fh:
        for seq, event in enumerate(events, start=1):
            record = {"ts": event.pop("ts", utc_now()), "seq": seq, **event}
            fh.write(json.dumps(record) + "\n")


def _seed_proposal(
    paths: SessionPaths,
    n: int = 1,
    surface: str = "agents-md-append",
    title: str = "an experiment",
    edit: str = "#### experiment: an experiment\n\nDetails.\n",
    status: str = "proposed",
    stamp: str = "20260610-120000",
) -> Path:
    paths.proposals_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "surface": surface,
        "title": title,
        "status": status,
        "signature": "sig",
        "rationale": "r",
        "expected_effect": "e",
    }
    path = paths.proposals_dir / f"{stamp}-{n}.md"
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n" + edit, "utf-8")
    return path


def _header_path() -> Path:
    resource = resources.files("loop_orchestrator.engine").joinpath(
        "contracts", "checkpoint-header.md"
    )
    with resources.as_file(resource) as header:
        return Path(header)


# ── weakness mining (pure) ──────────────────────────────────────────────────


def test_mine_clusters_from_seeded_state(project):
    paths = _paths(project)
    old = "2026-01-01T00:00:00Z"
    _write_events(
        paths,
        [
            {"event": "brain-failed", "error": "ancient, outside window", "ts": old},
            {"event": "brain-call", "response_path": "/t/b/r1.response.md"},
            {"event": "brain-retry", "attempt": 1, "error": "exit 1: harness hiccup"},
            {"event": "brain-failed", "error": "exit 1: harness died"},
            {"event": "decision-parse-error", "id": "d-1", "error": "no decision fence"},
            {"event": "action-failed", "lane": "web", "kind": "dispatch", "error": "pane gone"},
            {"event": "action-failed", "lane": "web", "kind": "dispatch", "error": "pane gone"},
            {"event": "ingest-timeout", "attempt": 1, "timeout_s": 600},
            {"event": "reply-timeout", "ask": "d-2-0", "lane": "web"},
        ],
    )
    recent = utc_now()
    (paths.decisions_dir / "d-rej.json").write_text(
        json.dumps(
            {
                "id": "d-rej",
                "status": "rejected",
                "decided_at": recent,
                "reason": "not now",
                "actions": [{"kind": "dispatch"}, {"kind": "steer"}],
            }
        ),
        encoding="utf-8",
    )
    (paths.decisions_dir / "d-ok.json").write_text(
        json.dumps({"id": "d-ok", "status": "approved", "decided_at": recent, "actions": []}),
        encoding="utf-8",
    )
    paths.lane_restarts.write_text(
        json.dumps({"timestamp": recent, "lane": "web", "target": "demo:web.1", "cmd": "claude"})
        + "\n"
        + json.dumps({"timestamp": recent, "lane": "web", "event": "giving-up"})
        + "\n"
        + json.dumps({"timestamp": old, "lane": "web"})  # outside the window
        + "\n",
        encoding="utf-8",
    )
    today = recent[:10]
    (project / "ops-wiki" / "log.md").write_text(
        f"## [{today}] metrics | tokens=900 pending=0 restarts24h=0\n\n"
        f"## [{today}] metrics | tokens=950 pending=4 restarts24h=0\n",
        encoding="utf-8",
    )

    evidence = improve.mine(paths)

    by_sig = {c["signature"]: c for c in evidence["clusters"]}
    assert by_sig["brain:brain-failed"]["count"] == 1  # the old one is excluded
    assert "r1.response.md" in by_sig["brain:brain-failed"]["samples"][0]
    assert by_sig["brain:brain-retry"]["count"] == 1
    assert by_sig["brain:decision-parse-error"]["inferred_surface"] == "checkpoint-header"
    assert by_sig["decisions:rejected"]["count"] == 1
    assert "not now" in by_sig["decisions:rejected"]["samples"][0]
    assert "dispatch,steer" in by_sig["decisions:rejected"]["samples"][0]
    failed = by_sig["action-failed:web:dispatch"]
    assert failed["count"] == 2 and failed["inferred_surface"] == "engine-config"
    lane = by_sig["lane-instability:web"]
    assert (lane["count"], lane["restarts"], lane["giveups"]) == (2, 1, 1)
    assert by_sig["ingest:timeout"]["count"] == 1
    trend = by_sig["ingest:pending-trend"]
    assert trend["count"] == 4 and trend["inferred_surface"] == "agents-md-append"
    assert by_sig["asks:reply-timeout"]["count"] == 1
    assert all(len(c["samples"]) <= 3 for c in evidence["clusters"])


def test_mine_empty_state(project):
    evidence = improve.mine(_paths(project))
    assert evidence["clusters"] == [] and evidence["window_days"] == 7


# ── SIGNAL 1: human-intervention mining ─────────────────────────────────────


def _write_msg(directory: Path, name: str, subject: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(f"---\nsubject: {subject}\n---\n\nbody\n", encoding="utf-8")


def test_human_intervention_mines_unsolicited_steers_only(project):
    paths = _paths(project)
    from loop_orchestrator.engine.events import utc_now as _now

    ts = _now()[:10].replace("-", "")  # today's YYYYMMDD, inside the window
    processed = paths.processed_dir
    # 3 unsolicited human steers (andrew -> coord, not "re:")
    _write_msg(processed, f"{ts}-090000-andrew-to-coord.md", "prove the dispatch path")
    _write_msg(processed, f"{ts}-091500-andrew-to-coord.md", "now wire up the deck")
    _write_msg(processed, f"{ts}-093000-andrew-to-coord.md", "ship the improve loop")
    # a solicited reply (re:) to a coordinator ask — NOT an intervention
    _write_msg(processed, f"{ts}-094500-web-to-coord.md", "re:d-20260611-0")
    # a coord-AUTHORED message — the coordinator working, not steered
    _write_msg(processed, f"{ts}-095000-coord-to-web.md", "go run make check")

    clusters = {c["signature"]: c for c in improve.mine(paths)["clusters"]}

    steer = clusters["human:unsolicited-steer"]
    assert steer["count"] == 3
    assert steer["inferred_surface"] == "checkpoint-header"
    blob = " ".join(steer["samples"])
    assert "prove the dispatch path" in blob
    assert "re:d-20260611-0" not in blob  # the reply is ignored
    assert "go run make check" not in blob  # the coord-authored msg is ignored


def test_human_intervention_excludes_spaced_reply_subject(project):
    # A sloppily-typed reply ("re :" with a space) is still a solicited reply,
    # not an unsolicited steer — it must not inflate the signal.
    paths = _paths(project)
    from loop_orchestrator.engine.events import utc_now as _now

    ts = _now()[:10].replace("-", "")
    _write_msg(paths.processed_dir, f"{ts}-090000-web-to-coord.md", "re : d-123-0")
    _write_msg(paths.processed_dir, f"{ts}-091500-web-to-coord.md", "RE:  d-123-1")

    clusters = {c["signature"]: c for c in improve.mine(paths)["clusters"]}

    assert "human:unsolicited-steer" not in clusters


def test_human_intervention_dedups_messages_and_processed(project):
    paths = _paths(project)
    from loop_orchestrator.engine.events import utc_now as _now

    ts = _now()[:10].replace("-", "")
    name = f"{ts}-100000-andrew-to-coord.md"
    # SAME basename in messages/ (unprocessed) and processed/ (acked) — count 1.
    _write_msg(paths.mailbox_dir, name, "the same steer, mid-ack")
    _write_msg(paths.processed_dir, name, "the same steer, mid-ack")

    clusters = {c["signature"]: c for c in improve.mine(paths)["clusters"]}

    assert clusters["human:unsolicited-steer"]["count"] == 1


def test_human_intervention_windows_out_old_steers(project):
    paths = _paths(project)
    # 8 days ago is outside the default 7-day window.
    _write_msg(paths.processed_dir, "20200101-000000-andrew-to-coord.md", "ancient steer")

    clusters = {c["signature"]: c for c in improve.mine(paths)["clusters"]}

    assert "human:unsolicited-steer" not in clusters


# ── SIGNAL 2: latency-regression mining ─────────────────────────────────────


def _latency_events(durations_s: list[int]) -> list[dict]:
    """A brain-call/decision pair per duration, on a steadily advancing clock."""
    events: list[dict] = []
    # Anchor inside mine()'s rolling 7-day window (cutoff = now - 7d) regardless
    # of the calendar date — a fixed past date silently falls out of the window
    # as time advances. The series spans only minutes, so now-1d stays in-window.
    base = datetime.now(timezone.utc) - timedelta(days=1)
    cursor = base
    for n, dur in enumerate(durations_s):
        events.append({"event": "brain-call", "ts": cursor.strftime("%Y-%m-%dT%H:%M:%SZ")})
        cursor = cursor + timedelta(seconds=dur)
        events.append(
            {"event": "decision", "id": f"d-{n}", "ts": cursor.strftime("%Y-%m-%dT%H:%M:%SZ")}
        )
        cursor = cursor + timedelta(seconds=30)  # gap before the next call
    return events


def test_latency_regression_mined_on_upward_trend(project):
    paths = _paths(project)
    # first third ~10s, last third ~120s -> last mean >= 2x first mean
    _write_events(paths, _latency_events([10, 10, 12, 40, 80, 120, 130, 200]))

    clusters = {c["signature"]: c for c in improve.mine(paths)["clusters"]}

    reg = clusters["latency:regression"]
    assert reg["inferred_surface"] == "checkpoint-header"
    assert reg["count"] == 8
    assert reg["last_third_mean_s"] >= 2 * reg["first_third_mean_s"]


def test_latency_flat_series_no_regression(project):
    paths = _paths(project)
    _write_events(paths, _latency_events([10, 11, 10, 12, 11, 10, 11, 12]))

    clusters = {c["signature"]: c for c in improve.mine(paths)["clusters"]}

    assert "latency:regression" not in clusters


def test_latency_needs_six_samples(project):
    paths = _paths(project)
    _write_events(paths, _latency_events([10, 20, 200]))  # only 3 pairs

    clusters = {c["signature"]: c for c in improve.mine(paths)["clusters"]}

    assert "latency:regression" not in clusters


def test_latency_healed_series_not_mined(project):
    # A recovered series (an early spike, then rotation healed it) must NOT
    # mine a regression even though the old max lingers in the window — else
    # the loop keeps proposing a fix it already applied.
    paths = _paths(project)
    _write_events(paths, _latency_events([200, 180, 150, 60, 30, 15, 12, 10]))

    clusters = {c["signature"]: c for c in improve.mine(paths)["clusters"]}

    assert "latency:regression" not in clusters


# ── SIGNAL 3: crash mining (report-only, surface "none") ────────────────────


def test_crash_mined_from_events_and_deck_log(project):
    paths = _paths(project)
    _write_events(
        paths,
        [
            {"event": "crash", "component": "engine", "error": "RuntimeError: cycle blew up"},
            {"event": "crash", "component": "engine", "error": "ValueError: bad parse"},
        ],
    )
    paths.deck_crash_log.write_text(
        "2026-06-11T00:00:00Z component=deck error=DuplicateKey: '0001'\n",
        encoding="utf-8",
    )

    clusters = {c["signature"]: c for c in improve.mine(paths)["clusters"]}

    engine_crash = clusters["crash:engine"]
    assert engine_crash["count"] == 2
    assert engine_crash["inferred_surface"] == "none"  # report-only, never applied
    deck_crash = clusters["crash:deck"]
    assert deck_crash["count"] == 1
    assert "DuplicateKey" in " ".join(deck_crash["samples"])
    assert deck_crash["inferred_surface"] == "none"


# ── brain failure_kind folds into the signature (quota != timeout) ──────────


def test_brain_failure_kind_splits_quota_from_timeout(project):
    paths = _paths(project)
    _write_events(
        paths,
        [
            {
                "event": "brain-failed",
                "error": "exit 1: hit the wall",
                "failure_kind": "quota",
                "stderr_excerpt": "Claude usage limit reached; resets 9:30pm",
            },
            {
                "event": "brain-failed",
                "error": "timed out after 300s",
                "failure_kind": "timeout",
                "stderr_excerpt": "",
            },
        ],
    )

    clusters = {c["signature"]: c for c in improve.mine(paths)["clusters"]}

    # The two failures must NOT collapse into one brain:brain-failed cluster.
    assert "brain:brain-failed:quota" in clusters
    assert "brain:brain-failed:timeout" in clusters
    assert clusters["brain:brain-failed:quota"]["count"] == 1
    assert clusters["brain:brain-failed:timeout"]["count"] == 1
    assert "resets 9:30pm" in " ".join(clusters["brain:brain-failed:quota"]["samples"])


def test_none_surface_proposal_is_report_only(project, capsys):
    paths = _paths(project)
    _seed_proposal(
        paths,
        surface="none",
        title="fix the DuplicateKey deck crash",
        edit="Patch DeckTable.rebuild to dedupe row keys before add_row.\n",
    )

    rc = cli.main(["--project-root", str(project), "--session", "demo", "improve", "--apply", "1"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "REPORT-ONLY" in out and "Patch DeckTable.rebuild" in out
    meta, _ = improve._split_proposal(improve.find_proposal(paths, 1))
    assert meta["status"] == "applied-manually-required"
    # never touched an editable surface
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == AGENTS_STUB


# ── proposal parsing (last fence wins, garbage -> clean error) ──────────────


def test_parse_proposals_last_fence_wins():
    text = (
        "draft:\n```proposals\nversion: 2\nproposals: []\n```\n"
        "final:\n```proposals\nversion: 1\nproposals:\n"
        "  - {surface: engine-config, title: t, edit: e}\n```\n"
    )
    parsed = improve.parse_proposals(text)
    assert len(parsed) == 1
    assert parsed[0]["surface"] == "engine-config" and parsed[0]["title"] == "t"


def test_parse_proposals_garbage_and_invalid():
    with pytest.raises(improve.ImproveError, match="no ```proposals fence"):
        improve.parse_proposals("nothing structured here, sorry")
    with pytest.raises(improve.ImproveError, match="not valid YAML"):
        improve.parse_proposals("```proposals\n{: nope\n```")
    with pytest.raises(improve.ImproveError, match="version"):
        improve.parse_proposals("```proposals\nversion: 9\nproposals: [{}]\n```")
    with pytest.raises(improve.ImproveError, match="must be a list"):
        improve.parse_proposals("```proposals\nversion: 1\nproposals: nope\n```")
    # An empty list is the brain honestly declining to invent edits — valid.
    assert improve.parse_proposals("```proposals\nversion: 1\nproposals: []\n```") == []
    with pytest.raises(improve.ImproveError, match="surface"):
        improve.parse_proposals(
            "```proposals\nversion: 1\nproposals:\n  - {surface: nope, title: t, edit: e}\n```"
        )
    with pytest.raises(improve.ImproveError, match="'edit'"):
        improve.parse_proposals(
            "```proposals\nversion: 1\nproposals:\n  - {surface: engine-config, title: t}\n```"
        )


def test_parse_proposals_caps_at_max():
    body = "\n".join(f"  - {{surface: engine-config, title: t{i}, edit: e{i}}}" for i in range(5))
    text = f"```proposals\nversion: 1\nproposals:\n{body}\n```"
    assert len(improve.parse_proposals(text, max_proposals=2)) == 2


# ── propose: brain one-shot -> filed proposals ──────────────────────────────


def test_improve_files_proposals(project, monkeypatch, tmp_path, capsys):
    script = tmp_path / "fake-improve-brain"
    script.write_text("#!/bin/sh\ncat <<'EOF'\n" + PROPOSALS_REPLY + "EOF\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    paths = _paths(project)

    rc = cli.main(["--project-root", str(project), "--session", "demo", "improve"])

    assert rc == 0
    files = sorted(paths.proposals_dir.glob("*.md"))
    assert [f.name[-5:] for f in files] == ["-1.md", "-2.md"]
    meta, edit = improve._split_proposal(files[0])
    assert meta["surface"] == "agents-md-append" and meta["status"] == "proposed"
    assert meta["title"] == "nudge docs lane harder"
    assert edit.startswith("#### experiment: nudge docs lane harder")
    proposed = [e for e in _events(project) if e["event"] == "improve-proposed"]
    assert [p["surface"] for p in proposed] == ["agents-md-append", "engine-config"]
    out = capsys.readouterr().out
    assert "filed 2 proposal(s)" in out and "--apply N" in out
    # the improve prompt carried the evidence + the declared edit surfaces
    prompt = sorted(paths.brain_dir.glob("*.prompt.md"))[0].read_text(encoding="utf-8")
    assert "--- mined evidence (last 7 days) ---" in prompt
    assert "--- current checkpoint header (full text) ---" in prompt
    assert "- Ingest protocol" in prompt and "- Experiment protocol" in prompt
    assert "log_after_cycle" in prompt  # engine config values are included


def test_improve_garbage_reply_is_a_clean_error(project, capsys):
    # fake-brain emits a ```decision fence — no ```proposals fence at all
    rc = cli.main(["--project-root", str(project), "--session", "demo", "improve"])

    assert rc == 1
    assert "no ```proposals fence" in capsys.readouterr().err
    assert not list(SessionPaths(project, "demo").proposals_dir.glob("*.md"))


# ── apply: human-gated promotion ────────────────────────────────────────────


def test_apply_agents_md_append(project, capsys):
    paths = _paths(project)
    _seed_proposal(paths, n=1, stamp="20260609-080000", title="stale run, must not win")
    path = _seed_proposal(
        paths,
        n=1,
        title="nudge docs lane harder",
        edit="#### experiment: nudge docs lane harder\n\nRaise ingest.timeout_s.\n",
    )
    agents = project / "AGENTS.md"
    original = agents.read_text(encoding="utf-8")

    rc = cli.main(["--project-root", str(project), "--session", "demo", "improve", "--apply", "1"])

    assert rc == 0
    new = agents.read_text(encoding="utf-8")
    assert new.startswith(original)  # append-only: original is an exact prefix
    assert (
        new[len(original) :]
        == "\n#### experiment: nudge docs lane harder\n\nRaise ingest.timeout_s.\n"
    )
    log = (project / "ops-wiki" / "log.md").read_text(encoding="utf-8")
    assert "experiment | nudge docs lane harder" in log
    meta, _ = improve._split_proposal(path)
    assert meta["status"] == "applied" and meta["applied_at"]
    kinds = [e["event"] for e in _events(project)]
    assert "improve-applied" in kinds and "metrics" in kinds  # baseline recorded
    out = capsys.readouterr().out
    assert "T0006 reminder" in out and ">= 3 checkpoint cycles" in out


def test_apply_checkpoint_header_overwrites_engine_source(project, request):
    header = _header_path()
    original = header.read_text(encoding="utf-8")
    request.addfinalizer(lambda: header.write_text(original, encoding="utf-8"))
    paths = _paths(project)
    # A valid replacement preserves the decision contract (fence + every kind).
    replacement = (
        "SENTINEL HEADER vNEXT\nthe whole replacement header body\n\n"
        "```decision\nversion: 1\n```\n"
        "kinds: dispatch add_lane drop_lane steer stop escalate\n"
    )
    path = _seed_proposal(paths, surface="checkpoint-header", title="lean header", edit=replacement)

    rc = cli.main(["--project-root", str(project), "--session", "demo", "improve", "--apply", "1"])

    assert rc == 0
    assert header.read_text(encoding="utf-8") == replacement
    meta, _ = improve._split_proposal(path)
    assert meta["status"] == "applied" and meta["applied_to"] == str(header)
    # the next engine cycle assembles its prompt with the replaced header
    assert run_once(project, "demo", EngineConfig()) == 0
    prompt = sorted(paths.brain_dir.glob("*.prompt.md"))[0].read_text(encoding="utf-8")
    assert "SENTINEL HEADER vNEXT" in prompt


def test_apply_checkpoint_header_rejects_truncated_contract(project, capsys):
    # A full-replacement header that drops the decision contract would brick the
    # coordinator — the apply must reject it and leave the header untouched.
    header = _header_path()
    original = header.read_text(encoding="utf-8")
    paths = _paths(project)
    truncated = "You are the coordinator.\nReply with a decision block. The body is YAML:\n"
    _seed_proposal(paths, surface="checkpoint-header", title="lean header", edit=truncated)

    rc = cli.main(["--project-root", str(project), "--session", "demo", "improve", "--apply", "1"])

    assert rc == 1
    assert "decision contract" in capsys.readouterr().err
    assert header.read_text(encoding="utf-8") == original  # unchanged


def test_apply_engine_config_is_manual_only(project, capsys):
    paths = _paths(project)
    path = _seed_proposal(
        paths, surface="engine-config", title="raise timeout", edit="engine:\n  poll: 5\n"
    )
    agents_before = (project / "AGENTS.md").read_text(encoding="utf-8")

    rc = cli.main(["--project-root", str(project), "--session", "demo", "improve", "--apply", "1"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "NEVER auto-applied" in out and "engine:\n  poll: 5" in out
    meta, _ = improve._split_proposal(path)
    assert meta["status"] == "applied-manually-required"
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == agents_before
    assert not (project / "ops-wiki" / "log.md").exists()  # no experiment entry yet
    kinds = [e["event"] for e in _events(project)]
    assert "improve-manual-required" in kinds and "improve-applied" not in kinds


def test_apply_missing_or_resolved_proposal_errors(project, capsys):
    base = ["--project-root", str(project), "--session", "demo", "improve", "--apply", "1"]
    assert cli.main(base) == 1
    assert "no proposal 1" in capsys.readouterr().err

    paths = _paths(project)
    _seed_proposal(paths, status="applied")
    assert cli.main(base) == 1
    assert "not 'proposed'" in capsys.readouterr().err

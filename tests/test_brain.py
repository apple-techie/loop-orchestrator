"""Brain invocation: argv shapes, budget guard, retries, transcripts."""

from __future__ import annotations

from pathlib import Path

import pytest

from loop_orchestrator.engine.brain import (
    Brain,
    BrainBudgetError,
    BrainInvocationError,
    oneshot_argv,
    run_oneshot,
)
from loop_orchestrator.engine.config import BrainConfig, EngineConfig
from loop_orchestrator.engine.events import EventLog
from loop_orchestrator.paths import SessionPaths
from loop_orchestrator.substrate import Substrate


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def env(tmp_path: Path) -> tuple[SessionPaths, EventLog]:
    paths = SessionPaths(tmp_path / "proj", "demo")
    paths.ensure()
    return paths, EventLog(paths.events_path)


def test_brain_cmd_override_and_transcripts(tmp_path, env, monkeypatch):
    paths, events = env
    script = _script(tmp_path, "brain", 'printf "echo-prompt:%s" "$1"\n')
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(), None, paths, events)

    reply = brain.invoke("hello brain")

    assert reply == "echo-prompt:hello brain"
    prompts = sorted(paths.brain_dir.glob("*.prompt.md"))
    responses = sorted(paths.brain_dir.glob("*.response.md"))
    assert len(prompts) == 1 and len(responses) == 1
    assert prompts[0].read_text(encoding="utf-8") == "hello brain"
    assert responses[0].read_text(encoding="utf-8") == reply
    calls = [e for e in events.tail(10) if e["event"] == "brain-call"]
    assert len(calls) == 1
    assert calls[0]["prompt_path"] == str(prompts[0])
    assert calls[0]["response_path"] == str(responses[0])


def test_argv_from_oneshot_template(env, fakes_env, monkeypatch):
    paths, events = env
    monkeypatch.delenv("LOOP_ENGINE_BRAIN_CMD", raising=False)
    config = EngineConfig(brain=BrainConfig(extra_args=["--max-turns", "1"]))
    brain = Brain(config, Substrate(paths.project_root, "demo"), paths, events)
    assert brain._argv("the prompt") == ["claude", "-p", "the prompt", "--max-turns", "1"]


def test_budget_guard_blocks_before_spawn(tmp_path, env, monkeypatch):
    paths, events = env
    for _ in range(12):  # default max_calls_per_hour
        events.append("brain-call")
    marker = tmp_path / "spawned"
    script = _script(tmp_path, "brain", f': > "{marker}"\nprintf ok\n')
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(), None, paths, events)

    with pytest.raises(BrainBudgetError):
        brain.invoke("p")
    assert not marker.exists()


def test_retry_after_one_failure_then_success(tmp_path, env, monkeypatch):
    paths, events = env
    state = tmp_path / "failed-once"
    script = _script(
        tmp_path,
        "brain",
        'if [ ! -f "$1" ]; then : > "$1"; echo boom >&2; exit 1; fi\nprintf recovered\n',
    )
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", f"{script} {state}")
    brain = Brain(EngineConfig(), None, paths, events)

    assert brain.invoke("p") == "recovered"

    kinds = [e["event"] for e in events.tail(20)]
    assert kinds.count("brain-retry") == 1
    assert "brain-failed" not in kinds
    responses = list(paths.brain_dir.glob("*.response.md"))
    assert len(responses) == 1
    assert responses[0].read_text(encoding="utf-8") == "recovered"


def test_exhausted_retries_raise_and_log(tmp_path, env, monkeypatch):
    paths, events = env
    script = _script(tmp_path, "brain", "echo nope >&2\nexit 3\n")
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(brain=BrainConfig(max_retries=1)), None, paths, events)

    with pytest.raises(BrainInvocationError, match="exit 3"):
        brain.invoke("p")

    kinds = [e["event"] for e in events.tail(20)]
    assert kinds.count("brain-retry") == 1
    assert "brain-failed" in kinds
    assert list(paths.brain_dir.glob("*.response.md")) == []
    assert len(list(paths.brain_dir.glob("*.prompt.md"))) == 1


# ── run_oneshot reuse (the headless-ingest path shares this machinery) ──────


def test_oneshot_argv_substitution_shapes():
    assert oneshot_argv("claude -p {prompt}", "the prompt") == ["claude", "-p", "the prompt"]
    assert oneshot_argv("hermes -z", "p") == ["hermes", "-z", "p"]  # no placeholder = append


def test_run_oneshot_prefixed_events_and_transcripts(tmp_path, env):
    paths, events = env
    script = _script(tmp_path, "oneshot", 'printf "ok:%s" "$1"\n')

    out = run_oneshot(
        [str(script), "the prompt"],
        "the prompt",
        paths.engine_dir / "ingest",
        30,
        events,
        "ingest",
        cwd=paths.project_root,
        harness="claude",
    )

    assert out == "ok:the prompt"
    prompts = list((paths.engine_dir / "ingest").glob("*.prompt.md"))
    responses = list((paths.engine_dir / "ingest").glob("*.response.md"))
    assert len(prompts) == 1 and len(responses) == 1
    assert prompts[0].read_text(encoding="utf-8") == "the prompt"
    calls = [e for e in events.tail(10) if e["event"] == "ingest-call"]
    assert len(calls) == 1 and calls[0]["harness"] == "claude"
    assert "brain-call" not in [e["event"] for e in events.tail(10)]  # no budget impact


def test_run_oneshot_failure_uses_prefix(tmp_path, env):
    paths, events = env
    script = _script(tmp_path, "oneshot", "echo nope >&2\nexit 9\n")

    with pytest.raises(BrainInvocationError, match="ingest failed"):
        run_oneshot(
            [str(script)],
            "p",
            paths.engine_dir / "ingest",
            30,
            events,
            "ingest",
            cwd=paths.project_root,
        )

    kinds = [e["event"] for e in events.tail(10)]
    assert "ingest-failed" in kinds and "brain-failed" not in kinds

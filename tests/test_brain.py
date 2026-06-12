"""Brain invocation: argv shapes, budget guard, retries, transcripts,
incremental (live-tailable) writes, timeout kill, stream-json reassembly."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from loop_orchestrator.engine.brain import (
    Brain,
    BrainBudgetError,
    BrainInvocationError,
    StreamRenderer,
    oneshot_argv,
    run_oneshot,
    stream_argv,
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


# ── failure_kind classification on the failure event ────────────────────────


def test_brain_failed_records_quota_failure_kind(tmp_path, env, monkeypatch):
    paths, events = env
    # a session-limit message on stderr, then a non-zero exit -> classified quota
    script = _script(
        tmp_path, "brain", 'echo "Claude usage limit reached; resets 9:30pm" >&2\nexit 1\n'
    )
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(brain=BrainConfig(max_retries=0)), None, paths, events)

    with pytest.raises(BrainInvocationError) as excinfo:
        brain.invoke("p")

    assert excinfo.value.failure_kind == "quota"
    assert "resets 9:30pm" in excinfo.value.stderr_excerpt
    failed = [e for e in events.tail(20) if e["event"] == "brain-failed"]
    assert failed and failed[-1]["failure_kind"] == "quota"
    assert "usage limit reached" in failed[-1]["stderr_excerpt"]


def test_brain_timeout_records_timeout_failure_kind(tmp_path, env, monkeypatch):
    paths, events = env
    script = _script(tmp_path, "brain", "echo started\nexec sleep 30\n")
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(brain=BrainConfig(timeout_s=1, max_retries=0)), None, paths, events)

    with pytest.raises(BrainInvocationError) as excinfo:
        brain.invoke("p")

    assert excinfo.value.failure_kind == "timeout"
    timeout_ev = [e for e in events.tail(20) if e["event"] == "brain-timeout"]
    assert timeout_ev and timeout_ev[-1]["failure_kind"] == "timeout"
    failed = [e for e in events.tail(20) if e["event"] == "brain-failed"]
    assert failed and failed[-1]["failure_kind"] == "timeout"


def test_brain_failed_plain_exit_is_not_quota(tmp_path, env, monkeypatch):
    paths, events = env
    script = _script(tmp_path, "brain", "echo generic boom >&2\nexit 2\n")
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(brain=BrainConfig(max_retries=0)), None, paths, events)

    with pytest.raises(BrainInvocationError) as excinfo:
        brain.invoke("p")

    assert excinfo.value.failure_kind == "exit"
    failed = [e for e in events.tail(20) if e["event"] == "brain-failed"]
    assert failed and failed[-1]["failure_kind"] == "exit"


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


# ── incremental transcripts (live-tailable from t=0) ────────────────────────


def test_response_transcript_is_live_tailable_mid_run(tmp_path, env):
    paths, events = env
    script = _script(
        tmp_path,
        "slow",
        "echo line-1\nsleep 0.4\necho line-2\nsleep 0.4\necho line-3\n",
    )
    result: dict = {}

    def run() -> None:
        result["out"] = run_oneshot(
            [str(script)], "p", paths.brain_dir, 30, events, "brain", cwd=paths.project_root
        )

    worker = threading.Thread(target=run)
    worker.start()
    partial = None
    deadline = time.time() + 5
    while time.time() < deadline:
        responses = list(paths.brain_dir.glob("*.response.md"))
        if responses:
            text = responses[0].read_text(encoding="utf-8")
            if "line-1" in text and "line-3" not in text:
                partial = text  # observed BEFORE the one-shot completed
                break
        time.sleep(0.02)
    worker.join()

    assert partial is not None, "no partial transcript observed mid-run"
    assert result["out"] == "line-1\nline-2\nline-3\n"
    responses = list(paths.brain_dir.glob("*.response.md"))
    assert responses[0].read_text(encoding="utf-8") == result["out"]


def test_stderr_streams_to_sibling_transcript(tmp_path, env):
    paths, events = env
    script = _script(tmp_path, "oneshot", "echo out\necho err >&2\n")

    out = run_oneshot(
        [str(script)], "p", paths.brain_dir, 30, events, "brain", cwd=paths.project_root
    )

    assert out == "out\n"
    stderrs = list(paths.brain_dir.glob("*.stderr.txt"))
    assert len(stderrs) == 1
    assert stderrs[0].read_text(encoding="utf-8") == "err\n"


def test_timeout_kills_process_and_brain_failed_path_intact(tmp_path, env, monkeypatch):
    paths, events = env
    script = _script(tmp_path, "brain", "echo started\nexec sleep 30\n")
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(brain=BrainConfig(timeout_s=1, max_retries=0)), None, paths, events)

    start = time.time()
    with pytest.raises(BrainInvocationError, match="timed out after 1s"):
        brain.invoke("p")
    assert time.time() - start < 10  # killed at the deadline, not after 30s

    kinds = [e["event"] for e in events.tail(20)]
    assert "brain-timeout" in kinds and "brain-failed" in kinds
    assert list(paths.brain_dir.glob("*.response.md")) == []  # failed runs leave none


# ── stream-json mode ────────────────────────────────────────────────────────


def test_stream_argv_appends_and_dedupes():
    assert stream_argv(["claude", "-p", "x"]) == [
        "claude",
        "-p",
        "x",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    # flags already present (any order/position): nothing duplicated
    already = ["claude", "-p", "--output-format", "stream-json", "--verbose", "x"]
    assert stream_argv(already) == already
    # template carries the flags -> streamable even for a non-claude binary
    assert stream_argv(["mycli", "--output-format", "stream-json", "x"]) == [
        "mycli",
        "--output-format",
        "stream-json",
        "x",
        "--verbose",
    ]
    # absolute path still recognized by basename
    assert stream_argv(["/usr/local/bin/claude", "-p", "x"]) is not None
    # non-claude binary without the flags: not streamable
    assert stream_argv(["hermes", "-z", "x"]) is None


def test_stream_json_reassembly_from_fixture(tmp_path, env, monkeypatch):
    paths, events = env
    fixture = Path(__file__).parent / "fixtures" / "claude-stream.jsonl"
    script = _script(tmp_path, "claude", f'cat "{fixture}"\n')
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(brain=BrainConfig(stream=True)), None, paths, events)

    out = brain.invoke("p")

    assert out == "hello-from-foo"  # the 'result' event text, verbatim
    response = next(iter(paths.brain_dir.glob("*.response.md"))).read_text(encoding="utf-8")
    assert "hello-from-foo" in response  # assistant text delta, verbatim
    assert "[tool] Read /private/tmp/claude-probe/foo.ts" in response  # tool one-liner
    assert response.startswith("=== ")  # system/init header line
    assert "hook_started" not in response  # noise events are not rendered
    raw = next(iter(paths.brain_dir.glob("*.stream.jsonl"))).read_text(encoding="utf-8")
    assert raw == fixture.read_text(encoding="utf-8")  # raw JSONL provenance


def test_stream_renderer_result_fallback_to_assistant_text():
    renderer = StreamRenderer()
    blocks = [{"type": "text", "text": "A"}]
    line = json.dumps({"type": "assistant", "message": {"content": blocks}})
    renderer.feed(line + "\n")
    assert renderer.result_text is None
    assert "".join(renderer.assistant_text) == "A"


def test_stream_unsupported_harness_warns_and_runs_plain(tmp_path, env, monkeypatch):
    paths, events = env
    script = _script(tmp_path, "hermes", 'printf "plain:%s" "$1"\n')
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(
        EngineConfig(brain=BrainConfig(harness="hermes", stream=True)), None, paths, events
    )

    assert brain.invoke("p") == "plain:p"  # plain run: raw stdout returned

    warnings = [e for e in events.tail(10) if e["event"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "brain-stream-unsupported"
    assert warnings[0]["harness"] == "hermes"
    assert list(paths.brain_dir.glob("*.stream.jsonl")) == []  # no stream artifacts


def test_stream_defaults_off_and_brain_cmd_path_identical(tmp_path, env, monkeypatch):
    assert BrainConfig().stream is False
    paths, events = env
    script = _script(tmp_path, "claude", 'printf "raw:%s" "$1"\n')
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(), None, paths, events)
    assert brain.invoke("p") == "raw:p"  # claude-named binary, stream off: plain
    assert list(paths.brain_dir.glob("*.stream.jsonl")) == []

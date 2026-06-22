"""Brain invocation: argv shapes, budget guard, retries, transcripts,
incremental (live-tailable) writes, timeout kill, stream-json reassembly/usage."""

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
    classify_failure,
    codex_cost,
    codex_usage,
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


# ── F3: model-unavailable failure kind (T0018) ──────────────────────────────


def test_classify_failure_model_unavailable_both_streams():
    # The F3 notice prints to STDOUT (why it was mislabeled), so both streams
    # are checked; model-unavailable outranks quota/timeout.
    assert classify_failure("", "the model is currently unavailable", False) == "model-unavailable"
    assert classify_failure("model foo is no longer available", "", False) == "model-unavailable"
    assert classify_failure("", "model unavailable", True) == "model-unavailable"  # beats timeout
    # regression: the existing kinds are unchanged.
    assert classify_failure("usage limit reached", "", False) == "quota"
    assert classify_failure("", "", True) == "timeout"
    assert classify_failure("generic boom", "ok output", False) == "exit"
    # no false positive on an ordinary error that merely mentions a file.
    assert classify_failure("cannot read file model.txt: not found", "", False) == "exit"


def test_brain_failed_records_model_unavailable_from_stdout(tmp_path, env, monkeypatch):
    paths, events = env
    # the notice prints to STDOUT then a non-zero exit — the exact F3 shape that
    # was mislabeled 'exit' before stdout was inspected.
    script = _script(
        tmp_path, "brain", 'echo "Error: model claude-fable-5 is currently unavailable"\nexit 1\n'
    )
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(brain=BrainConfig(max_retries=0)), None, paths, events)

    with pytest.raises(BrainInvocationError) as excinfo:
        brain.invoke("p")

    assert excinfo.value.failure_kind == "model-unavailable"
    failed = [e for e in events.tail(20) if e["event"] == "brain-failed"]
    assert failed and failed[-1]["failure_kind"] == "model-unavailable"


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
    usage = [e for e in events.tail(10) if e["event"] == "brain-usage"]
    assert len(usage) == 1
    assert usage[0]["usage_source"] == "stream-json"
    assert usage[0]["cost_source"] == "provider"
    assert usage[0]["model"] == "claude-fable-5"
    assert usage[0]["input_tokens"] == 13827
    assert usage[0]["output_tokens"] == 144
    assert usage[0]["cache_creation_input_tokens"] == 18982
    assert usage[0]["cache_read_input_tokens"] == 45421
    assert usage[0]["total_tokens"] == 78374
    assert usage[0]["cost_usd"] == pytest.approx(0.570531)


def test_stream_json_unpriced_usage_is_not_reported_as_zero(tmp_path, env, monkeypatch):
    paths, events = env
    stream = tmp_path / "unpriced.stream.jsonl"
    stream.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "ok",
                        "total_cost_usd": 0,
                        "usage": {
                            "input_tokens": 10.0,
                            "output_tokens": 5.0,
                        },
                        "modelUsage": {
                            "claude-retired-20260101": {
                                "inputTokens": 10,
                                "outputTokens": 5,
                            }
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script = _script(tmp_path, "claude", f'cat "{stream}"\n')
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(brain=BrainConfig(stream=True)), None, paths, events)

    assert brain.invoke("p") == "ok"

    usage = [e for e in events.tail(10) if e["event"] == "brain-usage"]
    assert len(usage) == 1
    assert usage[0]["model"] == "claude-retired-20260101"
    assert usage[0]["cost_source"] == "unpriced"
    assert usage[0]["cost_usd"] is None
    assert usage[0]["total_tokens"] == 15


def test_stream_json_multi_model_usage_keeps_model_label():
    renderer = StreamRenderer()
    renderer.feed(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "usage": {"input_tokens": 1, "output_tokens": 2},
                "modelUsage": {
                    "model-b": {"costUSD": 0.2},
                    "model-a": {"costUSD": 0.1},
                },
            }
        )
        + "\n"
    )

    assert renderer.model == "model-a,model-b"
    assert renderer.cost_source == "provider"
    assert renderer.cost_usd == pytest.approx(0.3)


def test_stream_json_failed_retry_records_attempt_usage(tmp_path, env, monkeypatch):
    paths, events = env
    state = tmp_path / "failed-once"
    script = _script(
        tmp_path,
        "claude",
        "\n".join(
            [
                f'if [ ! -f "{state}" ]; then',
                f'  : > "{state}"',
                "  cat <<'JSON'",
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "error_max_turns",
                        "is_error": True,
                        "result": "too many turns",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                        "modelUsage": {"model-a": {"costUSD": 0.15}},
                    }
                ),
                "JSON",
                "  exit 0",
                "fi",
                "cat <<'JSON'",
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "ok",
                        "usage": {"input_tokens": 20, "output_tokens": 10},
                        "modelUsage": {"model-a": {"costUSD": 0.3}},
                    }
                ),
                "JSON",
            ]
        )
        + "\n",
    )
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(brain=BrainConfig(stream=True, max_retries=1)), None, paths, events)

    assert brain.invoke("p") == "ok"

    usage = [e for e in events.tail(20) if e["event"] == "brain-usage"]
    assert [e["attempt_status"] for e in usage] == ["failed", "success"]
    assert [e["attempt"] for e in usage] == [1, 2]
    assert [e["total_tokens"] for e in usage] == [15, 30]
    assert [e["cost_usd"] for e in usage] == [0.15, 0.3]


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
    usage = [e for e in events.tail(10) if e["event"] == "brain-usage"]
    assert len(usage) == 1
    assert usage[0]["usage_source"] == "unavailable"
    assert usage[0]["cost_source"] == "unavailable"
    assert usage[0]["input_tokens"] is None
    assert usage[0]["cost_usd"] is None


def test_plain_brain_records_unavailable_usage_without_json_flags(tmp_path, env, monkeypatch):
    paths, events = env
    argv_file = tmp_path / "argv.txt"
    script = _script(
        tmp_path,
        "claude",
        f'printf "%s\\n" "$@" > "{argv_file}"\nprintf "plain decision"\n',
    )
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(), None, paths, events)

    assert brain.invoke("p") == "plain decision"

    argv_text = argv_file.read_text(encoding="utf-8")
    assert "--output-format" not in argv_text
    assert "json" not in argv_text
    assert "--verbose" not in argv_text
    records = events.tail(10)
    call = next(e for e in records if e["event"] == "brain-call")
    usage = next(e for e in records if e["event"] == "brain-usage")
    assert call["seq"] < usage["seq"]
    assert usage["usage_source"] == "unavailable"
    assert usage["cost_source"] == "unavailable"
    assert usage["total_tokens"] is None


def test_stream_json_error_result_raises_not_reply(tmp_path, env, monkeypatch):
    paths, events = env
    stream = tmp_path / "error.stream.jsonl"
    stream.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "init",
                        "model": "claude-fable-5",
                        "session_id": "s1",
                        "cwd": str(paths.project_root),
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "error_max_turns",
                        "is_error": True,
                        "result": "aborted turn",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script = _script(tmp_path, "claude", f'cat "{stream}"\n')
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(brain=BrainConfig(stream=True, max_retries=0)), None, paths, events)

    with pytest.raises(BrainInvocationError, match="stream-json result error"):
        brain.invoke("p")

    assert list(paths.brain_dir.glob("*.response.md")) == []
    assert "brain-usage" not in [e["event"] for e in events.tail(20)]
    assert "brain-failed" in [e["event"] for e in events.tail(20)]


def test_stream_defaults_off_and_brain_cmd_path_identical(tmp_path, env, monkeypatch):
    assert BrainConfig().stream is False
    paths, events = env
    script = _script(tmp_path, "claude", 'printf "raw:%s" "$1"\n')
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(script))
    brain = Brain(EngineConfig(), None, paths, events)
    assert brain.invoke("p") == "raw:p"  # claude-named binary, stream off: plain
    assert list(paths.brain_dir.glob("*.stream.jsonl")) == []


# ── codex usage + pricing (T0069: codex cost instrumentation) ──────────────────


def test_codex_usage_sums_turn_completed_events():
    """Real codex `exec --json` schema (live-probed): turn.completed.usage with
    input/cached/output/reasoning. input INCLUDES cached, reasoning bills as output,
    summed across turns; cached is not double-counted in total."""
    out = "\n".join(
        [
            '{"type":"thread.started"}',
            '{"type":"turn.completed","usage":{"input_tokens":1000,'
            '"cached_input_tokens":200,"output_tokens":50,"reasoning_output_tokens":10}}',
            "not json",
            '{"type":"turn.completed","usage":{"input_tokens":500,'
            '"cached_input_tokens":0,"output_tokens":20,"reasoning_output_tokens":5}}',
        ]
    )
    assert codex_usage(out) == {
        "input_tokens": 1500,
        "output_tokens": 85,  # (50+10) + (20+5)
        "cache_read_input_tokens": 200,
        "total_tokens": 1585,  # 1500 input + 85 output (cached is within input)
    }


def test_codex_usage_none_without_turn_completed():
    assert codex_usage('{"type":"turn.started"}\nplain stdout text\n') is None
    assert codex_usage("") is None


def test_codex_cost_unpriced_or_unavailable_without_rates():
    usage = {
        "input_tokens": 1000,
        "output_tokens": 100,
        "cache_read_input_tokens": 200,
        "total_tokens": 1100,
    }
    assert codex_cost(usage, None) == (None, "unpriced")
    assert codex_cost(usage, {}) == (None, "unpriced")
    assert codex_cost(usage, {"input": 1.0}) == (None, "unpriced")  # output rate missing
    assert codex_cost(None, {"input": 1.0, "output": 2.0}) == (None, "unavailable")


def test_codex_cost_computed_bills_cached_separately():
    usage = {
        "input_tokens": 1_000_000,  # includes 200k cached
        "output_tokens": 100_000,
        "cache_read_input_tokens": 200_000,
        "total_tokens": 1_100_000,
    }
    cost, source = codex_cost(usage, {"input": 1.0, "cached": 0.25, "output": 4.0})
    assert source == "computed"
    # 800k non-cached input @1 + 200k cached @0.25 + 100k output @4 = 1.25
    assert cost == pytest.approx(0.8 + 0.05 + 0.4)


def test_codex_cost_cached_defaults_to_input_rate():
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read_input_tokens": 200_000,
        "total_tokens": 1_000_000,
    }
    cost, source = codex_cost(usage, {"input": 2.0, "output": 8.0})  # no cached rate
    assert source == "computed"
    assert cost == pytest.approx(2.0)  # all 1M input @ $2 (cached billed at input rate)

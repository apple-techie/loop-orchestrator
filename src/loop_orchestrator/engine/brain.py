"""One-shot harness invocation: the engine brain + the headless ingest runner.

The ONLY module besides substrate.py allowed to import subprocess (CI
enforces). Never shell=True: the prompt travels as a single argv element.
:func:`run_oneshot` is the shared spawn/transcript/event machinery; the cost
guard (max_calls_per_hour) is Brain-only and is checked against events.jsonl
BEFORE spawning. Every invocation leaves a prompt/response transcript pair and
exactly one '<prefix>-call' event carrying both paths.

The response transcript is APPENDED to as stdout arrives (Popen + reader
thread, write+flush per chunk), so it is live-tailable from t=0; stderr
streams to a sibling .stderr.txt. With stream=True (claude
`--output-format stream-json`) the raw JSONL is kept in a sibling
.stream.jsonl, the response transcript gets a human-readable rendering, and
the 'result' event's text is the return value — decision parsing unchanged.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import shlex
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..paths import SessionPaths
from ..substrate import Substrate
from .config import EngineConfig
from .events import EventLog

BUDGET_WINDOW_S = 3600
KILL_GRACE_S = 5
_READ_CHUNK = 65536

# A quota/usage-limit stderr must never be misdiagnosed as a slow generation:
# the cure is to back off until the window resets, not to keep retrying.
_QUOTA_RE = re.compile(r"session.?limit|usage.?limit|\bquota\b|rate.?limit", re.IGNORECASE)
# F3 (live Fable-5 outage): the requested model going unavailable is its OWN
# failure class — back off / escalate, never retry into the wall. The notice
# printed to STDOUT (not stderr), which is why it was mislabeled 'exit'; both
# streams are inspected. Kept specific (the word 'model' near 'unavailable', or
# the standard '(currently|temporarily) unavailable' phrasings) so an ordinary
# error mentioning a file is not swept in.
_MODEL_UNAVAILABLE_RE = re.compile(
    r"\bmodel\b[^\n]{0,60}\bunavailable\b"
    r"|\bunavailable\b[^\n]{0,60}\bmodel\b"
    r"|model[^\n]{0,40}\b(?:not available|no longer available)\b"
    r"|currently unavailable"
    r"|temporarily unavailable",
    re.IGNORECASE,
)
# Length-capped excerpt of the stderr tail carried on the failure event so the
# watch loop (and the miner) can read the reset hint without re-opening files.
_STDERR_EXCERPT_LEN = 280
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _nonnegative_int(value) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and value >= 0 and value.is_integer():
        return int(value)
    return None


def _nonnegative_float(value) -> float | None:
    if type(value) in {int, float} and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _usage_fields(usage: object) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    out: dict[str, int] = {}
    for key in _TOKEN_FIELDS:
        value = _nonnegative_int(usage.get(key))
        if value is not None:
            out[key] = value
    if not out:
        return None
    out["total_tokens"] = sum(out.values())
    return out


# cost_source values stamped on brain-usage events — the contract scripts/
# loop-metrics.sh reads to aggregate cost/tokens. Single source of truth; the two
# files are in separate languages, so test_cost_source_contract_in_sync guards
# loop-metrics.sh against drift from these names.
COST_SOURCE_PROVIDER = "provider"  # real cost captured from the harness
COST_SOURCE_UNPRICED = "unpriced"  # usage present but no price for the model
COST_SOURCE_UNAVAILABLE = "unavailable"  # no usage/renderer: cost unknown
COST_SOURCE_COMPUTED = "computed"  # tokens captured (e.g. codex) + priced from config


def _model_usage_cost(obj: dict) -> tuple[float | None, str | None]:
    model_usage = obj.get("modelUsage")
    if not isinstance(model_usage, dict):
        return None, None
    total = 0.0
    seen = False
    for value in model_usage.values():
        if not isinstance(value, dict):
            continue
        cost = _nonnegative_float(value.get("costUSD"))
        if cost is None:
            return None, COST_SOURCE_UNPRICED
        total += cost
        seen = True
    return (total, COST_SOURCE_PROVIDER) if seen else (None, None)


def _result_cost(obj: dict, usage: dict[str, int] | None) -> tuple[float | None, str]:
    model_usage = obj.get("modelUsage")
    cost = _nonnegative_float(obj.get("total_cost_usd"))
    usage_tokens = usage.get("total_tokens", 0) if usage is not None else 0
    if isinstance(model_usage, dict) and model_usage:
        model_cost, source = _model_usage_cost(obj)
        if source == COST_SOURCE_UNPRICED:
            return None, COST_SOURCE_UNPRICED
        if cost is not None:
            if cost == 0 and usage_tokens > 0:
                return None, COST_SOURCE_UNPRICED
            return cost, COST_SOURCE_PROVIDER
        if model_cost is not None:
            if model_cost == 0 and usage_tokens > 0:
                return None, COST_SOURCE_UNPRICED
            return model_cost, COST_SOURCE_PROVIDER
    if cost is not None:
        if cost == 0 and usage_tokens > 0:
            return None, COST_SOURCE_UNPRICED
        return cost, COST_SOURCE_PROVIDER
    return None, COST_SOURCE_UNPRICED if usage is not None else COST_SOURCE_UNAVAILABLE


def _model_from_usage(obj: dict) -> str | None:
    model_usage = obj.get("modelUsage")
    if isinstance(model_usage, dict):
        models = sorted(model for model in model_usage if isinstance(model, str) and model)
        if models:
            return ",".join(models)
    return None


def codex_usage(stdout: str) -> dict[str, int] | None:
    """Sum the per-turn `usage` from codex `exec --json` `turn.completed` events into
    the brain-usage token shape. Codex reports {input_tokens, cached_input_tokens,
    output_tokens, reasoning_output_tokens} per turn; input_tokens already INCLUDES
    cached, and reasoning is billed as output, so total = input + output(+reasoning)
    and cached is NOT added again. A run has many turns -> sum across them. None when
    no usage-bearing turn.completed is present."""
    inp = cached = out = 0
    found = False
    for line in stdout.splitlines():
        if '"turn.completed"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "turn.completed" or not isinstance(obj.get("usage"), dict):
            continue
        usage = obj["usage"]
        inp += _nonnegative_int(usage.get("input_tokens")) or 0
        cached += _nonnegative_int(usage.get("cached_input_tokens")) or 0
        out += (_nonnegative_int(usage.get("output_tokens")) or 0) + (
            _nonnegative_int(usage.get("reasoning_output_tokens")) or 0
        )
        found = True
    if not found:
        return None
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cached,
        "total_tokens": inp + out,  # cached is a subset of input -> not added again
    }


def codex_cost(usage: dict[str, int] | None, pricing: dict | None) -> tuple[float | None, str]:
    """USD cost for a codex usage dict from a per-1M-token price table
    {input, cached, output} (USD per million tokens). Non-cached input is billed at
    `input`, cached input at `cached` (defaults to the input rate when absent), and
    output (incl. reasoning) at `output`. Empty/partial pricing -> (None, 'unpriced')
    — tokens are real but no rates configured; priced -> (cost, 'computed')."""
    if usage is None:
        return None, COST_SOURCE_UNAVAILABLE
    if not isinstance(pricing, dict) or not pricing:
        return None, COST_SOURCE_UNPRICED
    rate_in = _nonnegative_float(pricing.get("input"))
    rate_out = _nonnegative_float(pricing.get("output"))
    if rate_in is None or rate_out is None:
        return None, COST_SOURCE_UNPRICED
    rate_cached = _nonnegative_float(pricing.get("cached"))
    if rate_cached is None:
        rate_cached = rate_in
    cached = usage.get("cache_read_input_tokens", 0)
    non_cached_input = max(0, usage.get("input_tokens", 0) - cached)
    cost = (
        non_cached_input * rate_in + cached * rate_cached + usage.get("output_tokens", 0) * rate_out
    ) / 1_000_000
    return cost, COST_SOURCE_COMPUTED


def classify_failure(stderr: str, stdout: str, timed_out: bool) -> str:
    """failure_kind for a brain/one-shot failure: 'model-unavailable' (the
    requested model is down — back off or escalate, never retry; the notice can
    print to STDOUT, so both streams are checked), 'quota' (session/usage limit
    — back off until the window resets), 'timeout' (the deadline kill), else
    'exit'."""
    if _MODEL_UNAVAILABLE_RE.search(stdout or "") or _MODEL_UNAVAILABLE_RE.search(stderr or ""):
        return "model-unavailable"
    if _QUOTA_RE.search(stderr or ""):
        return "quota"
    if timed_out:
        return "timeout"
    return "exit"


def _stderr_excerpt(stderr: str) -> str:
    """Last `_STDERR_EXCERPT_LEN` chars of the stderr tail, single-lined."""
    text = " ".join((stderr or "").split())
    return text[-_STDERR_EXCERPT_LEN:]


class BrainError(RuntimeError):
    """Base for one-shot invocation failures."""


class BrainBudgetError(BrainError):
    """max_calls_per_hour reached; no process was spawned."""


class BrainInvocationError(BrainError):
    """Every allowed attempt exited non-zero, timed out, or failed to spawn.

    Carries the classified `failure_kind` ('model-unavailable' | 'quota' |
    'timeout' | 'exit') and a capped `stderr_excerpt` so the watch loop can
    apply backoff without re-reading events.jsonl.
    """

    def __init__(self, message: str, failure_kind: str = "exit", stderr_excerpt: str = ""):
        super().__init__(message)
        self.failure_kind = failure_kind
        self.stderr_excerpt = stderr_excerpt


def oneshot_argv(template: str, prompt: str) -> list[str]:
    """shlex-split a registry one-shot template; substitute {prompt} as ONE
    argv element (never shell interpolation). No placeholder = append."""
    tokens = shlex.split(template)
    if "{prompt}" in tokens:
        return [prompt if tok == "{prompt}" else tok for tok in tokens]
    return tokens + [prompt]


def stream_argv(argv: list[str]) -> list[str] | None:
    """argv with `--output-format stream-json --verbose` appended (deduped)
    when the one-shot can stream: the binary is claude, or the template
    already carries the stream-json flags. None = not streamable, run plain."""
    has_flags = "--output-format" in argv and "stream-json" in argv
    if not has_flags and Path(argv[0]).name != "claude":
        return None
    out = list(argv)
    if not has_flags:
        out += ["--output-format", "stream-json"]
    if "--verbose" not in out:
        out.append("--verbose")
    return out


def _transcript_paths(transcript_dir: Path) -> tuple[Path, Path]:
    """Unique <ts>.prompt.md / <ts>.response.md pair under transcript_dir."""
    transcript_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = stamp
    n = 1
    while (transcript_dir / f"{name}.prompt.md").exists():
        name = f"{stamp}-{n}"
        n += 1
    return (
        transcript_dir / f"{name}.prompt.md",
        transcript_dir / f"{name}.response.md",
    )


def _sibling(response_path: Path, suffix: str) -> Path:
    """<name>.stderr.txt / <name>.stream.jsonl beside <name>.response.md."""
    return response_path.with_name(response_path.name[: -len(".response.md")] + suffix)


def _tool_line(block: dict) -> str:
    """One-liner for a tool_use content block: '[tool] Read foo.ts'."""
    name = block.get("name") or "?"
    inputs = block.get("input")
    detail = ""
    if isinstance(inputs, dict):
        for key in ("file_path", "path", "command", "pattern", "url", "query", "prompt"):
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                detail = " " + " ".join(value.split())[:120]
                break
    return f"[tool] {name}{detail}\n"


class StreamRenderer:
    """Reassembles claude `--output-format stream-json` JSONL into a
    human-readable transcript (shape verified against a live probe:
    system/init header, assistant events carrying full message content
    blocks, a final result event with the result text).

    feed() renders every complete line seen so far; the 'result' event text
    lands in .result_text (the invocation's return value) and assistant text
    blocks accumulate in .assistant_text as the fallback.
    """

    def __init__(self):
        self._buf = ""
        self.result_text: str | None = None
        self.result_error: str | None = None
        self.assistant_text: list[str] = []
        self.model: str | None = None
        self.usage: dict[str, int] | None = None
        self.cost_usd: float | None = None
        self.cost_source: str = COST_SOURCE_UNAVAILABLE

    def feed(self, text: str) -> str:
        self._buf += text
        rendered: list[str] = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            rendered.append(self._render(line))
        return "".join(rendered)

    def finish(self) -> str:
        line, self._buf = self._buf, ""
        return self._render(line)

    def _render(self, line: str) -> str:
        line = line.strip()
        if not line:
            return ""
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return line + "\n"  # not JSONL: pass through verbatim
        if not isinstance(obj, dict):
            return ""
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            model = obj.get("model")
            if isinstance(model, str) and model:
                self.model = model
            return (
                f"=== {obj.get('model', '?')} session={obj.get('session_id', '?')} "
                f"cwd={obj.get('cwd', '?')} ===\n"
            )
        if kind == "assistant":
            message = obj.get("message") or {}
            if isinstance(message, dict):
                model = message.get("model")
                if isinstance(model, str) and model:
                    self.model = model
                usage = _usage_fields(message.get("usage"))
                if usage is not None:
                    self.usage = usage
            content = message.get("content") if isinstance(message, dict) else None
            out: list[str] = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    self.assistant_text.append(block["text"])
                    out.append(block["text"].rstrip("\n") + "\n")
                elif btype == "thinking" and block.get("thinking"):
                    out.append(block["thinking"].rstrip("\n") + "\n")
                elif btype == "tool_use":
                    out.append(_tool_line(block))
            return "".join(out)
        if kind == "result":
            subtype = obj.get("subtype")
            if obj.get("is_error") is True or (
                isinstance(subtype, str) and subtype.startswith("error")
            ):
                self.result_error = f"stream-json result error: {subtype or 'error'}"
            if isinstance(obj.get("result"), str):
                self.result_text = obj["result"]
            usage = _usage_fields(obj.get("usage"))
            if usage is not None:
                self.usage = usage
            model = _model_from_usage(obj)
            if model is not None:
                self.model = model
            cost, source = _result_cost(obj, usage)
            self.cost_source = source
            self.cost_usd = cost
            return f"=== result: {subtype or '?'} ===\n"
        return ""  # hooks, thinking_tokens, tool results, rate limits: noise


def _pump(stdout, on_text) -> None:
    """Reader-thread body: forward decoded stdout chunks as they arrive."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        chunk = stdout.read1(_READ_CHUNK)
        if not chunk:
            break
        text = decoder.decode(chunk)
        if text:
            on_text(text)
    tail = decoder.decode(b"", final=True)
    if tail:
        on_text(tail)


def _run_attempt(
    argv: list[str],
    timeout_s: int,
    cwd: Path,
    response_path: Path,
    renderer: StreamRenderer | None,
) -> tuple[int, str]:
    """One Popen attempt. Stdout is appended to response_path as it arrives
    (rendered through `renderer` when streaming — the raw JSONL then goes to
    a sibling .stream.jsonl); stderr streams to a sibling .stderr.txt.
    Returns (returncode, raw stdout); raises subprocess.TimeoutExpired after
    terminate -> SIGKILL past KILL_GRACE_S."""
    raw: list[str] = []
    out_fh = open(response_path, "w", encoding="utf-8")
    raw_fh = (
        open(_sibling(response_path, ".stream.jsonl"), "w", encoding="utf-8")
        if renderer is not None
        else None
    )
    err_fh = open(_sibling(response_path, ".stderr.txt"), "wb")

    def on_text(text: str) -> None:
        raw.append(text)
        try:
            if raw_fh is not None:
                raw_fh.write(text)
                raw_fh.flush()
                out_fh.write(renderer.feed(text))
            else:
                out_fh.write(text)
            out_fh.flush()
        except ValueError:  # file closed after an abandoned reader (timeout)
            pass

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=err_fh,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
        )
        reader = threading.Thread(target=_pump, args=(proc.stdout, on_text), daemon=True)
        reader.start()
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise
        finally:
            reader.join(timeout=KILL_GRACE_S)
            if renderer is not None:
                try:
                    out_fh.write(renderer.finish())
                except ValueError:
                    pass
    finally:
        for fh in (out_fh, raw_fh, err_fh):
            if fh is not None:
                fh.close()
    return proc.returncode, "".join(raw)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _emit_brain_usage(
    events: EventLog,
    renderer: StreamRenderer | None,
    call_fields: dict,
    *,
    attempt: int | None = None,
    attempt_status: str | None = None,
) -> None:
    harness = call_fields.get("harness")
    attempt_fields = {}
    if attempt is not None:
        attempt_fields["attempt"] = attempt
    if attempt_status is not None:
        attempt_fields["attempt_status"] = attempt_status
    if renderer is not None and renderer.usage is not None:
        events.append(
            "brain-usage",
            harness=harness,
            model=renderer.model,
            usage_source="stream-json",
            cost_source=renderer.cost_source,
            cost_usd=renderer.cost_usd,
            **attempt_fields,
            **renderer.usage,
        )
        return
    events.append(
        "brain-usage",
        harness=harness,
        model=renderer.model if renderer is not None else None,
        usage_source="unavailable",
        cost_source=COST_SOURCE_UNAVAILABLE,
        input_tokens=None,
        output_tokens=None,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
        total_tokens=None,
        cost_usd=None,
        **attempt_fields,
    )


def run_oneshot(
    argv: list[str],
    prompt: str,
    transcript_dir: Path,
    timeout_s: int,
    events: EventLog,
    kind_prefix: str,
    cwd: Path,
    max_retries: int = 0,
    stream: bool = False,
    **call_fields,
) -> str:
    """Spawn a one-shot harness and return its stdout (with stream=True: the
    stream-json 'result' text, so decision parsing is unchanged).

    Writes the prompt transcript up front and appends stdout to the response
    transcript as it arrives — live-tailable from t=0. Emits '<prefix>-call'
    (once, with both paths), '<prefix>-timeout', '<prefix>-retry', and
    '<prefix>-failed' events. Raises BrainInvocationError when every attempt
    fails; the partial response transcript of a failed run is removed (only
    successful runs leave one, as before). NO budget check here — the cost
    guard stays in Brain.invoke.
    """
    prompt_path, response_path = _transcript_paths(transcript_dir)
    prompt_path.write_text(prompt, encoding="utf-8")
    events.append(
        f"{kind_prefix}-call",
        prompt_path=str(prompt_path),
        response_path=str(response_path),
        **call_fields,
    )
    attempts = max_retries + 1
    last_error = ""
    last_failure_kind = "exit"
    last_excerpt = ""
    for attempt in range(1, attempts + 1):
        renderer = StreamRenderer() if stream else None
        try:
            returncode, stdout = _run_attempt(argv, timeout_s, cwd, response_path, renderer)
        except subprocess.TimeoutExpired:
            last_error = f"timed out after {timeout_s}s"
            # A quota/model-unavailable notice can land before the deadline kill
            # — read the stderr tail AND the partial stdout transcript so a
            # quota-then-timeout stays 'quota' and a model-unavailable notice
            # (which prints to stdout) is not mislabeled 'timeout'.
            stderr_text = _read_text(_sibling(response_path, ".stderr.txt"))
            stdout_text = _read_text(response_path)
            last_failure_kind = classify_failure(stderr_text, stdout_text, timed_out=True)
            last_excerpt = _stderr_excerpt(stderr_text)
            events.append(
                f"{kind_prefix}-timeout",
                attempt=attempt,
                timeout_s=timeout_s,
                failure_kind=last_failure_kind,
                stderr_excerpt=last_excerpt,
            )
        except OSError as exc:
            last_error = f"spawn failed: {exc}"
            last_failure_kind = "exit"
            last_excerpt = ""
        else:
            if returncode == 0:
                if renderer is not None:
                    if renderer.result_error is not None:
                        if kind_prefix == "brain" and renderer.usage is not None:
                            _emit_brain_usage(
                                events,
                                renderer,
                                call_fields,
                                attempt=attempt,
                                attempt_status="failed",
                            )
                        last_error = renderer.result_error
                        last_failure_kind = "exit"
                        last_excerpt = ""
                    else:
                        result = renderer.result_text or "".join(renderer.assistant_text) or stdout
                        if kind_prefix == "brain":
                            _emit_brain_usage(
                                events,
                                renderer,
                                call_fields,
                                attempt=attempt,
                                attempt_status="success",
                            )
                        return result
                else:
                    if kind_prefix == "brain":
                        _emit_brain_usage(
                            events,
                            None,
                            call_fields,
                            attempt=attempt,
                            attempt_status="success",
                        )
                    return stdout
            else:
                if kind_prefix == "brain" and renderer is not None and renderer.usage is not None:
                    _emit_brain_usage(
                        events,
                        renderer,
                        call_fields,
                        attempt=attempt,
                        attempt_status="failed",
                    )
                stderr_text = _read_text(_sibling(response_path, ".stderr.txt"))
                last_error = f"exit {returncode}: {stderr_text.strip()[:500]}"
                # stdout carries the model-unavailable notice (F3); the response
                # transcript already holds it, but `stdout` is the authoritative copy.
                last_failure_kind = classify_failure(stderr_text, stdout, timed_out=False)
                last_excerpt = _stderr_excerpt(stderr_text)
        if attempt < attempts:
            events.append(f"{kind_prefix}-retry", attempt=attempt, error=last_error)
    response_path.unlink(missing_ok=True)  # failed runs leave no response transcript
    events.append(
        f"{kind_prefix}-failed",
        error=last_error,
        failure_kind=last_failure_kind,
        stderr_excerpt=last_excerpt,
    )
    raise BrainInvocationError(
        f"{kind_prefix} failed after {attempts} attempt(s): {last_error}",
        failure_kind=last_failure_kind,
        stderr_excerpt=last_excerpt,
    )


class Brain:
    def __init__(
        self,
        config: EngineConfig,
        substrate: Substrate,
        paths: SessionPaths,
        events: EventLog,
    ):
        self.config = config
        self.substrate = substrate
        self.paths = paths
        self.events = events

    def _argv(self, prompt: str) -> list[str]:
        """LOOP_ENGINE_BRAIN_CMD overrides the registry's one-shot template."""
        override = os.environ.get("LOOP_ENGINE_BRAIN_CMD")
        if override:
            return shlex.split(override) + [prompt]
        argv = oneshot_argv(self.substrate.oneshot_template(self.config.brain.harness), prompt)
        return argv + list(self.config.brain.extra_args)

    def invoke(self, prompt: str) -> str:
        cfg = self.config.brain
        if self.events.count_since("brain-call", BUDGET_WINDOW_S) >= cfg.max_calls_per_hour:
            raise BrainBudgetError(
                f"brain budget exhausted: {cfg.max_calls_per_hour} calls in the last hour"
            )
        argv = self._argv(prompt)
        stream = False
        if cfg.stream:
            streamed = stream_argv(argv)
            if streamed is None:
                self.events.append("warning", kind="brain-stream-unsupported", harness=cfg.harness)
            else:
                argv, stream = streamed, True
        return run_oneshot(
            argv,
            prompt,
            self.paths.brain_dir,
            cfg.timeout_s,
            self.events,
            "brain",
            cwd=self.paths.project_root,
            max_retries=cfg.max_retries,
            stream=stream,
            harness=cfg.harness,
        )

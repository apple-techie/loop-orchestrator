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
# Length-capped excerpt of the stderr tail carried on the failure event so the
# watch loop (and the miner) can read the reset hint without re-opening files.
_STDERR_EXCERPT_LEN = 280


def classify_failure(stderr: str, timed_out: bool) -> str:
    """failure_kind for a brain/one-shot failure: 'quota' (session/usage limit
    — back off, do not retry), 'timeout' (the deadline kill), else 'exit'."""
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

    Carries the classified `failure_kind` ('quota' | 'timeout' | 'exit') and a
    capped `stderr_excerpt` so the watch loop can apply quota-aware backoff
    without re-reading events.jsonl.
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
        self.assistant_text: list[str] = []

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
            return (
                f"=== {obj.get('model', '?')} session={obj.get('session_id', '?')} "
                f"cwd={obj.get('cwd', '?')} ===\n"
            )
        if kind == "assistant":
            content = (obj.get("message") or {}).get("content")
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
            if isinstance(obj.get("result"), str):
                self.result_text = obj["result"]
            return f"=== result: {obj.get('subtype', '?')} ===\n"
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
            # A quota notice on stderr can land before the deadline kill — read
            # the tail so a quota-then-timeout is still classified 'quota'.
            stderr_text = _read_text(_sibling(response_path, ".stderr.txt"))
            last_failure_kind = classify_failure(stderr_text, timed_out=True)
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
                    return renderer.result_text or "".join(renderer.assistant_text) or stdout
                return stdout
            stderr_text = _read_text(_sibling(response_path, ".stderr.txt"))
            last_error = f"exit {returncode}: {stderr_text.strip()[:500]}"
            last_failure_kind = classify_failure(stderr_text, timed_out=False)
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

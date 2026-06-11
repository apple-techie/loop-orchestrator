"""One-shot harness invocation: the engine brain + the headless ingest runner.

The ONLY module besides substrate.py allowed to import subprocess (CI
enforces). Never shell=True: the prompt travels as a single argv element.
:func:`run_oneshot` is the shared spawn/transcript/event machinery; the cost
guard (max_calls_per_hour) is Brain-only and is checked against events.jsonl
BEFORE spawning. Every invocation leaves a prompt/response transcript pair and
exactly one '<prefix>-call' event carrying both paths.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..paths import SessionPaths
from ..substrate import Substrate
from .config import EngineConfig
from .events import EventLog

BUDGET_WINDOW_S = 3600


class BrainError(RuntimeError):
    """Base for one-shot invocation failures."""


class BrainBudgetError(BrainError):
    """max_calls_per_hour reached; no process was spawned."""


class BrainInvocationError(BrainError):
    """Every allowed attempt exited non-zero, timed out, or failed to spawn."""


def oneshot_argv(template: str, prompt: str) -> list[str]:
    """shlex-split a registry one-shot template; substitute {prompt} as ONE
    argv element (never shell interpolation). No placeholder = append."""
    tokens = shlex.split(template)
    if "{prompt}" in tokens:
        return [prompt if tok == "{prompt}" else tok for tok in tokens]
    return tokens + [prompt]


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


def run_oneshot(
    argv: list[str],
    prompt: str,
    transcript_dir: Path,
    timeout_s: int,
    events: EventLog,
    kind_prefix: str,
    cwd: Path,
    max_retries: int = 0,
    **call_fields,
) -> str:
    """Spawn a one-shot harness and return its stdout.

    Writes the prompt/response transcript pair under transcript_dir and emits
    '<prefix>-call' (once, with both paths), '<prefix>-timeout',
    '<prefix>-retry', and '<prefix>-failed' events. Raises
    BrainInvocationError when every attempt fails. NO budget check here —
    the cost guard stays in Brain.invoke.
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
    for attempt in range(1, attempts + 1):
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            last_error = f"timed out after {timeout_s}s"
            events.append(f"{kind_prefix}-timeout", attempt=attempt, timeout_s=timeout_s)
        except OSError as exc:
            last_error = f"spawn failed: {exc}"
        else:
            if proc.returncode == 0:
                response_path.write_text(proc.stdout, encoding="utf-8")
                return proc.stdout
            last_error = f"exit {proc.returncode}: {proc.stderr.strip()[:500]}"
        if attempt < attempts:
            events.append(f"{kind_prefix}-retry", attempt=attempt, error=last_error)
    events.append(f"{kind_prefix}-failed", error=last_error)
    raise BrainInvocationError(f"{kind_prefix} failed after {attempts} attempt(s): {last_error}")


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
        return run_oneshot(
            self._argv(prompt),
            prompt,
            self.paths.brain_dir,
            cfg.timeout_s,
            self.events,
            "brain",
            cwd=self.paths.project_root,
            max_retries=cfg.max_retries,
            harness=cfg.harness,
        )

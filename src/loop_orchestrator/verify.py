"""Standalone multi-agent verification runner.

Phase 3a only: run the deterministic local gate plus independent one-shot
review lenses, aggregate the verdict, and expose it through `loop-verify`.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shlex
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .engine.brain import BrainInvocationError, oneshot_argv, run_oneshot
from .engine.config import load_config
from .engine.events import EventLog, utc_now
from .paths import SessionPaths
from .substrate import Substrate, SubstrateError

DEFAULT_LENSES = ("code-review", "silent-failure", "adversarial")
DEFAULT_GATE_TIMEOUT_S = 900
DEFAULT_LENS_TIMEOUT_S = 300
_TAIL_LINES = 80
_VERDICTS = {"pass", "concerns", "fail"}
_FAIL_SEVERITIES = {"critical", "high"}
_CONCERN_SEVERITIES = {"medium", "low"}
_JSON_FENCE_RE = re.compile(r"```(?:json)?[ \t]*\n?(.*?)```", re.DOTALL | re.IGNORECASE)

_LENS_INSTRUCTIONS = {
    "code-review": (
        "Review for concrete correctness bugs, regressions, missing tests, and maintainability "
        "risks introduced by this diff. Ignore style nits unless they hide a bug."
    ),
    "silent-failure": (
        "Look specifically for paths where the change can fail silently: swallowed errors, "
        "missing observability, partial writes, skipped validation, or false success reports."
    ),
    "adversarial": (
        "Act as an adversarial reviewer. Try to falsify the claim that this diff is safe by "
        "looking for race conditions, security boundaries, bad assumptions, and edge cases."
    ),
}


@dataclass
class GateResult:
    passed: bool
    output_tail: str


@dataclass
class LensResult:
    lens: str
    verdict: str
    findings: list[dict] = field(default_factory=list)
    summary: str = ""
    error: str | None = None


@dataclass
class VerifyResult:
    overall: str
    gate: GateResult
    lenses: list[LensResult] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    generated_at: str | None = None

    def to_dict(self) -> dict:
        doc = dataclasses.asdict(self)
        if doc.get("generated_at") is None:
            doc.pop("generated_at", None)
        for lens in doc["lenses"]:
            if lens.get("error") is None:
                lens.pop("error", None)
        return doc


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    parts = text.splitlines()
    return "\n".join(parts[-lines:])


def _run_gate(worktree: str | Path, timeout_s: int = DEFAULT_GATE_TIMEOUT_S) -> GateResult:
    passed, output = Substrate(worktree, "verify").run_gate(timeout_s)
    return GateResult(passed, _tail(output))


def _git_diff(worktree: Path, base: str, tip: str, timeout_s: int = 60) -> str:
    try:
        return Substrate(worktree, "verify").git_diff(base, tip, timeout_s)
    except SubstrateError as exc:
        raise RuntimeError(_tail(str(exc))) from exc


def _prompt(lens: str, diff: str) -> str:
    instruction = _LENS_INSTRUCTIONS.get(lens, f"Review this diff through the {lens} lens.")
    return "\n".join(
        [
            f"Lens: {lens}",
            "",
            instruction,
            "",
            "Reply with ONLY one fenced JSON block using this shape:",
            '```json\n{"verdict":"pass|concerns|fail","findings":[{"severity":"critical|high|medium|low","title":"...","detail":"..."}],"summary":"..."}\n```',
            "",
            "--- diff ---",
            diff,
        ]
    )


def _argv_for_prompt(worktree: Path, prompt: str, harness: str | None) -> tuple[list[str], str]:
    override = os.environ.get("LOOP_VERIFY_CMD")
    if override:
        return shlex.split(override) + [prompt], harness or "override"

    config = load_config(worktree)
    effective_harness = harness or config.brain.harness
    argv = oneshot_argv(Substrate(worktree, "verify").oneshot_template(effective_harness), prompt)
    if effective_harness == config.brain.harness:
        argv += list(config.brain.extra_args)
    return argv, effective_harness


def _parse_lens_reply(lens: str, reply: str) -> LensResult:
    doc = None
    last_error: json.JSONDecodeError | None = None
    for raw in reversed([match.group(1).strip() for match in _JSON_FENCE_RE.finditer(reply)]):
        try:
            doc = json.loads(raw)
            break
        except json.JSONDecodeError as exc:
            last_error = exc
    if doc is None:
        try:
            doc = json.loads(reply.strip())
        except json.JSONDecodeError as exc:
            return _parse_note(lens, f"parse error: {last_error or exc}")
    if not isinstance(doc, dict):
        return _parse_note(lens, "parse error: JSON block is not an object")
    verdict = str(doc.get("verdict", "")).lower()
    if verdict not in _VERDICTS:
        return _parse_note(lens, f"parse error: invalid verdict {doc.get('verdict')!r}")
    findings = doc.get("findings", [])
    if not isinstance(findings, list):
        return _parse_note(lens, "parse error: findings is not a list")
    normalized = [_normalize_finding(lens, item) for item in findings if isinstance(item, dict)]
    return LensResult(
        lens=lens,
        verdict=verdict,
        findings=normalized,
        summary=str(doc.get("summary", "")),
    )


def _normalize_finding(lens: str, item: dict) -> dict:
    severity = str(item.get("severity", "low")).lower()
    if severity not in {"critical", "high", "medium", "low"}:
        severity = "low"
    return {
        "lens": lens,
        "severity": severity,
        "title": str(item.get("title", "finding")),
        "detail": str(item.get("detail", "")),
    }


def _parse_note(lens: str, message: str) -> LensResult:
    finding = {
        "lens": lens,
        "severity": "low",
        "title": "parse-note",
        "detail": message,
    }
    return LensResult(
        lens=lens,
        verdict="concerns",
        findings=[finding],
        summary="Lens reply could not be parsed.",
        error=message,
    )


def _error_note(lens: str, message: str) -> LensResult:
    finding = {
        "lens": lens,
        "severity": "low",
        "title": "lens-error",
        "detail": message,
    }
    return LensResult(
        lens=lens,
        verdict="concerns",
        findings=[finding],
        summary="Lens invocation failed.",
        error=message,
    )


def _lens_transcript_dir(base_dir: Path, lens: str) -> Path:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", lens).strip(".-") or "lens"
    return base_dir / name


def _result(
    *,
    overall: str,
    gate: GateResult,
    lenses: list[LensResult],
    findings: list[dict],
) -> VerifyResult:
    return VerifyResult(
        overall=overall,
        gate=gate,
        lenses=lenses,
        findings=findings,
        generated_at=utc_now(),
    )


def _run_lens(
    worktree: Path,
    lens: str,
    diff: str,
    harness: str | None,
    timeout_s: int,
    transcript_dir: Path,
    events: EventLog,
) -> LensResult:
    prompt = _prompt(lens, diff)
    argv, effective_harness = _argv_for_prompt(worktree, prompt, harness)
    try:
        reply = run_oneshot(
            argv,
            prompt,
            _lens_transcript_dir(transcript_dir, lens),
            timeout_s,
            events,
            "verify",
            cwd=worktree,
            max_retries=0,
            harness=effective_harness,
            lens=lens,
        )
    except BrainInvocationError as exc:
        return _error_note(lens, str(exc))
    return _parse_lens_reply(lens, reply)


def _aggregate(gate: GateResult, lenses: list[LensResult]) -> tuple[str, list[dict]]:
    findings = [finding for lens in lenses for finding in lens.findings]
    if not gate.passed:
        return "fail", findings
    if any(lens.verdict == "fail" for lens in lenses):
        return "fail", findings
    if any(str(f.get("severity", "")).lower() in _FAIL_SEVERITIES for f in findings):
        return "fail", findings
    if any(lens.verdict == "concerns" for lens in lenses):
        return "concerns", findings
    if any(str(f.get("severity", "")).lower() in _CONCERN_SEVERITIES for f in findings):
        return "concerns", findings
    return "pass", findings


def run_verify(
    worktree: str | Path,
    base: str,
    tip: str,
    *,
    harness: str | None = None,
    lenses: Sequence[str] = DEFAULT_LENSES,
    timeout_s: int = DEFAULT_LENS_TIMEOUT_S,
) -> VerifyResult:
    root = Path(worktree)
    gate = _run_gate(root)
    if not gate.passed:
        overall, findings = _aggregate(gate, [])
        return _result(overall=overall, gate=gate, lenses=[], findings=findings)

    try:
        diff = _git_diff(root, base, tip)
    except RuntimeError as exc:
        lens_result = _error_note("diff", str(exc))
        return _result(
            overall="fail",
            gate=gate,
            lenses=[lens_result],
            findings=lens_result.findings,
        )
    if not diff.strip():
        lens_result = _error_note("diff", f"nothing to review: git diff {base}..{tip} is empty")
        return _result(
            overall="concerns",
            gate=gate,
            lenses=[lens_result],
            findings=lens_result.findings,
        )

    paths = SessionPaths(root, "verify")
    paths.ensure()
    transcript_dir = paths.engine_dir / "verify"
    events = EventLog(paths.events_path)
    lens_names = tuple(lenses)
    by_lens: dict[str, LensResult] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(lens_names))) as pool:
        futures = {
            pool.submit(
                _run_lens,
                root,
                lens,
                diff,
                harness,
                timeout_s,
                transcript_dir,
                events,
            ): lens
            for lens in lens_names
        }
        for future in as_completed(futures):
            lens = futures[future]
            try:
                by_lens[lens] = future.result()
            except Exception as exc:
                by_lens[lens] = _error_note(lens, f"{type(exc).__name__}: {exc}")

    ordered = [by_lens[lens] for lens in lens_names]
    overall, findings = _aggregate(gate, ordered)
    return _result(overall=overall, gate=gate, lenses=ordered, findings=findings)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run standalone loop verification.")
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--tip", required=True)
    parser.add_argument("--out")
    parser.add_argument("--harness")
    args = parser.parse_args(argv)

    result = run_verify(args.worktree, args.base, args.tip, harness=args.harness)
    payload = json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0 if result.overall == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

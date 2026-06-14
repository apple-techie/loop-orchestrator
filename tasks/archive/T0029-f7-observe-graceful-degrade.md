---
id: T0029
title: "F7 — observe degrades gracefully instead of aborting the cycle on a slow/failed fan-out"
status: done
accepted: 2026-06-14
gate: "484/0"
commit: c493eb01
depends_on: []
scope: src/loop_orchestrator/engine/{observe.py,loop.py} + tests ONLY; ADDITIVE; no reinstall; no git push
loop: harness-governance
---

# T0029 — F7: observe graceful degradation

## Objective
Hardening. **F7 (found live 2026-06-14):** the engine's observe phase calls
`loop-lane-status --json --all` with a HARD 30s timeout; under a transient load
spike (two concurrent heavy xhigh builds) the fleet fan-out — which captures EVERY
pane at once — exceeded 30s, so observe `observe-failed` and ABORTED the whole
cycle, repeatedly, in BOTH live loops, stalling them ~2-3 min until load dropped.
A single-lane read stayed instant the entire time; only the all-lanes fan-out
under contention timed out.

## Required behavior (the fix)
Observe must DEGRADE GRACEFULLY, never abort the cycle on a transient fan-out
timeout:
- On fan-out timeout/failure, REUSE the last good `snapshot.json` (emit an
  `observe-stale` event with the snapshot age) so the cycle proceeds/skips sanely
  instead of failing.
- And/or an ADAPTIVE timeout (scale with lane count) + a bounded retry, not a flat
  30s.
- Optionally capture lanes with bounded concurrency so the fan-out is faster under
  load.

## Context you need
Files: `engine/observe.py` (the lane-status fan-out + atomic snapshot write
~:76-88), `engine/loop.py` (where `observe-failed` aborts the cycle). The
single-lane path is fast; the `--json --all` fan-out is the slow one under load.
`snapshot.json` is already the atomic last-observed state — reuse it.

## Deliverables
- Observe falls back to the last snapshot (or adaptive timeout/retry) on a fan-out
  timeout; emits `observe-stale` rather than aborting the cycle.
- Tests: a simulated fan-out timeout → cycle proceeds on the stale snapshot (no
  abort); a fresh fan-out resumes normally. Before/after note in
  `ops-wiki/loops/harness-governance.md`.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (no regressions). Commit per the commit
policy (cite T0029 / F7). Do NOT reinstall; do NOT push.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
Fixing the underlying machine load (operator concern). If acting on a STALE
snapshot would be unsafe (e.g. dispatching into a lane whose real state is
unknown), prefer to SKIP the cycle rather than act on stale data — never trade
a stall for an unsafe action.

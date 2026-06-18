---
id: T0041
title: "F16 — checkpoint_prompt over-ceiling must degrade gracefully, not abort the cycle before the brain runs"
status: done
accepted: 2026-06-17
gate: ""
commit: 2d21f74 (merged 5099bd8)
depends_on: []
scope: src/loop_orchestrator/engine/loop.py (wrap the checkpoint_prompt call in run_once/_assemble_prompt) + tests ONLY; ADDITIVE; NO reinstall; NO git push; do NOT add a watch.py backoff/suppress-re-trigger (see "out of scope"); the scripts/loop-checkpoint.sh overflow-signal is OPTIONAL/deprioritized and worktree-only if touched
loop: lo-janitor
---

# T0041 — F16: checkpoint overflow degrades, never aborts the cycle

## Objective
Hardening. **F16 (harvested from leo, 2026-06-16):** `checkpoint_prompt` is the ONLY
substrate call in `run_once` not wrapped in `try/except SubstrateError`. When the
assembled checkpoint prompt exceeds the hard token ceiling (default 48000),
`scripts/loop-checkpoint.sh` exits 3 (~:316-318) and `substrate.checkpoint_prompt`
raises `SubstrateError`. `run_once` calls it (around `loop.py:523`, via
`_assemble_prompt`) with NO guard, so the WHOLE cycle aborts before the brain ever
runs — unlike observe / ingest / roster / lanes, which all degrade gracefully (the
F7/F11 pattern). `watch._run_cycle` catches it generically and keeps the daemon
alive, but the brain never gets a chance to self-trim, so the loop can't fix its own
bloat. (Observed live on leo: 2 over-ceiling crashes; leo self-recovered once state
shrank via ingest/decision rotation — so this is a graceful-degrade consistency gap,
NOT a wedged-hot-loop.)

## Required behavior (the fix)
Wrap the `checkpoint_prompt`/`_assemble_prompt` call in `run_once` in
`try/except SubstrateError`, mirroring the existing F7/F11 degrade pattern:
- On overflow, emit a `checkpoint-overflow` event + resolve the cycle
  (`cycle-end` outcome) instead of propagating the error.
- Prefer to DEGRADE so the brain still runs: fall back to a header-only (or
  last-good-size truncated) prompt so the brain can self-trim `ops-wiki/checkpoint.md`
  + `index.md` on that cycle, rather than skipping the brain entirely. If a clean
  truncation is non-trivial, at minimum resolve the cycle gracefully (no abort).

## Context you need
Files: `src/loop_orchestrator/engine/loop.py` (`run_once` / `_assemble_prompt`,
~:523 — the unguarded call); compare to the already-guarded observe/ingest paths in
the same function for the exact degrade idiom + event names. `SubstrateError` +
`substrate.checkpoint_prompt` are in `substrate.py`; the exit-3 ceiling is in
`scripts/loop-checkpoint.sh` (~:316-318).

## Deliverables
- `run_once` degrades gracefully on a checkpoint-overflow `SubstrateError` (emits
  `checkpoint-overflow` + `cycle-end`, ideally runs the brain on a trimmed prompt),
  never aborts the cycle. Before/after note in `ops-wiki/loops/lo-janitor.md`.
- Test (red-before-green): stub `substrate.checkpoint_prompt` to raise
  `SubstrateError` (exit 3), assert `run_once` does NOT raise, emits the
  `checkpoint-overflow` + `cycle-end` events, and (if you implement the fallback) the
  brain still runs on the degraded prompt.

## Acceptance criteria
`make check-python` green in YOUR worktree, no regressions off the 554 baseline.
Commit to YOUR worktree branch citing T0041/F16.

## Out of scope / ESCALATE (hard gate)
- Do NOT add a `watch.py` backoff / suppress-re-trigger: leo's own events show the
  loop self-recovered with zero manual intervention, so the hot-loop that sub-fix
  would guard is NOT demonstrated. Keep this surgical — `loop.py` try/except only.
- Do NOT merge / push / reinstall / edit the main checkout. When green, STOP and
  escalate to the operator; the operator merges + reinstalls.

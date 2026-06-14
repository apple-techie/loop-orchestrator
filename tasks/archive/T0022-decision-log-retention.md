---
id: T0022
title: "decision-log retention in wiki.py (atomic rotate+archive) + hard token gate"
status: done
accepted: 2026-06-13
depends_on: []
scope: src/loop_orchestrator/engine/wiki.py + scripts/loop-checkpoint.sh (token gate) + engine config + tests ONLY; ADDITIVE; keep-N configurable; marker preserved exactly; compiled region untouched; no reinstall; no git push
loop: harness-governance
---

# T0022 — decision-log retention + hard token gate

## Objective
Phase 3 continuity. Durable fix for the unbounded coord-decisions partition (it
grew ooLEO's checkpoint.md to 235KB / ~60K tokens, ~9x the 24K boot budget, and
needed a manual hand-triage). Make `wiki.py`.`file_decision` ROTATE inside its
existing atomic read-modify-write: keep the last N decision entries below the
marker, append the overflow to `ops-wiki/decisions-archive.md`. Promote
`loop-checkpoint.sh`'s bytes/4 token estimate from a stderr WARNING to a hard
gate (configurable ceiling). Retires the unenforced AGENTS.md prose "decision
rotation experiment" by making it a code invariant.

## Context you need
Files: `engine/wiki.py` (`file_decision` + `_compose` at ~:52-74 — the ONLY
writer of the coord-decisions region; mtime-checked atomic RMW; the marker
contract), `scripts/loop-checkpoint.sh` (the token estimate warning). Keep-N from
engine config (default e.g. 10, matching the just-applied ooLEO hand-triage).
Archive append must be atomic w.r.t. the checkpoint rewrite. Must preserve the
`<!-- coord-decisions -->` marker exactly and never split a partial entry.

## Deliverables
- `wiki.py`: rotate-on-write (keep last N, append overflow to
  `decisions-archive.md`) within the atomic write. Configurable N. Token gate
  hardened in `loop-checkpoint.sh`.
- Tests (rotation keeps last N + archives the rest; marker preserved; idempotent;
  partial-entry-safe). Before/after note in `ops-wiki/loops/harness-governance.md`.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (no regressions). Existing tests pass
UNCHANGED. Commit per the commit policy (cite T0022). Do NOT reinstall; do NOT push.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev ruff check src tests && uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
The ledger projection (T0021). Do NOT touch the compiled region ABOVE the marker.
If rotation could ever drop the marker or corrupt a partial entry → STOP and escalate.

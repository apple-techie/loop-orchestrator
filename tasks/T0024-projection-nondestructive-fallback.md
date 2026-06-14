---
id: T0024
title: "F5 — ledger projection must be non-destructive per-field (sparse ledger preserves hand-authored)"
status: open
depends_on: []
scope: scripts/loop-checkpoint.sh + tests ONLY; ADDITIVE; absent OR sparse ledger must preserve the hand-authored compiled region; no reinstall; no git push
loop: harness-governance
---

# T0024 — F5: non-destructive ledger projection

## Objective
Phase 3 hardening. **F5 (found live during the Phase 3 merge close-out):** T0021's
`project_checkpoint` in `scripts/loop-checkpoint.sh` is DESTRUCTIVE against a
present-but-sparse ledger. Behavior observed against the live sessions (run with
`--project-root <session>`):
- **govern** has NO `.loop/orchestrator-state.json` → correctly falls back to the
  hand-authored region (`[[ ! -s "$ledger" ]]` at ~line 172). SAFE.
- **leo** has a ledger with `loops` (3) but NO `objective` key → the projection
  emits `## Current objective\n(none recorded in ledger)`, **destroying leo's
  real hand-authored objective**. This would degrade the brain's boot context on
  the next cycle.

Root cause: the projection replaces the WHOLE compiled region whenever the ledger
file merely *exists*, and substitutes `(none recorded in ledger)` for any field
the ledger lacks. The existing tests passed because they used a fully-populated
fixture ledger, not a sparse one.

## Required behavior (the fix)
The projection must be **non-destructive per field**. For each compiled-region
field (objective / loop states / open conflicts): project it from the ledger ONLY
when the ledger actually carries that field; otherwise **preserve the hand-authored
value** parsed from the current checkpoint.md compiled region. Never replace a
hand-authored field with `(none recorded)`. Absent ledger keeps the existing
full fallback. (Equivalently acceptable: fall back to the hand-authored region
entirely unless the ledger has a non-empty `objective` — but per-field preservation
is preferred so a loops-only ledger still projects its loops while keeping the
hand-authored objective.) This stays doctrine-aligned: the ledger remains canonical
for what it HAS; it just stops clobbering what it lacks.

## Context you need
Files: `scripts/loop-checkpoint.sh` — `project_checkpoint()` ~line 170 and the
inline `python3 -c` block ~line 176-215 (`fallback()` at ~183; the
`data.get("objective") or "(none recorded in ledger)"` at ~215 is the defect
site). The `<!-- coord-decisions -->` region (and everything below it) is
untouched — only the compiled region above the marker is in scope.

## Deliverables
- Non-destructive per-field projection in `loop-checkpoint.sh`.
- Tests covering: (a) absent ledger → full hand-authored fallback; (b)
  **loops-only ledger, no objective → projects loops, PRESERVES hand-authored
  objective** (the F5 regression); (c) fully-populated ledger → projects all
  (existing behavior). Before/after note in `ops-wiki/loops/harness-governance.md`.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (no regressions). Commit per the commit
policy (cite T0024 / F5). Do NOT reinstall; do NOT push.

## Verification
```
make check
bash scripts/loop-checkpoint.sh --print --project-root <a loops-only-ledger session>  # objective preserved, not "(none)"
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
Migrating session ledgers to ADD an objective field (data hygiene, separate).
The coord-decisions region. If the fix can't preserve a hand-authored field
without parsing risk, STOP and escalate rather than ship a destructive projection.

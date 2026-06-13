---
id: T0021
title: "orchestrator-state.json canonical; project checkpoint compiled-region from the ledger"
status: open
depends_on: []
scope: scripts/loop-checkpoint.sh + src/loop_orchestrator/ (read-only consumers) + CONTRACT.md + tests ONLY; ADDITIVE; absent/empty ledger = today's hand-authored region; coord-decisions region untouched; no reinstall; no git push
loop: harness-governance
---

# T0021 — orchestrator-state.json canonical; boot-prompt projection

## Objective
Phase 3 continuity. Operator decision (settled): `orchestrator-state.json` (the
loop ledger) is the single record of truth for "what are we doing and where are
we"; `checkpoint.md` becomes its compiled projection. Render checkpoint.md's
compiled region (Current objective / Loop states / Open conflicts — everything
ABOVE the `<!-- coord-decisions -->` marker) FROM `orchestrator-state.json` at
assembly time in `loop-checkpoint.sh --print`, the same way it already injects
the pending-mailbox summary. The brain then boots from a surface provably equal
to the ledger; no human or agent hand-edits a derived view.

## Context you need
Files: `scripts/loop-checkpoint.sh` (`--print` assembles header + checkpoint.md +
index.md + mailbox at ~:152-161 via the inline python renderer), `.loop/
orchestrator-state.json` (schema-v2: per-loop status / branch / blast_radius /
artifacts / commits), `CONTRACT.md`. The coord-decisions region (BELOW the
marker) is UNTOUCHED — only the compiled region above it is projected.
Absent/empty ledger = fall back to the existing hand-authored region (additive
default, byte-identical when no ledger).

## Deliverables
- `loop-checkpoint.sh` projects the compiled region from `orchestrator-state.json`
  when present; the marker + coord-owned region preserved byte-for-byte.
- `CONTRACT.md`: declare the ledger canonical + the projection contract.
- Tests (ledger → region projection; absent-ledger fallback). Before/after note
  in `ops-wiki/loops/harness-governance.md`.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (no regressions). Existing tests pass
UNCHANGED. Commit per the commit policy (cite T0021). Do NOT reinstall; do NOT push.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev ruff check src tests && uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
Changing the coord-decisions partition or `wiki.py` (that is T0022). Migrating
Jira / lanes / loops to derive from the ledger (future). If the projection would
change the brain's boot contract non-additively → STOP and escalate.

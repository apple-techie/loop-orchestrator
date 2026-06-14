---
id: T0026
title: "Phase 4 — conditional worktree provisioning rule + N>=3 integration lane"
status: done
accepted: 2026-06-14
gate: "465/0"
commit: 55559b2
depends_on: [T0025]
scope: src/loop_orchestrator/engine/{gate.py,actions.py,loop.py} + contracts/checkpoint-header.md + CONTRACT.md + tests ONLY; ADDITIVE; concurrency=1 path unchanged (stays shared); no reinstall; no git push
loop: harness-governance
---

# T0026 — conditional provisioning + integration lane (Phase 4)

## Objective
Phase 4, second half. Make worktree provisioning CONDITIONAL on concurrency, not
operator opinion (the plan's decisive point: "shared only while serialized").
Build on T0025's lifecycle.

**Decision rule — provision a worktree if ANY holds:** another implementation
lane currently holds dirty state (`loops` ledger / digest shows
`unpushed[].count > 0`); the lane's harness `isolation` is `worktree` (treat
unverified non-claude harnesses as `worktree` by default); or the lane must
build/test while another edits. **Stay shared only when ALL hold:** concurrency
provably 1, the writer is a verified narrow-stager, no test needs a frozen tree.
The default flips from "shared unless asked" to "shared only while serialized."

**Integration lane (N >= 3 sustained):** a sibling of `validate` on its own
integration worktree, the SOLE writer of `main` — making integration a partition
owner (the repo's existing single-writer instinct, e.g. docs owns
`ops-wiki/loops/`) and containing the O(N^2) reconciliation cost in one place.

## Context you need
Files: `engine/gate.py` (where add_lane is classified — add the conditional
provisioning verdict), `engine/loop.py` (resolves concurrency / dirty-lane state
per cycle — feed the rule), `engine/actions.py` (apply the rule when executing
add_lane → choose shared vs --worktree from T0025), `contracts/checkpoint-header.md`
(tell the brain the rule so it reasons about it). The asymmetry that justifies
the default: isolation ~200ms + a venv (bounded, front-loaded) vs shared-tree's
silent cross-lane commit-sweep (unbounded, invisible until review).

## Deliverables
- The conditional provisioning rule applied in actions/gate (concurrency=1 +
  narrow-stager + no-frozen-test → shared; else worktree). The
  unverified-non-claude → worktree default.
- The N>=3 integration-lane pattern (provision an integration lane as the sole
  main-writer; document the reconciliation flow).
- checkpoint-header guidance so the brain understands shared-vs-worktree.
- Tests: concurrency=1 stays shared (byte-identical); a 2nd concurrent
  code-writer triggers worktree; N>=3 spawns the integration lane. Before/after
  note in `ops-wiki/loops/harness-governance.md`.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (no regressions). Commit per the commit
policy (cite T0026). Do NOT reinstall; do NOT push; do NOT touch running daemons.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
Phase 5 (lane-handoff flush). Actually RUNNING >=2 concurrent code-writers in a
live session (that is an operator decision, separate from building the
machinery). If the concurrency=1 path would change behavior at all, STOP and
escalate — Phase 4 must be inert until concurrency genuinely exceeds 1.

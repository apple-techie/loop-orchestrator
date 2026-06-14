---
id: T0025
title: "Phase 4 — isolation registry field + add_lane --worktree provision/record/teardown"
status: done
accepted: 2026-06-14
gate: "456/0"
commit: e62200b
depends_on: [T0019]
scope: lib/harness-registry.sh + loop-tmux.sh (add-lane/drop-lane) + src/loop_orchestrator/engine/{actions.py,substrate.py} + CONTRACT.md + tests ONLY; ADDITIVE; default isolation=shared = today's behavior; no reinstall; no git push
loop: harness-governance
---

# T0025 — worktree isolation lifecycle (Phase 4 core)

## Objective
Phase 4 (conditional worktree isolation). NOTE: the plan defers Phase 4 until
concurrency > 1; the operator has explicitly green-lit building it NOW, ahead of
the trigger. It must therefore be ADDITIVE and DORMANT at concurrency = 1
(default `isolation: shared` = today's exact behavior — `add_lane` with no
worktree inherits PROJECT_ROOT). Build the lifecycle so the parallelism
machinery is ready when ≥2 concurrent code-writers run.

Add an `isolation ∈ {shared, worktree}` registry field (same additive pattern as
the governance/readiness fields; default `shared`; unverified non-claude harnesses
MAY declare `worktree`). Add an `add_lane --worktree` path that, when isolation
resolves to `worktree`: provisions `git worktree add .loop/worktrees/<session>/
<window>` on a new branch, sets the lane cwd there, records the branch in
`loops.<id>.branch` (so the digest + a future integration lane find it), rebuilds
the per-worktree `.venv` (worktrees do NOT carry `.venv`; `uv sync`), and on
`drop_lane` tears the worktree down — extending the existing `@loop_lane`
teardown guard so it NEVER orphans a tree. coord/ops/docs stay on PROJECT_ROOT.

## Context you need
Files: `lib/harness-registry.sh` (add `isolation` field, default shared),
`loop-tmux.sh` add-lane (~:246-346) + drop-lane (~:348-376; the `@loop_lane`
+ `--force` guard); the verified fact `loop-tmux.sh:304-313` — a no-`--repo`
add-lane inherits PROJECT_ROOT (that is the shared default to preserve). The
existing `--worktree-web` two-pane override generalizes to any dynamic lane.
`engine/actions.py` add_lane executor; `engine/substrate.py` add_lane wrapper.
GOTCHA (in the plan + memory): macOS iCloud UF_HIDDEN on `.pth` — a per-worktree
`.venv` under ~/Documents needs `chflags nohidden .venv/lib/python*/site-packages/*.pth`
before `uv run --no-sync`. `git worktree add` is ~100-200ms; `.git` objects are
shared (linked, not copied); untracked `.venv`/caches are NOT carried.

## Deliverables
- `isolation` registry field (default `shared`). `add_lane --worktree`
  provision + cwd + `loops.<id>.branch` record + per-worktree venv; `drop_lane`
  teardown that never orphans. ADDITIVE; absent/`shared` = today byte-identical.
- CONTRACT.md: the `--worktree` flag + `loops.<id>.branch` usage + the teardown
  guarantee.
- Tests: shared default unchanged; a worktree lane provisions + records the
  branch + tears down cleanly (no orphan). Before/after note in
  `ops-wiki/loops/harness-governance.md`.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (no regressions). Commit per the commit
policy (cite T0025). Do NOT reinstall; do NOT push; do NOT touch running daemons.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev pytest -q
git worktree list   # confirm a test worktree provisions + is torn down (no orphan)
git diff --stat
```

## Out of scope / escalate
The CONDITIONAL provisioning DECISION rule + the N>=3 integration lane (T0026).
If a teardown could orphan a worktree or sweep another lane's commit, STOP and
escalate. Never `git worktree remove --force` a tree with uncommitted work
without recording it first.

---
id: T0030
title: "F8 — seeder must seed jira blank and let push create+backfill, not guess a key"
status: done
accepted: 2026-06-14
gate: "487/0"
commit: 8b5ece96
depends_on: []
scope: the coordinator/engine task-SEED logic (checkpoint-header guidance + any seed helper) + src/loop_orchestrator/pm/ (jira create-path writeback) + tests ONLY; ADDITIVE; no reinstall; no git push
loop: harness-governance
---

# T0030 — F8: seed jira blank, let push create + backfill

## Objective
Hardening / PM correctness. **F8 (found live 2026-06-14 in ooLEO):** the
coordinator's task-seeding step pre-fills a GUESSED `jira:` key for work that has
no Jira issue yet (leo's Phase 2c task was seeded `jira: SCRUM-139`, never
created). A non-empty `jira:` means "this issue exists — reconcile it", so
`loop-pm` push did `GET /issue/SCRUM-139/status` → HTTP 404 → `errors:1` EVERY
cycle, and the loop could never transition the task to Done. The guessed key
violates the field's invariant.

## Required behavior (the fix)
- The seeder (coordinator seed logic + the checkpoint-header guidance that tells
  the brain how to create task files) must seed `jira:` **BLANK** for work with
  no existing issue — never a guessed/incremented key.
- `loop-pm` push's CREATE path (already creates issues for keyless tasks) must
  write back the TRUE key to the task file after creation, and link it under the
  parent epic (`--epic` / the configured epic). Verify it does this and is the
  path a blank-key task takes.
- A non-empty `jira:` continues to mean "reconcile this existing issue" (push
  must NOT invent/guess keys anywhere).

## Context you need
Files: the coordinator seed logic + `src/loop_orchestrator/engine/contracts/
checkpoint-header.md` (where the brain is told how to author tasks/T*.md), and
`src/loop_orchestrator/pm/` (the jira adapter create vs reconcile branch + key
writeback; `loop-pm sync ... push`). The bug is "task has a key it shouldn't" →
trace where seeded keys come from. leo got a project-level AGENTS.md rule for
this via mailbox; this task is the ENGINE-level root fix so it doesn't recur in
govern / any future project.

## Deliverables
- Seeder seeds `jira:` blank for issueless work; push create-path backfills the
  real key + links the epic. Tests: a blank-key task → push creates + backfills
  (no 404 reconcile); a real-key task → reconciles (unchanged); no path guesses a
  key. Before/after note in `ops-wiki/loops/harness-governance.md`.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (no regressions). Commit per the commit
policy (cite T0030 / F8). Do NOT reinstall; do NOT push.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
Re-keying existing Jira issues / leo's project AGENTS.md rule (handled
separately). If push cannot create an issue (creds/epic missing), it must leave
`jira:` blank + report — never fall back to guessing a key.

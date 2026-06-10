---
id: T0004
title: Tasks-as-files + Jira demoted to sync target
status: done
depends_on: [T0001]
scope: new tasks/ convention + lint script + sync stub; no live Jira credentials handled
---

# T0004 — Tasks-as-files; Jira becomes a sync target

## Objective
Move the source of truth for atomic work items from Jira (reached today via
MCP calls from the coordinator harness, a major source of tool-call volume and
context bloat) into git-tracked markdown task files that any single-shot agent
can execute with no chat context. Jira remains, but as a thin bidirectional
sync target, not the brain.

## Context you need
- ops-wiki and AGENTS.md exist (T0001). Log prefix convention:
  `## [YYYY-MM-DD] <type> | <title>`.
- Lanes consume work via `loop-dispatch --mode text <lane> "<payload>"`, so a
  task file's full text must stand alone as a dispatchable prompt.
- Loop state lives in `.loop/orchestrator-state.json`; a task usually maps to
  one loop or one step of a loop.

## Deliverables
1. `tasks/` convention, documented in AGENTS.md under `### Task files`:
   - `tasks/T<NNNN>-<slug>.md` open work; `tasks/archive/` done or dropped.
   - Frontmatter: id, title, status (open|in-progress|done|dropped),
     depends_on (list), loop (optional loop id), jira (optional issue key),
     scope (one line).
   - Body sections, all required: Objective, Context you need, Deliverables,
     Acceptance criteria, Verification, Out of scope. Context must be
     self-contained: a fresh agent with only this file and the repo succeeds.
   - Status<->location invariant: open/in-progress live in tasks/; done or
     dropped live in tasks/archive/.
2. `scripts/loop-task-lint.sh`: checks every tasks/**/*.md for valid filename,
   frontmatter keys, required sections, status<->location agreement, and
   depends_on ids that exist. Non-zero exit + per-file findings on violation.
3. `scripts/loop-jira-sync.sh` STUB: argument parsing and a documented contract
   only (`pull` = create/update task files from assigned Jira issues; `push` =
   update Jira status from frontmatter; conflict rule = file wins, log to
   ops-wiki/log.md as `## [date] sync | <issue>`). Body of each mode may be
   `echo "TODO: implement" >&2; exit 64` — but the help text, flags, and
   contract comments must be complete so a later task can fill it in.

## Acceptance criteria
- AGENTS.md documents the convention. tasks/ contains this very task pack
  (move the T000*.md files into tasks/ if they are not already there) and
  passes the linter.
- `bash -n` clean on both scripts; linter exits non-zero when fed a
  deliberately broken fixture (create one under /tmp during verification).

## Verification
```
bash -n scripts/loop-task-lint.sh scripts/loop-jira-sync.sh
scripts/loop-task-lint.sh && echo LINT-OK
scripts/loop-jira-sync.sh --help | grep -qi 'pull'
```

## Out of scope
Real Jira API calls, credentials, webhooks; per-client Jira mappings.

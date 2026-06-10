---
id: T0003
title: Stateless coordinator boot from compiled checkpoint
status: done
depends_on: [T0001, T0002]
scope: one new script + AGENTS.md section; no edits to existing loop-*.sh
---

# T0003 — Stateless coordinator boot (loop-checkpoint)

## Objective
Replace the long-running, context-accumulating coordinator with a fresh coord
invocation per checkpoint that boots from a constant-size compiled context:
AGENTS.md schema + ops-wiki/checkpoint.md + ops-wiki/index.md. Coordinator RAM
stays near-constant regardless of session age; its memory lives on disk.

## Context you need
- ops-wiki exists (T0001); docs lane compiles checkpoint.md on every ingest
  pass (T0002). index.md is the catalog agents read first, then open whole pages.
- Dispatching into lanes: `loop-dispatch --mode text <lane> "<payload>"`
  (paste-buffer based; --wait-ready polls loop-lane-status until idle).
  Dynamic lanes: `loop-tmux add-lane --window <w> --harness claude
  --auto-approve --wait-ready`.
- The checkpoint/critique pattern of record: Bootstrap -> Observe ->
  Checkpoint -> Critique -> Advance or stop.

## Deliverables
1. `scripts/loop-checkpoint.sh` (bash >= 3.2, mirrors existing script style:
   set -euo pipefail, usage(), --flags):
   - Assembles a checkpoint prompt from, in order: a fixed header (below),
     full text of ops-wiki/checkpoint.md, full text of ops-wiki/index.md,
     output of `scripts/loop-wiki-pending.sh`.
   - Fixed header (verbatim spirit): "You are the coordinator for one
     checkpoint cycle. Read the compiled state below. Drill into specific
     ops-wiki pages by path only if needed. Decide the single next step per
     lane or stop. Run the critique: what is unproven, what downstream state
     is only inferred, what would falsify confidence fastest. Write your
     decision and reasoning into the coord section of ops-wiki/checkpoint.md
     and into the relevant loop page. Do not implement; dispatch."
   - Modes: `--print` (emit the assembled prompt to stdout) and
     `--dispatch [lane]` (default lane `coord`; pipes through
     `loop-dispatch --mode text --wait-ready`).
   - `--header-file <path>`: substitute the fixed header with the file's
     contents. Rationale: an external engine drives the same assembly but
     needs a side-effect-free coordinator (emit a decision block instead of
     "write checkpoint.md and dispatch"); the default header stays as above.
   - Prints the assembled prompt's byte and approximate token count
     (bytes/4) so drift is visible. Warn if > 24000 tokens.
2. AGENTS.md section `### Coordinator contract`: coord is per-checkpoint and
   stateless; it never carries prior transcript; all durable reasoning is
   filed back (file-back rule from T0001); coord writes only its section of
   checkpoint.md plus loop-page decision notes.

## Acceptance criteria
- `loop-checkpoint.sh --print` produces a complete prompt whose size is
  independent of mailbox/processed history (checkpoint.md + index.md only,
  plus the pending summary).
- `--dispatch` path verified at least with `--print | head` + a dry
  explanation comment if no live tmux session exists in this environment.
- `bash -n` clean; `make check` still passes untouched.

## Verification
```
bash -n scripts/loop-checkpoint.sh
scripts/loop-checkpoint.sh --print | wc -c
scripts/loop-checkpoint.sh --print | grep -q 'critique'
make check
```

## Out of scope
Changing how coord pane is launched in loop-tmux.sh; metrics (T0006).

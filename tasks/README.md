# Compiled Coordinator — task pack for loop-orchestrator

Six self-contained task files implementing the "Compiled Coordinator" architecture
(Karpathy LLM Wiki pattern applied to orchestrator state). Each task is written so a
single-shot agent can pick it up and implement it with NO chat context.

## Order and dependencies

| Task  | Title                                  | Depends on |
|-------|----------------------------------------|------------|
| T0001 | Bootstrap ops-wiki skeleton + AGENTS.md schema | none |
| T0002 | Docs-lane ingest workflow + mailbox ack | T0001 |
| T0003 | Stateless coordinator boot (loop-checkpoint) | T0001, T0002 |
| T0004 | Tasks-as-files + Jira demoted to sync target | T0001 |
| T0005 | Nightly lint lane (batched, bias-aware, injection-aware) | T0002 |
| T0006 | Metrics + keep/discard gate in log.md | T0002, T0003 |

T0001 -> T0002 -> T0003 is the critical path. T0004 can run in parallel after T0001.

## Running these inside a Claude Code session

Option A — dedicated dynamic lane in a live loop-orchestrator session:

    # from inside the tmux session (coord lane or your own shell)
    loop-tmux add-lane --window wiki-t1 --harness claude --auto-approve --wait-ready
    loop-dispatch --mode text wiki-t1 "$(cat tasks/T0001-bootstrap-ops-wiki.md)"

    # when the lane reports done and you have verified:
    loop-tmux drop-lane --window wiki-t1

Option B — one-shot, non-interactive (Claude Code headless):

    claude -p "$(cat tasks/T0001-bootstrap-ops-wiki.md)" --dangerously-skip-permissions

Option C — interactive: open `claude` in the repo root and paste a task file.

## Rules

- One task per lane/session. Lane resets after completion (no context carryover).
- Run the task's Verification block before marking it done.
- Move finished task files to tasks/archive/ and append a line to ops-wiki/log.md
  (after T0001 exists): `## [YYYY-MM-DD] task | T000N done`.
- T0001's AGENTS.md becomes the schema of record; later tasks extend it, never fork it.

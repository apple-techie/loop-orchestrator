---
name: loop-orchestrator
description: Operate the loop-orchestrator multi-agent tmux system - boot lane sessions, dispatch/steer agent lanes, read fleet status as JSON, run the loop-engine decision cycle (approve/reject, watch daemon, self-improvement), drive the loop-deck TUI, and sync PM adapters. Use when working with loop-orchestrator, the loop-tmux/loop-dispatch/loop-lane-status/loop-engine/loop-deck/loop-pm commands, .loop/ state or ops-wiki conventions, or when orchestrating multiple coding agents across tmux lanes.
---

# loop-orchestrator

Three layers, each works without the one above: a **bash substrate** (tmux
lanes, dispatch, readiness, digest, ADR gate), a **Python engine**
(deterministic decision cycles with a swappable one-shot LLM brain), and a
**Textual deck** (interactive flight deck). The substrate surfaces you may
rely on are pinned in the repo's `CONTRACT.md`; in-session agent rules live
in the project's `AGENTS.md`.

## Quick start

```bash
# Boot a six-lane session (coord/web/infra/validate/ops/docs)
loop-tmux --project myapp --project-root ~/code/myapp --preset pi-claude --no-attach

# Fleet status as JSON (word form: loop-lane-status <session> <lane>)
loop-lane-status --json --all myapp

# Paste a prompt into a lane (at-most-once; never retry a dispatch blindly)
loop-dispatch --session myapp --mode text --wait-ready web "Audit checkout flow."

# One engine cycle: brain decides, actions queue at the human gate
loop-engine --session myapp once --approval manual
loop-engine --session myapp status          # read the pending decision
loop-engine --session myapp approve d-…     # execute + archive (or: reject d-… --reason …)

# The daemon (cycles on triggers) and the flight deck
loop-engine --session myapp watch &
loop-deck --project-root ~/code/myapp --session myapp
```

## Workflows

**Grow/shrink a running session** — `loop-tmux add-lane --window w2
--harness claude --auto-approve --wait-ready [--role impl]`, retire with
`loop-tmux drop-lane --window w2`. Dynamic lanes are first-class for
dispatch/status by window name.

**Steer a busy lane** — `loop-dispatch --interrupt <lane> "<new course>"`
(sends Escape first). To demand an answer, instruct the lane to reply via a
mailbox file `​.loop/messages/<UTC ts>-<lane>-to-coord.md` with
`subject: re:<ask-id>`; the engine matches replies and they trigger the next
cycle.

**Drive the engine** — config lives in the `engine:` key of
`lane-config.yaml` (approval mode, intervals, brain harness + budget, ingest
mode, destructive caps, PM adapters). The brain replies with a fenced
` ```decision ` block; the gate classifies actions safe / destructive /
blocked before anything executes. Details: [references/engine.md](references/engine.md).

**Self-improvement** — `loop-engine --session s improve` mines failure
clusters from the engine's own traces and files at most 3 proposals against
the declared edit surfaces; `--apply N` (1-based) applies one as a logged
experiment judged by the `loop-metrics.sh` non-regression gate. Zero mined
weaknesses → zero proposals, by design.

**PM sync** — `loop-pm sync --adapter jira pull --dry-run`. Task files under
`tasks/` are the source of truth (file wins on conflict); creds come only
from env (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`).

## Hard rules (the system's safety model — never bypass)

- **Never** pass `--force` to `drop-lane`; the dynamic-only guard is what
  protects base lanes from automation.
- **Never** run `loop-adr accept` from automation — ADR acceptance is
  human-only, and the engine blocks any payload containing it.
- **Never** target the `coord` lane with engine actions.
- Dispatch is **at-most-once**: a non-zero exit means not delivered; a blind
  retry can double-paste into a TUI composer.
- Respect write partitions (see
  [references/conventions.md](references/conventions.md)): mailbox files are
  add-only, `ops-wiki/log.md` is append-only, everything below the
  `<!-- coord-decisions -->` marker in `ops-wiki/checkpoint.md` is
  coordinator-owned.
- Only steer/dispatch-and-await-reply on lanes that actually run an agent
  harness — a shell lane will never answer (check `loop-tmux list-lanes
  --json` for the harness before asking).

## References

- [references/substrate.md](references/substrate.md) — all bash CLIs, JSON
  surfaces, file conventions, readiness taxonomy, ADR gate.
- [references/engine.md](references/engine.md) — cycle anatomy, decision
  contract, gating, watch triggers, ask/reply, improve, metrics, config keys.
- [references/conventions.md](references/conventions.md) — ops-wiki
  partitions, mailbox ack, tasks-as-files, what to tell lane agents.

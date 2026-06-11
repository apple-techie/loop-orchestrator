# Engine reference (loop-engine / loop-deck / loop-pm)

Install: `make install-python` (uv tool install; pip fallback; Python ≥ 3.10).
Session resolution everywhere: `--session S` or `$LOOP_SESSION`.

## Cycle anatomy (`loop-engine once`)

observe lanes → PM pull (if adapters configured) → ingest pending mail
(nudge the docs lane, or `ingest.mode: headless` = a one-shot agent performs
the docs-lane protocol itself) → assemble prompt via `loop-checkpoint.sh
--print --header-file <engine header>` + live lane statuses + outstanding
asks → **brain** one-shot (registry `oneshot_template` for
`engine.brain.harness`; `LOOP_ENGINE_BRAIN_CMD` overrides for tests) →
parse decision → gate → queue/execute → file the decision below the
checkpoint marker → PM push.

Exit codes: 0 = cycle completed (stdout says whether approval is needed),
3 = a decision is already pending (single in-flight invariant), 4 = brain
reply unusable after one corrective retry (a needs-human doc is filed),
5 = paused.

## The decision contract (what the brain must emit)

One fenced ` ```decision ` block, YAML: `{version: 1, critique: str,
actions: […]}` — last fence wins, ≤ 8 actions, every action needs a
`rationale`, payloads ≤ 16KB, `coord` untargetable. Action kinds:

| kind | required | optional |
|---|---|---|
| dispatch | lane, payload | mode=text, wait_ready |
| steer | lane, payload | interrupt, wait_for_idle, expects_reply, reply_timeout_s=1800 |
| add_lane | window, harness or cmd, brief | model, role, auto_approve |
| drop_lane | window | |
| stop / escalate | rationale / summary | |

## Gating

`safe` executes per mode; `destructive` (any drop_lane, steer with
interrupt, payload matching configured patterns like `rm -rf` / `git push
--force` / `reset --hard`, lane count over cap, dispatch+steer fan-out > 4
per cycle) queues unless mode is `full`; `blocked` (targets coord, or
payload invokes `loop-adr accept`) NEVER executes in any mode. Modes:
`manual` (default — everything queues), `auto` (safe executes, destructive
queues), `full` (blocked still held).

```bash
loop-engine --session S status            # pending decision + last events
loop-engine --session S approve d-… [--actions 0,2]   # partial approval supported
loop-engine --session S reject d-… --reason "…"
```

Approve executes the actions, archives to `engine/decisions/<id>.json`, and
appends the resolution below the checkpoint marker.

## The daemon (`loop-engine watch`)

Polls every `poll_interval_s` (10s default). Cycle triggers: checkpoint
interval elapsed (baseline = daemon start, so a fresh boot never burns a
brain call), new mailbox file, a lane finishing (working→idle/errored),
state-file mtime change, `loop-engine cycle-now` (the request survives
pause/pending suppression and fires once unblocked), or a matched ask reply.
Debounced by `min_cycle_interval_s`; suppressed while a decision is pending
or `paused` exists; brain calls capped by `max_calls_per_hour` (a sliding
window — expect `cycle-skip reason=budget` when poking it rapidly).
Singleton via pid file; SIGTERM stops cleanly. `pause` / `resume` gate brain
calls without stopping observation.

**Ask/reply**: a steer with `expects_reply: true` appends a footer telling
the lane to reply via mailbox with `subject: re:<decision_id>-<idx>`. The
engine records the ask (`engine/asks.json`), matches replies by peeking
frontmatter only, fires a cycle on reply, and emits `reply-timeout` events
past the deadline. Only ask lanes that run a real agent harness.

## Self-improvement (`loop-engine improve`)

Adapted from Self-Harness (arXiv:2606.09498), human-gated: mines failure
clusters from events.jsonl / rejected decisions / action failures / lane
giveups / ask timeouts → brain proposes ≤ 3 minimal edits targeting ONE
mined signature each, restricted to the declared surfaces: the engine
checkpoint header (full replacement), AGENTS.md (append-only `#### experiment:`
subsection), or engine-config (recommendation text, never auto-applied).
`proposals: []` is a valid honest outcome. Proposals land in
`engine/proposals/`; `--apply N` (1-based) applies one, logs the experiment
to the wiki, and the T0006 gate decides keep/revert: after ≥ 3 cycles,
`scripts/loop-metrics.sh` numbers must not regress.

## Config (`engine:` key in lane-config.yaml — invisible to bash)

```yaml
engine:
  approval_mode: manual          # manual | auto | full
  poll_interval_s: 10
  min_cycle_interval_s: 120
  checkpoint_interval_s: 900
  brain: {harness: claude, model: "", timeout_s: 300, max_retries: 1,
          max_calls_per_hour: 12, extra_args: []}
  ingest: {mode: lane, lane: docs, timeout_s: 600}   # or mode: headless
  destructive: {max_dispatches_per_cycle: 4, max_lanes: 12,
                payload_patterns: ["git push --force", "rm -rf", "reset --hard"]}
  metrics: {log_after_cycle: false}
  lint: {enabled: false, interval_h: 24}
  pm: {adapters: []}             # e.g. [jira]
```

Host overrides merge from `lane-config.<short-hostname>.yaml` (top-level
engine keys replace wholesale).

## loop-deck (Textual TUI)

`loop-deck --project-root P --session S` (or `--check` for a no-TTY smoke).
Runs standalone or in the coord pane (`--coord-cmd 'loop-deck …'`). Strict
non-writer: every mutation shells the audited CLIs. Keymap: `y`/`N` approve
or reject the pending decision · `s` steer · `d` dispatch · `n` add-lane ·
`x` drop-lane (typed-name confirm for base lanes) · `g` jump-to-tmux ·
`c` checkpoint now · `p` pause/resume · `a` ADRs (`A` accept = the human
gate) · `e` events · `enter` lane detail (live pane tail) · `q` quit.
Engine off → read-only OBSERVE MODE dashboard.

## loop-pm (PM adapters)

```bash
loop-pm list-adapters
loop-pm sync --adapter jira (pull|push|both) [--dry-run] [--tasks-dir D] [--project-root P]
```

Adapters discovered via the `loop_orchestrator.pm_adapters` entry-point
group. `tasks/` files win every conflict (divergence is logged to the wiki,
file untouched). Jira reference adapter: REST v3, env-only creds
(`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`); status mapping open↔To Do,
in-progress↔In Progress, done↔Done. Exit 64 = adapter unknown/unavailable.
Zero adapters = the engine's PM steps are no-ops.

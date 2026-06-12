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
| steer | lane, payload | mode=text, interrupt, wait_for_idle, expects_reply, reply_timeout_s=1800 |
| add_lane | window, harness or cmd, brief | model, role, auto_approve |
| drop_lane | window | |
| stop / escalate | rationale / summary | |

## Gating

Classification is on action **shape**, not a text blocklist — a regex over a
free-text payload can never enumerate every shell-injection vector, so any
action that runs a raw command is destructive by shape. `safe` executes per
mode; `destructive` queues unless mode is `full`; `blocked` (targets coord,
or payload invokes `loop-adr accept`) NEVER executes in any mode.

`destructive` covers: any `drop_lane`; `steer` with `interrupt`; a
`dispatch`/`steer` with `mode: command` (it injects a raw shell command);
an `add_lane` carrying a raw `cmd` (it spawns an arbitrary process);
`add_lane` over the lane cap; dispatch+steer fan-out > 4 per cycle; and —
as an *additional* trigger for text-mode dispatch/steer only — a payload
matching the configured patterns (`rm -rf` / `git push --force` / `reset
--hard`). The pattern list is a cheap extra catch, never the primary
boundary.

**Limitation (honest):** the gate is mode-based, not harness-based. It does
not carry the per-lane harness, so it cannot tell whether `command` mode is
even meaningful for a given lane; it conservatively treats *all* command-mode
dispatch/steer as destructive and all text-mode as safe. This is a deliberate
over-approximation. The `model` field of `add_lane` is additionally pinned at
parse time to `^[A-Za-z0-9._:/-]+$` (no shell metacharacters), since it is
interpolated into the harness command line; `harness` is registry-validated
downstream.

Modes: `manual` (default — everything queues), `auto` (safe executes,
destructive queues), `full` (blocked still held).

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

Adapted from Self-Harness (arXiv:2606.09498), human-gated: mines weakness
clusters from events.jsonl / rejected decisions / action failures / lane
giveups / ask timeouts → brain proposes ≤ 3 minimal edits targeting ONE
mined signature each, restricted to the declared surfaces: the engine
checkpoint header (full replacement), AGENTS.md (append-only `#### experiment:`
subsection), or engine-config (recommendation text, never auto-applied).
`proposals: []` is a valid honest outcome. Proposals land in
`engine/proposals/`; `--apply N` (1-based) applies one, logs the experiment
to the wiki, and the T0006 gate decides keep/revert: after ≥ 3 cycles,
`scripts/loop-metrics.sh` numbers must not regress.

**Learning from human interventions (not just internal failures).** Three
extra signals widen the miner beyond terminal failures:

- `human:unsolicited-steer` (surface `checkpoint-header`, **highest leverage**)
  — scans the mailbox dir + its `processed/` subdir for `<ts>-<from>-to-<to>.md`
  messages where `to==coord`, `from!=coord`, and the subject does NOT start
  with `re:`. An unsolicited steer of the coordinator is a human doing the
  coordinator's job because it failed to act autonomously — the richest "you
  should have acted and did not" signal. Deduped by basename across the two
  dirs, windowed by the filename UTC stamp. The proposal teaches the
  coordinator to self-discover that next-step class.
- `latency:regression` (surface `checkpoint-header`) — pairs each `brain-call`
  with the next `decision`, times the gap, and (≥ 6 samples) flags when the
  last third's mean is ≥ 2× the first third's, or the max is ≥ 3× the median.
  Catches the slow-but-succeeded drift (a 175 KB-checkpoint 290 s spike) before
  it becomes a terminal timeout.
- `crash:<component>` (surface **`none`** = report-only) — mines `crash` events
  from events.jsonl plus lines from the deck-owned `engine/deck-crash.log`. A
  crash needs a code fix outside the three editable surfaces, so it is
  surfaced as a recommendation, never auto-applied. The watch cycle emits a
  `crash` event on any unhandled cycle exception; the deck installs a Textual
  exception hook that appends to `deck-crash.log` (a plain diagnostic log — NOT
  engine STATE, so the deck's non-writer invariant over decisions/snapshot/wiki
  holds).

`failure_kind` (`quota` | `timeout` | `exit`) — classified from the brain's
stderr tail — is folded into the `brain-failed` signature, so a quota lockout
(`brain:brain-failed:quota`) is never pooled with a slow generation
(`brain:brain-failed:timeout`). The `none` surface mirrors how engine-config
recommendations are surfaced (printed for a human, marked
`applied-manually-required`), but is never applicable at all.

## Operations (`restart`, quota backoff, stale-daemon guard)

- **`loop-engine restart [--timeout S]`** — the singleton-safe restart. Reads
  the pid file; if a daemon is alive, SIGTERMs it and polls until exit (default
  60 s). If it does NOT exit in time, it reports and **does not start a second
  instance** (confirm-dead-before-start — the singleton trap). Once exited (or
  none was running) it starts `watch` exactly as the `watch` subcommand does.
  Emits `watch-stop-requested` / `watch-stopped` / `watch-stop-timeout`.
- **Quota-aware backoff** — when a brain failure is `failure_kind=="quota"`,
  the watch loop does NOT burn retries into the wall: it computes a backoff
  deadline (parsed from a `resets <time>` hint in the stderr excerpt, else
  `brain.quota_backoff_minutes`, default 60) and suppresses brain calls until
  then, emitting `cycle-skip reason=quota-backoff`. Observation and PM steps
  keep running; only the brain is gated. The backoff clears once the deadline
  passes (`quota-backoff-cleared`).
- **Stale-daemon guard** — `watch` records the on-disk mtime of the loaded
  `gate.py` (plus pid + start time) into `engine/daemon-build.json` at boot.
  `status` compares the current module mtime to the recorded one; if the
  on-disk file is newer, it prints `daemon is running stale code (installed
  <t2> > daemon start <t1>) — run: loop-engine restart` and emits
  `daemon-stale`. This is the stale-after-reinstall bite made visible.

## Config (`engine:` key in lane-config.yaml — invisible to bash)

```yaml
engine:
  approval_mode: manual          # manual | auto | full
  poll_interval_s: 10
  min_cycle_interval_s: 120
  checkpoint_interval_s: 900
  brain: {harness: claude, model: "", timeout_s: 300, max_retries: 1,
          max_calls_per_hour: 12, extra_args: [], stream: false,
          quota_backoff_minutes: 60}   # backoff when failure_kind==quota
  ingest: {mode: lane, lane: docs, timeout_s: 600}   # or mode: headless
  destructive: {max_dispatches_per_cycle: 4, max_lanes: 12,
                payload_patterns: ["git push --force", "rm -rf", "reset --hard"]}
  metrics: {log_after_cycle: false}
  lint: {enabled: false, interval_h: 24}
  pm: {adapters: []}             # e.g. [jira]
```

`brain.stream: true` streams claude token events into the live response
transcript (raw JSONL kept in a sibling `.stream.jsonl`); the deck's `b`
panel watches it. Host overrides merge from
`lane-config.<short-hostname>.yaml` (top-level engine keys replace
wholesale).

## loop-deck (Textual TUI)

`loop-deck --project-root P --session S` (or `--check` for a no-TTY smoke).
Runs standalone or in the coord pane (`--coord-cmd 'loop-deck …'`). Strict
non-writer: every mutation shells the audited CLIs. Keymap: `y`/`N` approve
or reject the pending decision · `s` steer · `d` dispatch · `n` add-lane ·
`x` drop-lane (typed-name confirm for base lanes) · `g` jump-to-tmux ·
`c` checkpoint now · `p` pause/resume · `a` ADRs (`A` accept = the human
gate) · `e` events · `b` brain activity (live one-shot transcript tail) ·
`enter` lane detail (live pane tail) · `q` quit.
Engine off → read-only OBSERVE MODE dashboard.

## loop-pm (PM adapters)

```bash
loop-pm list-adapters
loop-pm sync --adapter jira (pull|push|both) [--dry-run] [--tasks-dir D] [--project-root P]
                            [--project K] [--epic KEY] [--sprint ID|active] [--board B]
loop-pm jira ensure-epic --name N [--project K]          # prints epic key (found or created)
loop-pm jira sprint-status [--board B]                   # active sprint id/name | 'no active sprint'
loop-pm jira move-to-sprint (--sprint ID | --active) KEY...
loop-pm jira start-sprint (--sprint ID | --next | --create NAME) [--board B]
                          [--duration-days N] [--goal TEXT]
loop-pm jira complete-sprint (--sprint ID | --active) [--board B]
loop-pm jira retro --epic KEY [--title T] (--body-file F | --body TEXT) [--as-issue]
```

Adapters discovered via the `loop_orchestrator.pm_adapters` entry-point
group. `tasks/` files win every conflict (divergence is logged to the wiki,
file untouched). Jira reference adapter: REST v3 + Agile 1.0, env-only creds
(`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`; optional `JIRA_PROJECT_KEY`
for creation/search and `JIRA_BOARD_ID` for sprints — flags override); status
mapping open↔To Do, in-progress↔In Progress, done↔Done. `sync push` also
creates issues for open/in-progress tasks without a `jira:` key (in
`JIRA_PROJECT_KEY`, under `--epic` if given), writes the new key back into
the task frontmatter and logs `sync | <key> created from <task-id>` to the
wiki. `start-sprint` activates a sprint (start=now UTC, end after
`--duration-days`, optional `--goal`; `--next` = earliest future sprint,
`--create NAME` makes one first). `complete-sprint` refuses non-active
sprints and notes that Jira moves incomplete issues back to the backlog —
ceremony decisions stay human, but the verbs make the cadence
operator-invokable. `retro` posts an ADF comment on the epic; `--as-issue`
creates a Task labeled `retrospective` instead. Epic links use the
team-managed `parent` field — company-managed projects reject it, the
issue is then created
unlinked with a warning (per-site epic-link customfield ids are never
guessed). Exit 64 = adapter unknown/unavailable or required env missing
(creds always; `JIRA_PROJECT_KEY`/`JIRA_BOARD_ID` only for verbs that need
them), 1 = API errors (response error messages surfaced). Zero adapters =
the engine's PM steps are no-ops.

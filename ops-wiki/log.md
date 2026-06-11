# ops-wiki log

Append-only. Entries start `## [YYYY-MM-DD] <type> | <title>`. Never edit or reorder past entries.

## [2026-06-10] init | ops-wiki bootstrapped
Skeleton created per tasks/T0001-bootstrap-ops-wiki.md: index, log, checkpoint,
lane pages (coord, web, infra, validate, ops, docs), empty loops/ and decisions/.
AGENTS.md schema of record added at repo root.

## [2026-06-10] task | T0001 done
ops-wiki bootstrapped and verified in a Claude Code session; T0001 archived.

## [2026-06-10] schema | checkpoint.md ownership amended
T0002: docs lane now compiles checkpoint.md (compilation is docs' job); coord
owns only the decision-notes section below the `<!-- coord-decisions -->`
marker line. Docs recompiles must preserve the marker and everything below it
byte-for-byte. See AGENTS.md "Ingest protocol".

## [2026-06-10] ingest | 20260610-130500-web-to-docs.md
T0002 acceptance fixture. Created loops/loop-t0002-test.md (status implement,
branch feature/t0002-test), added index entry. Moved to processed/.

## [2026-06-10] ingest | 20260610-131200-validate-to-docs.md
T0002 acceptance fixture. loops/loop-t0002-test.md updated: validation green,
status implement -> verify. Moved to processed/.

## [2026-06-10] ingest | state sync
Diffed orchestrator-state.json loops against loop pages: loop-t0002-test is
verify (updated_at 2026-06-10T13:15:00Z, updated_by validate), matching the
page after the mailbox pass. No reconciliation needed; state citation added
to the page.

## [2026-06-10] ingest | test artifacts removed
T0002 acceptance pass verified; fixture retired: loops/loop-t0002-test.md
deleted, index entry reverted, fabricated loop removed from
orchestrator-state.json, checkpoint.md recompiled (coord-decisions section
preserved byte-for-byte). The two processed test messages stay in
.loop/messages/processed/ as worked examples.

## [2026-06-10] task | T0002 done
Ingest protocol + processed/ ack convention in AGENTS.md, coord-decisions
marker in checkpoint.md, scripts/loop-wiki-pending.sh added; acceptance ingest
pass run on 2 fixture messages and verified; T0002 archived.

## [2026-06-10] schema | coordinator contract added
T0003: AGENTS.md gains "### Coordinator contract" — coord is per-checkpoint
and stateless, boots from the constant-size compiled context assembled by
scripts/loop-checkpoint.sh, files all durable reasoning back, and writes only
its coord-decisions section of checkpoint.md plus appended decision notes on
loop pages (amending the docs-only ops-wiki/loops/ partition for appends).

## [2026-06-10] task | T0003 done
scripts/loop-checkpoint.sh added (--print / --dispatch [lane] / --header-file,
constant-size coord boot prompt, bytes+~tokens on stderr, 24k-token warning)
plus AGENTS.md "Coordinator contract"; verified and archived.

## [2026-06-10] schema | task-files convention added
T0004: AGENTS.md gains "### Task files" — tasks/T<NNNN>-<slug>.md is the
source of truth for atomic work items (Jira demoted to a sync target via
scripts/loop-jira-sync.sh, file wins on conflict); frontmatter, required
body sections, status<->location invariant, and acyclic depends_on enforced
by scripts/loop-task-lint.sh.

## [2026-06-10] task | T0004 done
scripts/loop-task-lint.sh (lints tasks/ + tasks/archive/, per-file findings,
non-zero exit) and scripts/loop-jira-sync.sh stub (pull/push/both contract,
exit 64) added; AGENTS.md "### Task files" appended; verified against the
live task pack and a broken /tmp fixture; T0004 archived.

## [2026-06-10] schema | substrate contract v1 (CONTRACT.md)
Machine-readable surfaces added additively: loop-lane-status --json/--all/
--print-target, loop-digest --json, loop-tmux list-lanes --json,
loop-dispatch --interrupt, harness-registry oneshot_template + oneshot verb.
All pre-existing outputs byte-identical (regression-diffed). scripts/ folded
into make check.

## [2026-06-10] schema | loop-engine landed (Python layer)
Deterministic engine over the substrate: stateless brain checkpoints via
oneshot templates, decision contract v1 (fenced decision YAML), gate
safe/destructive/blocked, pending-decision CAS + human approve/reject CLI,
events.jsonl audit. Live demo gate passed on a real tmux session. Engine
state lives in .loop/sessions/<s>/engine/; coord decisions file below the
checkpoint.md marker.

## [2026-06-10] schema | loop-deck landed (Textual flight deck)
Interactive non-writer deck: fleet/loops/mailbox/decision-queue/ADR views,
approve/reject, steer/add/drop lanes, jump-to-tmux. 132 tests green across
the Python layer. README + CONTRACT.md updated.

## [2026-06-10] schema | lint protocol added
T0005: AGENTS.md gains "### Lint protocol" — batched (5 pages/run-batch),
shuffled-per-run wiki lint with a persistent gitignored scratchpad
(ops-wiki/.lint-scratchpad.md), five finding headings (CONTRADICTION / STALE /
ORPHAN / MISSING-LINK / SUSPECT-INSTRUCTION), auto-fix limited to
MISSING-LINK/ORPHAN, CONTRADICTION + SUSPECT-INSTRUCTION queued in the
checkpoint.md lint review queue above the coord-decisions marker, and a
documented (not installed) nightly crontab line. Prompt assembled/dispatched
by scripts/loop-wiki-lint.sh.

## [2026-06-10] lint | 9 pages, 2 findings
Trial run per T0005 against the seeded wiki (shuffled order, 2 batches of <=5,
scratchpad at ops-wiki/.lint-scratchpad.md). Two CONTRADICTION findings queued
in the checkpoint.md lint review queue (lanes/coord.md and lanes/docs.md
sole-writer claims vs the T0002/T0003 partition amendments in log.md); no
STALE, ORPHAN, MISSING-LINK, or SUSPECT-INSTRUCTION findings.

## [2026-06-10] task | T0005 done
scripts/loop-wiki-lint.sh added (--print / --dispatch [--lane <name>],
shuffled-per-run page list, protocol header + scratchpad in the prompt,
dynamic lint window left for operator drop-lane) plus AGENTS.md
"### Lint protocol"; scratchpad gitignore entry verified; trial lint run
produced the lint log entry and 2 review-queue items; T0005 archived.

## [2026-06-10] schema | experiment protocol added
T0006: AGENTS.md gains "### Experiment protocol" — every change to AGENTS.md
rules, the ingest protocol, or checkpoint assembly is an experiment logged
`## [date] experiment | <change>`, run for >= 3 checkpoint cycles, kept only
if checkpoint_tokens and restarts have not regressed and pending_messages
does not trend up (else reverted + logged). Experiments may modify only
AGENTS.md protocol sections, the engine checkpoint header, and the engine:
config section — never the substrate scripts. Numbers come from
scripts/loop-metrics.sh (--log appends a metrics entry here).

## [2026-06-10] metrics | tokens=1022 pending=0 restarts24h=0 giveups24h=0 ingests7d=4 lints7d=1 checkpoints7d=0 experiments=0

## [2026-06-10] task | T0006 done
scripts/loop-metrics.sh added (checkpoint_tokens via loop-checkpoint --print
bytes/4, pending via loop-wiki-pending --quiet, restarts_24h/giveups_24h from
lane-restarts.jsonl with the no-event-field restart convention, 7d log.md
prefix counts, python3 date math for BSD/GNU portability, missing inputs
degrade to 0/n-a with notes, --log appends one metrics entry) plus AGENTS.md
"### Experiment protocol" with the 3-cycle keep/discard gate; verified
against the live repo and a fixture jsonl; T0006 archived.

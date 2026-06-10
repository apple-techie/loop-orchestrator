---
id: T0002
title: Docs-lane ingest workflow + mailbox ack convention
status: done
depends_on: [T0001]
scope: ops-wiki content rules + one new helper script; no edits to existing loop-*.sh
---

# T0002 — Docs-lane ingest workflow + mailbox ack convention

## Objective
Make the docs lane a disciplined wiki compiler: every mailbox message and every
orchestrator-state change gets ingested into ops-wiki exactly once, with an ack
convention so the digest of pending work is real. This kills knowledge
evaporation between checkpoints.

## Context you need
- ops-wiki/ and AGENTS.md exist per T0001 (three layers, write partitions,
  greppable log prefix `## [YYYY-MM-DD] <type> | <title>`).
- Mailbox: `.loop/messages/YYYYMMDD-HHMMSS-<from>-to-<to>.md`, `subject:` in
  frontmatter. Currently messages are never marked processed; the live digest
  (loop-digest.sh) just shows the newest 4 by mtime.
- State: `.loop/orchestrator-state.json` (schema v2, `loops.<id>` with
  status/branch/artifacts/updated_at/updated_by).

## Deliverables
1. **Ack convention**: create `.loop/messages/processed/`. After the docs lane
   ingests a message it MOVES the file there (filename unchanged). Pending work
   = files still in `.loop/messages/`. Document this in AGENTS.md under
   `### Ingest protocol`.
2. **Ingest procedure** appended to AGENTS.md, exactly this loop:
   a. List unprocessed messages oldest-first.
   b. For each: read it; update `ops-wiki/loops/<loop>.md` (or the relevant
      lane page if loop-unrelated); flag contradictions with existing wiki
      claims inline as `> CONFLICT: <claim A> vs <claim B> (sources)`;
      update index.md if a page was created; append
      `## [date] ingest | <mailbox filename>` to log.md; move the file to
      processed/.
   c. Then diff `loops` in orchestrator-state.json against the loop pages'
      recorded status; reconcile pages and log `## [date] ingest | state sync`.
   d. Finally rewrite `ops-wiki/checkpoint.md` (coord's boot page is compiled
      HERE, by docs, since docs owns compilation; coord owns only its decision
      notes section at the bottom of checkpoint.md — adjust the T0001
      partition rule accordingly and note the amendment in log.md).
      The coord section is delimited by a `<!-- coord-decisions -->` marker
      line: the recompile MUST preserve, byte-for-byte, the marker and
      everything below it. This makes docs recompiles and coord appends
      commute (an external coordinator appends decisions below the marker).
3. **Helper script** `scripts/loop-wiki-pending.sh` (bash >= 3.2, self-contained,
   mirrors the style of the existing loop-*.sh scripts): prints unprocessed
   mailbox files oldest-first, count of processed, and the last 5 log.md
   entries (`grep "^## \[" ops-wiki/log.md | tail -5`). `--quiet` prints only
   the pending count (for use in prompts and cron).

## Acceptance criteria
- AGENTS.md contains the ingest protocol and the amended checkpoint ownership.
- Running an ingest pass on 2+ fabricated test messages (create them, ingest,
  then delete the test artifacts OR leave them clearly marked as examples in
  processed/) produces: updated loop page, index entry, two log entries, files
  moved to processed/.
- `scripts/loop-wiki-pending.sh` passes `bash -n` and produces correct counts.

## Verification
```
bash -n scripts/loop-wiki-pending.sh
scripts/loop-wiki-pending.sh --quiet      # integer
grep -q 'Ingest protocol' AGENTS.md
ls .loop/messages/processed/ | head
```

## Out of scope
Lint passes (T0005), coordinator boot (T0003), any change to loop-digest.sh.

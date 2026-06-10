# ops-wiki index

Read this file first. Find the page you need, then open it whole. No chunk retrieval.

## Core
- [checkpoint.md](checkpoint.md) — current compiled state + decision needed; coord's boot page
- [log.md](log.md) — append-only chronological record; entries `## [YYYY-MM-DD] <type> | <title>`

## Lanes
- [lanes/coord.md](lanes/coord.md) — coordinator / checkpoint lane
- [lanes/web.md](lanes/web.md) — primary-repo implementation lane
- [lanes/infra.md](lanes/infra.md) — secondary-repo implementation lane
- [lanes/validate.md](lanes/validate.md) — proving lane
- [lanes/ops.md](lanes/ops.md) — downstream-truth lane
- [lanes/docs.md](lanes/docs.md) — synthesis / wiki-compiler lane

## Loops
(none yet — one page per loop id, compiled from `.loop/orchestrator-state.json` + mailbox)

## Decisions
(none yet — one stub per ADR, linking to `docs/adr/NNNN-*.md`)

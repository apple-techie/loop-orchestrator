# checkpoint

Coord boots from this page. Compiled by the docs lane; coord writes only the
section below the coord-decisions marker line near the bottom (docs recompiles
preserve the marker and everything below it byte-for-byte — see AGENTS.md
"Ingest protocol"). Keep this page small — drill into loop/lane pages by path.

## Current objective
(none — no active loops)

## Loop states
(none — `.loop/orchestrator-state.json` has no loops)

## Open conflicts
(none)

## Last compiled
2026-06-10 — recompile after T0002 test-artifact removal (fixture loop
loop-t0002-test retired; processed test messages remain in
.loop/messages/processed/ as examples)

<!-- coord-decisions -->
## Decision needed
(none)
### [2026-06-10T22:18:00Z] decision d-20260610-221800 (pending)

fleet is healthy; web should prove the dispatch path end-to-end

0. dispatch web [safe/awaiting-approval]: demo
### [2026-06-10T22:18:23Z] decision d-20260610-221800 (approved)

fleet is healthy; web should prove the dispatch path end-to-end

0. dispatch web [safe/executed]: demo
### [2026-06-10T22:19:11Z] decision d-20260610-221911 (needs-human)

brain reply unusable after corrective re-prompt: no ```decision fence found in the reply — respond with exactly one fenced block whose info-string is 'decision' and whose body is YAML {version: 1, critique: ..., actions: [...]}

### [2026-06-10T22:19:24Z] decision d-20260610-221911 (rejected)

brain reply unusable after corrective re-prompt: no ```decision fence found in the reply — respond with exactly one fenced block whose info-string is 'decision' and whose body is YAML {version: 1, critique: ..., actions: [...]}


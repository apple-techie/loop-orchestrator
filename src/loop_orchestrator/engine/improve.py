"""Self-harness improve loop (P7): mine weaknesses -> propose edits -> apply.

Adapted from the Self-Harness loop (arXiv 2606.09498) to this repo's gates:
mining and proposing are automated; PROMOTION stays human-gated through the
AGENTS.md '### Experiment protocol' (T0006) — an applied edit is an
experiment, evaluated after >= 3 cycles against loop-metrics, kept only if
the numbers do not regress.

Mining is pure Python over engine-owned state (events.jsonl, the decisions/
archive, lane-restarts.jsonl, ops-wiki/log.md metrics lines) — no LLM. The
proposal step reuses the brain one-shot path (LOOP_ENGINE_BRAIN_CMD honored,
cost guard applies) and parses a last-fence-wins ```proposals block, exactly
like decision.py parses ```decision. Proposals are FILED, never auto-applied;
`loop-engine improve --apply N` applies only the declared edit surfaces
(checkpoint header, AGENTS.md append) — engine-config proposals stay
recommendation text for a human.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

import yaml

from ..paths import SessionPaths
from ..pm.taskfiles import append_log
from ..substrate import Substrate, SubstrateError
from .brain import Brain
from .config import EngineConfig
from .events import EventLog, parse_ts, utc_now

SURFACES = ("checkpoint-header", "agents-md-append", "engine-config")
MAX_SAMPLES = 3

_HEADER_RESOURCE = ("contracts", "checkpoint-header.md")
_FENCE_RE = re.compile(r"```proposals[ \t]*\n(.*?)```", re.DOTALL)
_PROPOSAL_NAME_RE = re.compile(r"^(\d{8}-\d{6})-(\d+)\.md$")
_METRICS_LINE_RE = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})\] metrics \|.*\bpending=(\d+)\b")
_RESTART_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

T0006_REMINDER = (
    "T0006 reminder: this edit is an experiment. Evaluate after >= 3 checkpoint "
    "cycles; keep it only if loop-metrics does not regress (checkpoint_tokens, "
    "restarts, pending_messages). To revert, undo the edit and append "
    "'## [YYYY-MM-DD] experiment | reverted: <title>' to ops-wiki/log.md."
)


class ImproveError(Exception):
    """Mining/proposal/apply failure with a human-readable message."""


# ── weakness mining (pure, no LLM) ──────────────────────────────────────────


def _events_since(paths: SessionPaths, cutoff: datetime) -> list[dict]:
    try:
        lines = paths.events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            ts = parse_ts(obj["ts"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if isinstance(obj, dict) and ts >= cutoff:
            out.append(obj)
    return out


def _cluster(signature: str, count: int, samples: list[str], surface: str) -> dict:
    return {
        "signature": signature,
        "count": count,
        "samples": samples[:MAX_SAMPLES],
        "inferred_surface": surface,
    }


def _brain_clusters(events: list[dict]) -> list[dict]:
    """brain-failed / brain-retry / decision-parse-error, with the raw-response
    transcript path of the nearest preceding brain-call as evidence."""
    buckets: dict[str, list[str]] = {}
    last_response = ""
    for event in events:
        kind = event.get("event")
        if kind == "brain-call":
            last_response = str(event.get("response_path") or "")
            continue
        if kind not in ("brain-failed", "brain-retry", "decision-parse-error"):
            continue
        sample = str(event.get("error") or "")
        if last_response:
            sample = f"{sample} (raw response: {last_response})".strip()
        buckets.setdefault(kind, []).append(sample)
    return [
        _cluster(f"brain:{kind}", len(samples), samples, "checkpoint-header")
        for kind, samples in sorted(buckets.items())
    ]


def _rejected_decision_clusters(paths: SessionPaths, cutoff: datetime) -> list[dict]:
    samples: list[str] = []
    count = 0
    for path in sorted(paths.decisions_dir.glob("*.json")) if paths.decisions_dir.is_dir() else []:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict) or doc.get("status") != "rejected":
            continue
        try:
            stamp = parse_ts(doc.get("decided_at") or doc.get("created_at") or "")
        except (TypeError, ValueError):
            continue
        if stamp < cutoff:
            continue
        count += 1
        kinds = ",".join(a.get("kind", "?") for a in doc.get("actions") or []) or "(none)"
        samples.append(f"{doc.get('id')} reason={doc.get('reason') or '(none)'} kinds={kinds}")
    if not count:
        return []
    return [_cluster("decisions:rejected", count, samples, "checkpoint-header")]


def _action_failure_clusters(events: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str], list[str]] = {}
    for event in events:
        if event.get("event") != "action-failed":
            continue
        key = (str(event.get("lane") or "-"), str(event.get("kind") or "?"))
        buckets.setdefault(key, []).append(str(event.get("error") or ""))
    return [
        _cluster(f"action-failed:{lane}:{kind}", len(samples), samples, "engine-config")
        for (lane, kind), samples in sorted(buckets.items())
    ]


def _lane_instability_clusters(paths: SessionPaths, cutoff: datetime) -> list[dict]:
    """lane-restarts.jsonl: missing `event` field = restart, giving-up = giveup
    (CONTRACT.md convention), bucketed by lane."""
    try:
        lines = paths.lane_restarts.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    buckets: dict[str, dict] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            stamp = datetime.strptime(rec.get("timestamp", ""), _RESTART_TS_FORMAT).replace(
                tzinfo=timezone.utc
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(rec, dict) or stamp < cutoff:
            continue
        if "event" in rec and rec.get("event") != "giving-up":
            continue
        lane = str(rec.get("lane") or "-")
        bucket = buckets.setdefault(lane, {"restarts": 0, "giveups": 0, "samples": []})
        bucket["giveups" if "event" in rec else "restarts"] += 1
        if len(bucket["samples"]) < MAX_SAMPLES:
            bucket["samples"].append(line)
    out: list[dict] = []
    for lane, bucket in sorted(buckets.items()):
        cluster = _cluster(
            f"lane-instability:{lane}",
            bucket["restarts"] + bucket["giveups"],
            bucket["samples"],
            "engine-config",
        )
        cluster["restarts"] = bucket["restarts"]
        cluster["giveups"] = bucket["giveups"]
        out.append(cluster)
    return out


def _ingest_clusters(events: list[dict], paths: SessionPaths, cutoff: datetime) -> list[dict]:
    out: list[dict] = []
    timeouts = [e for e in events if e.get("event") == "ingest-timeout"]
    if timeouts:
        samples = [f"ts={e.get('ts')} timeout_s={e.get('timeout_s')}" for e in timeouts]
        out.append(_cluster("ingest:timeout", len(timeouts), samples, "agents-md-append"))
    trend = _pending_trend(paths.ops_wiki / "log.md", cutoff)
    if trend is not None:
        first_line, last_line, delta = trend
        out.append(
            _cluster("ingest:pending-trend", delta, [first_line, last_line], "agents-md-append")
        )
    return out


def _pending_trend(log_md: Path, cutoff: datetime) -> tuple[str, str, int] | None:
    """(first metrics line, last metrics line, pending increase) when the
    `pending=` numbers in log.md metrics entries trend up inside the window."""
    try:
        lines = log_md.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    points: list[tuple[str, int]] = []
    for line in lines:
        match = _METRICS_LINE_RE.match(line)
        if not match or match.group(1) < cutoff.strftime("%Y-%m-%d"):
            continue
        points.append((line, int(match.group(2))))
    if len(points) < 2 or points[-1][1] <= points[0][1]:
        return None
    return points[0][0], points[-1][0], points[-1][1] - points[0][1]


def _ask_timeout_clusters(events: list[dict]) -> list[dict]:
    timeouts = [e for e in events if e.get("event") == "reply-timeout"]
    if not timeouts:
        return []
    samples = [f"ask={e.get('ask')} lane={e.get('lane')}" for e in timeouts]
    return [_cluster("asks:reply-timeout", len(timeouts), samples, "agents-md-append")]


def mine(paths: SessionPaths, window_days: int = 7) -> dict:
    """Evidence bundle: weakness clusters mined from the last `window_days`."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    events = _events_since(paths, cutoff)
    clusters = (
        _brain_clusters(events)
        + _rejected_decision_clusters(paths, cutoff)
        + _action_failure_clusters(events)
        + _lane_instability_clusters(paths, cutoff)
        + _ingest_clusters(events, paths, cutoff)
        + _ask_timeout_clusters(events)
    )
    return {
        "window_days": window_days,
        "generated_at": utc_now(),
        "clusters": [c for c in clusters if c["count"] > 0],
    }


# ── proposal: prompt assembly + parsing ─────────────────────────────────────


def _header_resource_text() -> str:
    resource = resources.files("loop_orchestrator.engine").joinpath(*_HEADER_RESOURCE)
    with resources.as_file(resource) as header:
        return Path(header).read_text(encoding="utf-8")


def _agents_section_titles(project_root: Path) -> list[str]:
    try:
        text = (project_root / "AGENTS.md").read_text(encoding="utf-8")
    except OSError:
        return []
    return [line[4:].strip() for line in text.splitlines() if line.startswith("### ")]


def build_prompt(paths: SessionPaths, config: EngineConfig, evidence: dict, k: int) -> str:
    """Evidence bundle + the CURRENT declared edit surfaces + strict reply
    instructions (one ```proposals fence, YAML, <= k minimal edits)."""
    titles = "\n".join(f"- {t}" for t in _agents_section_titles(paths.project_root)) or "(none)"
    parts = [
        "You are the self-harness reviewer for this orchestration engine. Below is a",
        "mined evidence bundle of recent weaknesses and the CURRENT declared edit",
        f"surfaces. Propose AT MOST {k} minimal, independent edits. Each proposal",
        "must target exactly ONE mined cluster signature.",
        "",
        "Reply with EXACTLY ONE fenced code block whose info-string is `proposals`,",
        "and NOTHING else after it. The body is YAML:",
        "",
        "```proposals",
        "version: 1",
        "proposals:",
        "  - surface: checkpoint-header | agents-md-append | engine-config",
        "    title: <short imperative title>",
        "    signature: <the ONE mined signature this targets>",
        "    rationale: <why this edit addresses that signature>",
        "    edit: <per the surface rules below>",
        "    expected_effect: <which metric or signature should improve>",
        "```",
        "",
        "Surface rules:",
        "- checkpoint-header: `edit` is the FULL replacement content of the engine",
        "  checkpoint header file (current text below).",
        "- agents-md-append: `edit` is a self-contained '#### experiment: <title>'",
        "  subsection to APPEND to AGENTS.md. AGENTS.md is append-only — never",
        "  rewrite or reorder existing content.",
        "- engine-config: `edit` is RECOMMENDATION TEXT for the `engine:` section of",
        "  lane-config.yaml. It is never auto-applied; a human edits the config.",
        "",
        f"--- mined evidence (last {evidence.get('window_days')} days) ---",
        json.dumps(evidence, indent=2, sort_keys=True),
        "",
        "--- current checkpoint header (full text) ---",
        _header_resource_text().rstrip("\n"),
        "",
        "--- AGENTS.md '###' section titles ---",
        titles,
        "",
        "--- engine config (current values) ---",
        yaml.safe_dump(asdict(config), sort_keys=False).rstrip("\n"),
    ]
    return "\n".join(parts) + "\n"


def parse_proposals(text: str, max_proposals: int = 3) -> list[dict]:
    """Last ```proposals fence wins (mirrors decision.parse); clean errors."""
    fences = _FENCE_RE.findall(text)
    if not fences:
        raise ImproveError(
            "no ```proposals fence found in the reply — expected one fenced block "
            "whose info-string is 'proposals' with YAML {version: 1, proposals: [...]}"
        )
    try:
        raw = yaml.safe_load(fences[-1])
    except yaml.YAMLError as exc:
        raise ImproveError(f"the proposals fence body is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ImproveError("the proposals fence body must be a YAML mapping")
    if raw.get("version") != 1:
        raise ImproveError(f"unsupported proposals version {raw.get('version')!r}; expected 1")
    items = raw.get("proposals")
    if not isinstance(items, list) or not items:
        raise ImproveError("'proposals' must be a non-empty list")
    out: list[dict] = []
    for idx, item in enumerate(items[:max_proposals]):
        if not isinstance(item, dict):
            raise ImproveError(f"proposal {idx}: must be a mapping")
        surface = item.get("surface")
        if surface not in SURFACES:
            raise ImproveError(
                f"proposal {idx}: surface {surface!r} is not one of {', '.join(SURFACES)}"
            )
        for field in ("title", "edit"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ImproveError(f"proposal {idx}: '{field}' must be a non-empty string")
        out.append(
            {
                "surface": surface,
                "title": item["title"].strip(),
                "signature": str(item.get("signature") or ""),
                "rationale": str(item.get("rationale") or ""),
                "edit": item["edit"],
                "expected_effect": str(item.get("expected_effect") or ""),
            }
        )
    return out


# ── proposal files (frontmatter + the edit as the body, byte-preserved) ─────


def _render_proposal(meta: dict, edit: str) -> str:
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    body = edit if edit.endswith("\n") else edit + "\n"
    return f"---\n{front}---\n{body}"


def _split_proposal(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ImproveError(f"{path}: not a proposal file (missing frontmatter)")
    end = text.find("\n---\n", 3)
    if end < 0:
        raise ImproveError(f"{path}: unterminated frontmatter")
    meta = yaml.safe_load(text[4 : end + 1])
    if not isinstance(meta, dict):
        raise ImproveError(f"{path}: frontmatter is not a mapping")
    return meta, text[end + 5 :]


def propose(
    paths: SessionPaths,
    substrate: Substrate,
    config: EngineConfig,
    events: EventLog,
    max_proposals: int = 3,
) -> tuple[dict, list[tuple[Path, dict]]]:
    """Mine -> brain one-shot -> file proposals. NO automatic application.

    Raises BrainError (incl. the cost guard) and ImproveError (unusable reply).
    """
    evidence = mine(paths)
    prompt = build_prompt(paths, config, evidence, max_proposals)
    reply = Brain(config, substrate, paths, events).invoke(prompt)
    proposals = parse_proposals(reply, max_proposals)
    paths.proposals_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filed: list[tuple[Path, dict]] = []
    for n, proposal in enumerate(proposals, start=1):
        meta = {
            "surface": proposal["surface"],
            "title": proposal["title"],
            "status": "proposed",
            "signature": proposal["signature"],
            "rationale": proposal["rationale"],
            "expected_effect": proposal["expected_effect"],
            "created_at": utc_now(),
        }
        path = paths.proposals_dir / f"{stamp}-{n}.md"
        path.write_text(_render_proposal(meta, proposal["edit"]), encoding="utf-8")
        events.append(
            "improve-proposed",
            file=path.name,
            surface=proposal["surface"],
            title=proposal["title"],
            signature=proposal["signature"],
        )
        filed.append((path, meta))
    return evidence, filed


# ── apply (human-gated promotion; T0006 experiment protocol) ────────────────


def find_proposal(paths: SessionPaths, n: int) -> Path | None:
    """Proposal N of the most recent improve run (<latest ts>-<n>.md)."""
    best: tuple[str, Path] | None = None
    if not paths.proposals_dir.is_dir():
        return None
    for path in paths.proposals_dir.glob("*.md"):
        match = _PROPOSAL_NAME_RE.match(path.name)
        if not match or int(match.group(2)) != n:
            continue
        if best is None or match.group(1) > best[0]:
            best = (match.group(1), path)
    return best[1] if best else None


def _apply_checkpoint_header(edit: str) -> Path:
    """Overwrite the checkpoint header the engine actually uses — resolved via
    importlib.resources exactly like loop._assemble_prompt does."""
    resource = resources.files("loop_orchestrator.engine").joinpath(*_HEADER_RESOURCE)
    with resources.as_file(resource) as header:
        target = Path(header)
        target.write_text(edit if edit.endswith("\n") else edit + "\n", encoding="utf-8")
        return target


def _apply_agents_append(project_root: Path, title: str, edit: str) -> Path:
    """Append the experiment subsection to AGENTS.md (append-only: open 'a')."""
    agents = project_root / "AGENTS.md"
    block = edit.strip("\n")
    if not block.startswith("#### experiment:"):
        block = f"#### experiment: {title}\n\n{block}"
    prefix = ""
    try:
        existing = agents.read_bytes()
        if existing and not existing.endswith(b"\n"):
            prefix = "\n"
    except OSError:
        pass
    with open(agents, "a", encoding="utf-8") as fh:
        fh.write(prefix + "\n" + block + "\n")
    return agents


def apply_proposal(
    paths: SessionPaths,
    substrate: Substrate,
    events: EventLog,
    n: int,
) -> tuple[Path, dict, str]:
    """Apply proposal N. checkpoint-header overwrites the contracts file;
    agents-md-append appends the subsection; engine-config is never applied —
    it is marked applied-manually-required for a human. Applied edits log the
    T0006 experiment entry and record a best-effort metrics baseline."""
    path = find_proposal(paths, n)
    if path is None:
        raise ImproveError(f"no proposal {n} under {paths.proposals_dir}")
    meta, edit = _split_proposal(path)
    if meta.get("status") != "proposed":
        raise ImproveError(f"{path.name}: status is {meta.get('status')!r}, not 'proposed'")
    surface = meta.get("surface")
    title = str(meta.get("title") or path.stem)

    if surface == "engine-config":
        meta["status"] = "applied-manually-required"
        path.write_text(_render_proposal(meta, edit), encoding="utf-8")
        events.append("improve-manual-required", file=path.name, title=title)
        return path, meta, edit

    if surface == "checkpoint-header":
        target = _apply_checkpoint_header(edit)
    elif surface == "agents-md-append":
        target = _apply_agents_append(paths.project_root, title, edit)
    else:
        raise ImproveError(f"{path.name}: unknown surface {surface!r}")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    append_log(paths.ops_wiki / "log.md", f"## [{today}] experiment | {title}")
    try:
        substrate.metrics_log()
    except SubstrateError as exc:
        events.append("error", kind="metrics-baseline-failed", error=str(exc))
    else:
        events.append("metrics", baseline_for=title)

    meta["status"] = "applied"
    meta["applied_at"] = utc_now()
    meta["applied_to"] = str(target)
    path.write_text(_render_proposal(meta, edit), encoding="utf-8")
    events.append("improve-applied", file=path.name, surface=surface, title=title)
    return path, meta, edit

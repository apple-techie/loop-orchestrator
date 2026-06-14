"""Observation pass: lane statuses + digest -> EngineSnapshot + snapshot.json.

All substrate access goes through the injected Substrate instance; this module
never spawns processes itself. snapshot.json is engine-owned state under
paths.engine_dir and is written atomically so readers never see a torn file.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ..locking import atomic_write_json
from ..paths import SessionPaths
from ..substrate import Substrate, SubstrateError
from .events import parse_ts, utc_now


@dataclass
class EngineSnapshot:
    generated_at: str
    lanes: dict[str, dict[str, str]]  # lane -> {status, target, kind}
    loops: dict
    mailbox_pending: list[str]
    processed_count: int
    restarts_tail: list[dict]
    checkpoint_tokens: int | None

    def to_dict(self) -> dict:
        return {"contract_version": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict) -> EngineSnapshot:
        """Rebuild a snapshot from its persisted form (snapshot.json) — the
        inverse of to_dict(). Tolerates the extra contract_version key and any
        missing/unknown field (old or partial snapshots degrade to empty
        defaults) so a stale-fallback read never raises on schema drift."""
        return cls(
            generated_at=str(data.get("generated_at") or ""),
            lanes=data.get("lanes") or {},
            loops=data.get("loops") or {},
            mailbox_pending=list(data.get("mailbox_pending") or []),
            processed_count=int(data.get("processed_count") or 0),
            restarts_tail=list(data.get("restarts_tail") or []),
            checkpoint_tokens=data.get("checkpoint_tokens"),
        )


def _restarts_tail(path: Path, n: int = 10) -> list[dict]:
    """Last n parseable lines of lane-restarts.jsonl; missing file => []."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        if len(out) == n:
            break
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    out.reverse()
    return out


# F7 (T0029): observe must degrade gracefully when the all-lanes fan-out is slow
# under load instead of aborting the cycle. A flat 30s starved large sessions
# (every pane captured at once); the timeout should scale with the lane count,
# and on a fan-out failure the engine reuses the last good snapshot.json rather
# than failing the whole cycle.
FANOUT_BASE_TIMEOUT_S = 30.0
FANOUT_PER_LANE_TIMEOUT_S = 5.0
FANOUT_TIMEOUT_CAP_S = 120.0


def adaptive_timeout(
    lane_count: int,
    base: float = FANOUT_BASE_TIMEOUT_S,
    per_lane: float = FANOUT_PER_LANE_TIMEOUT_S,
    cap: float = FANOUT_TIMEOUT_CAP_S,
) -> float:
    """Fan-out timeout that scales with lane count, bounded by `cap` — a large
    session's all-pane capture needs more than a flat base, but never unbounded.
    Pure; lane_count <= 0 yields the base."""
    return min(cap, base + per_lane * max(0, lane_count))


def _age_seconds(generated_at: str, now: str) -> float | None:
    """Seconds between a snapshot's generated_at and `now` (both contract TS),
    clamped at 0; None when either timestamp is absent or unparseable."""
    if not generated_at:
        return None
    try:
        delta_s = (parse_ts(now) - parse_ts(generated_at)).total_seconds()
    except ValueError:
        return None
    return max(0.0, delta_s)


def load_last_snapshot(snapshot_path: Path) -> EngineSnapshot | None:
    """The last persisted snapshot (snapshot.json) as an EngineSnapshot, or None
    when it is absent/empty/corrupt — the stale-fallback source on a fan-out
    failure."""
    try:
        raw = snapshot_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return EngineSnapshot.from_dict(data)


class Observer:
    def __init__(self, substrate: Substrate, paths: SessionPaths):
        self.substrate = substrate
        self.paths = paths

    def snapshot(self) -> EngineSnapshot:
        statuses = self.substrate.lane_status_all()
        digest = self.substrate.digest()
        state = digest.get("state")
        loops = state.get("loops") if isinstance(state, dict) else None
        mailbox = digest.get("mailbox") or {}
        pending = [
            item["file"] if isinstance(item, dict) else str(item)
            for item in mailbox.get("pending") or []
        ]
        try:
            checkpoint_tokens: int | None = len(self.substrate.checkpoint_prompt()) // 4
        except SubstrateError:
            checkpoint_tokens = None
        snap = EngineSnapshot(
            generated_at=utc_now(),
            lanes={
                name: {"status": st.status, "target": st.target, "kind": st.kind}
                for name, st in statuses.items()
            },
            loops=loops if isinstance(loops, dict) else {},
            mailbox_pending=pending,
            processed_count=int(mailbox.get("processed_count") or 0),
            restarts_tail=_restarts_tail(self.paths.lane_restarts),
            checkpoint_tokens=checkpoint_tokens,
        )
        atomic_write_json(self.paths.snapshot_path, snap.to_dict())
        return snap

    def snapshot_or_stale(self) -> tuple[EngineSnapshot, bool, float | None]:
        """A fresh snapshot, or — when the all-lanes fan-out fails (e.g. a
        transient load spike times it out) — the last good snapshot reused so the
        cycle proceeds on known state instead of aborting. Returns
        (snapshot, stale, age_s): (fresh, False, None) on success; (last, True,
        age_seconds) on fallback. Re-raises the SubstrateError only when there is
        no prior snapshot to fall back to (nothing safe to proceed on)."""
        try:
            return self.snapshot(), False, None
        except SubstrateError:
            last = load_last_snapshot(self.paths.snapshot_path)
            if last is None:
                raise
            return last, True, _age_seconds(last.generated_at, utc_now())


def delta(prev_dict: dict | None, cur_dict: dict) -> list[tuple[str, dict]]:
    """Lane status transitions + new mailbox files between two snapshot dicts."""
    events: list[tuple[str, dict]] = []
    prev_lanes = (prev_dict or {}).get("lanes") or {}
    for name, info in (cur_dict.get("lanes") or {}).items():
        old = (prev_lanes.get(name) or {}).get("status")
        new = info.get("status")
        if old != new:
            events.append(("lane-status", {"lane": name, "from": old, "to": new}))
    prev_mail = set((prev_dict or {}).get("mailbox_pending") or [])
    for name in cur_dict.get("mailbox_pending") or []:
        if name not in prev_mail:
            events.append(("mailbox-new", {"file": name}))
    return events

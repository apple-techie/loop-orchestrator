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
from .events import utc_now


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

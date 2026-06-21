"""Append-only engine event log (events.jsonl).

One JSON object per line: {ts, seq, event, ...fields}. The P5 flow has TWO
writer processes by design (the watch daemon and the approve/reject CLI), so
each append takes an fcntl lock on a sibling .lock file and derives seq from
the file tail inside the critical section — seq stays unique and monotonic
across concurrent writers and restarts.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> str:
    """Current UTC time in the contract's 'YYYY-MM-DDTHH:MM:SSZ' format."""
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc)


class EventLog:
    """Append-only JSONL event log with cross-process monotonic seq."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def _last_seq(self) -> int:
        """Seq of the last parseable line; scans backwards past corruption."""
        for line in reversed(self._lines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("seq"), int):
                return obj["seq"]
        return 0

    def _last_seq_tail(self) -> int:
        """_last_seq over only the final 8KB (an event line is well under 1KB),
        falling back to the full scan when the tail is all corruption."""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 8192))
                chunk = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return 0
        for line in reversed(chunk.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("seq"), int):
                return obj["seq"]
        return self._last_seq() if size > 8192 else 0

    def _needs_newline(self) -> bool:
        """True when a torn last line (crash mid-write) must be terminated so
        the next record starts on its own line."""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(-1, 2)
                return fh.read(1) != b"\n"
        except OSError:  # missing or empty file
            return False

    def append(self, kind: str, /, **fields) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            event = {"ts": utc_now(), "seq": self._last_seq_tail() + 1, "event": kind, **fields}
            prefix = "\n" if self._needs_newline() else ""
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(prefix + json.dumps(event) + "\n")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return event

    def tail(self, n: int) -> list[dict]:
        """Last n parseable events, oldest first; corrupt lines are skipped."""
        out: list[dict] = []
        for line in reversed(self._lines()):
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

    def read_all(self) -> list[dict]:
        """All parseable events, oldest first; corrupt lines are skipped."""
        out: list[dict] = []
        for line in self._lines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def count_since(self, kind: str, seconds: float) -> int:
        """Events of `kind` within the last `seconds`; relies on ts monotonicity
        to stop scanning at the first line older than the cutoff."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        count = 0
        for line in reversed(self._lines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                ts = parse_ts(obj["ts"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if ts < cutoff:
                break
            if obj.get("event") == kind:
                count += 1
        return count

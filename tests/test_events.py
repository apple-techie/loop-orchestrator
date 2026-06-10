from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from loop_orchestrator.engine.events import TS_FORMAT, EventLog

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_append_tail_count_since(tmp_path):
    log = EventLog(tmp_path / "engine" / "events.jsonl")
    e1 = log.append("cycle-start")
    e2 = log.append("brain-call", harness="claude")
    e3 = log.append("cycle-end", actions=2)

    assert (e1["seq"], e2["seq"], e3["seq"]) == (1, 2, 3)
    assert e2["event"] == "brain-call"
    assert e2["harness"] == "claude"
    assert TS_RE.match(e1["ts"])

    assert log.tail(2) == [e2, e3]
    assert log.tail(10) == [e1, e2, e3]
    assert log.count_since("brain-call", 3600) == 1
    assert log.count_since("cycle-start", 3600) == 1
    assert log.count_since("escalate", 3600) == 0


def test_seq_continues_across_instances(tmp_path):
    path = tmp_path / "events.jsonl"
    EventLog(path).append("cycle-start")
    EventLog(path).append("observe")
    log = EventLog(path)
    assert log.append("cycle-end")["seq"] == 3
    assert [e["seq"] for e in log.tail(3)] == [1, 2, 3]


def test_corrupt_last_line_tolerated(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append("cycle-start")
    log.append("observe")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-06-10T12:00:0')  # torn write

    fresh = EventLog(path)
    assert fresh.append("cycle-end")["seq"] == 3
    assert [e["event"] for e in fresh.tail(10)] == ["cycle-start", "observe", "cycle-end"]


def test_count_since_excludes_old_events(tmp_path):
    path = tmp_path / "events.jsonl"
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(TS_FORMAT)
    path.write_text(
        json.dumps({"ts": old_ts, "seq": 1, "event": "brain-call"}) + "\n", encoding="utf-8"
    )
    log = EventLog(path)
    log.append("brain-call")
    assert log.count_since("brain-call", 3600) == 1
    assert log.count_since("brain-call", 3 * 3600) == 2

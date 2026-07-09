from __future__ import annotations

import json
import logging

from osw.log import (
    enable_file_logging,
    emit_event,
    format_event_line,
    read_events,
)


def test_file_logging_is_idempotent_per_prefix(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        first = enable_file_logging(tmp_path, prefix="agent_001")
        second = enable_file_logging(tmp_path, prefix="agent_001")

        assert first == second
        matching = [
            h for h in root.handlers
            if getattr(h, "baseFilename", "") == str(first)
        ]
        assert len(matching) == 1
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()


def test_emit_and_read_events_are_agent_filterable(tmp_path):
    emit_event(
        tmp_path,
        component="watcher",
        event="turn_sent",
        agent_id="agent_001",
        terminal="term-a",
        phase="task",
        message="prompt sent",
        data={"chars": 10},
    )
    emit_event(
        tmp_path,
        component="watcher",
        event="turn_sent",
        agent_id="agent_002",
        terminal="term-b",
    )

    events = read_events(tmp_path, agent_id="agent_001")

    assert len(events) == 1
    assert events[0]["event"] == "turn_sent"
    assert events[0]["agent_id"] == "agent_001"
    assert events[0]["data"] == {"chars": 10}
    assert json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])


def test_format_event_line_is_single_line_and_includes_context():
    line = format_event_line({
        "ts": "2026-07-08T00:00:00Z",
        "level": "INFO",
        "component": "watcher",
        "event": "turn_sent",
        "agent_id": "agent_001",
        "terminal": "term-a",
        "phase": "task",
        "message": "hello\nworld",
    })

    assert "\n" not in line
    assert "agent_001" in line
    assert "term-a" in line
    assert "turn_sent" in line
    assert "hello world" in line

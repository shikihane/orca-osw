from __future__ import annotations

import json

from osw import notify, state
from osw.log import emit_event, read_events

OLD = "2020-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"


def _record(root, agent_id="agent_001", finished_at=OLD, **extra):
    agent = {
        "agent_id": agent_id,
        "state": "done",
        "terminal": "term-1",
        "completion_source": "task_done",
        "handoff_path": "",
        "report_file": "",
    }
    if finished_at:
        agent["finished_at"] = finished_at
    agent.update(extra)
    state.write_agent(root, agent)
    return agent


def _event(root, event, agent_id="agent_001", **data):
    return emit_event(
        state.logs_dir(root),
        component="watcher",
        event=event,
        agent_id=agent_id,
        data=data,
    )


def _events(root):
    return read_events(state.logs_dir(root))


def _skipped_line(agent_id="agent_001"):
    return (
        f"# [osw] task-finished agent={agent_id} terminal=term-1 "
        f"report=r.json summary=did the thing"
    )


def test_skipped_notice_is_pending_with_captured_line(tmp_path):
    _record(tmp_path)
    line = _skipped_line()
    _event(tmp_path, "notify_skipped", line=line)

    items = notify.pending(tmp_path)

    assert len(items) == 1
    item = items[0]
    assert item["line"] == line
    assert item["key"] == f"agent_001@{OLD}"
    assert item["summary"] == "did the thing"
    assert item["state"] == "done"
    assert item["completion_source"] == "task_done"
    assert item["terminal"] == "term-1"


def test_delivered_at_or_after_finished_at_removes_notice(tmp_path):
    agent = _record(tmp_path)
    _event(tmp_path, "notify_skipped", line=_skipped_line())
    delivered = _event(tmp_path, "notify_delivered", line=_skipped_line(),
                       channel="terminal")

    assert delivered["ts"] >= OLD
    assert notify.delivered(_events(tmp_path), agent) is True
    assert notify.pending(tmp_path) == []


def test_delivered_before_finished_at_does_not_count(tmp_path):
    agent = _record(tmp_path, finished_at=FUTURE)
    _event(tmp_path, "notify_delivered", line=_skipped_line(),
           channel="terminal")

    assert notify.delivered(_events(tmp_path), agent) is False
    items = notify.pending(tmp_path)
    assert [item["key"] for item in items] == [f"agent_001@{FUTURE}"]


def test_ack_is_idempotent_and_counts_only_new_keys(tmp_path):
    _record(tmp_path, agent_id="agent_001")
    _record(tmp_path, agent_id="agent_002")
    _event(tmp_path, "notify_skipped", agent_id="agent_001",
           line=_skipped_line("agent_001"))
    _event(tmp_path, "notify_skipped", agent_id="agent_002",
           line=_skipped_line("agent_002"))
    first, second = (item["key"] for item in notify.pending(tmp_path))

    assert notify.ack(tmp_path, [first]) == 1
    assert notify.ack(tmp_path, [first]) == 0
    assert notify.ack(tmp_path, [first, second, second]) == 1
    assert notify.ack(tmp_path, [second]) == 0
    assert notify.pending(tmp_path) == []

    store = json.loads(
        (state.state_dir(tmp_path) / notify.ACKED_RELATIVE)
        .read_text(encoding="utf-8")
    )
    assert set(store["acked"]) == {first, second}


def test_ack_treats_corrupt_store_as_empty(tmp_path):
    _record(tmp_path)
    _event(tmp_path, "notify_skipped", line=_skipped_line())
    key = notify.pending(tmp_path)[0]["key"]
    path = state.state_dir(tmp_path) / notify.ACKED_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert notify.ack(tmp_path, [key]) == 1
    store = json.loads(path.read_text(encoding="utf-8"))
    assert set(store["acked"]) == {key}
    assert store["acked"][key]


def test_second_finished_at_is_a_fresh_notice(tmp_path):
    _record(tmp_path, finished_at=OLD)
    _event(tmp_path, "notify_delivered", line=_skipped_line(),
           channel="terminal")
    assert notify.pending(tmp_path) == []

    _record(tmp_path, finished_at=FUTURE)

    items = notify.pending(tmp_path)
    assert [item["key"] for item in items] == [f"agent_001@{FUTURE}"]
    assert items[0]["line"].startswith(
        "# [osw] task-finished agent=agent_001 terminal=term-1"
    )


def test_pending_count_matches_pending(tmp_path):
    _record(tmp_path, agent_id="agent_001")
    _record(tmp_path, agent_id="agent_002")
    _event(tmp_path, "notify_skipped", agent_id="agent_001",
           line=_skipped_line("agent_001"))
    _event(tmp_path, "notify_delivered", agent_id="agent_002",
           line=_skipped_line("agent_002"), channel="terminal")

    assert notify.pending_count(tmp_path) == len(notify.pending(tmp_path)) == 1


def test_record_without_finished_at_is_never_pending(tmp_path):
    _record(tmp_path, finished_at=None)
    _event(tmp_path, "notify_skipped", line=_skipped_line())

    assert notify.pending(tmp_path) == []
    assert notify.pending_count(tmp_path) == 0


def test_format_line_builds_from_record_and_newest_report(tmp_path):
    _record(tmp_path, report_file="r.json")
    reports = state.reports_dir(tmp_path)
    reports.mkdir(parents=True, exist_ok=True)
    for name, summary in (
        ("agent_001_20200101_000000.json", "first"),
        ("agent_001_20200101_000100.json", "newest"),
    ):
        (reports / name).write_text(
            json.dumps({
                "last_assistant_message": summary,
                "tools_observed": False,
            }),
            encoding="utf-8",
        )

    items = notify.pending(tmp_path)

    assert len(items) == 1
    assert items[0]["summary"] == "newest"
    assert items[0]["warning"] == "no_tool_activity"
    assert "summary=newest" in items[0]["line"]
    assert "warning=no_tool_activity" in items[0]["line"]


def test_mailbox_suppressed_notice_counts_as_delivered(tmp_path):
    """The spool report is the mailbox channel's durable copy."""
    agent = _record(tmp_path)
    _event(tmp_path, "completion_notify_suppressed",
           state="done", source="task_done")

    assert notify.delivered(_events(tmp_path), agent) is True
    assert notify.pending(tmp_path) == []


def test_suppressed_before_finished_at_does_not_count(tmp_path):
    agent = _record(tmp_path, finished_at=FUTURE)
    _event(tmp_path, "completion_notify_suppressed",
           state="done", source="task_done")

    assert notify.delivered(_events(tmp_path), agent) is False
    assert [item["key"] for item in notify.pending(tmp_path)] == [
        f"agent_001@{FUTURE}"]


def test_reconcile_legacy_acks_only_records_with_no_notify_event(tmp_path):
    _record(tmp_path, agent_id="legacy_001")
    _record(tmp_path, agent_id="skipped_001")
    _record(tmp_path, agent_id="failed_001")
    _event(tmp_path, "notify_skipped", agent_id="skipped_001",
           line=_skipped_line("skipped_001"))
    _event(tmp_path, "notify_failed", agent_id="failed_001",
           line=_skipped_line("failed_001"))

    assert notify.reconcile_legacy(tmp_path) == 1
    assert sorted(item["agent_id"] for item in notify.pending(tmp_path)) == [
        "failed_001", "skipped_001"]

    store = json.loads(
        (state.state_dir(tmp_path) / notify.ACKED_RELATIVE)
        .read_text(encoding="utf-8")
    )
    assert set(store["acked"]) == {f"legacy_001@{OLD}"}
    assert store["acked"][f"legacy_001@{OLD}"].endswith(" reconcile_legacy")


def test_reconcile_legacy_marker_makes_second_call_a_no_op(tmp_path):
    _record(tmp_path)
    marker = state.state_dir(tmp_path) / notify.LEGACY_MARKER_RELATIVE

    assert notify.reconcile_legacy(tmp_path) == 1
    first = json.loads(marker.read_text(encoding="utf-8"))
    assert first["reconciled"] == 1
    assert first["at"]

    assert notify.reconcile_legacy(tmp_path) == 0
    assert json.loads(marker.read_text(encoding="utf-8")) == first


def test_reconcile_legacy_skips_unfinished_and_keeps_agent_records(tmp_path):
    agent = _record(tmp_path, agent_id="legacy_001")
    running = _record(tmp_path, agent_id="running_001", finished_at=None,
                      state="running")
    before = json.loads(
        (state.agents_dir(tmp_path) / "legacy_001.json")
        .read_text(encoding="utf-8")
    )

    assert notify.reconcile_legacy(tmp_path) == 1

    after = json.loads(
        (state.agents_dir(tmp_path) / "legacy_001.json")
        .read_text(encoding="utf-8")
    )
    assert before == after == agent
    assert state.read_agent(tmp_path, "running_001") == running
    assert notify.pending(tmp_path) == []


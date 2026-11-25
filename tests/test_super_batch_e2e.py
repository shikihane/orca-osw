"""End to end: one `super` fan-out -> one batch Ticket -> one conclusion.

The unit slices are covered in test_fanout.py, test_mailbox_integration.py and
test_mailbox_daemon.py; this file proves they compose: dispatch with a ticket,
two reports attached by ``correlation.ticket``, the daemon concludes once, and
the one line is delivered to the caller's address.
"""

from __future__ import annotations

import functools

import anyio
import pytest

from osw import fanout
from osw.mailbox import daemon, spool
from osw.mailbox.paths import db_path
from osw.mailbox.store import Store


class Hooks:
    """The minimum the daemon tick calls, plus the sent lines."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    async def notify(self, conversation: str, line: str) -> None:
        self.lines.append((conversation, line))


def _dispatch_fake():
    """A fake `run_command` that answers each `new` with a created agent."""
    seen: list[list[str]] = []

    async def fake_run(argv, cwd, **kwargs):
        seen.append(list(argv))
        if "new" in argv:
            index = len([row for row in seen if "new" in row])
            return fanout.CommandResult(
                returncode=0,
                stdout=f"Created code_{index:03d} on terminal term-{index}\n",
                stderr="",
            )
        return fanout.CommandResult(returncode=0, stdout="{}", stderr="")

    return fake_run, seen


def _report(root, ticket_id: str, agent: str) -> None:
    spool.write_envelope(root, {
        "from": {"role": "watcher", "id": agent},
        "to": {"role": "router", "id": ""},
        "kind": "report",
        "correlation": {"agent": agent, "ticket": ticket_id},
        "thread_key": agent,
        "dedupe_key": f"report_{agent}_1",
        "body": {
            "outcome": "ready",
            "summary": "handoff written",
            "handoff_path": "handoffs/x.md",
            "completion_source": "ps_done",
        },
    })


def test_super_fanout_concludes_with_one_line(tmp_path, monkeypatch):
    fake_run, seen = _dispatch_fake()
    monkeypatch.setattr("osw.fanout.run_command", fake_run)
    monkeypatch.setattr(
        "osw.fanout._start_mailbox",
        _async_return({"running": True, "pid": 1}),
    )
    monkeypatch.setattr(
        "osw.fanout.caller_pane_key", _async_return("tab-a:leaf-b"),
    )

    code = fanout.run(
        root=tmp_path,
        provider="pi",
        tasks=["first task", "second task"],
        caller_terminal="term-boss",
        mailbox=True,
        json_output=True,
    )

    assert code == 0
    news = [argv for argv in seen if "new" in argv]
    assert len(news) == 2

    store = Store(db_path(tmp_path))
    try:
        tickets = store.tickets()
        assert len(tickets) == 1
        ticket = tickets[0]
        ticket_id = ticket["id"]
        # Every dispatch carried the same ticket, and it reached the Ticket.
        assert all(argv[argv.index("--ticket") + 1] == ticket_id for argv in news)
        assert ticket["stage"] == "working"
        assert len(ticket["assignments"]) == 2
        assert ticket["origin"]["id"] == "terminal:term-boss"
        assert ticket["origin"]["pane_key"] == "tab-a:leaf-b"
        assert {a["state"] for a in ticket["assignments"]} == {"working"}

        _report(tmp_path, ticket_id, "code_001")
        _report(tmp_path, ticket_id, "code_002")

        hooks = Hooks()
        tick = functools.partial(daemon.tick, tmp_path, store, hooks, owner="test")
        anyio.run(tick)
        anyio.run(tick)

        assert len(hooks.lines) == 1
        conversation, line = hooks.lines[0]
        assert conversation == "terminal:term-boss"
        assert line.startswith("# [osw] batch-finished ticket=")
        assert "ready=2" in line and "failed=0" in line

        settled = store.ticket(ticket_id)
        assert settled["stage"] == "done"
        assert len(settled["reports"]) == 2
        assert store.outbox_pending(now=0) == []
    finally:
        store.close()


def test_super_without_mailbox_dispatches_direct(tmp_path, monkeypatch):
    fake_run, seen = _dispatch_fake()
    monkeypatch.setattr("osw.fanout.run_command", fake_run)
    called = []
    monkeypatch.setattr(
        "osw.fanout._start_mailbox", _async_record(called, {"running": True, "pid": 1}),
    )

    code = fanout.run(
        root=tmp_path,
        provider="pi",
        tasks=["only task"],
        caller_terminal="term-boss",
        mailbox=False,
        json_output=True,
    )

    assert code == 0
    assert called == []
    news = [argv for argv in seen if "new" in argv]
    assert len(news) == 1
    assert "--ticket" not in news[0]
    assert not db_path(tmp_path).exists()


def _async_return(value):
    async def inner(*args, **kwargs):
        return value
    return inner


def _async_record(sink, value):
    async def inner(*args, **kwargs):
        sink.append((args, kwargs))
        return value
    return inner

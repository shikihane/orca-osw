"""Integration scenarios for the OSW control plane (spec §19).

The ten end-to-end scenarios, the Addendum A hardening cases (watcher-shaped
reports and asks joined through ``Store.ticket_for_agent``, an expired Effect
claim reclaimed after a restart, a duplicate retry that cannot vanish) and the
Windows platform cases. They run the real ``Store`` on ``tmp_path``, a real
daemon tick, fake dispatcher hooks and the contract CLI through ``CliRunner``.
Orca and the OSW CLI subprocess are always mocked: no test touches a live
Orca, a real terminal or the network.
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest
from typer.testing import CliRunner

from osw import dispatcher, hosts, mandate, orca_cli, state
from osw.cli import app
from osw.log import read_events
from osw.mailbox import (
    adapters, daemon, envelope, identity, paths, spool,
)
from osw.mailbox.errors import MailboxError
from osw.mailbox.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mailbox"
runner = CliRunner()

DEFAULT_OWNER = "router:test"


# ---------------------------------------------------------------------------
# Hermetic environment: fake Orca, fake host processes, real files
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def orca(monkeypatch):
    """Every Orca call is mocked; tests that count replies receive this mock."""
    mock = AsyncMock(return_value={"ok": True, "result": {}})
    monkeypatch.setattr(orca_cli, "run_orca", mock)
    return mock


@pytest.fixture(autouse=True)
def _no_live_host(monkeypatch):
    """The host seam never spawns ssh/sftp; a bare success answers any call."""
    async def fake(argv, **kwargs):
        return hosts.Result(0, b"", b"")

    monkeypatch.setattr(hosts, "run_process", fake)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def mandate_doc(name: str = "mandate_default.json") -> dict:
    return fixture(name)


@pytest.fixture
def root(tmp_path):
    """An initialized project root with a committed-shape mandate file."""
    state.init_state_dir(tmp_path)
    paths.ensure_mailbox_dirs(tmp_path)
    write_mandate(tmp_path, mandate_doc())
    return tmp_path


def write_mandate(root: Path, doc: dict, *, name: str = "mandate.json") -> None:
    path = root / "skill" / "osw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def store(root):
    opened = Store(paths.db_path(root))
    opened.root = root
    yield opened
    opened.close()


# ---------------------------------------------------------------------------
# Envelope and dispatcher helpers
# ---------------------------------------------------------------------------


def make(root: Path, doc: dict, **overrides) -> dict:
    raw = json.loads(json.dumps(doc))
    for key, value in overrides.items():
        if key in ("body", "correlation", "from", "to") and isinstance(value, dict):
            raw.setdefault(key, {}).update(value)
        else:
            raw[key] = value
    return envelope.validate_envelope(raw)


def send(root: Path, doc: dict, **overrides) -> dict:
    env = make(root, doc, **overrides)
    spool.write_envelope(root, env)
    return env


def decide(action: str, *, ticket: str = "", message_id: str = "",
           payload: dict | None = None, author: str = "router") -> dict:
    return {
        "decision_id": envelope.new_id("dec"),
        "message_id": message_id,
        "action": action,
        "ticket": ticket,
        "reason": f"integration test {action}",
        "payload": payload or {},
        "author": author,
        "confidence": 0.9,
    }


class FakeHooks:
    """The dispatcher's five hooks, recording calls instead of touching Orca."""

    def __init__(self, orca_mock=None):
        self.orca = orca_mock
        self.launched: list[tuple[dict, dict]] = []
        self.followups: list[tuple[dict, dict]] = []
        self.answers: list[tuple[dict, dict]] = []
        self.stops: list[dict] = []
        self.notifications: list[tuple] = []

    async def launch(self, ticket: dict, payload: dict) -> dict:
        self.launched.append((ticket, payload))
        number = len(self.launched)
        return {
            "agent_id": f"agent_{number:03d}",
            "terminal": f"term-{number}",
            "dispatch_id": f"dispatch_{number}",
        }

    async def followup(self, ticket: dict, payload: dict) -> None:
        self.followups.append((ticket, payload))

    async def answer(self, ticket: dict, payload: dict) -> None:
        self.answers.append((ticket, payload))
        ask = str(payload.get("ask") or "")
        line = str(payload.get("template") or payload.get("text") or "")
        if self.orca is not None:
            await self.orca("orchestration", "reply", "--id", ask, "--body", line)

    async def stop(self, ticket: dict) -> None:
        self.stops.append(ticket)

    async def notify(self, conversation, line):
        self.notifications.append(("notify", conversation, line))

    async def notify_human(self, *args, **kwargs):
        self.notifications.append(("notify_human", args, kwargs))

    def __getattr__(self, name):
        # Any extra hook the daemon's CLIHooks grows is recorded, not run.
        async def record(*args, **kwargs):
            self.notifications.append((name, args, kwargs))
            return None

        return record


def tick(root: Path, hooks=None) -> None:
    """Run exactly one real daemon tick with fake hooks."""
    hooks = FakeHooks() if hooks is None else hooks
    anyio.run(functools.partial(daemon.run, root, once=True, hooks=hooks))


def apply_pending(store: Store, hooks, *, now: float | None = None) -> list[dict]:
    return anyio.run(
        functools.partial(dispatcher.run_pending, store, hooks, now=now)
    )


def lease_card(store: Store, *, owner: str = DEFAULT_OWNER) -> dict:
    rows = store.lease(owner, to_role="router", limit=1)
    assert len(rows) == 1, f"expected one leased card, got {len(rows)}"
    return rows[0]


def card_for(store: Store, row: dict) -> dict:
    ticket_id = str((row.get("correlation") or {}).get("ticket") or "")
    ticket = store.ticket(ticket_id) if ticket_id else None
    return envelope.card(row, ticket=ticket)


def total_queued(store: Store) -> int:
    return sum(store.counts().values())


def effect_rows(root: Path) -> list[tuple[str, str, str]]:
    """Durable Effect rows as ``(kind, key, state)``, read from the store file."""
    conn = sqlite3.connect(str(paths.db_path(root)))
    try:
        rows = conn.execute(
            "SELECT kind, key, state FROM effects ORDER BY kind, key"
        ).fetchall()
    finally:
        conn.close()
    return [tuple(row) for row in rows]


# ---------------------------------------------------------------------------
# §19.1 Human request -> card -> accept -> new once -> report -> close
# ---------------------------------------------------------------------------


def test_human_request_to_card_accept_new_once_report_close(
    root, store, orca, monkeypatch,
):
    monkeypatch.chdir(root)
    result = runner.invoke(
        app,
        ["mailbox", "send", "--kind", "request", "--json",
         "--text", "把 watcher 的超时改成可配置"],
    )
    assert result.exit_code == 0, result.output
    message_id = json.loads(result.output)["id"]

    hooks = FakeHooks(orca)
    tick(root, hooks)

    token = identity.generate_token(root)
    monkeypatch.setenv("OSW_ROLE", "router")
    monkeypatch.setenv("OSW_ROLE_TOKEN", token)
    waited = runner.invoke(
        app, ["mailbox", "wait", "--as", "router", "--timeout", "0", "--json"]
    )
    assert waited.exit_code == 0, waited.output
    cards = json.loads(waited.output)
    assert len(cards) == 1
    card = cards[0]
    assert card["message_id"] == message_id
    assert card["kind"] == "request"
    assert card["attention"] == "must_answer"
    assert "超时" in card["summary"]

    accepted = decide(
        "accept",
        ticket="code_001",
        message_id=message_id,
        payload={"provider": "claude", "model": "sonnet", "thinking": "high"},
    )
    assert dispatcher.commit(store, accepted, message_id=message_id) is True
    apply_pending(store, hooks)
    assert hooks.launched and len(hooks.launched) == 1
    ticket = store.ticket("code_001")
    assert ticket is not None and ticket["stage"] == "working"
    assignment = store.assignments("code_001")[0]
    assert assignment["agent_id"] == "agent_001"
    # The Ticket is keyed on its origin thread, never on the agent id: the
    # report can only reach it through the agent assignment.
    assert ticket["thread_key"] != "agent_001"

    # The report is exactly what the real watcher sink writes: the agent is
    # the only correlation and the thread key is the agent id, because the
    # agent record carries no ticket id.
    state.write_agent(root, {
        "agent_id": "agent_001",
        "terminal": "term-1",
        "provider": "claude",
        "created_at": "2026-11-14T00:00:00+00:00",
    })
    from osw.watcher import Watcher

    watcher = Watcher(root, "agent_001")
    assert watcher._thread_key() == "agent_001"
    watcher._spool("report", fixture("report_ready.json")["body"])
    assert spool.pending_count(root) == 1
    report = spool.read_pending(root)[0]
    assert {key for key, value in report["correlation"].items() if value} == {"agent"}
    assert report["correlation"]["agent"] == "agent_001"
    assert report["thread_key"] == "agent_001"

    # The fast path joins the report to the Ticket through
    # Store.ticket_for_agent, finalizes the assignment and parks it at review.
    assert store.ticket_for_agent("agent_001")["id"] == "code_001"
    tick(root, hooks)
    assert store.get_message(report["id"]) is not None
    assert store.reports("code_001")
    assert store.assignments("code_001")[0]["state"] == "done"
    assert store.ticket("code_001")["stage"] == "review"

    # No assignment state is written by hand: the report did it, so close is
    # allowed and runs exactly once.
    closed = decide("close", ticket="code_001", message_id=report["id"])
    assert dispatcher.commit(store, closed, message_id=report["id"]) is True
    apply_pending(store, hooks)
    final = store.ticket("code_001")
    assert final["stage"] == "done"
    assert final["closed_at"]
    assert len(hooks.launched) == 1  # one worker for the whole Ticket


# ---------------------------------------------------------------------------
# Contract CLI: `mailbox decide` applies locally through the fake hooks
# ---------------------------------------------------------------------------


def test_mailbox_decide_cli_applies_locally_when_no_daemon_runs(
    root, store, orca, monkeypatch,
):
    from osw import mailbox_cli

    assert mailbox_cli._dispatcher() is dispatcher

    monkeypatch.chdir(root)
    store.create_ticket(
        origin={"role": "human", "text": "run it"},
        mandate=mandate_doc(),
        ticket_id="code_001",
    )
    message = send(root, fixture("request.json"))
    spool.ingest(root, store)
    hooks = FakeHooks(orca)
    monkeypatch.setattr(daemon, "CLIHooks", lambda _root: hooks)

    result = runner.invoke(
        app,
        [
            "mailbox", "decide", message["id"],
            "--action", "accept",
            "--ticket", "code_001",
            "--reason", "明确人工请求",
            "--payload-json", json.dumps({"provider": "claude"}),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["decision_id"]
    assert len(hooks.launched) == 1
    assert store.ticket("code_001")["stage"] == "working"


# ---------------------------------------------------------------------------
# §19.2 Worker ask -> card -> answer -> one orchestration reply
# ---------------------------------------------------------------------------


def test_worker_ask_to_card_to_answer_to_one_orchestration_reply(root, store, orca):
    # A human request opens the Ticket and launches one worker; the ask the
    # real watcher then spools names only the agent, so the fast path joins it
    # to this Ticket through Store.ticket_for_agent.
    store.create_ticket(
        origin={"role": "human", "id": "terminal:term_abc", "text": "run it"},
        mandate=mandate_doc(),
        ticket_id="code_001",
        thread_key="chat:main",
    )
    accepted = decide(
        "accept",
        ticket="code_001",
        payload={"provider": "claude", "prompt": "run it"},
    )
    hooks = FakeHooks(orca)
    assert dispatcher.commit(store, accepted) is True
    apply_pending(store, hooks)
    assert [a["agent_id"] for a in store.assignments("code_001")] == ["agent_001"]
    assert store.ticket("code_001")["thread_key"] != "agent_001"
    assert store.ticket_for_agent("agent_001")["id"] == "code_001"

    raw = fixture("ask.json")
    assert "ticket" not in raw["correlation"]
    assert raw["thread_key"] == raw["correlation"]["agent"] == "agent_001"
    ask = send(root, raw)
    tick(root, hooks)

    row = lease_card(store)
    card = card_for(store, row)
    assert card["message_id"] == ask["id"]
    assert card["kind"] == "ask"
    assert card["attention"] == "must_answer"
    assert card["tickets"] == []  # an agent-only card names no Ticket id
    assert store.ticket("code_001")["stage"] == "waiting_answer"
    assert store.is_ask_open("code_001", "orca_msg_77")

    answered = decide(
        "answer",
        ticket="code_001",
        message_id=ask["id"],
        payload={"ask": "orca_msg_77", "text": "keep the current lock format"},
    )
    assert dispatcher.commit(store, answered) is True
    apply_pending(store, hooks)
    assert len(hooks.answers) == 1
    assert store.is_ask_open("code_001", "orca_msg_77") is False
    reply_calls = [
        call for call in orca.await_args_list
        if call.args[:2] == ("orchestration", "reply")
    ]
    assert len(reply_calls) == 1
    assert reply_calls[0].args[3] == "orca_msg_77"
    assert answered["decision_id"] in reply_calls[0].args[5]

    # A second, distinct answer for the same ask is recorded, never sent twice.
    second = decide(
        "answer",
        ticket="code_001",
        message_id=ask["id"],
        payload={"ask": "orca_msg_77", "text": "keep the old lock format"},
    )
    assert second["decision_id"] != answered["decision_id"]
    assert dispatcher.commit(store, second) is True
    assert store.decision(second["decision_id"])["state"] == "applied"
    assert store.decision(second["decision_id"])["result"] == "ask_already_answered"
    apply_pending(store, hooks)
    reply_calls = [
        call for call in orca.await_args_list
        if call.args[:2] == ("orchestration", "reply")
    ]
    assert len(reply_calls) == 1
    assert len(hooks.answers) == 1
    assert store.decision(answered["decision_id"])["state"] == "applied"


# ---------------------------------------------------------------------------
# §19.2b A human exact stop command -> one stop Effect -> one hook call
# ---------------------------------------------------------------------------


def test_human_exact_stop_command_plans_one_effect_and_stops_once(root, store):
    store.create_ticket(
        origin={"role": "human", "id": "terminal:term_abc", "text": "run it"},
        mandate=mandate_doc(),
        ticket_id="code_001",
        thread_key="chat:main",
        stage="working",
    )
    store.add_assignment(
        "code_001",
        {"agent_id": "agent_001", "terminal": "term-1", "state": "running"},
    )
    command = send(root, fixture("human_command_stop.json"))
    hooks = FakeHooks()
    tick(root, hooks)

    assert [row for row in effect_rows(root) if row[0] == "stop"] == [
        ("stop", "code_001", "done")
    ]
    assert len(hooks.stops) == 1
    ticket = store.ticket("code_001")
    assert ticket["stage"] == "stopped"
    assert [a["state"] for a in store.assignments("code_001")] == ["stopped"]
    assert store.get_message(command["id"])["state"] == "settled"

    # The command was one Decision with one Effect; a later tick is a no-op.
    tick(root, hooks)
    assert len(hooks.stops) == 1
    assert [row for row in effect_rows(root) if row[0] == "stop"] == [
        ("stop", "code_001", "done")
    ]


# ---------------------------------------------------------------------------
# Watcher fixtures carry the real sink shape, not a hand-written ticket
# ---------------------------------------------------------------------------


def test_watcher_fixtures_match_the_real_watcher_sink(root):
    state.write_agent(root, {
        "agent_id": "agent_001",
        "terminal": "term-1",
        "provider": "claude",
        "created_at": "2026-11-14T00:00:00+00:00",
    })
    from osw.watcher import Watcher

    watcher = Watcher(root, "agent_001")
    watcher._spool("report", {"outcome": "ready", "summary": "done"})
    watcher._spool(
        "ask",
        {"text": "should I update the lock file format?"},
        dedupe_key="orca_msg_77",
        correlation={"ask": "orca_msg_77"},
        priority=1,
    )
    emitted = {env["kind"]: env for env in spool.read_pending(root)}
    assert {k for k, v in emitted["report"]["correlation"].items() if v} == {"agent"}
    assert emitted["ask"]["correlation"]["ask"] == "orca_msg_77"

    for name, kind in (("report_ready.json", "report"), ("ask.json", "ask")):
        doc = fixture(name)
        real = emitted[kind]
        assert "ticket" not in doc["correlation"]
        assert real["correlation"]["ticket"] == ""
        assert doc["thread_key"] == real["thread_key"] == "agent_001"
        assert set(doc["correlation"]) <= set(real["correlation"])


# ---------------------------------------------------------------------------
# §19.3 System trigger outside open_from -> orphan card
# ---------------------------------------------------------------------------


def test_system_trigger_outside_open_from_becomes_orphan_card(root, store):
    trigger = send(root, fixture("system_trigger.json"))
    tick(root, FakeHooks())

    row = lease_card(store)
    card = card_for(store, row)
    assert card["message_id"] == trigger["id"]
    assert card["kind"] == "system"
    assert card["attention"] == "fyi"
    assert card["tickets"] == []
    assert store.tickets() == []
    # The default mandate allows a human only; this source cannot open a Ticket.
    assert mandate.check_open_from(mandate.load_mandate(root), "system") is False

    # An accept cannot smuggle such a source past open_from: the base Mandate
    # is checked before the Ticket row exists, so the refusal is clean.
    accepted = decide(
        "accept",
        ticket="build_001",
        message_id=trigger["id"],
        payload={"provider": "claude"},
    )
    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(store, accepted, message_id=trigger["id"])
    assert excinfo.value.code == "mandate_denied"
    assert store.ticket("build_001") is None
    assert store.tickets() == []
    assert store.decision(accepted["decision_id"]) is None
    assert effect_rows(root) == []


# ---------------------------------------------------------------------------
# §19.4 System trigger with open_from -> Ticket -> accept
# ---------------------------------------------------------------------------


def test_system_trigger_with_open_from_becomes_ticket_then_accept(root, store, orca):
    write_mandate(root, mandate_doc("mandate_system_open.json"))
    md = mandate.load_mandate(root)
    assert mandate.check_open_from(md, "system") is True
    assert mandate.check_open_from(md, "") is True  # "" is the human
    assert mandate.check_open_from(md, "watcher") is False

    trigger = send(root, fixture("system_trigger.json"))
    hooks = FakeHooks(orca)
    tick(root, hooks)
    assert card_for(store, lease_card(store))["message_id"] == trigger["id"]

    store.create_ticket(
        origin={
            "role": "system",
            "id": "adapter:build_result",
            "text": "BUILD FAILED in 3 targets",
            "source": "system",
        },
        mandate=md,
        ticket_id="build_001",
        thread_key="build:ci-42",
    )
    accepted = decide(
        "accept",
        ticket="build_001",
        message_id=trigger["id"],
        payload={"provider": "claude"},
    )
    assert dispatcher.commit(store, accepted, message_id=trigger["id"]) is True
    apply_pending(store, hooks)
    assert len(hooks.launched) == 1
    assert store.ticket("build_001")["stage"] == "working"


# ---------------------------------------------------------------------------
# §19.5 Mailbox killed mid-lease -> restart -> redelivery -> one apply
# ---------------------------------------------------------------------------


def test_mailbox_killed_mid_lease_restart_redelivers_and_applies_once(root, store, orca):
    request = send(root, fixture("request.json"))
    assert spool.ingest(root, store)["ingested"] == 1

    first = store.lease("mailbox-before-crash", to_role="router", lease_s=30.0)
    assert [row["id"] for row in first] == [request["id"]]
    assert first[0]["attempts"] == 1

    # The mailbox dies: no ack, no release. Its lease simply expires.
    assert store.expire_leases(now=time.time() + 31.0) == 1

    restarted = Store(paths.db_path(root))
    restarted.root = root
    try:
        again = restarted.lease("mailbox-after-restart", to_role="router")
        assert [row["id"] for row in again] == [request["id"]]
        assert again[0]["attempts"] == 2
        assert restarted.ack(request["id"], "mailbox-before-crash") is False
        assert restarted.ack(request["id"], "mailbox-after-restart") is True
        assert restarted.ack(request["id"], "mailbox-after-restart") is True
        assert store.lease("nobody", to_role="router") == []

        store.create_ticket(
            origin={"role": "human", "text": "request"},
            mandate=mandate_doc(),
            ticket_id="code_001",
        )
        hooks = FakeHooks(orca)
        accepted = decide("accept", ticket="code_001", message_id=request["id"])
        assert dispatcher.commit(store, accepted, message_id=request["id"]) is True
        apply_pending(store, hooks)
        apply_pending(store, hooks)
        assert len(hooks.launched) == 1
        assert store.decision(accepted["decision_id"])["state"] == "applied"
    finally:
        restarted.close()


# ---------------------------------------------------------------------------
# §19.6 Router killed after decide, before Dispatcher -> retry -> one apply
# ---------------------------------------------------------------------------


def test_router_killed_after_decide_before_dispatcher_retries_once(root, store, orca):
    store.create_ticket(
        origin={"role": "human", "text": "run it"},
        mandate=mandate_doc(),
        ticket_id="code_001",
    )
    message = send(root, fixture("request.json"))
    hooks = FakeHooks(orca)

    accepted = decide("accept", ticket="code_001", message_id=message["id"])
    assert dispatcher.commit(store, accepted, message_id=message["id"]) is True
    # Router dies here: the Decision is committed, no effect has run yet.
    assert store.decision(accepted["decision_id"])["state"] == "committed"
    assert store.pending_effects()
    assert not hooks.launched

    apply_pending(store, hooks, now=time.time() + 5.0)
    apply_pending(store, hooks, now=time.time() + 600.0)
    assert len(hooks.launched) == 1
    assert store.decision(accepted["decision_id"])["state"] == "applied"
    assert store.dead_letters() == []


# ---------------------------------------------------------------------------
# §19.7 Duplicate spool from a watcher restart -> one message
# ---------------------------------------------------------------------------


def test_duplicate_spool_from_watcher_restart_yields_one_message(root, store):
    first = make(root, fixture("report_ready.json"), id="msg_restart_001")
    second = make(root, fixture("report_ready.json"), id="msg_restart_002")
    second["dedupe_key"] = first["dedupe_key"]

    spool.write_envelope(root, first)
    spool.write_envelope(root, second)
    counts = spool.ingest(root, store)
    assert counts["ingested"] == 1
    assert counts["duplicates"] == 1
    assert total_queued(store) == 1
    stored = store.get_message(first["id"]) or store.get_message(second["id"])
    assert stored is not None

    # A second ingest of the very same id is also absorbed by the primary key.
    spool.write_envelope(root, stored)
    counts = spool.ingest(root, store)
    assert counts["duplicates"] == 1
    assert total_queued(store) == 1


# ---------------------------------------------------------------------------
# §19.7b A second retry without payload.attempt is never silently dropped
# ---------------------------------------------------------------------------


def test_second_retry_without_attempt_fails_with_one_attention_notice(root, store):
    store.create_ticket(
        origin={"role": "human", "text": "run it"},
        mandate=mandate_doc(),
        ticket_id="code_001",
        stage="working",
    )
    store.add_assignment(
        "code_001",
        {"agent_id": "agent_001", "terminal": "term-1", "state": "done"},
    )
    hooks = FakeHooks()
    payload = {"provider": "claude", "prompt": "try again"}

    first = decide("retry", ticket="code_001", payload=payload)
    assert dispatcher.commit(store, first) is True
    apply_pending(store, hooks)
    assert len(hooks.launched) == 1
    assert store.decision(first["decision_id"])["state"] == "applied"

    # Same Ticket and same (missing) attempt: the planned Effect key already
    # exists. The Decision must still be visible as failed, with one notice.
    second = decide("retry", ticket="code_001", payload=payload)
    assert second["decision_id"] != first["decision_id"]
    assert "attempt" not in second["payload"]
    assert dispatcher.commit(store, second) is True
    stored = store.decision(second["decision_id"])
    assert stored["state"] == "failed"
    assert stored["result"] == "duplicate_effect"

    apply_pending(store, hooks)
    assert len(hooks.launched) == 1
    assert effect_rows(root) == [("retry", "code_001:0", "done")]

    notices = [
        row for row in store.outbox_pending()
        if row["payload"].get("decision_id") == second["decision_id"]
    ]
    assert len(notices) == 1
    assert notices[0]["payload"]["kind"] == "notice"
    assert "duplicate_effect" in notices[0]["payload"]["line"]


# ---------------------------------------------------------------------------
# §19.7c An expired running claim is reclaimed after a daemon restart, once
# ---------------------------------------------------------------------------


def test_expired_running_effect_claim_is_reclaimed_after_daemon_restart(
    root, store, orca,
):
    store.create_ticket(
        origin={"role": "human", "text": "run it"},
        mandate=mandate_doc(),
        ticket_id="code_001",
    )
    message = send(root, fixture("request.json"))
    spool.ingest(root, store)
    accepted = decide("accept", ticket="code_001", message_id=message["id"])
    assert dispatcher.commit(store, accepted, message_id=message["id"]) is True

    pending = store.pending_effects()
    assert [(e["kind"], e["key"]) for e in pending] == [("launch", "code_001")]
    effect_id = pending[0]["id"]

    # Daemon A claims the Effect and dies before applying or finishing it.
    claimed_at = time.time()
    assert store.claim_effect(
        effect_id, "mailbox:before-crash", lease_s=5.0, now=claimed_at,
    )
    assert store.pending_effects(now=claimed_at + 1.0) == []
    store.close()

    restarted = Store(paths.db_path(root))
    restarted.root = root
    hooks = FakeHooks(orca)
    try:
        due = restarted.pending_effects(now=claimed_at + 6.0)
        assert [row["id"] for row in due] == [effect_id]

        # One real daemon pass on the new Store reclaims the expired claim.
        anyio.run(functools.partial(
            daemon.tick, root, restarted, hooks,
            now=claimed_at + 6.0, owner="mailbox:after-restart",
        ))
        assert len(hooks.launched) == 1
        assert restarted.decision(accepted["decision_id"])["state"] == "applied"
        assert effect_rows(root) == [("launch", "code_001", "done")]

        # Applied exactly once: a later pass has nothing due and never relaunches.
        anyio.run(functools.partial(
            daemon.tick, root, restarted, hooks,
            now=claimed_at + 600.0, owner="mailbox:after-restart",
        ))
        assert len(hooks.launched) == 1
        assert effect_rows(root) == [("launch", "code_001", "done")]
    finally:
        restarted.close()


# ---------------------------------------------------------------------------
# §19.8 Stop race: stop before accept -> the launch is skipped
# ---------------------------------------------------------------------------


def test_stop_race_before_accept_skips_launch(root, store, orca):
    store.create_ticket(
        origin={"role": "human", "text": "run it"},
        mandate=mandate_doc(),
        ticket_id="code_001",
        stage="working",
    )
    message = send(root, fixture("request.json"))
    store.update_ticket("code_001", stage="stopped")
    store.add_assignment(
        "code_001",
        {"agent_id": "agent_001", "terminal": "term-1", "state": "running"},
    )
    hooks = FakeHooks(orca)

    accepted = decide("accept", ticket="code_001", message_id=message["id"])
    committed = False
    try:
        committed = dispatcher.commit(store, accepted, message_id=message["id"])
    except MailboxError as exc:
        # Refusing at commit time is also a correct stop race resolution.
        assert exc.code in {
            "mandate_denied", "close_precondition_failed", "decision_invalid",
        }
    apply_pending(store, hooks)
    apply_pending(store, hooks)

    assert hooks.launched == []
    assert store.ticket("code_001")["stage"] == "stopped"
    assert committed is False  # the accept never became an Effect


# ---------------------------------------------------------------------------
# §19.9 Remote watcher through the host seam -> a local card
# ---------------------------------------------------------------------------


def test_remote_watcher_through_host_seam_produces_local_card(root, store):
    hosts.save_host(
        root,
        hosts.Host(
            name="fnnas",
            ssh="fnnas",
            environment="fnnas",
            worktree="path:/vol1/1000/orca/workspaces",
            remote_root="/vol1/1000/orca/workspaces",
            home="/vol1/1000/shiki",
        ),
    )
    state.write_agent(root, {
        "agent_id": "agent_001",
        "terminal": "term-remote",
        "provider": "claude",
        "host": "fnnas",
        "ticket_id": "code_001",
        "created_at": "2026-11-14T00:00:00+00:00",
    })

    store.create_ticket(
        origin={"role": "human", "text": "remote task"},
        mandate=mandate_doc(),
        ticket_id="code_001",
        thread_key="code_001",
        stage="working",
    )

    from osw.watcher import Watcher

    watcher = Watcher(root, "agent_001")
    assert watcher.host.is_local is False
    assert watcher.host.name == "fnnas"
    watcher._spool(
        "report",
        {
            "outcome": "ready",
            "summary": "remote turn finished",
            "completion_source": "handoff",
            "tools_observed": True,
        },
        dedupe_key="report_key_remote_1",
    )

    # The event landed in the local spool, not on the remote machine.
    assert spool.pending_count(root) == 1
    remote_env = spool.read_pending(root)[0]
    assert remote_env["from"]["role"] == "watcher"
    assert remote_env["correlation"]["agent"] == "agent_001"
    assert remote_env["thread_key"] == "code_001"
    assert remote_env["body"]["summary"] == "remote turn finished"

    tick(root, FakeHooks())
    card = card_for(store, lease_card(store))
    assert card["message_id"] == remote_env["id"]
    assert card["kind"] == "report"
    assert card["attention"] == "must_answer"
    # The report attached to the local Ticket through its thread key.
    assert store.ticket("code_001")["stage"] == "review"
    assert store.reports("code_001")


# ---------------------------------------------------------------------------
# §19.10 Adapter failure -> one system event, core still works
# ---------------------------------------------------------------------------


def test_adapter_failure_emits_one_system_event_and_core_still_works(
    root, store, monkeypatch,
):
    logs = root / "build.log"
    logs.write_text("BUILD FAILED: three targets\n", encoding="utf-8")
    config = {
        "enabled": {"log_file": True, "build_result": True},
        "log_file": {"paths": [str(root / "watched.log")]},
        "build_result": {"paths": [str(logs)]},
    }
    paths.mailbox_dir(root).mkdir(parents=True, exist_ok=True)
    adapters.config_path(root).write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )

    def explode(*args, **kwargs):
        raise RuntimeError("adapter exploded")

    monkeypatch.setitem(adapters._HANDLERS, "log_file", explode)

    first = adapters.run_once(root)
    second = adapters.run_once(root)
    assert first["log_file"]["failed"] is True
    assert second["log_file"].get("disabled") is True
    assert first["build_result"]["envelopes"] == 1

    failed = [
        event for event in read_events(state.logs_dir(root))
        if event.get("event") == "adapter_failed"
        and (event.get("data") or {}).get("adapter") == "log_file"
    ]
    assert len(failed) == 1
    assert adapters.load_state(root, "log_file")["disabled"] is True

    # The one healthy adapter's digest still reaches the control plane.
    tick(root, FakeHooks())
    card = card_for(store, lease_card(store))
    assert card["kind"] == "system"
    assert card["attention"] == "fyi"
    assert card["summary"].startswith("BUILD FAILED")


# ---------------------------------------------------------------------------
# Platform: Windows atomic replace and a writer under a concurrent reader
# ---------------------------------------------------------------------------


def test_atomic_replace_leaves_no_tmp_and_ignores_a_crashed_tmp(root):
    env = send(root, fixture("request.json"))
    final = paths.spool_dir(root) / f"{env['id']}.json"
    assert final.is_file()
    assert list(paths.spool_dir(root).glob("*.tmp")) == []

    # A writer killed mid-write leaves only a .tmp; readers ignore it.
    crashed = paths.spool_dir(root) / "msg_crashed_001.json.tmp"
    crashed.write_text('{"v": 1, "id": "msg_cr', encoding="utf-8")
    assert spool.pending_count(root) == 1
    assert [item["id"] for item in spool.read_pending(root)] == [env["id"]]


def test_spool_writes_stay_atomic_under_a_concurrent_reader(root):
    written = 40
    done = threading.Event()
    errors: list[BaseException] = []

    def reader():
        while not done.is_set():
            for path in paths.spool_dir(root).glob("*.json"):
                try:
                    raw = path.read_text(encoding="utf-8")
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict) or not parsed.get("id"):
                        errors.append(AssertionError(f"partial envelope: {raw!r}"))
                except (OSError, ValueError) as exc:
                    errors.append(exc)
            for item in spool.read_pending(root):
                if not item["id"]:
                    errors.append(AssertionError("card without id"))

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(written):
            send(root, fixture("request.json"), thread_key=f"chat:{index}")
    finally:
        done.set()
        thread.join(timeout=10)

    assert not errors, errors[:3]
    assert not thread.is_alive()
    assert spool.pending_count(root) == written
    assert len({item["id"] for item in spool.read_pending(root)}) == written


# ---------------------------------------------------------------------------
# Platform: detached daemon lock recovery
# ---------------------------------------------------------------------------


def _write_lock(root: Path, pid: int) -> None:
    paths.mailbox_dir(root).mkdir(parents=True, exist_ok=True)
    paths.lock_path(root).write_text(
        json.dumps({"pid": pid, "started_at": time.time()}),
        encoding="utf-8",
    )


def test_mailbox_start_is_idempotent_while_a_daemon_holds_the_lock(root, monkeypatch):
    """`start` never spawns a second detached daemon for a live lock."""
    monkeypatch.chdir(root)
    held = daemon.acquire_lock(root)
    try:
        result = runner.invoke(app, ["mailbox", "start", "--json"])
    finally:
        daemon.release_lock(root)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["running"] is True
    assert data["pid"] == held["pid"] == os.getpid()


def test_lock_recovery_reclaims_a_dead_pid_and_refuses_a_live_foreign_pid(root):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=30)
    _write_lock(root, dead.pid)
    recovered = daemon.acquire_lock(root)
    assert recovered["pid"] == os.getpid()
    assert daemon.is_running(root)[0] is True
    daemon.release_lock(root)
    assert daemon.is_running(root)[0] is False

    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_lock(root, live.pid)
        with pytest.raises(MailboxError) as excinfo:
            daemon.acquire_lock(root)
        assert excinfo.value.code == "mailbox_locked"
    finally:
        live.terminate()
        try:
            live.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            live.kill()
        daemon.release_lock(root)


# ---------------------------------------------------------------------------
# Platform: long paths and Unicode bodies
# ---------------------------------------------------------------------------


def test_long_paths_round_trip_the_store_and_spool(tmp_path):
    deep = tmp_path
    for index in range(5):
        deep = deep / (f"d{index}" + "x" * 50)
    assert len(str(deep)) > 260
    state.init_state_dir(deep)
    paths.ensure_mailbox_dirs(deep)
    write_mandate(deep, mandate_doc())

    env = send(deep, fixture("request.json"))
    assert spool.pending_count(deep) == 1
    opened = Store(paths.db_path(deep))
    try:
        assert spool.ingest(deep, opened)["ingested"] == 1
        assert opened.get_message(env["id"]) is not None
        assert opened.status()["schema_version"] == 1
    finally:
        opened.close()
    assert len(str(paths.db_path(deep))) > 260


def test_unicode_bodies_survive_spool_store_and_card(root, store):
    env = send(root, fixture("unicode_request.json"))
    text = "修复 café ☕ 的 🚀 panic：ログを確認して"
    assert spool.read_pending(root)[0]["body"]["text"] == text

    assert spool.ingest(root, store)["ingested"] == 1
    stored = store.get_message(env["id"])
    assert stored["body"]["text"] == text
    assert stored["summary"].startswith("émoji")

    tick(root, FakeHooks())
    card = card_for(store, lease_card(store))
    assert "☕" in card["summary"] or "🚀" in card["summary"]
    assert len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= 1024

    # The body cap counts UTF-8 bytes, not characters.
    envelope.validate_envelope({
        "from": {"role": "human"}, "to": {"role": "router"},
        "kind": "request", "body": {"text": "x" * 16000},
    })
    with pytest.raises(MailboxError) as excinfo:
        envelope.validate_envelope({
            "from": {"role": "human"}, "to": {"role": "router"},
            "kind": "request", "body": {"text": "x" * 17000},
        })
    assert excinfo.value.code == "body_too_large"


# ---------------------------------------------------------------------------
# Platform: status with the daemon stopped and the database write-locked
# ---------------------------------------------------------------------------


def test_status_with_daemon_stopped_and_db_write_locked(root, monkeypatch):
    monkeypatch.chdir(root)
    Store(paths.db_path(root)).close()

    stopped = runner.invoke(app, ["mailbox", "status", "--json"])
    assert stopped.exit_code == 0, stopped.output
    stopped_data = json.loads(stopped.output)
    assert stopped_data["running"] is False
    assert stopped_data["pid"] == 0

    writer = sqlite3.connect(str(paths.db_path(root)), timeout=1.0,
                             isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES ('probe', 'held')"
        )
        locked = runner.invoke(app, ["mailbox", "status", "--json"])
    finally:
        writer.rollback()
        writer.close()

    assert locked.exit_code == 0, locked.output
    locked_data = json.loads(locked.output)
    assert locked_data["running"] is False
    assert locked_data["queues"] == {
        "router": 0, "human": 0, "worker": 0, "broadcast": 0,
    }

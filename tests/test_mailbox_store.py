"""Tests for the Mailbox SQLite store (plan section 6)."""

from __future__ import annotations

import sqlite3
import time

import pytest

from osw.mailbox.errors import MailboxError
from osw.mailbox.store import Store


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "mailbox.db")
    try:
        yield store
    finally:
        store.close()


def envelope(
    message_id: str,
    *,
    ts: str = "2026-11-14T13:15:00.000Z",
    to_role: str = "router",
    to_id: str = "",
    kind: str = "request",
    from_role: str = "human",
    from_id: str = "terminal:term_1",
    trust: str = "trusted",
    priority: int = 2,
    dedupe_key: str = "",
    ticket: str = "",
    agent: str = "",
    dispatch: str = "",
    ask: str = "",
    decision: str = "",
    thread_key: str = "",
    body: dict | None = None,
    ttl_s: int = 0,
) -> dict:
    return {
        "v": 1,
        "id": message_id,
        "ts": ts,
        "from": {"role": from_role, "id": from_id, "trust": trust},
        "to": {"role": to_role, "id": to_id},
        "kind": kind,
        "correlation": {
            "ticket": ticket,
            "agent": agent,
            "dispatch": dispatch,
            "ask": ask,
            "decision": decision,
        },
        "thread_key": thread_key,
        "reply_to": "",
        "priority": priority,
        "ttl_s": ttl_s,
        "dedupe_key": dedupe_key,
        "body": {"text": "hello", "summary": "hello"} if body is None else body,
        "state": "queued",
        "attempts": 0,
        "lease": None,
        "trace": "",
    }


def decision(decision_id: str = "dec_1", **overrides) -> dict:
    data = {
        "decision_id": decision_id,
        "message_id": "msg_1",
        "action": "accept",
        "ticket": "tk_1",
        "reason": "clear request",
        "payload": {"provider": "claude"},
        "author": "router",
        "confidence": 0.9,
    }
    data.update(overrides)
    return data


def test_enqueue_and_duplicate_dedupe(store):
    row, inserted = store.enqueue(envelope("msg_1", dedupe_key="report:turn-1"))

    assert inserted is True
    assert row is not None
    assert row["id"] == "msg_1"
    assert row["message_id"] == "msg_1"
    assert row["state"] == "queued"
    assert row["body"] == {"text": "hello", "summary": "hello"}
    assert row["summary"] == "hello"
    assert row["from"] == {
        "role": "human", "id": "terminal:term_1", "trust": "trusted",
    }
    assert row["to"] == {"role": "router", "id": ""}
    assert row["lease"] is None
    assert row["attempts"] == 0

    again, inserted = store.enqueue(
        envelope("msg_2", dedupe_key="report:turn-1")
    )
    assert (again, inserted) == (None, False)

    same_id, inserted = store.enqueue(envelope("msg_1"))
    assert (same_id, inserted) == (None, False)

    assert store.counts() == {
        "router": 1, "human": 0, "worker": 0, "broadcast": 0,
    }
    assert store.get_message("msg_2") is None


def test_fifo_within_correlation_and_priority_across(store):
    store.enqueue(envelope(
        "msg_a1", priority=2, ticket="tk_a",
        ts="2026-11-14T13:15:00.000Z",
    ))
    store.enqueue(envelope(
        "msg_a2", priority=0, ticket="tk_a",
        ts="2026-11-14T13:15:01.000Z",
    ))
    store.enqueue(envelope(
        "msg_b1", priority=0, ticket="tk_b",
        ts="2026-11-14T13:15:02.000Z",
    ))

    leased = store.lease("router", limit=5, lease_s=60.0)

    # priority orders correlations, FIFO holds the newer same-ticket row back
    assert [row["id"] for row in leased] == ["msg_b1", "msg_a1"]
    assert store.get_message("msg_a2")["state"] == "queued"
    assert store.lease("router", limit=5) == []

    assert store.ack("msg_a1", "router") is True
    assert [row["id"] for row in store.lease("router")] == ["msg_a2"]


def test_lease_expire_ack_idempotency_and_release(store):
    store.enqueue(envelope("msg_1"))
    store.enqueue(envelope("msg_2", ts="2026-11-14T13:15:01.000Z"))

    leased = store.lease("router", lease_s=10.0, now=0.0)
    assert [row["id"] for row in leased] == ["msg_1"]
    assert leased[0]["lease"]["owner"] == "router"
    assert leased[0]["lease"]["until"] == 10.0
    assert leased[0]["attempts"] == 1
    assert store.leased_count() == 1

    assert store.ack("msg_1", "router") is True
    assert store.ack("msg_1", "router") is True          # idempotent repeat
    assert store.ack("msg_1", "someone_else") is False
    assert store.ack("msg_missing", "router") is False
    assert store.get_message("msg_1")["state"] == "acked"
    assert store.get_message("msg_1")["settled_at"] is not None

    leased = store.lease("router", lease_s=10.0, now=0.0)
    assert [row["id"] for row in leased] == ["msg_2"]
    assert store.expire_leases(now=time.time()) == 1
    expired = store.get_message("msg_2")
    assert expired["state"] == "queued"
    assert expired["lease"] is None

    leased = store.lease("router", lease_s=10.0, now=0.0)
    assert [row["id"] for row in leased] == ["msg_2"]
    store.release("msg_2", "wrong_owner")
    assert store.get_message("msg_2")["state"] == "leased"
    store.release("msg_2", "router", error="consumer crashed")
    released = store.get_message("msg_2")
    assert released["state"] == "queued"
    assert "consumer crashed" in released["trace"]

    store.lease("router", lease_s=10.0, now=0.0)
    store.release("msg_2", "router", dead=True)
    assert store.get_message("msg_2")["state"] == "dead"
    assert store.dead_count() == 1

    with pytest.raises(MailboxError) as info:
        store.settle("msg_missing")
    assert info.value.code == "unknown_message"


def test_decision_effect_settlement_and_outbox_commit(store):
    store.enqueue(envelope("msg_1", ticket="tk_1"))
    effects = [{
        "kind": "launch",
        "key": "tk_1",
        "payload": {"provider": "claude"},
    }]
    outbox = [
        {
            "ticket_id": "tk_1",
            "conversation": "terminal:term_1",
            "payload": {"line": "ack"},
        },
        {
            "ticket_id": "tk_1",
            "conversation": "terminal:term_1",
            "payload": {"line": "answer"},
        },
    ]

    assert store.commit_decision(
        decision(), effects=effects, settlements=["msg_1"], outbox=outbox
    ) is True

    stored = store.decision("dec_1")
    assert stored["state"] == "committed"
    assert stored["action"] == "accept"
    assert stored["payload"] == {"provider": "claude"}
    assert stored["confidence"] == pytest.approx(0.9)

    message = store.get_message("msg_1")
    assert message["state"] == "settled"
    assert message["settled_at"] is not None
    assert message["lease"] is None

    pending = store.pending_effects(now=time.time())
    assert [row["kind"] for row in pending] == ["launch"]
    assert pending[0]["payload"] == {"provider": "claude"}
    assert pending[0]["state"] == "pending"

    outbox_rows = store.outbox_pending()
    assert [(row["conversation"], row["seq"]) for row in outbox_rows] == [
        ("terminal:term_1", 1),
        ("terminal:term_1", 2),
    ]
    assert [row["payload"]["line"] for row in outbox_rows] == ["ack", "answer"]

    # A repeated decision id is an idempotent repeat: no writes at all.
    assert store.commit_decision(
        decision("dec_1"),
        effects=[{"kind": "stop", "key": "tk_1", "payload": {}}],
        settlements=["msg_1"],
        outbox=[{
            "ticket_id": "tk_1",
            "conversation": "terminal:term_1",
            "payload": {"line": "again"},
        }],
    ) is False
    assert [row["kind"] for row in store.pending_effects(now=time.time())] == [
        "launch"
    ]
    assert len(store.outbox_pending()) == 2
    assert store.decision("dec_1")["state"] == "committed"


def test_commit_decision_rejects_invalid_input_without_writes(store):
    store.enqueue(envelope("msg_1", ticket="tk_1"))

    with pytest.raises(MailboxError) as info:
        store.commit_decision(decision("dec_bad", action="launch"))
    assert info.value.code == "decision_invalid"
    assert store.decision("dec_bad") is None

    with pytest.raises(MailboxError) as info:
        store.commit_decision({
            "decision_id": "dec_bad",
            "action": "accept",
            "ticket": "tk_1",
        })
    assert info.value.code == "decision_invalid"
    assert store.decision("dec_bad") is None

    # A bad outbox row aborts the whole transaction: no decision, no effect.
    with pytest.raises(MailboxError) as info:
        store.commit_decision(
            decision("dec_atomic"),
            effects=[{"kind": "launch", "key": "tk_atomic", "payload": {}}],
            outbox=[{
                "ticket_id": "tk_1",
                "conversation": "",
                "payload": {},
            }],
        )
    assert info.value.code == "decision_invalid"
    assert store.decision("dec_atomic") is None
    assert store.pending_effects(now=time.time()) == []
    assert store.get_message("msg_1")["state"] == "queued"


def test_outbox_ordering_receipts_and_dead(store):
    one = store.outbox_put(
        ticket_id="tk_1", conversation="terminal:a", payload={"line": "1"}
    )
    two = store.outbox_put(
        ticket_id="tk_1", conversation="terminal:b", payload={"line": "b1"}
    )
    three = store.outbox_put(
        ticket_id="tk_1", conversation="terminal:a", payload={"line": "2"}
    )

    pending = store.outbox_pending()
    assert [(row["conversation"], row["seq"]) for row in pending] == [
        ("terminal:a", 1),
        ("terminal:a", 2),
        ("terminal:b", 1),
    ]

    store.outbox_sent(one, "sent")
    assert one not in [row["id"] for row in store.outbox_pending()]

    now = time.time()
    store.outbox_retry(two, next_at=now + 60.0, error="delivery failed")
    assert [row["id"] for row in store.outbox_pending(now=now)] == [three]
    later = {
        row["id"] for row in store.outbox_pending(now=now + 61.0)
    }
    assert later == {two, three}

    store.outbox_dead(three)
    assert three not in {row["id"] for row in store.outbox_pending(now=now + 61.0)}
    assert store.status()["dead"] == 1


def test_status_counts_and_applied_window(store):
    store.enqueue(envelope("msg_r1", ts="2026-11-14T13:15:00.000Z"))
    store.enqueue(envelope("msg_r2", ts="2026-11-14T13:15:01.000Z"))
    store.enqueue(envelope("msg_h1", to_role="human", priority=0))
    store.enqueue(envelope("msg_w1", to_role="worker"))
    store.enqueue(envelope("msg_b1", to_role="broadcast"))
    store.enqueue(envelope("msg_dead", to_role="dead"))

    store.lease("worker_owner", to_role="worker")
    store.lease("dead_owner", to_role="dead")
    store.release("msg_dead", "dead_owner", dead=True)

    effect = store.add_effect("launch", "tk_dead", {})
    store.claim_effect(effect, "owner")
    store.finish_effect(effect, state="dead", error="boom")
    outbox_id = store.outbox_put(
        ticket_id="tk_1", conversation="terminal:a", payload={}
    )
    store.outbox_dead(outbox_id)
    store.kv_set("doorbell:state", "pending")

    assert store.commit_decision(
        decision("dec_now", action="noop", ticket="tk_1")
    ) is True
    assert store.commit_decision(
        decision("dec_old", action="noop", ticket="tk_1")
    ) is True
    store.mark_decision("dec_now", "applied")
    store.mark_decision("dec_old", "applied")

    conn = sqlite3.connect(str(store.path))
    try:
        conn.execute(
            "UPDATE decisions SET ts = ? WHERE decision_id = ?",
            (time.time() - 400.0, "dec_old"),
        )
        conn.commit()
    finally:
        conn.close()

    status = store.status()

    assert status["schema_version"] == 1
    assert status["running"] is False
    assert status["pid"] == 0
    assert status["queues"] == {
        "router": 2, "human": 1, "worker": 1, "broadcast": 1,
    }
    assert status["leased"] == 1
    assert status["dead"] == 3
    assert status["spool_lag"] == 0
    assert status["doorbell"] == "pending"
    assert status["oldest_age_s"] >= 0.0
    assert status["decisions_applied_5m"] == 1

    assert set(status) == {
        "running", "pid", "schema_version", "queues", "oldest_age_s",
        "leased", "dead", "spool_lag", "doorbell", "decisions_applied_5m",
    }

    store.kv_set("doorbell:state", "consumed")
    assert store.status()["doorbell"] == "consumed"


def test_tickets_assignments_asks_reports_and_close(store):
    ticket_id = store.create_ticket(
        origin={"role": "human", "id": "terminal:term_1", "text": "fix watcher"},
        mandate={"actions": ["accept", "stop"], "answer": "free_text"},
        ticket_id="code_001",
    )
    assert ticket_id == "code_001"

    ticket = store.ticket(ticket_id)
    assert ticket["id"] == "code_001"
    assert ticket["ticket_id"] == "code_001"
    assert ticket["stage"] == "open"
    assert ticket["origin"]["text"] == "fix watcher"
    assert ticket["mandate"]["actions"] == ["accept", "stop"]
    assert ticket["assignments"] == []
    assert ticket["reports"] == []
    assert ticket["open_asks"] == []
    assert ticket["decisions"] == []
    assert ticket["closed_at"] is None

    assert store.add_assignment(ticket_id, {
        "agent_id": "code_001", "provider": "claude", "terminal": "term_1",
    }) == 1
    assert store.add_assignment(ticket_id, {
        "agent_id": "code_001", "provider": "claude", "terminal": "term_9",
    }) == 2
    assignments = store.assignments(ticket_id)
    assert [item["attempt"] for item in assignments] == [1, 2]

    store.set_assignment_state(ticket_id, "code_001", "done")
    assert {
        item["state"] for item in store.assignments(ticket_id)
    } == {"done"}

    store.open_ask(
        ticket_id, "ask_1", agent="code_001", message_id="msg_1"
    )
    store.open_ask(ticket_id, "ask_1", agent="code_001")
    assert len(store.ticket(ticket_id)["open_asks"]) == 1
    assert store.is_ask_open(ticket_id, "ask_1") is True
    store.close_ask(ticket_id, "ask_1")
    assert store.is_ask_open(ticket_id, "ask_1") is False
    store.close_ask(ticket_id, "ask_1")      # idempotent

    store.add_report(ticket_id, {"outcome": "ready", "tools_observed": True})
    assert [item["outcome"] for item in store.reports(ticket_id)] == ["ready"]

    store.add_decision_ref(ticket_id, "dec_1")
    store.add_decision_ref(ticket_id, "dec_1")
    assert store.ticket(ticket_id)["decisions"] == ["dec_1"]

    updated = store.update_ticket(
        ticket_id, stage="working", mandate={"actions": ["accept"]}
    )
    assert updated["stage"] == "working"
    assert updated["mandate"]["actions"] == ["accept"]

    store.close_ticket(ticket_id, reason="done")
    closed = store.ticket(ticket_id)
    assert closed["stage"] == "done"
    assert closed["closed_at"] is not None

    assert [item["id"] for item in store.tickets()] == ["code_001"]

    generated = store.create_ticket(origin={}, mandate={}, thread_key="chat:1")
    assert generated.startswith("tkt_")
    assert store.ticket(generated)["thread_key"] == "chat:1"

    with pytest.raises(MailboxError) as info:
        store.create_ticket(origin={}, mandate={}, ticket_id="code_001")
    assert info.value.code == "invalid_field"


@pytest.mark.parametrize("method,args", [
    ("assignments", ("missing",)),
    ("reports", ("missing",)),
    ("close_ticket", ("missing",)),
    ("close_ask", ("missing", "ask_1")),
])
def test_ticket_mutations_refuse_unknown_ticket(store, method, args):
    with pytest.raises(MailboxError) as info:
        getattr(store, method)(*args)
    assert info.value.code == "unknown_ticket"


def test_ticket_for_agent_finds_the_newest_live_ticket(store):
    def stamp(ticket_id: str, updated_at: float) -> None:
        conn = sqlite3.connect(str(store.path))
        try:
            conn.execute(
                "UPDATE tickets SET updated_at = ? WHERE id = ?",
                (updated_at, ticket_id),
            )
            conn.commit()
        finally:
            conn.close()

    assert store.ticket_for_agent("agent_001") is None
    assert store.ticket_for_agent("") is None

    store.create_ticket(origin={}, mandate={}, ticket_id="t_old")
    store.add_assignment("t_old", {"agent_id": "agent_001", "state": "running"})
    stamp("t_old", 100.0)

    store.create_ticket(origin={}, mandate={}, ticket_id="t_fresh")
    store.add_assignment("t_fresh", {"agent_id": "agent_001", "state": "running"})
    stamp("t_fresh", 200.0)

    assert store.ticket_for_agent("agent_001")["id"] == "t_fresh"
    assert store.ticket_for_agent("agent_002") is None

    # Finished Tickets are not current cases, however recent they are.
    store.update_ticket("t_fresh", stage="done")
    stamp("t_fresh", 300.0)
    assert store.ticket_for_agent("agent_001")["id"] == "t_old"
    store.update_ticket("t_old", stage="stopped")
    assert store.ticket_for_agent("agent_001") is None

    # A report or a decision ref is not an assignment.
    store.create_ticket(origin={}, mandate={}, ticket_id="t_other")
    store.add_report("t_other", {"outcome": "ready"})
    store.add_decision_ref("t_other", "dec_1")
    store.update_ticket("t_other", stage="review")
    assert store.ticket_for_agent("agent_001") is None


def test_update_ticket_refuses_unknown_ticket_and_stage(store):
    with pytest.raises(MailboxError) as info:
        store.update_ticket("missing", stage="done")
    assert info.value.code == "unknown_ticket"

    ticket_id = store.create_ticket(origin={}, mandate={})
    with pytest.raises(MailboxError) as info:
        store.update_ticket(ticket_id, stage="shipping")
    assert info.value.code == "invalid_field"


def test_effect_exists_by_idempotency_key(store):
    assert store.effect_exists("launch", "tk_1") is False

    effect = store.add_effect("launch", "tk_1", {"provider": "claude"})

    assert store.effect_exists("launch", "tk_1") is True
    assert store.effect_exists("stop", "tk_1") is False
    store.finish_effect(effect, state="dead", error="gave up")
    assert store.effect_exists("launch", "tk_1") is True


def test_pending_effects_reclaims_an_expired_running_claim(store):
    effect = store.add_effect("launch", "tk_1", {})
    assert store.claim_effect(effect, "daemon_a", now=100.0, lease_s=60.0) is True

    # A live claim is not due and cannot be taken over.
    assert store.pending_effects(now=159.0) == []
    assert store.claim_effect(effect, "daemon_b", now=159.0) is False

    # The owner died: the claim expired, so the row is due again.
    reclaimed = store.pending_effects(now=161.0)
    assert [row["id"] for row in reclaimed] == [effect]
    assert reclaimed[0]["state"] == "running"
    assert reclaimed[0]["claim_owner"] == "daemon_a"

    assert store.claim_effect(effect, "daemon_b", now=161.0) is True
    row = store.pending_effects(now=300.0)[0]
    assert row["attempts"] == 2
    assert row["claim_owner"] == "daemon_b"

    store.finish_effect(effect, state="done")
    assert store.pending_effects(now=10**9) == []


def test_effect_claim_backoff_and_dead_letter(store):
    first = store.add_effect("launch", "tk_1", {"provider": "codex"})
    assert store.add_effect("launch", "tk_1", {"provider": "claude"}) == first

    pending = store.pending_effects(now=time.time())
    assert [row["id"] for row in pending] == [first]
    assert pending[0]["payload"] == {"provider": "codex"}
    assert pending[0]["attempts"] == 0

    assert store.claim_effect(first, "owner_a", now=100.0) is True
    assert store.claim_effect(first, "owner_b", now=110.0) is False

    store.finish_effect(first, state="pending", error="hook failed", next_at=500.0)
    retried = store.pending_effects(now=10**9)
    assert retried[0]["attempts"] == 1
    assert retried[0]["last_error"] == "hook failed"
    assert retried[0]["claim_owner"] is None
    assert store.pending_effects(now=499.0) == []

    assert store.claim_effect(first, "owner_b", now=501.0) is True
    store.finish_effect(first, state="done")
    assert store.pending_effects(now=10**9) == []
    assert store.dead_letters() == []

    second = store.add_effect("stop", "tk_1", {})
    assert store.claim_effect(second, "owner_c") is True
    store.finish_effect(second, state="dead", error="gave up")
    assert [row["id"] for row in store.dead_letters()] == [second]
    assert store.dead_letters()[0]["last_error"] == "gave up"


def test_kv_and_router_binding(store):
    assert store.kv_get("doorbell:state") is None
    store.kv_set("doorbell:state", "pending")
    assert store.kv_get("doorbell:state") == "pending"
    store.kv_set("doorbell:state", "consumed")
    assert store.kv_get("doorbell:state") == "consumed"

    assert store.router_binding() is None
    store.bind_router("terminal:term_router", "abc123")
    binding = store.router_binding()
    assert binding["address"] == "terminal:term_router"
    assert binding["token"] == "abc123"
    assert binding["bound_at"] > 0

    store.bind_router("terminal:term_router2", "def456")
    assert store.router_binding()["token"] == "def456"


def test_unicode_bodies_and_tickets(store):
    body = {"text": "把 watcher 的超时改成可配置 ⏱️", "summary": "配置超时"}
    row, inserted = store.enqueue(
        envelope("msg_uni", body=body, ticket="代码_001")
    )
    assert inserted is True
    assert row["body"] == body
    assert row["correlation"]["ticket"] == "代码_001"

    fresh = store.get_message("msg_uni")
    assert fresh["body"] == body
    assert fresh["summary"] == "配置超时"

    ticket_id = store.create_ticket(
        origin={"role": "human", "text": "修复中文"},
        mandate={"actions": ["accept"]},
        ticket_id="工单_001",
    )
    assert store.ticket(ticket_id)["origin"]["text"] == "修复中文"
    assert store.ticket(ticket_id)["ticket_id"] == "工单_001"


def test_second_connection_sees_leases_and_acks(tmp_path):
    path = tmp_path / "mailbox.db"
    first = Store(path)
    second = Store(path)
    try:
        assert first.enqueue(envelope("msg_1"))[1] is True
        assert second.get_message("msg_1")["state"] == "queued"

        leased = second.lease("router_b", lease_s=60.0)
        assert [row["id"] for row in leased] == ["msg_1"]
        assert first.lease("router_a") == []
        assert first.ack("msg_1", "router_a") is False
        assert second.ack("msg_1", "router_b") is True
        assert first.get_message("msg_1")["state"] == "acked"

        assert first.counts() == {
            "router": 0, "human": 0, "worker": 0, "broadcast": 0,
        }
        assert first.enqueue(envelope("msg_2"))[1] is True
        assert second.get_message("msg_2") is not None
    finally:
        first.close()
        second.close()


def test_writer_while_reader_snapshot_is_open(tmp_path):
    path = tmp_path / "mailbox.db"
    store = Store(path)
    reader = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    try:
        assert store.enqueue(envelope("msg_1"))[1] is True
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1

        # WAL: a writer commits while a reader snapshot is open.
        assert store.enqueue(envelope("msg_2"))[1] is True
        leased = store.lease("router")
        assert [row["id"] for row in leased] == ["msg_1"]

        reader.execute("COMMIT")
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    finally:
        reader.close()
        store.close()

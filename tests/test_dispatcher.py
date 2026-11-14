from __future__ import annotations

import json

import pytest

from osw import dispatcher
from osw.mailbox.errors import MailboxError
from osw.mailbox.store import Store

MANDATE = {
    "actions": [
        "accept",
        "followup",
        "answer",
        "stop",
        "retry",
        "reject",
        "escalate",
        "noop",
        "close",
    ],
    "answer": "free_text",
    "max_retries": 2,
    "max_workers": 4,
    "providers": ["claude", "codex", "pi", "omp", "kimi", "grok", "dsh"],
    "open_from": [],
    "close": "router",
    "expires_at": "",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(tmp_path):
    mailbox = tmp_path / ".orca" / "osw" / "mailbox"
    mailbox.mkdir(parents=True)
    s = Store(mailbox / "mailbox.db")
    s.root = tmp_path
    mandate_file = tmp_path / "skill" / "osw" / "mandate.json"
    mandate_file.parent.mkdir(parents=True)
    mandate_file.write_text(json.dumps(MANDATE), encoding="utf-8")
    try:
        yield s
    finally:
        s.close()


class FakeHooks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_launch = 0
        self.launch_result = {
            "agent_id": "agent_001",
            "terminal": "term_1",
            "dispatch_id": "disp_1",
        }

    def count(self, kind: str) -> int:
        return sum(1 for name, _ in self.calls if name == kind)

    async def launch(self, ticket: dict, payload: dict) -> dict:
        self.calls.append(("launch", payload))
        if self.fail_launch > 0:
            self.fail_launch -= 1
            raise RuntimeError("boom")
        return dict(self.launch_result)

    async def followup(self, ticket: dict, payload: dict) -> None:
        self.calls.append(("followup", payload))

    async def answer(self, ticket: dict, payload: dict) -> None:
        self.calls.append(("answer", payload))

    async def stop(self, ticket: dict) -> None:
        self.calls.append(("stop", {}))


def make_ticket(store, ticket_id: str = "t_001", **over) -> str:
    mandate = dict(MANDATE)
    mandate.update(over.pop("mandate", {}))
    store.create_ticket(
        origin={"source": "test"},
        mandate=mandate,
        ticket_id=ticket_id,
        thread_key=ticket_id,
        **over,
    )
    return ticket_id


def decision(**over) -> dict:
    base = {
        "decision_id": "dec_1",
        "message_id": "",
        "action": "accept",
        "ticket": "t_001",
        "reason": "test decision",
        "payload": {},
        "author": "router",
        "confidence": 0.9,
    }
    base.update(over)
    return base


def queued_message(
    store,
    message_id: str = "msg_1",
    *,
    role: str = "human",
    source: str = "terminal:term_1",
) -> str:
    """Enqueue one queued message so a settlement has a row to change."""
    store.enqueue(
        {
            "id": message_id,
            "ts": "2026-11-14T13:15:00.000Z",
            "from": {"role": role, "id": source, "trust": "trusted"},
            "to": {"role": "router", "id": ""},
            "kind": "request",
            "correlation": {
                "ticket": "",
                "agent": "",
                "dispatch": "",
                "ask": "",
                "decision": "",
            },
            "thread_key": "",
            "reply_to": "",
            "priority": 2,
            "ttl_s": 0,
            "dedupe_key": "",
            "body": {"text": "please look", "summary": "please look"},
            "state": "queued",
            "attempts": 0,
            "lease": None,
            "trace": "",
        }
    )
    return message_id


# ---------------------------------------------------------------------------
# validation and planning


def test_validate_decision_defaults_and_closed_fields():
    d = dispatcher.validate_decision(
        {
            "decision_id": "dec_1",
            "action": "accept",
            "ticket": "t_001",
            "reason": " 明确请求 ",
        }
    )

    assert d["payload"] == {}
    assert d["author"] == "router"
    assert d["confidence"] == 0.0
    assert d["reason"] == "明确请求"
    assert d["message_id"] == ""


@pytest.mark.parametrize(
    "broken",
    [
        {"action": "explode"},
        {"reason": "   "},
        {"reason": None},
        {"confidence": 1.5},
        {"confidence": "high"},
        {"author": "watcher"},
        {"payload": []},
        {"action": "answer", "payload": {"ask": "a_1", "text": "\x00\x01"}},
        {"action": "answer", "payload": {"ask": "", "text": "ok"}},
        {"action": "answer", "payload": {"ask": "a_1", "text": ""}},
        {"action": "retry", "payload": {"attempt": -1}},
        {"action": "retry", "payload": {"attempt": "1"}},
        {"action": "accept", "payload": {"provider": 7}},
        {"ticket": 7},
        {"decision_id": "dec_1 extra"},
        {"decision_id": "decision_1"},
        {"decision_id": "dec_" + "a" * 121},
    ],
)
def test_validate_decision_rejects_invalid(broken):
    raw = {
        "decision_id": "dec_1",
        "action": "accept",
        "ticket": "t_001",
        "reason": "reason",
        "payload": {},
    }
    raw.update(broken)

    with pytest.raises(MailboxError) as excinfo:
        dispatcher.validate_decision(raw)

    assert excinfo.value.code == "decision_invalid"


@pytest.mark.parametrize(
    "action", ["accept", "followup", "answer", "stop", "retry", "close"]
)
def test_validate_decision_requires_a_ticket_where_the_spec_does(action):
    raw = {
        "decision_id": "dec_1",
        "action": action,
        "ticket": "",
        "reason": "reason",
        "payload": {},
    }

    with pytest.raises(MailboxError) as excinfo:
        dispatcher.validate_decision(raw)

    assert excinfo.value.code == "decision_invalid"
    assert "ticket is required" in excinfo.value.message
    assert action in excinfo.value.message


@pytest.mark.parametrize("action", ["noop", "reject", "escalate"])
def test_validate_decision_allows_a_ticketless_message_action(action):
    d = dispatcher.validate_decision(
        {
            "decision_id": "dec_1",
            "action": action,
            "reason": "reason",
            "payload": {},
        }
    )

    assert d["action"] == action
    assert d["ticket"] == ""


def test_validate_decision_hardens_the_id_and_flattens_answer_text():
    d = dispatcher.validate_decision(
        {
            "decision_id": "dec_20261114T131500000Z_ab12cd",
            "action": "answer",
            "ticket": "t_001",
            "reason": "answer the worker",
            "payload": {"ask": "ask_1", "text": "  yes\r\nplease\tnow\x00"},
        }
    )

    assert d["decision_id"] == "dec_20261114T131500000Z_ab12cd"
    assert d["payload"]["text"] == "  yes please now "
    assert "\n" not in d["payload"]["text"]
    assert "\t" not in d["payload"]["text"]

    # The stored decision keeps the flattened text, so the template is one line.
    assert dispatcher.plan_effects(d)[0]["payload"]["text"] == "  yes please now "


def test_plan_effects_keys_follow_the_spec_table():
    def key(**over):
        effects = dispatcher.plan_effects(decision(**over))
        return [(e["kind"], e["key"]) for e in effects]

    assert key(action="accept") == [("launch", "t_001")]
    assert key(action="followup", message_id="msg_1") == [("launch", "t_001:msg_1")]
    assert key(
        action="answer",
        payload={"ask": "ask_9", "text": "1) yes"},
    ) == [("answer", "ask_9")]
    assert key(action="stop") == [("stop", "t_001")]
    assert key(action="retry", payload={"attempt": 1}) == [("retry", "t_001:1")]
    assert key(action="reject", message_id="msg_1") == [("notify", "msg_1")]
    assert key(action="escalate", message_id="msg_1") == [("escalate", "msg_1")]
    assert key(action="noop", message_id="msg_1") == []
    assert key(action="close") == [("close_ticket", "t_001")]


# ---------------------------------------------------------------------------
# commit


def test_duplicate_decision_id_is_an_idempotent_repeat(store):
    make_ticket(store)
    hooks = FakeHooks()

    assert dispatcher.commit(store, decision()) is True
    assert dispatcher.commit(store, decision()) is False
    # Same id, different action: still the already-committed decision.
    assert dispatcher.commit(store, decision(action="stop")) is False

    effects = store.pending_effects()
    assert [(e["kind"], e["key"]) for e in effects] == [("launch", "t_001")]
    assert store.pending_effects(limit=50)  # still exactly one due effect


def test_accept_creates_the_missing_ticket_and_loads_the_mandate(store):
    assert store.ticket("t_new") is None

    assert dispatcher.commit(store, decision(ticket="t_new", decision_id="dec_new")) is True

    ticket = store.ticket("t_new")
    assert ticket is not None
    assert ticket["mandate"]["providers"] == MANDATE["providers"]
    assert [e["kind"] for e in store.pending_effects()] == ["launch"]


def test_non_accept_on_a_missing_ticket_is_refused(store):
    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(store, decision(action="stop"))

    assert excinfo.value.code == "unknown_ticket"


def test_accept_from_an_unlisted_source_creates_no_ticket(store):
    queued_message(store, "msg_sys", role="system", source="adapter:build")

    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(
            store,
            decision(ticket="t_sys", decision_id="dec_sys", message_id="msg_sys"),
        )

    assert excinfo.value.code == "mandate_denied"
    assert store.ticket("t_sys") is None
    assert store.tickets() == []
    assert store.decision("dec_sys") is None
    assert store.pending_effects() == []
    assert store.outbox_pending() == []
    assert store.get_message("msg_sys")["state"] == "queued"


def test_accept_from_a_listed_source_opens_the_ticket(store):
    (store.root / "skill" / "osw" / "mandate.json").write_text(
        json.dumps(dict(MANDATE, open_from=["system"])), encoding="utf-8"
    )
    queued_message(store, "msg_sys", role="system", source="adapter:build")

    assert dispatcher.commit(
        store,
        decision(ticket="t_sys", decision_id="dec_sys", message_id="msg_sys"),
    ) is True

    ticket = store.ticket("t_sys")
    assert ticket["origin"]["role"] == "system"
    assert [effect["kind"] for effect in store.pending_effects()] == ["launch"]


def test_accept_from_a_human_message_needs_no_open_from_entry(store):
    queued_message(store, "msg_human")

    assert dispatcher.commit(
        store,
        decision(
            ticket="t_human",
            decision_id="dec_human",
            message_id="msg_human",
        ),
    ) is True

    assert store.ticket("t_human")["origin"]["role"] == "human"


def test_a_second_decision_with_the_same_effect_is_recorded_not_swallowed(store):
    make_ticket(store, mandate={"max_retries": 2})

    assert dispatcher.commit(
        store,
        decision(action="retry", decision_id="dec_retry_a", payload={}),
    ) is True

    # A retry with no ``attempt`` repeats key t_001:0, so the Effect is
    # skipped; the second Decision must still be recorded.
    assert dispatcher.commit(
        store,
        decision(action="retry", decision_id="dec_retry_b", payload={}),
    ) is True

    second = store.decision("dec_retry_b")
    assert second is not None
    assert second["state"] == "failed"
    assert second["result"] == "duplicate_effect"
    assert store.decision("dec_retry_a")["state"] == "committed"
    assert [effect["key"] for effect in store.pending_effects()] == ["t_001:0"]

    def notices():
        return [row for row in store.outbox_pending() if row["conversation"] == "human"]

    assert len(notices()) == 1
    line = notices()[0]["payload"]["line"]
    assert line.startswith("# [osw] attention duplicate_effect")
    assert "dec_retry_b" in line
    assert "retry:t_001:0" in line
    assert "\n" not in line

    # A third repeat is recorded the same way and adds exactly one notice.
    assert dispatcher.commit(
        store,
        decision(action="retry", decision_id="dec_retry_c", payload={}),
    ) is True
    assert store.decision("dec_retry_c")["result"] == "duplicate_effect"
    assert len(notices()) == 2


def test_noop_without_a_ticket_settles_the_message(store):
    queued_message(store, "msg_1")

    assert dispatcher.commit(
        store,
        decision(
            action="noop",
            ticket="",
            message_id="msg_1",
            decision_id="dec_noop",
            reason="bookkeeping only",
        ),
    ) is True

    message = store.get_message("msg_1")
    assert message["state"] == "settled"
    assert message["settled_at"]
    assert store.decision("dec_noop")["state"] == "applied"
    assert store.tickets() == []
    assert store.pending_effects() == []
    assert store.outbox_pending() == []
    # The decision id is the idempotency guard: a repeat writes nothing.
    assert dispatcher.commit(
        store,
        decision(
            action="noop",
            ticket="",
            message_id="msg_1",
            decision_id="dec_noop",
            reason="bookkeeping only",
        ),
    ) is False
    assert store.decision("dec_noop")["state"] == "applied"
    assert store.pending_effects() == []


def test_reject_without_a_ticket_notifies_the_sender_once(store):
    queued_message(store, "msg_7", source="terminal:term_9")

    assert dispatcher.commit(
        store,
        decision(
            action="reject",
            ticket="",
            message_id="msg_7",
            decision_id="dec_rej",
            reason="out of scope",
        ),
    ) is True

    assert store.get_message("msg_7")["state"] == "settled"
    assert store.tickets() == []
    assert store.pending_effects() == []
    rows = store.outbox_pending()
    assert len(rows) == 1
    assert rows[0]["conversation"] == "terminal:term_9"
    assert rows[0]["ticket_id"] == ""
    line = rows[0]["payload"]["line"]
    assert line.startswith("# [osw] attention reject")
    assert "out of scope" in line
    assert "ticket=" not in line
    assert "\n" not in line


def test_escalate_without_a_ticket_queues_one_human_notice(store):
    queued_message(store, "msg_9")

    assert dispatcher.commit(
        store,
        decision(
            action="escalate",
            ticket="",
            message_id="msg_9",
            decision_id="dec_esc",
            reason="needs a human decision",
        ),
    ) is True

    assert store.get_message("msg_9")["state"] == "settled"
    assert store.tickets() == []
    assert store.pending_effects() == []
    rows = store.outbox_pending()
    assert len(rows) == 1
    assert rows[0]["conversation"] == "human"
    assert rows[0]["ticket_id"] == ""
    assert rows[0]["payload"]["attention"] == "human"
    line = rows[0]["payload"]["line"]
    assert line.startswith("# [osw] attention escalate")
    assert "needs a human decision" in line
    assert "ticket=" not in line
    assert "\n" not in line


def test_mandate_denies_action_provider_and_expiry(store):
    make_ticket(store)
    denied_action = dict(MANDATE, actions=["accept", "noop"])
    store.update_ticket("t_001", mandate=denied_action)

    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(store, decision(action="stop", decision_id="dec_stop"))
    assert excinfo.value.code == "mandate_denied"

    make_ticket(store, "t_002")
    store.update_ticket("t_002", mandate=dict(MANDATE, providers=["claude"]))
    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(
            store,
            decision(ticket="t_002", payload={"provider": "grok"}),
        )
    assert excinfo.value.code == "mandate_denied"

    make_ticket(store, "t_003")
    store.update_ticket("t_003", mandate=dict(MANDATE, expires_at="2000-01-01T00:00:00Z"))
    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(store, decision(ticket="t_003", decision_id="dec_exp"))
    assert excinfo.value.code == "mandate_expired"

    assert store.decision("dec_stop") is None
    assert store.decision("dec_exp") is None


def test_answer_limits_enforce_the_cap_and_the_mode(store):
    make_ticket(store)
    store.open_ask("t_001", "ask_1", agent="agent_001", message_id="msg_1")

    # The text is flattened before the cap, so a multi-line answer commits as
    # one line instead of being refused.
    assert dispatcher.commit(
        store,
        decision(
            action="answer",
            decision_id="dec_multiline",
            payload={"ask": "ask_1", "text": "yes\nplease"},
        ),
    ) is True
    assert store.decision("dec_multiline")["payload"]["text"] == "yes please"

    make_ticket(store, "t_long")
    store.open_ask("t_long", "ask_long", agent="agent_301")
    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(
            store,
            decision(
                action="answer",
                decision_id="dec_too_long",
                ticket="t_long",
                payload={"ask": "ask_long", "text": "x" * 4080},
            ),
        )
    assert excinfo.value.code == "mandate_denied"

    assert dispatcher.commit(
        store,
        decision(
            action="answer",
            decision_id="dec_ok",
            ticket="t_long",
            payload={"ask": "ask_long", "text": "x" * 4000},
        ),
    ) is True

    make_ticket(store, "t_002", mandate={"answer": "options_only"})
    store.open_ask("t_002", "ask_2", agent="agent_002")
    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(
            store,
            decision(
                action="answer",
                decision_id="dec_free",
                ticket="t_002",
                payload={"ask": "ask_2", "text": "sure thing"},
            ),
        )
    assert excinfo.value.code == "mandate_denied"
    assert dispatcher.commit(
        store,
        decision(
            action="answer",
            decision_id="dec_option",
            ticket="t_002",
            payload={"ask": "ask_2", "text": "1) sure"},
        ),
    ) is True


# ---------------------------------------------------------------------------
# run_pending / apply_effect


@pytest.mark.anyio
async def test_accept_launches_and_records_the_assignment(store):
    make_ticket(store)
    hooks = FakeHooks()
    dispatcher.commit(store, decision())

    results = await dispatcher.run_pending(store, hooks)

    assert [r["state"] for r in results] == ["done"]
    assert results[0]["result"] == "launched"
    assert hooks.count("launch") == 1
    assignments = store.assignments("t_001")
    assert [(a["agent_id"], a["state"]) for a in assignments] == [("agent_001", "running")]
    assert store.ticket("t_001")["stage"] == "working"
    assert store.pending_effects() == []


@pytest.mark.anyio
async def test_accept_reuses_an_existing_assignment(store):
    make_ticket(store)
    store.add_assignment(
        "t_001",
        {"agent_id": "agent_001", "terminal": "term_1", "state": "running"},
    )
    hooks = FakeHooks()
    dispatcher.commit(store, decision())

    results = await dispatcher.run_pending(store, hooks)

    assert results[0]["result"] == "reused"
    assert hooks.count("launch") == 0
    assert hooks.count("followup") == 1
    assert len(store.assignments("t_001")) == 1


@pytest.mark.anyio
async def test_answer_is_dropped_when_the_ask_is_already_closed(store):
    make_ticket(store)
    store.open_ask("t_001", "ask_1", agent="agent_001", message_id="msg_1")
    store.close_ask("t_001", "ask_1")
    hooks = FakeHooks()
    assert dispatcher.commit(
        store,
        decision(
            action="answer",
            decision_id="dec_late",
            payload={"ask": "ask_1", "text": "too late"},
        ),
    ) is True

    results = await dispatcher.run_pending(store, hooks)

    assert results[0]["state"] == "done"
    assert results[0]["result"] == "ask_not_open"
    assert hooks.count("answer") == 0
    assert store.decision("dec_late")["state"] == "applied"


@pytest.mark.anyio
async def test_answer_passes_the_fixed_template_while_the_ask_is_open(store):
    make_ticket(store)
    store.open_ask("t_001", "ask_1", agent="agent_001", message_id="msg_1")
    hooks = FakeHooks()
    dispatcher.commit(
        store,
        decision(
            action="answer",
            decision_id="dec_answer",
            payload={"ask": "ask_1", "text": "1) yes"},
        ),
    )

    results = await dispatcher.run_pending(store, hooks)

    assert results[0]["result"] == "answered"
    payload = hooks.calls[0][1]
    assert payload["template"] == "# [osw] answer dec_answer: 1) yes"
    assert "\n" not in payload["template"]


@pytest.mark.anyio
async def test_stop_happens_once_and_a_stopped_ticket_refuses_accept(store):
    make_ticket(store)
    hooks = FakeHooks()
    dispatcher.commit(store, decision(decision_id="dec_accept"))
    await dispatcher.run_pending(store, hooks)

    dispatcher.commit(store, decision(action="stop", decision_id="dec_stop_1"))
    await dispatcher.run_pending(store, hooks)
    assert hooks.count("stop") == 1
    assert store.ticket("t_001")["stage"] == "stopped"
    assert [a["state"] for a in store.assignments("t_001")] == ["stopped"]

    # The stop Effect is keyed on the ticket: a second stop adds no Effect.
    assert dispatcher.commit(
        store,
        decision(action="stop", decision_id="dec_stop_2"),
    ) is True
    assert await dispatcher.run_pending(store, hooks) == []
    assert hooks.count("stop") == 1

    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(store, decision(decision_id="dec_again"))
    assert excinfo.value.code == "mandate_denied"


def test_retry_budget_and_worker_budget(store):
    make_ticket(store, mandate={"max_retries": 1, "max_workers": 4})

    assert dispatcher.commit(
        store,
        decision(action="retry", decision_id="dec_retry_0", payload={"attempt": 0}),
    ) is True
    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(
            store,
            decision(action="retry", decision_id="dec_retry_1", payload={"attempt": 1}),
        )
    assert excinfo.value.code == "mandate_denied"
    assert store.decision("dec_retry_1") is None
    assert [e["key"] for e in store.pending_effects()] == ["t_001:0"]

    make_ticket(store, "t_002", mandate={"max_retries": 2, "max_workers": 1})
    store.add_assignment("t_002", {"agent_id": "agent_001", "state": "running"})
    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(
            store,
            decision(
                ticket="t_002",
                action="retry",
                decision_id="dec_crowded",
                payload={"attempt": 0},
            ),
        )
    assert excinfo.value.code == "mandate_denied"


def test_close_requires_no_open_ask_and_no_running_assignment(store):
    make_ticket(store)
    store.open_ask("t_001", "ask_1", agent="agent_001")

    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(store, decision(action="close", decision_id="dec_close_1"))
    assert excinfo.value.code == "close_precondition_failed"

    store.close_ask("t_001", "ask_1")
    store.add_assignment("t_001", {"agent_id": "agent_001", "state": "running"})
    with pytest.raises(MailboxError) as excinfo:
        dispatcher.commit(store, decision(action="close", decision_id="dec_close_2"))
    assert excinfo.value.code == "close_precondition_failed"

    store.set_assignment_state("t_001", "agent_001", "done")
    assert dispatcher.commit(
        store,
        decision(action="close", decision_id="dec_close_3"),
    ) is True
    assert store.decision("dec_close_1") is None


@pytest.mark.anyio
async def test_close_closes_the_ticket(store):
    make_ticket(store)
    hooks = FakeHooks()
    dispatcher.commit(store, decision(action="close", decision_id="dec_close"))

    results = await dispatcher.run_pending(store, hooks)

    assert results[0]["result"] == "closed"
    ticket = store.ticket("t_001")
    assert ticket["closed_at"] or ticket["stage"] in ("done", "closed")


@pytest.mark.anyio
async def test_answer_closes_the_ask_and_a_second_answer_is_recorded(store):
    make_ticket(store)
    store.open_ask("t_001", "ask_1", agent="agent_001", message_id="msg_1")
    hooks = FakeHooks()
    assert dispatcher.commit(
        store,
        decision(
            action="answer",
            decision_id="dec_answer_1",
            payload={"ask": "ask_1", "text": "1) yes"},
        ),
    ) is True

    first = await dispatcher.run_pending(store, hooks)

    assert first[0]["result"] == "answered"
    assert hooks.count("answer") == 1
    assert store.is_ask_open("t_001", "ask_1") is False
    assert store.decision("dec_answer_1")["state"] == "applied"
    assert store.decision("dec_answer_1")["result"] == "answered"

    assert dispatcher.commit(
        store,
        decision(
            action="answer",
            decision_id="dec_answer_2",
            payload={"ask": "ask_1", "text": "1) no"},
        ),
    ) is True

    second = store.decision("dec_answer_2")
    assert second["state"] == "applied"
    assert second["result"] == "ask_already_answered"
    assert await dispatcher.run_pending(store, hooks) == []
    assert hooks.count("answer") == 1
    assert [
        row for row in store.outbox_pending() if row["conversation"] == "human"
    ] == []


@pytest.mark.anyio
async def test_run_pending_reclaims_an_effect_whose_daemon_died(store):
    make_ticket(store)
    hooks = FakeHooks()
    dispatcher.commit(store, decision())

    effect = store.pending_effects(now=0.0)[0]
    assert store.claim_effect(effect["id"], "dead_daemon", now=0.0, lease_s=60.0)
    assert store.pending_effects(now=59.0) == []

    results = await dispatcher.run_pending(store, hooks, now=61.0)

    assert [row["state"] for row in results] == ["done"]
    assert results[0]["attempts"] == 2
    assert hooks.count("launch") == 1
    assert store.decision("dec_1")["state"] == "applied"


@pytest.mark.anyio
async def test_hook_failure_returns_to_pending_with_exponential_backoff(store):
    make_ticket(store)
    hooks = FakeHooks()
    hooks.fail_launch = 2
    dispatcher.commit(store, decision())
    start = 1_000.0

    first = await dispatcher.run_pending(store, hooks, now=start)
    assert first[0]["state"] == "pending"
    assert first[0]["attempts"] == 1
    effect = store.pending_effects(now=start + 1.99)
    assert effect == []
    due = store.pending_effects(now=start + 2.0)
    assert due and due[0]["attempts"] == 1

    second = await dispatcher.run_pending(store, hooks, now=start + 3.0)
    assert second[0]["state"] == "pending"
    assert second[0]["attempts"] == 2
    assert store.pending_effects(now=start + 6.9) == []
    assert store.pending_effects(now=start + 7.0)

    third = await dispatcher.run_pending(store, hooks, now=start + 10.0)
    assert third[0]["state"] == "done"
    assert hooks.count("launch") == 3
    assert store.pending_effects(now=start + 100.0) == []
    assert store.decision("dec_1")["state"] == "applied"


@pytest.mark.anyio
async def test_hook_failure_dead_letters_after_eight_attempts(store):
    make_ticket(store)
    hooks = FakeHooks()
    hooks.fail_launch = 100
    dispatcher.commit(store, decision())
    now = 5_000.0

    for _ in range(dispatcher.MAX_ATTEMPTS):
        results = await dispatcher.run_pending(store, hooks, now=now)
        now += 1_000.0

    assert results[0]["state"] == "dead"
    assert results[0]["attempts"] == dispatcher.MAX_ATTEMPTS
    assert hooks.count("launch") == dispatcher.MAX_ATTEMPTS
    assert len(store.dead_letters()) == 1
    assert store.decision("dec_1")["state"] == "failed"

    notices = [
        row
        for row in store.outbox_pending()
        if row["conversation"] == "human"
    ]
    assert notices, "a dead letter must enqueue a human notice"
    line = notices[0]["payload"]["line"]
    assert line.startswith("# [osw] attention dead_letter")
    assert "boom" in line
    assert "\n" not in line


@pytest.mark.anyio
async def test_escalate_enqueues_one_human_notice(store):
    make_ticket(store)
    hooks = FakeHooks()
    assert dispatcher.commit(
        store,
        decision(
            action="escalate",
            decision_id="dec_esc",
            message_id="msg_9",
            reason="needs a human decision",
        ),
    ) is True

    rows = store.outbox_pending()
    assert len(rows) == 1
    assert rows[0]["conversation"] == "human"
    line = rows[0]["payload"]["line"]
    assert line.startswith("# [osw] attention escalate")
    assert "needs a human decision" in line
    assert "\n" not in line

    results = await dispatcher.run_pending(store, hooks)
    assert results[0]["state"] == "done"
    assert results[0]["result"] == "queued"
    assert hooks.calls == []


@pytest.mark.anyio
async def test_events_are_emitted_through_store_root(store):
    make_ticket(store)
    hooks = FakeHooks()
    dispatcher.commit(store, decision())
    await dispatcher.run_pending(store, hooks)

    events = (store.root / ".orca" / "osw" / "logs" / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "router_decision" in events
    assert "router_applied" in events


@pytest.mark.anyio
async def test_reject_notifies_the_sender_once(store):
    make_ticket(store)
    hooks = FakeHooks()
    assert dispatcher.commit(
        store,
        decision(
            action="reject",
            decision_id="dec_rej",
            message_id="msg_7",
            reason="out of scope",
        ),
    ) is True

    rows = store.outbox_pending()
    assert len(rows) == 1
    assert rows[0]["payload"]["line"].startswith("# [osw] attention reject")
    assert rows[0]["payload"]["line"].count("out of scope") == 1

    results = await dispatcher.run_pending(store, hooks)
    assert results[0]["result"] == "queued"
    assert hooks.calls == []

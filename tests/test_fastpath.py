"""Replay tests for the fast path (design §8, plan §8, addendum 5/6).

Every shipped rule carries one fixture under ``tests/fixtures/fastpath``; the
tests replay each case through the rule and through :func:`fastpath.evaluate`,
then check that :func:`fastpath.apply` performs only the contract's
bookkeeping and never touches a launch, answer or provider path. The
store-backed tests use the watcher-produced shape (``correlation.agent`` and
``thread_key`` = the agent id, no ``correlation.ticket``), so attachment goes
through ``Store.ticket_for_agent``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from osw import dispatcher, fastpath
from osw.mailbox.envelope import validate_envelope
from osw.mailbox.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "fastpath"
REPO_ROOT = Path(__file__).resolve().parent.parent

RESULT_KEYS = {
    "intent",
    "attention",
    "priority",
    "wake",
    "decision",
    "bookkeeping",
    "reason",
}
BOOKKEEPING_OPS = {"attach", "stage", "settle", "expire", "outbox"}
ALLOWED_STORE_METHODS = {
    # fast-path bookkeeping
    "update_ticket",
    "add_report",
    "set_assignment_state",
    "open_ask",
    "settle",
    "outbox_put",
    "ticket_for_agent",
    "commit_decision",
    # reads and writes the Dispatcher reaches through a human-command Decision
    "decision",
    "ticket",
    "get_message",
    "create_ticket",
    "assignments",
    "effect_exists",
    "mark_decision",
}

#: A permissive Mandate for the store double; the real checks live in
#: ``tests/test_mandate.py`` and the committed ``skill/osw/mandate.json``.
FAKE_MANDATE = {
    "actions": [
        "accept", "followup", "answer", "stop", "retry", "reject",
        "escalate", "noop", "close",
    ],
    "answer": "free_text",
    "max_retries": 2,
    "max_workers": 4,
    "providers": ["claude", "codex", "pi", "omp", "kimi", "grok", "dsh"],
    "open_from": [],
    "close": "router",
    "expires_at": "",
}

#: The worker every watcher-shaped envelope names.
WATCHER_AGENT = "code_001"


def _load(rule_name: str) -> dict:
    return json.loads((FIXTURES / f"{rule_name}.json").read_text(encoding="utf-8"))


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


def _case_tuples() -> list[tuple[str, dict]]:
    pairs: list[tuple[str, dict]] = []
    for path in _fixture_paths():
        doc = json.loads(path.read_text(encoding="utf-8"))
        pairs.extend((doc["rule"], case) for case in doc["cases"])
    return pairs


CASES = [
    pytest.param(rule, case, id=f"{rule}:{case['name']}")
    for rule, case in _case_tuples()
]

#: Cases whose bookkeeping is all the fast path itself may perform. A
#: human-command case becomes a Decision the Dispatcher commits, so it is
#: covered by the dedicated command tests below.
BOOKKEEPING_CASES = [
    pytest.param(rule, case, id=f"{rule}:{case['name']}")
    for rule, case in _case_tuples()
    if not (case["expect"] or {}).get("decision")
]


def _case(rule_name: str, case_name: str) -> dict:
    for case in _load(rule_name)["cases"]:
        if case["name"] == case_name:
            return case
    raise AssertionError(f"no fixture case {rule_name}/{case_name}")


class FakeStore:
    """Store double exposing exactly the methods the fast path may reach.

    Bookkeeping is the fast path's own surface; a human command reaches the
    Dispatcher, which reads the Decision, the Ticket and the Mandate. There is
    no ``add_effect``: the fast path never queues an Effect.
    """

    root = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"fast path called a non-contract store method: {name}")

    def _record(self, name: str, args: tuple, kwargs: dict) -> None:
        self.calls.append((name, args, kwargs))

    def update_ticket(self, ticket_id: str, **kwargs) -> dict:
        self._record("update_ticket", (ticket_id,), kwargs)
        return {"id": ticket_id}

    def add_report(self, ticket_id: str, report: dict) -> None:
        self._record("add_report", (ticket_id, report), {})

    def set_assignment_state(self, ticket_id: str, agent_id: str, state: str) -> None:
        self._record("set_assignment_state", (ticket_id, agent_id, state), {})

    def open_ask(
        self, ticket_id: str, ask_id: str, *, agent: str = "", message_id: str = ""
    ) -> None:
        self._record(
            "open_ask", (ticket_id, ask_id), {"agent": agent, "message_id": message_id}
        )

    def settle(self, message_id: str, *, outcome: str = "") -> None:
        self._record("settle", (message_id,), {"outcome": outcome})

    def outbox_put(
        self, *, ticket_id: str, conversation: str, payload: dict
    ) -> str:
        self._record(
            "outbox_put",
            (),
            {
                "ticket_id": ticket_id,
                "conversation": conversation,
                "payload": payload,
            },
        )
        return f"out_{len(self.calls)}"

    def ticket_for_agent(self, agent_id: str) -> dict | None:
        self._record("ticket_for_agent", (agent_id,), {})
        return None

    # -- reads the Dispatcher makes for a human-command Decision -----------

    def decision(self, decision_id: str) -> dict | None:
        self._record("decision", (decision_id,), {})
        return None

    def ticket(self, ticket_id: str) -> dict:
        self._record("ticket", (ticket_id,), {})
        return {
            "id": ticket_id,
            "stage": "working",
            "mandate": dict(FAKE_MANDATE),
            "assignments": [],
            "reports": [],
            "open_asks": [],
            "thread_key": "",
        }

    def get_message(self, message_id: str) -> dict | None:
        self._record("get_message", (message_id,), {})
        return None

    def create_ticket(self, **kwargs) -> str:
        self._record("create_ticket", (), kwargs)
        return str(kwargs.get("ticket_id") or "tkt_fake")

    def assignments(self, ticket_id: str) -> list[dict]:
        self._record("assignments", (ticket_id,), {})
        return []

    def effect_exists(self, kind: str, key: str) -> bool:
        self._record("effect_exists", (kind, key), {})
        return False

    def mark_decision(
        self, decision_id: str, state: str, *, result: str = ""
    ) -> None:
        self._record(
            "mark_decision",
            (decision_id,),
            {"state": state, "result": result},
        )

    def commit_decision(
        self,
        decision: dict,
        *,
        effects: tuple = (),
        settlements: tuple = (),
        outbox: tuple = (),
    ) -> bool:
        self._record(
            "commit_decision",
            (decision,),
            {"effects": effects, "settlements": settlements, "outbox": outbox},
        )
        return True

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def kwargs(self, name: str) -> list[dict]:
        return [call[2] for call in self.calls if call[0] == name]


def _evaluate(case: dict) -> dict:
    return fastpath.evaluate(copy.deepcopy(case["envelope"]), copy.deepcopy(case["context"]))


# ---------------------------------------------------------------------------
# fixtures and rule table


def test_rules_are_the_contract_ten_in_order():
    assert [rule.name for rule in fastpath.RULES] == [
        "expire_ttl",
        "human_command",
        "human_request",
        "ask",
        "blocked",
        "report_result",
        "report_progress",
        "status_request",
        "notice",
        "wake",
    ]


def test_every_rule_has_one_fixture():
    assert {path.stem for path in _fixture_paths()} == {
        rule.name for rule in fastpath.RULES
    }
    for rule in fastpath.RULES:
        doc = _load(rule.name)
        assert doc["rule"] == rule.name
        assert doc["cases"], f"{rule.name} has no cases"
        for case in doc["cases"]:
            assert set(case) == {"name", "envelope", "context", "expect"}
            assert case["envelope"]["id"]
            assert isinstance(case["context"], dict)


@pytest.mark.parametrize("rule_name,case", CASES)
def test_fixture_replays_through_rule_and_evaluate(rule_name: str, case: dict):
    rule = fastpath.RULE_BY_NAME[rule_name]
    env = copy.deepcopy(case["envelope"])
    ctx = copy.deepcopy(case["context"])
    expect = case["expect"]
    if expect is None:
        assert rule.apply(env, ctx) is None
        return
    assert rule.apply(env, ctx) == expect
    assert fastpath.evaluate(env, ctx) == expect


@pytest.mark.parametrize("rule_name,case", CASES)
def test_evaluate_returns_the_contract_keys(rule_name: str, case: dict):
    result = _evaluate(case)
    assert set(result) == RESULT_KEYS
    assert result["attention"] in {"fyi", "must_answer", "human"}
    assert result["wake"] in {"doorbell", ""}
    assert isinstance(result["priority"], int)
    for op in result["bookkeeping"]:
        assert op["op"] in BOOKKEEPING_OPS


@pytest.mark.parametrize("rule_name,case", CASES)
def test_evaluate_does_not_mutate_its_inputs(rule_name: str, case: dict):
    env = copy.deepcopy(case["envelope"])
    ctx = copy.deepcopy(case["context"])
    untouched = copy.deepcopy((env, ctx))
    fastpath.evaluate(env, ctx)
    assert (env, ctx) == untouched


# ---------------------------------------------------------------------------
# first match wins and attachment


def test_expired_human_command_takes_the_ttl_path():
    env = copy.deepcopy(_case("human_command", "stop interrupts the ticket")["envelope"])
    env["ts"] = "2026-11-14T13:10:00.000Z"
    env["ttl_s"] = 30
    ctx = {"now": "2026-11-14T13:20:00.000Z", "tickets": [], "doorbell": "none",
           "queued_router": 0}
    result = fastpath.evaluate(env, ctx)
    assert result["intent"] == "expire"
    assert result["decision"] is None
    assert result["bookkeeping"] == [{"op": "expire", "message_id": env["id"]}]


def test_correlation_ticket_beats_thread_key():
    case = _case("ask", "attaches by thread key when correlation is empty")
    env = copy.deepcopy(case["envelope"])
    env["correlation"]["ticket"] = "code_001"
    env["thread_key"] = "code_002"
    ctx = {
        "tickets": [
            {"id": "code_001", "stage": "working", "thread_key": "code_001"},
            {"id": "code_002", "stage": "review", "thread_key": "code_002"},
        ],
        "doorbell": "none",
        "queued_router": 0,
    }
    result = fastpath.evaluate(env, ctx)
    attached = [op for op in result["bookkeeping"] if op["op"] == "attach"]
    assert attached[0]["ticket"] == "code_001"


# ---------------------------------------------------------------------------
# apply: bookkeeping through the contract store methods only


@pytest.mark.parametrize("rule_name,case", BOOKKEEPING_CASES)
def test_apply_uses_only_contract_store_methods(rule_name: str, case: dict):
    store = FakeStore()
    result = fastpath.apply(
        store, copy.deepcopy(case["envelope"]), copy.deepcopy(case["context"])
    )
    assert set(store.names()) <= ALLOWED_STORE_METHODS
    assert result["applied"] == [op["op"] for op in result["bookkeeping"]]
    assert "commit_decision" not in store.names()
    assert result["committed"] is False


def test_apply_commits_the_human_stop_through_the_dispatcher():
    """A human command is planned by the Dispatcher, not written half-way."""
    case = _case("human_command", "stop interrupts the ticket")
    store = FakeStore()
    result = fastpath.apply(store, case["envelope"], case["context"])
    assert result["committed"] is True
    assert result["applied"] == []
    assert "add_effect" not in store.names()
    commits = store.kwargs("commit_decision")
    assert len(commits) == 1
    decision = store.calls[store.names().index("commit_decision")][1][0]
    assert decision["author"] == "human"
    assert decision["action"] == "stop"
    assert decision["ticket"] == "code_001"
    assert [(e["kind"], e["key"]) for e in commits[0]["effects"]] == [
        ("stop", "code_001")
    ]
    assert commits[0]["settlements"] == [case["envelope"]["id"]]


def test_apply_ask_opens_the_question_and_parks_the_ticket():
    case = _case("ask", "attaches by correlation ticket")
    store = FakeStore()
    fastpath.apply(store, case["envelope"], case["context"])
    assert store.names() == ["open_ask", "update_ticket"]
    assert store.calls[0][1] == ("code_001", "ask_orca_77")
    assert store.calls[0][2] == {"agent": "code_001",
                                 "message_id": case["envelope"]["id"]}
    assert store.calls[1][1] == ("code_001",)
    assert store.calls[1][2]["stage"] == "waiting_answer"


def test_apply_report_records_the_report_finalizes_and_stages():
    case = _case("report_result", "a ready report stages review and rings")
    store = FakeStore()
    fastpath.apply(store, case["envelope"], case["context"])
    assert store.names() == ["add_report", "set_assignment_state", "update_ticket"]
    assert store.calls[0][1][0] == "code_001"
    assert store.calls[0][1][1]["outcome"] == "ready"
    assert store.calls[1][1] == ("code_001", "code_001", "done")
    assert store.calls[2][2]["stage"] == "review"


def test_apply_progress_does_not_finalize_the_assignment():
    case = _case(
        "report_progress", "a progress report settles the message and stages working"
    )
    store = FakeStore()
    fastpath.apply(store, case["envelope"], case["context"])
    assert "set_assignment_state" not in store.names()


def test_apply_status_request_writes_the_outbox_not_an_effect():
    case = _case("status_request", "one outbox notice carries the counts")
    store = FakeStore()
    result = fastpath.apply(store, case["envelope"], case["context"])
    assert store.names() == ["outbox_put"]
    assert result["applied"] == ["outbox"]
    kwargs = store.calls[0][2]
    assert kwargs["ticket_id"] == ""
    assert kwargs["conversation"] == "terminal:term_abc"
    assert kwargs["payload"] == {
        "line": "# [osw] status pending=2 human=1 worker=3 dead=0"
    }


def test_apply_expire_settles_the_message():
    case = _case("expire_ttl", "notice past its ttl settles as expired")
    store = FakeStore()
    fastpath.apply(store, case["envelope"], case["context"])
    assert store.names() == ["settle"]
    assert store.calls[0][1] == (case["envelope"]["id"],)
    assert store.calls[0][2] == {"outcome": "expired"}


def test_apply_progress_settles_after_staging():
    case = _case(
        "report_progress", "a progress report settles the message and stages working"
    )
    store = FakeStore()
    fastpath.apply(store, case["envelope"], case["context"])
    assert store.names() == ["add_report", "update_ticket", "settle"]
    assert store.calls[1][2]["stage"] == "working"
    assert store.calls[2][2] == {"outcome": "progress"}


# ---------------------------------------------------------------------------
# the same calls against the real store


def test_apply_against_the_real_store(tmp_path: Path):
    raw = _case("ask", "attaches by correlation ticket")
    env = validate_envelope(copy.deepcopy(raw["envelope"]))
    store = Store(tmp_path / "mailbox.db")
    try:
        store.create_ticket(
            origin={"role": "human", "text": "fix the watcher"},
            mandate={"actions": ["answer"]},
            ticket_id="code_001",
            thread_key="code_001",
            stage="working",
        )
        store.enqueue(env)
        ctx = dict(raw["context"], tickets=store.tickets())
        result = fastpath.apply(store, env, ctx)
        ticket = store.ticket("code_001")
        assert ticket is not None
        assert ticket["stage"] == "waiting_answer"
        assert ticket["open_asks"][0]["id"] == "ask_orca_77"
        assert store.is_ask_open("code_001", "ask_orca_77")
        assert result["wake"] == "doorbell"
        assert store.get_message(env["id"])["state"] == "queued"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# the watcher-produced shape: only correlation.agent ties the message
# to its Ticket, so attachment goes through Store.ticket_for_agent


def _mandate() -> dict:
    return json.loads(
        (REPO_ROOT / "skill" / "osw" / "mandate.json").read_text(encoding="utf-8")
    )


def _live_ticket(store: Store, *, ticket_id: str = "code_001") -> dict:
    """A Ticket whose only tie to its worker is the assignment entry."""
    store.create_ticket(
        origin={"role": "human", "id": "terminal:term_abc", "text": "fix the watcher"},
        mandate=_mandate(),
        ticket_id=ticket_id,
        thread_key="",
        stage="working",
    )
    store.add_assignment(
        ticket_id,
        {
            "agent_id": WATCHER_AGENT,
            "terminal": "term_1",
            "dispatch_id": "",
            "state": "running",
        },
    )
    return store.ticket(ticket_id)


def _watcher_envelope(**overrides) -> dict:
    """Exactly what the watcher sinks: correlation.agent, thread_key = agent id."""
    envelope = {
        "v": 1,
        "id": "msg_20261114T140000000Z_w10001",
        "ts": "2026-11-14T14:00:00.000Z",
        "from": {"role": "watcher", "id": WATCHER_AGENT},
        "to": {"role": "router", "id": ""},
        "kind": "report",
        "correlation": {"agent": WATCHER_AGENT},
        "thread_key": WATCHER_AGENT,
        "priority": 2,
        "dedupe_key": "code_001:turn:7",
        "body": {
            "outcome": "ready",
            "handoff": "reports/code_001.md",
            "completion_source": "handoff",
            "tools_observed": True,
        },
    }
    envelope.update(overrides)
    return envelope


def _context(store: Store) -> dict:
    return {
        "now": "2026-11-14T14:00:30.000Z",
        "tickets": store.tickets(),
        "doorbell": "none",
        "queued_router": 1,
    }


@pytest.mark.parametrize(
    "outcome,stage,state",
    [
        ("ready", "review", "done"),
        ("failed", "review", "done"),
        ("dropped", "stopped", "stopped"),
    ],
)
def test_a_watcher_report_reaches_its_ticket_through_the_agent(
    tmp_path: Path, outcome: str, stage: str, state: str
):
    store = Store(tmp_path / "mailbox.db")
    try:
        _live_ticket(store)
        env = validate_envelope(
            _watcher_envelope(
                body={"outcome": outcome, "completion_source": "handoff"}
            )
        )
        ctx = _context(store)
        untouched = copy.deepcopy(ctx)
        result = fastpath.apply(store, env, ctx)

        ticket = store.ticket("code_001")
        assert result["intent"] == "accept_result"
        assert result["wake"] == "doorbell"
        assert ticket["stage"] == stage
        assert [report["outcome"] for report in ticket["reports"]] == [outcome]
        assert ticket["assignments"][0]["state"] == state
        # the caller's context is never mutated by the agent resolution
        assert ctx == untouched
    finally:
        store.close()


def test_a_watcher_ask_opens_the_question_on_the_agent_ticket(tmp_path: Path):
    store = Store(tmp_path / "mailbox.db")
    try:
        _live_ticket(store)
        env = validate_envelope(
            _watcher_envelope(
                id="msg_20261114T140001000Z_w10002",
                kind="ask",
                correlation={"agent": WATCHER_AGENT, "ask": "ask_orca_77"},
                priority=1,
                dedupe_key="ask_orca_77",
                body={"text": "Proceed with the migration?"},
            )
        )
        result = fastpath.apply(store, env, _context(store))

        ticket = store.ticket("code_001")
        assert result["intent"] == "answer"
        assert ticket["stage"] == "waiting_answer"
        assert [ask["id"] for ask in ticket["open_asks"]] == ["ask_orca_77"]
        assert store.is_ask_open("code_001", "ask_orca_77")
        # an ask parks the Ticket; the worker is still running
        assert ticket["assignments"][0]["state"] == "running"
    finally:
        store.close()


def test_a_thread_match_beats_the_agent_resolution(tmp_path: Path):
    store = Store(tmp_path / "mailbox.db")
    try:
        _live_ticket(store)
        store.create_ticket(
            origin={"role": "human", "text": "other"},
            mandate=_mandate(),
            ticket_id="code_002",
            thread_key=WATCHER_AGENT,
            stage="working",
        )
        env = validate_envelope(
            _watcher_envelope(
                id="msg_20261114T140002000Z_w10003",
                kind="ask",
                correlation={"agent": WATCHER_AGENT, "ask": "ask_9"},
                body={"text": "which branch"},
            )
        )
        result = fastpath.apply(store, env, _context(store))

        assert [op["ticket"] for op in result["bookkeeping"]] == [
            "code_002",
            "code_002",
        ]
        assert store.ticket("code_001")["stage"] == "working"
        assert store.ticket("code_002")["stage"] == "waiting_answer"
    finally:
        store.close()


def test_a_finished_ticket_is_not_resolved_from_its_agent(tmp_path: Path):
    store = Store(tmp_path / "mailbox.db")
    try:
        store.create_ticket(
            origin={"role": "human", "text": "old case"},
            mandate=_mandate(),
            ticket_id="code_001",
            thread_key="",
            stage="done",
        )
        store.add_assignment(
            "code_001",
            {"agent_id": WATCHER_AGENT, "terminal": "term_1", "state": "done"},
        )
        env = validate_envelope(_watcher_envelope())
        result = fastpath.apply(store, env, _context(store))

        assert result["bookkeeping"] == []
        assert store.ticket("code_001")["reports"] == []
    finally:
        store.close()


def test_a_report_finishes_the_assignment_so_close_can_commit(tmp_path: Path):
    """Closing needs no hand-edited assignment state: the report settles it."""
    store = Store(tmp_path / "mailbox.db")
    try:
        _live_ticket(store)
        env = validate_envelope(_watcher_envelope())
        fastpath.apply(store, env, _context(store))
        assert store.assignments("code_001")[0]["state"] == "done"

        decision = {
            "decision_id": "dec_close_1",
            "message_id": "",
            "action": "close",
            "ticket": "code_001",
            "reason": "the report is in",
            "payload": {},
            "author": "router",
            "confidence": 1.0,
        }
        assert dispatcher.commit(store, decision) is True
        assert [(e["kind"], e["key"]) for e in store.pending_effects()] == [
            ("close_ticket", "code_001")
        ]
    finally:
        store.close()


def test_an_exact_stop_command_plans_one_stop_effect(tmp_path: Path):
    case = _case("human_command", "stop interrupts the ticket")
    env = validate_envelope(copy.deepcopy(case["envelope"]))
    store = Store(tmp_path / "mailbox.db")
    try:
        store.create_ticket(
            origin={"role": "human", "text": "fix the watcher"},
            mandate=_mandate(),
            ticket_id="code_001",
            thread_key="code_001",
            stage="working",
        )
        store.enqueue(env)
        result = fastpath.apply(
            store, env, dict(case["context"], tickets=store.tickets())
        )

        assert result["committed"] is True
        assert [(e["kind"], e["key"]) for e in store.pending_effects()] == [
            ("stop", "code_001")
        ]
        assert store.dead_letters() == []
        decision = store.decision(case["expect"]["decision"]["decision_id"])
        assert decision["author"] == "human"
        assert decision["state"] == "committed"
        assert store.get_message(env["id"])["state"] == "settled"
    finally:
        store.close()


def test_a_status_request_writes_one_outbox_row_and_no_effect(tmp_path: Path):
    case = _case("status_request", "one outbox notice carries the counts")
    env = validate_envelope(copy.deepcopy(case["envelope"]))
    store = Store(tmp_path / "mailbox.db")
    try:
        store.enqueue(env)
        result = fastpath.apply(store, env, copy.deepcopy(case["context"]))

        assert result["applied"] == ["outbox"]
        assert store.pending_effects() == []
        assert store.dead_letters() == []
        rows = store.outbox_pending()
        assert len(rows) == 1
        assert rows[0]["ticket_id"] == ""
        assert rows[0]["conversation"] == "terminal:term_abc"
        assert rows[0]["payload"] == {
            "line": "# [osw] status pending=2 human=1 worker=3 dead=0"
        }
        assert store.get_message(env["id"])["state"] == "queued"
    finally:
        store.close()

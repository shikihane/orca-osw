"""Tests for the mailbox CLI verbs (plan section 13).

Every test drives the real ``osw`` app through ``CliRunner`` against an
isolated project root with a real ``Store``; nothing here needs a live
Orca, a live daemon or the network.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from osw import state as state_mod
from osw.cli import _mark_lost_if_orphaned, app
from osw.mailbox import identity, paths, spool
from osw.mailbox.store import Store

runner = CliRunner()

MANDATE = {
    "actions": ["accept", "followup", "answer", "stop", "retry", "reject",
                "escalate", "noop", "close"],
    "answer": "free_text",
    "max_retries": 2,
    "max_workers": 4,
    "providers": ["claude", "codex", "pi", "omp", "kimi", "grok", "dsh"],
    "open_from": [],
    "close": "router",
    "expires_at": "",
}


@pytest.fixture
def root(tmp_path, monkeypatch):
    """An isolated project root in which OSW state is initialized."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(identity.ROLE_ENV, raising=False)
    monkeypatch.delenv(identity.TOKEN_ENV, raising=False)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.init_state_dir(tmp_path)
    return tmp_path


@contextmanager
def _store(root):
    store = Store(paths.db_path(root))
    try:
        yield store
    finally:
        store.close()


def run(*args):
    return runner.invoke(app, list(args))


def payload(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def step(*args):
    """Run a command and return its JSON payload (asserts success)."""
    return payload(run(*args))


def _router_env(root, monkeypatch) -> str:
    token = identity.generate_token(root)
    monkeypatch.setenv(identity.ROLE_ENV, "router")
    monkeypatch.setenv(identity.TOKEN_ENV, token)
    return token


# ---------------------------------------------------------------------------
# send / wait
# ---------------------------------------------------------------------------

def test_send_then_wait_returns_a_card(root):
    sent = step("mailbox", "send", "--kind", "request",
                "--text", "fix the watcher timeout",
                "--to-role", "human", "--json")

    cards = step("mailbox", "wait", "--as", "human", "--timeout", "0", "--json")

    assert [card["message_id"] for card in cards] == [sent["id"]]
    card = cards[0]
    assert card["kind"] == "request"
    assert card["from"].startswith("human")
    assert card["trust"] == "trusted"
    assert card["attention"] == "must_answer"
    assert card["priority"] == 0
    assert card["summary"] == "fix the watcher timeout"
    assert card["tickets"] == []
    assert card["detail_hint"] == f"osw mailbox show {sent['id']}"


def test_wait_acks_the_message_and_defaults_to_human(root):
    sent = step("mailbox", "send", "--kind", "request", "--text", "hello",
                "--to-role", "human", "--json")

    result = run("mailbox", "wait", "--timeout", "0")
    assert result.exit_code == 0, result.output
    assert sent["id"] in result.output

    with _store(root) as store:
        message = store.get_message(sent["id"])
    assert message["state"] == "acked"
    assert message["from"]["role"] == "human"
    assert message["body"] == {"text": "hello"}

    # A second scan has nothing left to hand out.
    second = step("mailbox", "wait", "--timeout", "0", "--json")
    assert second == []


def test_send_defaults_to_the_router_and_keeps_dedupe(root):
    sent = step("mailbox", "send", "--kind", "request", "--text", "plan",
                "--dedupe-key", "dg-1", "--priority", "2", "--json")

    raw = spool.read_pending(root)
    assert raw[0]["to"]["role"] == "router"
    assert raw[0]["dedupe_key"] == "dg-1"
    assert raw[0]["priority"] == 2
    assert sent["id"] == raw[0]["id"]


def test_send_rejects_an_unknown_kind(root):
    result = run("mailbox", "send", "--kind", "nonsense", "--text", "x")
    assert result.exit_code == 1
    assert "unknown_kind" in result.output


def test_send_rejects_both_body_flags(root):
    result = run("mailbox", "send", "--kind", "request",
                 "--text", "x", "--body-json", '{"a": 1}')
    assert result.exit_code == 1
    assert "invalid_envelope" in result.output


def test_wait_as_router_binds_the_router_and_consumes_the_doorbell(
    root, monkeypatch,
):
    sent = step("mailbox", "send", "--kind", "request", "--text", "decide",
                "--json")
    # The human sends; the Router may not (plan Addendum A item 10).
    token = _router_env(root, monkeypatch)

    with _store(root) as store:
        store.kv_set("doorbell:state", "pending")

    cards = step("mailbox", "wait", "--as", "router", "--timeout", "0", "--json")

    assert [card["message_id"] for card in cards] == [sent["id"]]
    with _store(root) as store:
        binding = store.router_binding()
        assert binding["token"] == token
        assert binding["address"]
        assert store.kv_get("doorbell:state") == "none"


def test_wait_as_router_without_token_is_forbidden(root, monkeypatch):
    monkeypatch.setenv(identity.ROLE_ENV, "router")

    result = run("mailbox", "wait", "--as", "router", "--timeout", "0", "--json")

    assert result.exit_code == 1
    assert "router_forbidden" in result.output


def test_wait_rejects_an_unknown_role(root):
    result = run("mailbox", "wait", "--as", "worker", "--timeout", "0")
    assert result.exit_code == 1
    assert "unknown_role" in result.output


# ---------------------------------------------------------------------------
# Router identity keeps dispatch with the human
# ---------------------------------------------------------------------------

def test_new_and_use_refuse_the_router_role(root, monkeypatch):
    _router_env(root, monkeypatch)

    for args in (["new", "pi", "do the thing"],
                 ["use", "agent_001", "do the thing"]):
        result = run(*args)
        assert result.exit_code == 1
        assert "router_forbidden" in result.output


def test_send_refuses_the_router_role(root, monkeypatch):
    """The Router writes Decisions; sending is the Dispatcher's surface."""
    _router_env(root, monkeypatch)

    result = run("mailbox", "send", "--kind", "request", "--text", "go",
                 "--json")

    assert result.exit_code == 1
    assert "router_forbidden" in result.output
    assert spool.pending_count(root) == 0


def test_decide_author_human_refuses_the_router_role(root, monkeypatch):
    """A Router must not forge human authority (mandate close:human)."""
    sent = step("mailbox", "send", "--kind", "request", "--text", "ask",
                "--json")
    _router_env(root, monkeypatch)
    monkeypatch.setattr("osw.mailbox.daemon.is_running", lambda _root: (False, 0))

    result = run("mailbox", "decide", sent["id"], "--action", "noop",
                 "--reason", "forged", "--author", "human", "--json")

    assert result.exit_code == 1
    assert "router_forbidden" in result.output
    with _store(root) as store:
        spool.ingest(root, store)
        assert store.get_message(sent["id"])["state"] == "queued"


def test_decide_author_router_needs_the_router_identity(root, monkeypatch):
    sent = step("mailbox", "send", "--kind", "request", "--text", "ask",
                "--json")
    monkeypatch.setattr("osw.mailbox.daemon.is_running", lambda _root: (False, 0))

    result = run("mailbox", "decide", sent["id"], "--action", "noop",
                 "--reason", "forged", "--author", "router", "--json")

    assert result.exit_code == 1
    assert "router_forbidden" in result.output
    with _store(root) as store:
        spool.ingest(root, store)
        assert store.get_message(sent["id"])["state"] == "queued"


def test_router_decide_defaults_the_author_to_router(root, monkeypatch):
    sent = step("mailbox", "send", "--kind", "request", "--text", "ask",
                "--json")
    _router_env(root, monkeypatch)
    monkeypatch.setattr("osw.mailbox.daemon.is_running", lambda _root: (False, 0))

    decision = step("mailbox", "decide", sent["id"], "--action", "noop",
                    "--reason", "nothing to execute", "--json")

    with _store(root) as store:
        assert store.decision(decision["decision_id"])["author"] == "router"
    # The same terminal still refuses the human-only surfaces.
    refused = run("mailbox", "send", "--kind", "request", "--text", "go")
    assert refused.exit_code == 1
    assert "router_forbidden" in refused.output


def test_a_plain_human_sends_and_decides(root, monkeypatch):
    """No role marker, or a wrong token, keeps the human identity."""
    monkeypatch.setenv(identity.ROLE_ENV, "router")
    monkeypatch.setenv(identity.TOKEN_ENV, "not-the-project-token")
    monkeypatch.setattr("osw.mailbox.daemon.is_running", lambda _root: (False, 0))

    sent = step("mailbox", "send", "--kind", "request", "--text", "fix it",
                "--json")
    decision = step("mailbox", "decide", sent["id"], "--action", "noop",
                    "--reason", "nothing to execute", "--json")

    assert spool.read_pending(root) == []
    with _store(root) as store:
        assert store.decision(decision["decision_id"])["author"] == "human"


# ---------------------------------------------------------------------------
# status / show / trace
# ---------------------------------------------------------------------------

def test_status_shape(root):
    step("mailbox", "send", "--kind", "request", "--text", "queued", "--json")

    data = step("mailbox", "status", "--json")

    assert list(data) == [
        "running", "pid", "schema_version", "queues", "oldest_age_s",
        "leased", "dead", "spool_lag", "doorbell", "decisions_applied_5m",
    ]
    assert data["running"] is False
    assert data["pid"] == 0
    assert data["schema_version"] == 1
    assert data["queues"] == {
        "router": 0, "human": 0, "worker": 0, "broadcast": 0}
    assert data["leased"] == 0
    assert data["dead"] == 0
    assert data["spool_lag"] == 1  # not ingested: status never drains
    assert data["doorbell"] == "none"
    assert data["decisions_applied_5m"] == 0
    assert isinstance(data["oldest_age_s"], (int, float))


def test_status_text_names_every_field(root):
    result = run("mailbox", "status")
    assert result.exit_code == 0, result.output
    for line in ("running:", "queues:", "spool_lag:", "doorbell:"):
        assert line in result.output


def test_show_returns_envelope_card_and_ticket(root):
    with _store(root) as store:
        store.create_ticket(
            origin={"provider": "pi", "prompt": "fix it"},
            mandate=MANDATE, ticket_id="code_001",
        )
    sent = step("mailbox", "send", "--kind", "request", "--text", "fix it",
                "--ticket", "code_001", "--json")

    data = step("mailbox", "show", sent["id"], "--json")

    assert data["message"]["id"] == sent["id"]
    assert data["message"]["body"] == {"text": "fix it"}
    assert data["card"]["message_id"] == sent["id"]
    assert data["card"]["tickets"] == [{"id": "code_001", "stage": "open"}]
    assert data["ticket"]["id"] == "code_001"


def test_show_unknown_message_is_typed(root):
    result = run("mailbox", "show", "msg_missing", "--json")
    assert result.exit_code == 1
    assert "unknown_message" in result.output


def test_trace_reports_message_ticket_decisions_effects_outbox(
    root, monkeypatch,
):
    with _store(root) as store:
        store.create_ticket(
            origin={"provider": "pi", "prompt": "fix it"},
            mandate=MANDATE, ticket_id="code_001",
        )
    sent = step("mailbox", "send", "--kind", "request", "--text", "fix it",
                "--ticket", "code_001", "--json")
    step("mailbox", "wait", "--as", "human", "--timeout", "0", "--json")

    monkeypatch.setattr("osw.mailbox.daemon.is_running", lambda _root: (False, 0))
    decision = step("mailbox", "decide", sent["id"], "--action", "noop",
                    "--reason", "nothing to execute", "--json")

    traced = step("mailbox", "trace", sent["id"], "--json")

    assert traced["message"]["id"] == sent["id"]
    assert traced["ticket"]["id"] == "code_001"
    assert [d["decision_id"] for d in traced["decisions"]] == [
        decision["decision_id"]]
    assert traced["effects"] == []
    assert traced["outbox"] == []


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------

def test_decide_commits_with_a_stopped_daemon(root, monkeypatch):
    with _store(root) as store:
        store.create_ticket(
            origin={"provider": "pi", "prompt": "status?"},
            mandate=MANDATE, ticket_id="code_001",
        )
    sent = step("mailbox", "send", "--kind", "request", "--text", "status?",
                "--ticket", "code_001", "--json")
    step("mailbox", "wait", "--as", "human", "--timeout", "0", "--json")
    monkeypatch.setattr("osw.mailbox.daemon.is_running", lambda _root: (False, 0))

    result = step("mailbox", "decide", sent["id"], "--action", "noop",
                  "--ticket", "code_001",
                  "--reason", "nothing to execute", "--json")

    with _store(root) as store:
        decision = store.decision(result["decision_id"])
        message = store.get_message(sent["id"])
    assert decision["action"] == "noop"
    assert decision["author"] == "human"
    assert decision["state"] in ("committed", "applied")
    assert message["state"] in ("settled", "acked")


def test_decide_twice_records_two_decisions(root, monkeypatch):
    with _store(root) as store:
        store.create_ticket(
            origin={"provider": "pi", "prompt": "x"},
            mandate=MANDATE, ticket_id="code_001",
        )
    sent = step("mailbox", "send", "--kind", "request", "--text", "x",
                "--ticket", "code_001", "--json")
    monkeypatch.setattr("osw.mailbox.daemon.is_running", lambda _root: (False, 0))
    first = step("mailbox", "decide", sent["id"], "--action", "noop",
                 "--ticket", "code_001", "--reason", "once", "--json")

    second = step("mailbox", "decide", sent["id"], "--action", "noop",
                  "--ticket", "code_001", "--reason", "again", "--json")

    assert first["decision_id"] != second["decision_id"]
    with _store(root) as store:
        assert store.decision(first["decision_id"]) is not None
        assert store.decision(second["decision_id"]) is not None


def test_decide_applies_locally_with_hooks(root, monkeypatch):
    """With no daemon, `decide` applies the decision through the hooks."""
    mandate_file = root / "skill" / "osw" / "mandate.json"
    mandate_file.parent.mkdir(parents=True)
    mandate_file.write_text(json.dumps(MANDATE), encoding="utf-8")

    with _store(root) as store:
        store.create_ticket(
            origin={"provider": "pi", "prompt": "fix it"},
            mandate=MANDATE, ticket_id="code_001",
        )
        store.open_ask("code_001", "msg_ask", agent="code_001")

    sent = step("mailbox", "send", "--kind", "ask", "--text", "may I edit?",
                "--ticket", "code_001", "--json")
    step("mailbox", "wait", "--as", "human", "--timeout", "0", "--json")

    calls = []

    class FakeHooks:
        async def launch(self, ticket, payload):
            calls.append(("launch", payload))
            return {"agent_id": "a_001", "terminal": "term_a",
                    "dispatch_id": "dsp_1"}

        async def followup(self, ticket, payload):
            calls.append(("followup", payload))

        async def answer(self, ticket, payload):
            calls.append(("answer", payload))

        async def stop(self, ticket):
            calls.append(("stop", ticket))

    monkeypatch.setattr("osw.mailbox_cli._local_hooks", lambda _root: FakeHooks())
    monkeypatch.setattr("osw.mailbox.daemon.is_running", lambda _root: (False, 0))

    result = step(
        "mailbox", "decide", sent["id"], "--action", "answer",
        "--reason", "the ask is inside the mandate",
        "--ticket", "code_001",
        "--payload-json", json.dumps({"ask": "msg_ask",
                                      "text": "yes, edit the file"}),
        "--json",
    )

    assert calls and calls[0][0] == "answer"
    with _store(root) as store:
        assert store.decision(result["decision_id"]) is not None


# ---------------------------------------------------------------------------
# tickets
# ---------------------------------------------------------------------------

def test_tickets_and_ticket(root):
    with _store(root) as store:
        store.create_ticket(
            origin={"provider": "pi", "prompt": "fix it"},
            mandate=MANDATE, ticket_id="code_001",
        )

    rows = step("mailbox", "tickets", "--json")
    assert [row["id"] for row in rows] == ["code_001"]

    one = step("mailbox", "ticket", "code_001", "--json")
    assert one["id"] == "code_001"
    assert one["origin"]["prompt"] == "fix it"

    missing = run("mailbox", "ticket", "nope", "--json")
    assert missing.exit_code == 1
    assert "unknown_ticket" in missing.output


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------

def test_start_and_stop_are_idempotent(root, monkeypatch):
    daemon_state = {"running": False, "spawned": 0}

    def fake_is_running(_root):
        if paths.stop_path(_root).exists():
            daemon_state["running"] = False
        return (daemon_state["running"],
                4242 if daemon_state["running"] else 0)

    def fake_spawn(_root):
        daemon_state["spawned"] += 1
        daemon_state["running"] = True
        return 4242

    monkeypatch.setattr("osw.mailbox.daemon.is_running", fake_is_running)
    monkeypatch.setattr("osw.mailbox_cli._spawn_serve", fake_spawn)

    assert step("mailbox", "start", "--json") == {"running": True, "pid": 4242}
    token = paths.token_path(root)
    assert token.exists() and len(token.read_text(encoding="utf-8").strip()) == 64
    assert step("mailbox", "start", "--json") == {"running": True, "pid": 4242}
    assert daemon_state["spawned"] == 1

    assert step("mailbox", "stop", "--json") == {"stopped": True}
    assert not paths.stop_path(root).exists()
    assert step("mailbox", "stop", "--json") == {"stopped": True}
    assert daemon_state["spawned"] == 1


# ---------------------------------------------------------------------------
# The lost watcher feeds the control plane
# ---------------------------------------------------------------------------

def test_lost_watcher_writes_one_system_envelope(root):
    state_mod.write_agent(root, {
        "agent_id": "agent_001", "terminal": "t", "state": "working",
        "watcher_pid": 1234,
    })

    with patch("osw.cli.pid_is_running", return_value=False):
        record = _mark_lost_if_orphaned(
            root, state_mod.read_agent(root, "agent_001"))

    assert record["state"] == "lost"
    pending = spool.read_pending(root)
    assert len(pending) == 1
    assert pending[0]["kind"] == "system"
    assert pending[0]["body"] == {"event": "watcher_died", "agent": "agent_001"}
    assert pending[0]["from"]["role"] == "system"
    assert pending[0]["to"]["role"] == "router"


def test_settled_watcher_writes_no_envelope(root):
    state_mod.write_agent(root, {
        "agent_id": "agent_002", "terminal": "t", "state": "done",
        "watcher_pid": 1234,
    })

    with patch("osw.cli.pid_is_running", return_value=False):
        _mark_lost_if_orphaned(root, state_mod.read_agent(root, "agent_002"))

    assert spool.pending_count(root) == 0

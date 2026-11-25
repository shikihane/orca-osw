"""Outbox delivery: one rebind retry after a dead ``terminal:`` address."""

from __future__ import annotations

import anyio

from osw.mailbox import delivery
from osw.mailbox.paths import db_path
from osw.mailbox.store import Store


def _deliver(store, notify, *, now=1000.0, hooks=None):
    async def main() -> list[dict]:
        return await delivery.deliver_outbox(
            store, notify, hooks=hooks, now=now
        )

    return anyio.run(main)


def _store(tmp_path) -> Store:
    return Store(db_path(tmp_path))


def _row(store: Store, outbox_id: str) -> dict:
    pending = store.outbox_pending(limit=10, now=float("inf"))
    return {row["id"]: row for row in pending}[outbox_id]


def test_a_dead_terminal_is_rebound_and_the_send_retried_once(tmp_path):
    store = _store(tmp_path)
    try:
        outbox_id = store.outbox_put(
            ticket_id="tk_1",
            conversation="terminal:term_dead",
            payload={"line": "hello"},
        )
        calls: list[tuple[str, str]] = []

        class Hooks:
            async def notify(self, conversation: str, line: str) -> None:
                calls.append((conversation, line))
                if conversation == "terminal:term_dead":
                    raise RuntimeError("no such terminal")

            async def rebind_conversation(self, conversation, payload):
                assert conversation == "terminal:term_dead"
                assert payload == {"line": "hello"}
                return "terminal:term_live"

        outcome = _deliver(store, Hooks().notify)[0]

        assert calls == [
            ("terminal:term_dead", "hello"),
            ("terminal:term_live", "hello"),
        ]
        assert outcome["state"] == "sent"
        assert outcome["receipt"] == "sent"
        assert outcome["attempts"] == 0
        assert store.outbox_pending(limit=10, now=float("inf")) == []
    finally:
        store.close()


def test_a_hooks_object_may_be_passed_instead_of_its_notify(tmp_path):
    store = _store(tmp_path)
    try:
        store.outbox_put(
            ticket_id="tk_1",
            conversation="terminal:term_dead",
            payload={"line": "hello"},
        )
        calls: list[str] = []

        class Hooks:
            async def notify(self, conversation: str, line: str) -> None:
                calls.append(conversation)
                if conversation == "terminal:term_dead":
                    raise RuntimeError("no such terminal")

            def rebind_conversation(self, conversation, payload):
                return "terminal:term_live"

        outcome = _deliver(store, Hooks())[0]

        assert calls == ["terminal:term_dead", "terminal:term_live"]
        assert outcome["state"] == "sent"
    finally:
        store.close()


def test_without_a_rebind_hook_the_backoff_path_is_unchanged(tmp_path):
    store = _store(tmp_path)
    try:
        outbox_id = store.outbox_put(
            ticket_id="tk_1",
            conversation="terminal:term_dead",
            payload={"line": "hello"},
        )
        calls: list[tuple[str, str]] = []

        class Hooks:
            async def notify(self, conversation: str, line: str) -> None:
                calls.append((conversation, line))
                raise RuntimeError("no such terminal")

        outcome = _deliver(store, Hooks().notify)[0]

        assert calls == [("terminal:term_dead", "hello")]
        assert outcome["state"] == "waiting"
        assert outcome["receipt"] == "waiting"
        assert outcome["attempts"] == 1
        assert outcome["next_at"] == 1002.0
        assert outcome["error"] == "no such terminal"
        row = _row(store, outbox_id)
        assert row["attempts"] == 1
        assert row["next_at"] == 1002.0
    finally:
        store.close()


def test_an_empty_rebind_answer_does_not_retry(tmp_path):
    store = _store(tmp_path)
    try:
        store.outbox_put(
            ticket_id="tk_1",
            conversation="terminal:term_dead",
            payload={"line": "hello"},
        )
        calls: list[tuple[str, str]] = []

        class Hooks:
            async def notify(self, conversation: str, line: str) -> None:
                calls.append((conversation, line))
                raise RuntimeError("no such terminal")

            async def rebind_conversation(self, conversation, payload):
                return ""

        outcome = _deliver(store, Hooks().notify)[0]

        assert calls == [("terminal:term_dead", "hello")]
        assert outcome["state"] == "waiting"
        assert outcome["attempts"] == 1
    finally:
        store.close()


def test_the_same_rebound_address_does_not_retry(tmp_path):
    store = _store(tmp_path)
    try:
        store.outbox_put(
            ticket_id="tk_1",
            conversation="terminal:term_dead",
            payload={"line": "hello"},
        )
        calls: list[tuple[str, str]] = []

        class Hooks:
            async def notify(self, conversation: str, line: str) -> None:
                calls.append((conversation, line))
                raise RuntimeError("no such terminal")

            async def rebind_conversation(self, conversation, payload):
                return "terminal:term_dead"

        outcome = _deliver(store, Hooks().notify)[0]

        assert calls == [("terminal:term_dead", "hello")]
        assert outcome["state"] == "waiting"
    finally:
        store.close()


def test_a_failing_rebind_hook_is_ignored(tmp_path):
    store = _store(tmp_path)
    try:
        store.outbox_put(
            ticket_id="tk_1",
            conversation="terminal:term_dead",
            payload={"line": "hello"},
        )
        calls: list[tuple[str, str]] = []

        class Hooks:
            async def notify(self, conversation: str, line: str) -> None:
                calls.append((conversation, line))
                raise RuntimeError("no such terminal")

            async def rebind_conversation(self, conversation, payload):
                raise RuntimeError("binding store is down")

        outcome = _deliver(store, Hooks().notify)[0]

        assert calls == [("terminal:term_dead", "hello")]
        assert outcome["state"] == "waiting"
        assert outcome["error"] == "no such terminal"
    finally:
        store.close()


def test_a_session_address_is_never_rebound(tmp_path):
    store = _store(tmp_path)
    try:
        store.outbox_put(
            ticket_id="tk_1",
            conversation="session:s_dead",
            payload={"line": "hello"},
        )
        calls: list[str] = []
        rebound: list[str] = []

        class Hooks:
            async def notify(self, conversation: str, line: str) -> None:
                calls.append(conversation)
                raise RuntimeError("session gone")

            async def rebind_conversation(self, conversation, payload):
                rebound.append(conversation)
                return "terminal:term_live"

        outcome = _deliver(store, Hooks().notify)[0]

        assert calls == ["session:s_dead"]
        assert rebound == []
        assert outcome["state"] == "waiting"
    finally:
        store.close()


def test_a_second_failure_after_the_rebind_takes_the_backoff_path(tmp_path):
    store = _store(tmp_path)
    try:
        outbox_id = store.outbox_put(
            ticket_id="tk_1",
            conversation="terminal:term_dead",
            payload={"line": "hello"},
        )
        calls: list[str] = []

        class Hooks:
            async def notify(self, conversation: str, line: str) -> None:
                calls.append(conversation)
                raise RuntimeError("everything is gone")

            async def rebind_conversation(self, conversation, payload):
                return "terminal:term_also_dead"

        outcome = _deliver(store, Hooks().notify)[0]

        assert calls == ["terminal:term_dead", "terminal:term_also_dead"]
        assert outcome["state"] == "waiting"
        assert outcome["attempts"] == 1
        assert outcome["next_at"] == 1002.0
        assert outcome["error"] == "everything is gone"
        assert _row(store, outbox_id)["attempts"] == 1
    finally:
        store.close()

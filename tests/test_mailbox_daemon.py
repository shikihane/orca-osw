"""Daemon, lock, doorbell and Outbox delivery (spec §9.4, §13, §14, §17)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import anyio
import pytest

from osw import state
from osw.log import read_events
from osw.mailbox import adapters, daemon, delivery, spool
from osw.mailbox.envelope import validate_envelope
from osw.mailbox.errors import MailboxError
from osw.mailbox.paths import (
    db_path,
    ensure_mailbox_dirs,
    lock_path,
    mailbox_dir,
    spool_dir,
    stop_path,
)
from osw.mailbox.store import Store

DOORBELL = '# [osw] mail pending={n} run="osw mailbox wait --as router --timeout 0"'


# ---------------------------------------------------------------------------
# helpers


def _store(root) -> Store:
    return Store(db_path(root))


def _envelope(**overrides) -> dict:
    raw = {
        "from": {"role": "watcher", "id": "terminal:term_w"},
        "to": {"role": "router"},
        "kind": "report",
        "body": {"outcome": "progress", "text": "working"},
    }
    raw.update(overrides)
    return validate_envelope(raw)


def _mandate() -> dict:
    return {
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


class FakeHooks:
    """Dispatcher hooks that record instead of touching Orca or the CLI."""

    def __init__(self) -> None:
        self.launched: list[tuple[dict, dict]] = []
        self.followed: list[tuple[dict, dict]] = []
        self.answered: list[tuple[dict, dict]] = []
        self.stopped: list[dict] = []
        self.notified: list[tuple[str, str]] = []

    async def launch(self, ticket: dict, payload: dict) -> dict:
        self.launched.append((ticket, payload))
        return {"agent_id": "code_001", "terminal": "term_x", "dispatch_id": ""}

    async def followup(self, ticket: dict, payload: dict) -> None:
        self.followed.append((ticket, payload))

    async def answer(self, ticket: dict, payload: dict) -> None:
        self.answered.append((ticket, payload))

    async def stop(self, ticket: dict) -> None:
        self.stopped.append(ticket)

    async def notify(self, conversation: str, line: str) -> None:
        self.notified.append((conversation, line))


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return int(proc.pid)


def _live_pid() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )


def _write_lock(root, pid: int) -> None:
    ensure_mailbox_dirs(root)
    lock_path(root).write_text(
        json.dumps({"pid": pid, "started_at": "2026-11-14T00:00:00+00:00"}),
        encoding="utf-8",
    )


def _deliver(store: Store, notify, *, now: float) -> list[dict]:
    async def main() -> list[dict]:
        return await delivery.deliver_outbox(store, notify, now=now)

    return anyio.run(main)


def _pending_lines(store: Store) -> list[str]:
    rows = store.outbox_pending(limit=100, now=float("inf"))
    return [str((row.get("payload") or {}).get("line") or "") for row in rows]


# ---------------------------------------------------------------------------
# lock


def test_acquire_lock_reclaims_a_dead_pid(tmp_path):
    root = tmp_path
    dead = _dead_pid()
    if state.pid_is_running(dead):
        pytest.skip("the dead pid was reused before the test ran")
    _write_lock(root, dead)

    payload = daemon.acquire_lock(root)

    assert payload["pid"] == os.getpid()
    on_disk = json.loads(lock_path(root).read_text(encoding="utf-8"))
    assert on_disk["pid"] == os.getpid()
    assert daemon.is_running(root) == (True, os.getpid())

    daemon.release_lock(root)

    assert not lock_path(root).exists()
    assert daemon.is_running(root) == (False, 0)


def test_acquire_lock_refuses_a_live_foreign_pid(tmp_path):
    root = tmp_path
    proc = _live_pid()
    try:
        _write_lock(root, proc.pid)

        with pytest.raises(MailboxError) as exc:
            daemon.acquire_lock(root)

        assert exc.value.code == "mailbox_locked"
        # The foreign lock is untouched, and release never steals it.
        on_disk = json.loads(lock_path(root).read_text(encoding="utf-8"))
        assert on_disk["pid"] == proc.pid
        daemon.release_lock(root)
        assert lock_path(root).exists()
        assert daemon.is_running(root) == (True, proc.pid)
    finally:
        proc.terminate()
        proc.wait()


# ---------------------------------------------------------------------------
# delivery


def test_outbox_sends_in_order_per_conversation_and_backs_off(tmp_path):
    store = _store(tmp_path)
    try:
        store.outbox_put(
            ticket_id="code_001", conversation="terminal:term_a",
            payload={"line": "one"},
        )
        store.outbox_put(
            ticket_id="code_001", conversation="terminal:term_a",
            payload={"line": "two"},
        )
        store.outbox_put(
            ticket_id="code_001", conversation="terminal:term_a",
            payload={"line": "three"},
        )
        store.outbox_put(
            ticket_id="code_001", conversation="terminal:term_b",
            payload={"line": "other"},
        )
        calls: list[tuple[str, str]] = []

        def notify(conversation: str, line: str) -> None:
            calls.append((conversation, line))
            if line == "two":
                raise RuntimeError("terminal gone")

        outcomes = _deliver(store, notify, now=1000.0)

        # "two" fails and blocks "three"; the other conversation is
        # independent and still leaves.
        assert calls == [
            ("terminal:term_a", "one"),
            ("terminal:term_a", "two"),
            ("terminal:term_b", "other"),
        ]
        by_line = {outcome["line"]: outcome for outcome in outcomes}
        assert by_line["one"]["receipt"] == "sent"
        assert by_line["other"]["receipt"] == "sent"
        assert by_line["two"]["receipt"] == "waiting"
        assert by_line["two"]["next_at"] == 1002.0
        assert "three" not in by_line

        # Before the backoff deadline nothing leaves; "three" must not
        # overtake its blocked predecessor.
        calls.clear()
        _deliver(store, notify, now=1001.9)
        assert calls == []

        # Backoff doubles on the next failure: 2 s, then 4 s.
        calls.clear()
        _deliver(store, notify, now=1002.0)
        assert [line for _conversation, line in calls] == ["two"]
        rows = {
            row["payload"]["line"]: row
            for row in store.outbox_pending(limit=10, now=float("inf"))
        }
        assert rows["two"]["attempts"] == 2
        assert rows["two"]["next_at"] == 1006.0
    finally:
        store.close()


def test_outbox_failure_sequence_ends_in_a_dead_letter(tmp_path):
    store = _store(tmp_path)
    try:
        store.outbox_put(
            ticket_id="code_001", conversation="terminal:term_a",
            payload={"line": "never"},
        )

        async def notify(conversation: str, line: str) -> None:
            raise RuntimeError("no such terminal")

        delays = [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
        now = 1000.0
        for index, delay in enumerate(delays):
            outcomes = _deliver(store, notify, now=now)
            assert outcomes[0]["state"] == "waiting"
            assert outcomes[0]["attempts"] == index + 1
            assert outcomes[0]["next_at"] == now + delay
            now += delay

        outcomes = _deliver(store, notify, now=now)

        assert outcomes[0]["state"] == "dead"
        assert outcomes[0]["receipt"] == "gone"
        assert outcomes[0]["attempts"] == delivery.MAX_ATTEMPTS
        assert store.outbox_pending(limit=10, now=float("inf")) == []
    finally:
        store.close()


def test_conversation_address_reads_the_transport():
    assert delivery.conversation_address({"terminal": "term_x"}) == (
        "terminal:term_x"
    )
    assert delivery.conversation_address({"session": "s1"}) == "session:s1"
    assert delivery.conversation_address(
        {"address": "terminal:term_y"}
    ) == "terminal:term_y"


def test_a_process_local_address_is_not_a_delivery_target():
    assert delivery.is_deliverable("terminal:term_a") is True
    assert delivery.is_deliverable("session:s1") is True
    assert delivery.is_deliverable("human") is True
    assert delivery.is_deliverable(f"local:{os.getpid()}") is False
    assert delivery.is_deliverable("") is False


# ---------------------------------------------------------------------------
# doorbell


def _queue_router_card(store: Store, index: int = 0) -> str:
    env = _envelope(
        **{
            "from": {"role": "human", "id": f"terminal:term_{index}"},
            "to": {"role": "router"},
            "kind": "request",
            "body": {"text": "please fix it"},
        },
    )
    store.enqueue(env)
    return env["id"]


def test_doorbell_one_pending_suppressed_then_rerings_and_wakes_a_human(tmp_path):
    root = tmp_path
    store = _store(root)
    try:
        _queue_router_card(store, 0)
        _queue_router_card(store, 1)
        store.bind_router("terminal:term_router", "token")

        daemon.router_notify(store, root, now=1000.0)
        assert _pending_lines(store) == [DOORBELL.format(n=2)]

        # A second doorbell while one is pending is suppressed.
        daemon.router_notify(store, root, now=1010.0)
        assert _pending_lines(store) == [DOORBELL.format(n=2)]

        # After the lease/120 s window it rings once more.
        daemon.router_notify(store, root, now=1121.0)
        assert _pending_lines(store) == [
            DOORBELL.format(n=2), DOORBELL.format(n=2),
        ]

        # After another window, a human attention line, exactly once.
        daemon.router_notify(store, root, now=1242.0)
        lines = _pending_lines(store)
        assert len(lines) == 3
        assert lines[2].startswith("# [osw] attention ")
        assert "pending=2" in lines[2]

        daemon.router_notify(store, root, now=2000.0)
        assert len(_pending_lines(store)) == 3
    finally:
        store.close()


def test_doorbell_does_not_ring_without_queued_cards(tmp_path):
    root = tmp_path
    store = _store(root)
    try:
        store.bind_router("terminal:term_router", "token")

        daemon.router_notify(store, root, now=1000.0)

        assert _pending_lines(store) == []
        assert store.kv_get("doorbell:state") in (None, "none")
    finally:
        store.close()


def _skips(root) -> list[dict]:
    return [
        event
        for event in read_events(state.logs_dir(root))
        if event.get("event") == "doorbell_skipped"
    ]


def test_headless_router_binding_never_dead_letters_the_doorbell(tmp_path):
    root = tmp_path
    store = _store(root)
    try:
        _queue_router_card(store, 0)
        # A headless Router registered its own pid, not a conversation.
        store.bind_router(f"local:{os.getpid()}", "token")

        # Nothing is queued for a process-local address: an Outbox row there
        # could only retry and dead-letter the doorbell.
        daemon.router_notify(store, root, now=1000.0)
        assert _pending_lines(store) == []
        assert store.kv_get("doorbell:state") == "pending"

        # Inside the interval nothing more is logged.
        daemon.router_notify(store, root, now=1001.0)
        assert len(_skips(root)) == 1

        # After the interval the undeliverable ring is retried, once.
        daemon.router_notify(store, root, now=1122.0)
        assert len(_skips(root)) == 2
        assert _pending_lines(store) == []
        assert store.dead_letters() == []
    finally:
        store.close()


def test_doorbell_attention_wakes_the_ticket_origin(tmp_path):
    root = tmp_path
    store = _store(root)
    try:
        store.create_ticket(
            origin={
                "role": "human",
                "id": "terminal:term_h",
                "text": "fix the bug",
            },
            mandate=_mandate(),
            ticket_id="code_001",
        )
        _queue_router_card(store, 0)
        store.bind_router(f"local:{os.getpid()}", "token")

        daemon.router_notify(store, root, now=1000.0)
        daemon.router_notify(store, root, now=1121.0)
        assert _pending_lines(store) == []

        # The human fallback is addressed to whoever opened the Ticket, not
        # to the headless Router pid.
        daemon.router_notify(store, root, now=1242.0)
        rows = store.outbox_pending(limit=10, now=float("inf"))
        assert len(rows) == 1
        assert rows[0]["conversation"] == "terminal:term_h"
        assert str(rows[0]["payload"]["line"]).startswith("# [osw] attention")
        assert store.kv_get("doorbell:state") == "human"
        assert store.dead_letters() == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# the loop


def test_one_tick_ingests_a_spool_envelope_and_settles_it(tmp_path):
    root = tmp_path
    store = _store(root)
    try:
        store.create_ticket(
            origin={
                "role": "human",
                "id": "terminal:term_h",
                "text": "do the thing",
            },
            mandate=_mandate(),
            ticket_id="code_001",
        )
    finally:
        store.close()

    envelope = _envelope(
        **{
            "from": {"role": "watcher", "id": "terminal:term_w"},
            "to": {"role": "router"},
            "kind": "report",
            "correlation": {"ticket": "code_001", "agent": "code_001"},
            "thread_key": "code_001",
            "body": {"outcome": "progress", "text": "halfway", "summary": "ok"},
        },
    )
    spool.write_envelope(root, envelope)
    hooks = FakeHooks()

    anyio.run(lambda: daemon.run(root, once=True, hooks=hooks))

    store = _store(root)
    try:
        row = store.get_message(envelope["id"])
        assert row is not None
        assert row["state"] == "settled"
        assert store.ticket("code_001")["stage"] == "working"
        assert not (spool_dir(root) / f"{envelope['id']}.json").exists()
        assert (
            spool_dir(root) / "ingested" / f"{envelope['id']}.json"
        ).exists()
    finally:
        store.close()

    status = daemon.read_status(root)
    assert status is not None
    assert status["running"] is True
    assert status["pid"] == os.getpid()
    assert status["spool_lag"] == 0
    assert status["schema_version"] == 1
    # once=True still releases the lock on the way out.
    assert daemon.is_running(root) == (False, 0)


def test_stop_file_ends_the_loop(tmp_path):
    root = tmp_path
    ensure_mailbox_dirs(root)
    stop_path(root).write_text("stop\n", encoding="utf-8")

    async def run_daemon() -> None:
        with anyio.fail_after(10):
            await daemon.run(root, poll_s=0.01, hooks=FakeHooks())

    anyio.run(run_daemon)

    assert not stop_path(root).exists()
    assert not lock_path(root).exists()
    status = daemon.read_status(root)
    assert status is not None
    assert status["running"] is True


def test_a_tick_without_an_adapter_config_does_no_adapter_work(tmp_path):
    root = tmp_path

    anyio.run(lambda: daemon.run(root, once=True, hooks=FakeHooks()))

    # No config, no enabled adapter: no state file and no spool envelope.
    assert list((mailbox_dir(root) / "adapters").glob("*.json")) == []
    assert spool.read_pending(root) == []


def test_a_tick_turns_a_matching_log_file_into_one_spool_envelope(tmp_path):
    root = tmp_path
    log = root / "app.log"
    log.write_text("alpha\nbeta\n", encoding="utf-8")
    config = adapters.config_path(root)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps({"log_file": {"paths": [str(log)]}}), encoding="utf-8",
    )

    anyio.run(lambda: daemon.run(root, once=True, hooks=FakeHooks()))

    pending = spool.read_pending(root)
    assert len(pending) == 1
    assert pending[0]["from"]["id"] == "adapter:log_file"
    assert pending[0]["body"]["count"] == 2
    # The adapter ran inside the tick and advanced its own read position.
    state = adapters.load_state(root, "log_file")
    assert state["files"][str(log)]["offset"] == log.stat().st_size


# ---------------------------------------------------------------------------
# production hooks (argv only; nothing is launched)


def test_cli_hooks_launch_uses_the_origin_terminal(tmp_path):
    hooks = daemon.CLIHooks(tmp_path)
    captured: list[str] = []

    async def fake_run(argv: list[str]) -> str:
        captured.extend(argv)
        return "Created code_001 on terminal term_new (provider: codex)"

    hooks._run_cli = fake_run  # type: ignore[method-assign]
    ticket = {
        "origin": {
            "role": "human",
            "id": "terminal:term_origin",
            "text": "fix the bug",
        },
        "mandate": _mandate(),
        "assignments": [],
    }

    result = anyio.run(
        hooks.launch, ticket, {"provider": "codex", "task": "fix the bug"},
    )

    assert captured[2:4] == ["new", "codex"]
    assert captured[-3:] == ["--caller-terminal", "term_origin", "fix the bug"]
    assert result == {
        "agent_id": "code_001",
        "terminal": "term_new",
        "dispatch_id": "",
    }


def test_cli_hooks_launch_falls_back_to_no_notify(tmp_path):
    hooks = daemon.CLIHooks(tmp_path)
    captured: list[str] = []

    async def fake_run(argv: list[str]) -> str:
        captured.extend(argv)
        return "Created code_002 on terminal term_y (provider: claude)"

    hooks._run_cli = fake_run  # type: ignore[method-assign]
    ticket = {
        "origin": {"role": "system", "id": "", "text": "run it"},
        "mandate": _mandate(),
        "assignments": [],
    }

    anyio.run(hooks.launch, ticket, {"prompt": "run it"})

    assert "--no-notify" in captured
    assert "--caller-terminal" not in captured


class _FakeProc:
    returncode = 0

    async def communicate(self):
        return (b"Created code_003 on terminal term_new (provider: codex)\n", b"")


def test_cli_hooks_launch_strips_the_inherited_router_identity(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OSW_ROLE", "router")
    monkeypatch.setenv("OSW_ROLE_TOKEN", "secret")
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_router")
    captured: dict = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(daemon.asyncio, "create_subprocess_exec", fake_exec)
    # The Router shell's own identity must not reach the child: otherwise
    # ``osw new`` refuses with router_forbidden and the launch dead-letters.
    hooks = daemon.CLIHooks(tmp_path)
    ticket = {
        "origin": {
            "role": "human",
            "id": "terminal:term_origin",
            "text": "fix the bug",
        },
        "mandate": _mandate(),
        "assignments": [],
    }

    result = anyio.run(
        hooks.launch, ticket, {"provider": "codex", "task": "fix the bug"},
    )

    assert result == {
        "agent_id": "code_003",
        "terminal": "term_new",
        "dispatch_id": "",
    }
    env = captured["env"]
    assert "OSW_ROLE" not in env
    assert "OSW_ROLE_TOKEN" not in env
    assert "ORCA_TERMINAL_HANDLE" not in env
    assert env.get("PATH") == os.environ.get("PATH")
    assert captured["cwd"] == str(tmp_path)
    assert list(captured["argv"])[2:4] == ["new", "codex"]


def test_daemon_inheriting_a_router_shell_builds_a_clean_hook_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OSW_ROLE", "router")
    monkeypatch.setenv("OSW_ROLE_TOKEN", "secret")
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_router")
    built: list[daemon.CLIHooks] = []

    class Recording(daemon.CLIHooks):
        def __init__(self, root):
            super().__init__(root)
            built.append(self)

    monkeypatch.setattr(daemon, "CLIHooks", Recording)

    anyio.run(lambda: daemon.run(tmp_path, once=True))

    assert built
    env = built[0].env
    assert "OSW_ROLE" not in env
    assert "OSW_ROLE_TOKEN" not in env
    assert "ORCA_TERMINAL_HANDLE" not in env


def test_cli_hooks_answer_sends_the_fixed_template(tmp_path, monkeypatch):
    hooks = daemon.CLIHooks(tmp_path)
    calls: list[tuple] = []

    async def fake_orca(*args: str) -> dict:
        calls.append(args)
        return {"ok": True}

    monkeypatch.setattr(daemon, "run_orca", fake_orca)
    ticket = {"origin": {}, "mandate": _mandate(), "assignments": []}

    anyio.run(
        hooks.answer,
        ticket,
        {
            "ask": "msg_1",
            "text": "raw text",
            "template": "# [osw] answer dec_1: raw text",
        },
    )

    assert calls == [(
        "orchestration",
        "reply",
        "--id",
        "msg_1",
        "--body",
        "# [osw] answer dec_1: raw text",
    )]


def test_cli_hooks_notify_relays_a_logical_role_to_the_router(tmp_path, monkeypatch):
    store = _store(tmp_path)
    try:
        store.bind_router("terminal:term_router", "token")
    finally:
        store.close()
    sent: list[tuple[str, str]] = []

    async def fake_send(handle: str, line: str) -> None:
        sent.append((handle, line))

    monkeypatch.setattr(daemon, "terminal_send", fake_send)
    hooks = daemon.CLIHooks(tmp_path)

    anyio.run(hooks.notify, "human", "# [osw] attention escalate x")

    assert sent == [("term_router", "# [osw] attention escalate x")]

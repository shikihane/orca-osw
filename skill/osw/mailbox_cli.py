"""Mailbox CLI verbs: the human and Router surface of the control plane.

The verbs here are thin: they read and write the Mailbox through the
store and spool modules, and never decide anything themselves. Execution
stays with the Dispatcher, and a decision written by ``decide`` is only
applied here when no daemon owns the queue.

Spec: ``docs/superpowers/specs/2026-11-14-osw-control-plane-design.md``
sections 9 and 18. Shapes are fixed by
``docs/superpowers/plans/2026-11-14-control-plane-interfaces.md`` section 13.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import anyio
import typer

from osw.log import emit_event, get_logger
from osw.mailbox import envelope, identity, paths, spool
from osw.mailbox.errors import MailboxError
from osw.mailbox.store import Store
from osw.process import hidden_subprocess_kwargs
from osw.state import logs_dir, resolve_project_root

log = get_logger("mailbox_cli")

mailbox_app = typer.Typer(
    help="Mailbox control plane: send, wait, decide, inspect",
    no_args_is_help=True,
)

SPAWN_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 10.0
POLL_S = 0.1
LEASE_S = 60.0
ROLES = ("router", "human", "worker", "broadcast")
CORRELATION_KEYS = ("ticket", "agent", "dispatch", "ask", "decision")


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

@contextmanager
def _errors() -> Iterator[None]:
    """Turn a typed mailbox failure into one readable line and exit 1."""
    try:
        yield
    except MailboxError as exc:
        typer.echo(f"Error: {exc.code}: {exc.message}")
        raise typer.Exit(1)


@contextmanager
def _open_store(root: Path) -> Iterator[Store]:
    """One store handle per command; always closed (Windows keeps locks)."""
    store = Store(paths.db_path(root))
    try:
        store.root = root  # optional event channel; read-only for behavior
    except Exception:  # pragma: no cover - a store that refuses the attribute
        pass
    try:
        yield store
    finally:
        store.close()


def _echo_json(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False))


def _echo_result(json_out: bool, value: Any, line: str) -> None:
    if json_out:
        _echo_json(value)
    else:
        typer.echo(line)


def _daemon() -> Any:
    """The daemon module, imported on demand (only start/stop/serve/decide)."""
    from osw.mailbox import daemon

    return daemon


def _dispatcher() -> Any:
    from osw import dispatcher

    return dispatcher


def _local_hooks(root: Path) -> Any:
    """Production hooks for a local apply when no daemon is running."""
    return _daemon().CLIHooks(root)


def _script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "osw.py"


def _spawn_serve(root: Path) -> int:
    """Start the daemon detached; the caller waits for the lock."""
    cmd = [sys.executable, str(_script_path()), "mailbox", "serve"]
    kwargs: dict[str, Any] = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        **hidden_subprocess_kwargs(),
    }
    if os.name == "nt":
        # CREATE_NO_WINDOW is already set by `hidden_subprocess_kwargs()`;
        # DETACHED_PROCESS would contradict it and Windows 11 shows an empty
        # terminal window for the child.
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        kwargs["start_new_session"] = True
    return int(subprocess.Popen(cmd, **kwargs).pid)


def _drain(root: Path, store: Store) -> dict:
    """Hand the pending spool to the store; the daemon is not required."""
    return spool.ingest(root, store)


def _transport_address() -> str:
    handle = os.environ.get("ORCA_TERMINAL_HANDLE", "").strip()
    return f"terminal:{handle}" if handle else ""


def _load_json_object(raw: str | None, code: str, what: str) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise MailboxError(code, f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise MailboxError(code, f"{what} must be a JSON object")
    return value


def _correlation(row: dict) -> dict:
    """Correlation keys, from a reassembled envelope or from flat row columns."""
    value = row.get("correlation")
    if isinstance(value, dict) and any(value.get(key) for key in CORRELATION_KEYS):
        return value
    return {key: str(row.get(key) or "") for key in CORRELATION_KEYS}


def _envelope(row: dict) -> dict:
    """Rebuild the canonical envelope from a store row.

    Rows are flat columns with the JSON parts decoded; ``card`` and the
    JSON surfaces expect the §7 envelope shape.
    """
    correlation = _correlation(row)
    return {
        "v": int(row.get("v") or 1),
        "id": str(row.get("id") or ""),
        "ts": str(row.get("ts") or ""),
        "from": {
            "role": str(row.get("from_role") or ""),
            "id": str(row.get("from_id") or ""),
            "trust": str(row.get("trust") or ""),
        },
        "to": {
            "role": str(row.get("to_role") or ""),
            "id": str(row.get("to_id") or ""),
        },
        "kind": str(row.get("kind") or ""),
        "correlation": {
            key: str(correlation.get(key) or row.get(key) or "")
            for key in CORRELATION_KEYS
        },
        "thread_key": str(row.get("thread_key") or ""),
        "reply_to": str(row.get("reply_to") or ""),
        "priority": int(row.get("priority") or 0),
        "ttl_s": int(row.get("ttl_s") or 0),
        "dedupe_key": str(row.get("dedupe_key") or ""),
        "body": row.get("body") or {},
        "state": str(row.get("state") or ""),
        "attempts": int(row.get("attempts") or 0),
        "lease": row.get("lease"),
        "trace": str(row.get("trace") or ""),
    }


def _card_for(store: Store, row: dict) -> dict:
    ticket_id = str(_correlation(row).get("ticket") or "")
    ticket = store.ticket(ticket_id) if ticket_id else None
    return envelope.card(_envelope(row), ticket=ticket)


def _owner(role: str) -> str:
    return f"{role}-{os.getpid()}"


def _resolve_author(root: Path, author: str | None) -> str:
    """The Decision author: explicit, else the caller's role, never forged.

    The Router writes Decisions and a human writes Decisions; neither may
    speak as the other. An explicit ``--author`` must match the identity, and
    the default follows it, so a human in the same terminal keeps the human
    identity (spec §16, plan Addendum A item 10).
    """
    role = identity.current_role(root)
    chosen = (author or role).strip().lower()
    if chosen not in ("router", "human"):
        raise MailboxError(
            "invalid_field", f"--author takes router or human, got {chosen!r}")
    if chosen == "human" and role == "router":
        identity.require_human_identity(
            root, command="mailbox decide --author human")
    if chosen == "router" and role != "router":
        raise MailboxError(
            "router_forbidden",
            "decide --author router needs OSW_ROLE=router and the project role token",
        )
    return chosen


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@mailbox_app.command("start")
def start(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Start the mailbox daemon for this project (idempotent)."""
    root = resolve_project_root()
    with _errors():
        daemon = _daemon()
        paths.ensure_mailbox_dirs(root)
        # The first Router session needs a token to exist before it can
        # register; `start` provisions it once, 0600, and reuses it after.
        identity.generate_token(root)
        running, pid = daemon.is_running(root)
        if not running:
            _spawn_serve(root)
            running, pid = _wait_lock(root)
            if not running:
                raise MailboxError(
                    "mailbox_unavailable",
                    f"the daemon did not take the lock within {SPAWN_TIMEOUT_S:.0f}s",
                )
        _echo_result(
            json_out, {"running": True, "pid": int(pid)}, f"mailbox running (pid {pid})",
        )


def _wait_lock(root: Path) -> tuple[bool, int]:
    deadline = time.monotonic() + SPAWN_TIMEOUT_S
    while True:
        running, pid = _daemon().is_running(root)
        if running or time.monotonic() >= deadline:
            return running, pid
        time.sleep(POLL_S)


@mailbox_app.command("stop")
def stop(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Ask the daemon to stop and wait for it (idempotent)."""
    root = resolve_project_root()
    with _errors():
        daemon = _daemon()
        paths.ensure_mailbox_dirs(root)
        request = paths.stop_path(root)
        request.write_text(str(os.getpid()), encoding="utf-8")
        deadline = time.monotonic() + STOP_TIMEOUT_S
        running, _pid = daemon.is_running(root)
        while running and time.monotonic() < deadline:
            time.sleep(POLL_S)
            running, _pid = daemon.is_running(root)
        if running:
            raise MailboxError(
                "mailbox_unavailable",
                f"the daemon is still running after {STOP_TIMEOUT_S:.0f}s",
            )
        try:
            request.unlink()
        except FileNotFoundError:
            pass
        _echo_result(json_out, {"stopped": True}, "mailbox stopped")


@mailbox_app.command("serve", hidden=True)
def serve() -> None:
    """Run the mailbox daemon in the foreground (spawned by `start`)."""
    root = resolve_project_root()
    with _errors():
        anyio.run(_daemon().run, root)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@mailbox_app.command("status")
def status(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Show the control-plane status of this project (spec section 18)."""
    root = resolve_project_root()
    with _errors(), _open_store(root) as store:
        data = store.status()
        running, pid = _daemon().is_running(root)
        queues = data.get("queues") or {}
        result = {
            "running": bool(running),
            "pid": int(pid or 0),
            "schema_version": int(data.get("schema_version") or 1),
            "queues": {role: int(queues.get(role) or 0) for role in ROLES},
            "oldest_age_s": data.get("oldest_age_s") or 0,
            "leased": int(data.get("leased") or 0),
            "dead": int(data.get("dead") or 0),
            "spool_lag": spool.pending_count(root),
            "doorbell": str(data.get("doorbell") or "none"),
            "decisions_applied_5m": int(data.get("decisions_applied_5m") or 0),
        }
        if json_out:
            _echo_json(result)
            return
        typer.echo(f"running: {'yes' if result['running'] else 'no'} (pid {result['pid']})")
        typer.echo(f"schema: {result['schema_version']}")
        typer.echo("queues: " + " ".join(
            f"{role}={result['queues'][role]}" for role in ROLES))
        typer.echo(f"oldest_age_s: {result['oldest_age_s']}")
        typer.echo(f"leased: {result['leased']}")
        typer.echo(f"dead: {result['dead']}")
        typer.echo(f"spool_lag: {result['spool_lag']}")
        typer.echo(f"doorbell: {result['doorbell']}")
        typer.echo(f"decisions_applied_5m: {result['decisions_applied_5m']}")


# ---------------------------------------------------------------------------
# Producers and the Router loop
# ---------------------------------------------------------------------------

@mailbox_app.command("send")
def send(
    kind: str = typer.Option(..., "--kind", help="Envelope kind"),
    body_json: str = typer.Option(None, "--body-json", help="Body as a JSON object"),
    text: str = typer.Option(None, "--text", help="Body text (sent as {\"text\": ...})"),
    from_role: str = typer.Option(None, "--from-role", help="Author role"),
    from_id: str = typer.Option(None, "--from-id", help="Author address"),
    to_role: str = typer.Option(None, "--to-role", help="Target role"),
    ticket: str = typer.Option(None, "--ticket", help="Ticket id to correlate with"),
    agent: str = typer.Option(None, "--agent", help="Agent id to correlate with"),
    thread_key: str = typer.Option(None, "--thread-key", help="Local thread identity"),
    dedupe_key: str = typer.Option(None, "--dedupe-key", help="Producer dedupe key"),
    priority: int = typer.Option(None, "--priority", help="0..3, higher is later"),
    reply_to: str = typer.Option(None, "--reply-to", help="Message id this replies to"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Write one envelope to the spool (the mailbox ingests it)."""
    root = resolve_project_root()
    with _errors():
        # Sending is the Dispatcher's surface: the Router writes a Decision
        # and lets the Dispatcher execute it (spec §9.2, §16).
        identity.require_human_identity(root, command="mailbox send")
        if body_json and text:
            raise MailboxError("invalid_envelope", "use --body-json or --text, not both")
        body = _load_json_object(body_json, "invalid_envelope", "--body-json")
        if text:
            body = {"text": text}
        role = (from_role or "human").strip()
        target = (to_role or ("human" if role == "router" else "router")).strip()
        raw: dict[str, Any] = {
            "id": envelope.new_id(),
            "from": {"role": role, "id": (from_id or _transport_address()).strip()},
            "to": {"role": target, "id": ""},
            "kind": kind.strip(),
            "correlation": {"ticket": (ticket or "").strip(),
                            "agent": (agent or "").strip()},
            "thread_key": (thread_key or "").strip(),
            "reply_to": (reply_to or "").strip(),
            "dedupe_key": (dedupe_key or "").strip(),
            "body": body,
        }
        if priority is not None:
            raw["priority"] = priority
        message_id = spool.write_envelope(root, raw)
        _echo_result(json_out, {"id": message_id}, message_id)


@mailbox_app.command("wait")
def wait(
    as_role: str = typer.Option(None, "--as", help="router or human (default: human)"),
    timeout: float = typer.Option(30.0, "--timeout", help="Seconds to wait; 0 = one scan"),
    max_cards: int = typer.Option(1, "--max", help="Maximum cards to lease"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Lease and acknowledge decision cards for a role."""
    root = resolve_project_root()
    with _errors(), _open_store(root) as store:
        role = (as_role or "human").strip()
        if role not in ("router", "human"):
            raise MailboxError("unknown_role", f"wait --as takes router or human, got {role!r}")
        if role == "router":
            _require_router_identity(root)
            _bind_router(root, store)
        owner = _owner(role)
        _drain(root, store)
        cards: list[dict] = []
        limit = max(1, int(max_cards))
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            rows = store.lease(
                owner, to_role=role, limit=max(1, limit - len(cards)), lease_s=LEASE_S,
            )
            for row in rows:
                if not store.ack(row["id"], owner):
                    continue
                cards.append(_card_for(store, row))
            if len(cards) >= limit or time.monotonic() >= deadline:
                break
            time.sleep(POLL_S)
            _drain(root, store)
        if role == "router":
            _consume_doorbell(root, store)
        if json_out:
            _echo_json(cards)
            return
        if not cards:
            typer.echo("no cards")
        for card in cards:
            typer.echo(
                f"[{card['priority']}] {card['message_id']} {card['intent']} "
                f"{card['from']} :: {card['summary']}"
            )


def _require_router_identity(root: Path) -> None:
    if identity.current_role(root) != "router":
        raise MailboxError(
            "router_forbidden",
            "wait --as router needs OSW_ROLE=router and the project role token",
        )


def _bind_router(root: Path, store: Store) -> None:
    try:
        store.bind_router(_router_address(), identity.generate_token(root))
    except Exception as exc:  # pragma: no cover - binding is not a card blocker
        log.warning("router binding failed: %s", exc)


def _router_address() -> str:
    """The Router's transport address; a process-local fallback when headless."""
    return _transport_address() or f"local:{os.getpid()}"


def _consume_doorbell(root: Path, store: Store) -> None:
    if store.kv_get("doorbell:state") != "pending":
        return
    store.kv_set("doorbell:state", "none")
    emit_event(
        logs_dir(root),
        component="mailbox",
        event="doorbell_consumed",
        message="router consumed the doorbell",
    )


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

@mailbox_app.command("show")
def show(
    message_id: str = typer.Argument(..., help="Message id"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Show one envelope with its card and ticket."""
    root = resolve_project_root()
    with _errors(), _open_store(root) as store:
        _drain(root, store)
        message = store.get_message(message_id)
        if message is None:
            raise MailboxError("unknown_message", message_id)
        ticket_id = str(_correlation(message).get("ticket") or "")
        ticket = store.ticket(ticket_id) if ticket_id else None
        shown = _envelope(message)
        card = envelope.card(shown, ticket=ticket)
        if json_out:
            _echo_json({"message": shown, "card": card, "ticket": ticket})
            return
        author = shown["from"]
        typer.echo(f"id: {shown['id']}")
        typer.echo(f"kind: {shown['kind']}")
        typer.echo(
            f"from: {author['role']}:{author['id']} ({shown['trust']})"
        )
        typer.echo(f"to: {shown['to']['role']}")
        typer.echo(f"state: {shown['state']}")
        typer.echo(f"priority: {shown['priority']}")
        typer.echo(f"ticket: {ticket_id}")
        typer.echo(f"summary: {card['summary']}")
        typer.echo("body: " + json.dumps(shown["body"], ensure_ascii=False))


@mailbox_app.command("trace")
def trace(
    target: str = typer.Argument(..., help="Message id or ticket id"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Trace one message or ticket through decisions, effects and outbox."""
    root = resolve_project_root()
    with _errors(), _open_store(root) as store:
        _drain(root, store)
        message = store.get_message(target)
        ticket_id = target
        if message is not None:
            ticket_id = str(_correlation(message).get("ticket") or "")
        ticket = store.ticket(ticket_id) if ticket_id else None
        rows = _trace_rows(
            paths.db_path(root),
            message_id=message["id"] if message else "",
            ticket_id=ticket["id"] if ticket else "",
        )
        result = {
            "message": _envelope(message) if message else None,
            "ticket": ticket,
            "decisions": rows["decisions"],
            "effects": rows["effects"],
            "outbox": rows["outbox"],
        }
        if json_out:
            _echo_json(result)
            return
        typer.echo(f"message: {result['message']['id'] if message else '(none)'}")
        typer.echo(f"ticket: {ticket['id'] if ticket else '(none)'}")
        typer.echo(f"decisions: {len(result['decisions'])}")
        for decision in result["decisions"]:
            typer.echo(
                f"  {decision.get('decision_id', '')} "
                f"{decision.get('action', '')} {decision.get('state', '')}"
            )
        typer.echo(f"effects: {len(result['effects'])}")
        typer.echo(f"outbox: {len(result['outbox'])}")


_JSON_COLUMNS = {
    "decisions": ("payload", "result"),
    "effects": ("payload",),
    "outbox": ("payload", "receipt"),
}


def _trace_rows(db: Path, *, message_id: str, ticket_id: str) -> dict[str, list[dict]]:
    """Read-only rows around one correlation, straight from the store's DB.

    The store owns no listing API for every effect and outbox row, and a
    trace must show delivered and dead rows too, not only pending ones.
    """
    rows: dict[str, list[dict]] = {"decisions": [], "effects": [], "outbox": []}
    if not db.exists():
        return rows
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        log.warning("trace cannot open the store: %s", exc)
        return rows
    try:
        conn.row_factory = sqlite3.Row
        rows["decisions"] = _query(
            conn, "decisions",
            "SELECT * FROM decisions WHERE message_id = ? OR ticket = ? "
            "ORDER BY ts",
            (message_id, ticket_id),
        )
        decision_ids = [str(d.get("decision_id") or "") for d in rows["decisions"]]
        if decision_ids:
            marks = ",".join("?" for _ in decision_ids)
            rows["effects"] = _query(
                conn, "effects",
                f"SELECT * FROM effects WHERE decision_id IN ({marks}) ORDER BY next_at",
                tuple(decision_ids),
            )
        if ticket_id:
            rows["outbox"] = _query(
                conn, "outbox",
                "SELECT * FROM outbox WHERE ticket_id = ? ORDER BY seq",
                (ticket_id,),
            )
    except sqlite3.Error as exc:
        log.warning("trace read failed: %s", exc)
    finally:
        conn.close()
    return rows


def _query(conn: sqlite3.Connection, table: str, sql: str, params: tuple) -> list[dict]:
    result: list[dict] = []
    for row in conn.execute(sql, params):
        item = dict(row)
        for column in _JSON_COLUMNS.get(table, ()):
            item[column] = _maybe_json(item.get(column))
        result.append(item)
    return result


def _maybe_json(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


# ---------------------------------------------------------------------------
# Decisions and tickets
# ---------------------------------------------------------------------------

@mailbox_app.command("decide")
def decide(
    message_id: str = typer.Argument(..., help="Message id the decision answers"),
    action: str = typer.Option(..., "--action", help="Decision action"),
    reason: str = typer.Option(..., "--reason", help="One-sentence reason"),
    ticket: str = typer.Option(None, "--ticket", help="Ticket id (default: the message's)"),
    payload_json: str = typer.Option(None, "--payload-json", help="Payload as a JSON object"),
    author: str = typer.Option(None, "--author", help="router or human"),
    confidence: float = typer.Option(None, "--confidence", help="0.0..1.0"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Record a Decision; a stopped daemon is replaced by a local apply."""
    root = resolve_project_root()
    with _errors(), _open_store(root) as store:
        author_role = _resolve_author(root, author)
        _drain(root, store)
        message = store.get_message(message_id)
        if message is None:
            raise MailboxError("unknown_message", message_id)
        payload = _load_json_object(payload_json, "decision_invalid", "--payload-json")
        dispatcher = _dispatcher()
        raw: dict[str, Any] = {
            "decision_id": envelope.new_id("dec"),
            "message_id": message_id,
            "action": action.strip(),
            "ticket": (ticket or str(_correlation(message).get("ticket") or "")).strip(),
            "reason": reason,
            "payload": payload,
            "author": author_role,
            "confidence": 1.0 if confidence is None else float(confidence),
        }
        decision = dispatcher.validate_decision(raw)
        if not dispatcher.commit(store, decision, message_id=message_id):
            raise MailboxError(
                "duplicate_decision", str(decision.get("decision_id") or ""))
        running, _pid = _daemon().is_running(root)
        applied = False
        if not running:
            anyio.run(dispatcher.run_pending, store, _local_hooks(root))
            applied = True
        decision_id = str(decision["decision_id"])
        _echo_result(
            json_out,
            {"decision_id": decision_id},
            f"decision {decision_id} {'applied' if applied else 'committed'}",
        )


@mailbox_app.command("tickets")
def tickets(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """List tickets."""
    root = resolve_project_root()
    with _errors(), _open_store(root) as store:
        rows = store.tickets()
        if json_out:
            _echo_json(rows)
            return
        if not rows:
            typer.echo("no tickets")
        for ticket in rows:
            typer.echo(
                f"{ticket.get('id', ''):<20} {ticket.get('stage', ''):<12} "
                f"assigned={len(ticket.get('assignments') or [])} "
                f"reports={len(ticket.get('reports') or [])}"
            )


@mailbox_app.command("ticket")
def ticket(
    ticket_id: str = typer.Argument(..., help="Ticket id"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Show one ticket with its assignments, reports and decisions."""
    root = resolve_project_root()
    with _errors(), _open_store(root) as store:
        row = store.ticket(ticket_id)
        if row is None:
            raise MailboxError("unknown_ticket", ticket_id)
        if json_out:
            _echo_json(row)
            return
        typer.echo(f"id: {row.get('id', '')}")
        typer.echo(f"stage: {row.get('stage', '')}")
        typer.echo("origin: " + json.dumps(row.get("origin") or {}, ensure_ascii=False))
        typer.echo(f"assignments: {len(row.get('assignments') or [])}")
        typer.echo(f"reports: {len(row.get('reports') or [])}")
        typer.echo(f"open_asks: {len(row.get('open_asks') or [])}")
        typer.echo(f"decisions: {len(row.get('decisions') or [])}")

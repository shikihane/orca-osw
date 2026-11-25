"""The Mailbox SQLite store: messages, tickets, decisions, effects, outbox.

One database file (``mailbox.db``) behind the whole control plane. Every
mutation runs in one short transaction so a decision, its effects, the message
settlement and the outbox rows commit together or not at all. The store owns
no policy: it validates shapes, keeps the counters and hands back plain dicts
with JSON columns decoded.

Design: ``docs/superpowers/specs/2026-11-14-osw-control-plane-design.md``
sections 10-13 and 17-18.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from osw.mailbox.errors import MailboxError

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
QUEUE_ROLES = ("router", "human", "worker", "broadcast")
CORRELATION_KEYS = ("ticket", "agent", "dispatch", "ask", "decision")
ACTIONS = frozenset({
    "accept", "followup", "answer", "stop", "retry", "reject", "escalate",
    "noop", "close",
})
STAGES = frozenset({
    "open", "queued", "working", "waiting_answer", "blocked", "review",
    "done", "stopped", "lost",
})
EFFECT_STATES = frozenset({"pending", "running", "done", "dead"})

_TICKET_JSON = frozenset({"origin", "mandate", "assignments", "reports",
                          "open_asks", "decisions"})

_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    ts TEXT,
    from_role TEXT,
    from_id TEXT,
    trust TEXT,
    to_role TEXT,
    to_id TEXT,
    kind TEXT,
    ticket TEXT,
    agent TEXT,
    dispatch TEXT,
    ask TEXT,
    decision TEXT,
    thread_key TEXT,
    reply_to TEXT,
    priority INTEGER,
    ttl_s INTEGER,
    dedupe_key TEXT,
    body TEXT,
    state TEXT,
    attempts INTEGER,
    lease_owner TEXT,
    lease_until REAL,
    trace TEXT,
    summary TEXT,
    created_at REAL,
    settled_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedupe
    ON messages(dedupe_key) WHERE dedupe_key <> '';
CREATE INDEX IF NOT EXISTS idx_messages_queue
    ON messages(to_role, state, priority, ts);

CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    origin TEXT,
    mandate TEXT,
    stage TEXT,
    assignments TEXT,
    reports TEXT,
    open_asks TEXT,
    decisions TEXT,
    thread_key TEXT,
    created_at REAL,
    updated_at REAL,
    closed_at REAL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    message_id TEXT,
    action TEXT,
    ticket TEXT,
    reason TEXT,
    payload TEXT,
    author TEXT,
    confidence REAL,
    state TEXT,
    result TEXT,
    ts REAL
);

CREATE TABLE IF NOT EXISTS effects (
    id TEXT PRIMARY KEY,
    decision_id TEXT,
    kind TEXT,
    key TEXT,
    payload TEXT,
    state TEXT,
    attempts INTEGER,
    next_at REAL,
    last_error TEXT,
    claim_owner TEXT,
    claim_until REAL,
    UNIQUE(kind, key)
);

CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    ticket_id TEXT,
    conversation TEXT,
    seq INTEGER,
    payload TEXT,
    state TEXT,
    attempts INTEGER,
    next_at REAL,
    sent_at REAL,
    receipt TEXT,
    created_at REAL,
    UNIQUE(ticket_id, conversation, seq)
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _clock(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise MailboxError("store_corrupt", f"invalid JSON column: {value!r}") from exc


def _load_list(value: Any, column: str) -> list:
    decoded = _loads(value, [])
    if not isinstance(decoded, list):
        raise MailboxError("store_corrupt", f"{column} is not a JSON list")
    return decoded


def _load_dict(value: Any, column: str) -> dict:
    decoded = _loads(value, {})
    if not isinstance(decoded, dict):
        raise MailboxError("store_corrupt", f"{column} is not a JSON object")
    return decoded


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _new_id(prefix: str) -> str:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


def new_ticket_id(prefix: str = "batch") -> str:
    """A fresh id for a Ticket the caller creates itself (a fan-out batch)."""
    return _new_id(prefix)


def _require_str(value: Any, field: str, *, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise MailboxError("invalid_field", f"{field} must be a string")
    return value


def _settle_message(
    conn: sqlite3.Connection, message_id: str, outcome: str = ""
) -> None:
    """Mark one message settled inside the caller's transaction."""
    stamp = time.time()
    row = conn.execute(
        "SELECT trace FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        raise MailboxError("unknown_message", f"no message {message_id!r}")
    trace = row["trace"] or ""
    if outcome:
        note = f"settled:{outcome}"
        trace = f"{trace}\n{note}" if trace else note
    conn.execute(
        "UPDATE messages SET state = 'settled', settled_at = ?,"
        " lease_owner = NULL, lease_until = NULL, trace = ? WHERE id = ?",
        (stamp, trace, message_id),
    )


class Store:
    """One SQLite connection to ``mailbox.db``.

    Callers keep a single ``Store`` per process; extra processes open their own
    connection on the same file (WAL plus a 5 s busy timeout). ``root`` is
    optional and only carried for event logging by callers.
    """

    path: Path
    root: Path | None

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.root = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                str(self.path),
                timeout=BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.executescript(_DDL)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise MailboxError(
                    "store_corrupt",
                    f"schema version {version} is not {SCHEMA_VERSION}",
                )
        except sqlite3.DatabaseError as exc:
            if conn is not None:
                conn.close()
            raise MailboxError(
                "store_corrupt", f"cannot open {self.path}: {exc}"
            ) from exc
        except BaseException:
            if conn is not None:
                conn.close()
            raise
        self._conn: sqlite3.Connection = conn

    # -- transactions -----------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """A short write transaction (``BEGIN IMMEDIATE``)."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException as exc:
            conn.rollback()
            if isinstance(exc, sqlite3.OperationalError) and (
                "locked" in str(exc) or "busy" in str(exc)
            ):
                raise MailboxError("mailbox_unavailable", str(exc)) from exc
            raise
        else:
            conn.commit()

    @contextmanager
    def _snapshot(self) -> Iterator[sqlite3.Connection]:
        """A consistent read-only snapshot (deferred transaction)."""
        conn = self._conn
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException as exc:
            conn.rollback()
            if isinstance(exc, sqlite3.OperationalError) and (
                "locked" in str(exc) or "busy" in str(exc)
            ):
                raise MailboxError("mailbox_unavailable", str(exc)) from exc
            raise
        else:
            conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- messages ---------------------------------------------------------

    def enqueue(self, envelope: dict) -> tuple[dict | None, bool]:
        """Persist one validated envelope; a duplicate id/dedupe key is ignored."""
        if not isinstance(envelope, dict):
            raise MailboxError("invalid_envelope", "envelope must be an object")
        message_id = envelope.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise MailboxError("invalid_envelope", "envelope.id is required")
        sender = envelope.get("from") or {}
        receiver = envelope.get("to") or {}
        correlation = envelope.get("correlation") or {}
        if not isinstance(sender, dict) or not isinstance(receiver, dict):
            raise MailboxError("invalid_envelope", "from/to must be objects")
        if not isinstance(correlation, dict):
            raise MailboxError("invalid_envelope", "correlation must be an object")
        body = envelope.get("body") or {}
        if not isinstance(body, dict):
            raise MailboxError("invalid_envelope", "body must be an object")
        lease = envelope.get("lease") or {}
        if not isinstance(lease, dict):
            raise MailboxError("invalid_envelope", "lease must be an object or null")
        summary = body.get("summary")
        if not isinstance(summary, str):
            summary = ""
        lease_owner = lease.get("owner")
        lease_until = lease.get("until")
        params = (
            message_id,
            _require_str(envelope.get("ts"), "ts"),
            _require_str(sender.get("role"), "from.role"),
            _require_str(sender.get("id"), "from.id"),
            _require_str(sender.get("trust"), "from.trust"),
            _require_str(receiver.get("role"), "to.role"),
            _require_str(receiver.get("id"), "to.id"),
            _require_str(envelope.get("kind"), "kind"),
            _require_str(correlation.get("ticket"), "correlation.ticket"),
            _require_str(correlation.get("agent"), "correlation.agent"),
            _require_str(correlation.get("dispatch"), "correlation.dispatch"),
            _require_str(correlation.get("ask"), "correlation.ask"),
            _require_str(correlation.get("decision"), "correlation.decision"),
            _require_str(envelope.get("thread_key"), "thread_key"),
            _require_str(envelope.get("reply_to"), "reply_to"),
            _as_int(envelope.get("priority"), 3),
            _as_int(envelope.get("ttl_s"), 0),
            _require_str(envelope.get("dedupe_key"), "dedupe_key"),
            _dumps(body),
            _require_str(envelope.get("state"), "state", default="queued") or "queued",
            _as_int(envelope.get("attempts"), 0),
            lease_owner if isinstance(lease_owner, str) and lease_owner else None,
            _as_float(lease_until, 0.0) if lease_until is not None else None,
            _require_str(envelope.get("trace"), "trace"),
            summary,
            time.time(),
            None,
        )
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO messages (id, ts, from_role, from_id, trust,"
                    " to_role, to_id, kind, ticket, agent, dispatch, ask, decision,"
                    " thread_key, reply_to, priority, ttl_s, dedupe_key, body,"
                    " state, attempts, lease_owner, lease_until, trace, summary,"
                    " created_at, settled_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    params,
                )
        except sqlite3.IntegrityError:
            return None, False
        return self.get_message(message_id), True

    def get_message(self, message_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return None if row is None else _message_row(row)

    def lease(
        self,
        owner: str,
        *,
        to_role: str = "router",
        limit: int = 1,
        lease_s: float = 60.0,
        now: float | None = None,
    ) -> list[dict]:
        """Lease up to ``limit`` eligible messages for ``to_role``.

        Only the oldest outstanding row of a correlation is eligible, so a
        correlation is delivered in FIFO order while priorities order distinct
        correlations.
        """
        limit = int(limit)
        if limit <= 0:
            return []
        stamp = _clock(now)
        select_sql = """
            WITH outstanding AS (
                SELECT m.*,
                       COALESCE(NULLIF(m.ticket, ''), NULLIF(m.agent, ''),
                                NULLIF(m.dispatch, ''), NULLIF(m.ask, ''),
                                NULLIF(m.decision, ''), NULLIF(m.thread_key, ''),
                                m.id) AS grp
                FROM messages AS m
                WHERE m.to_role = ? AND m.state IN ('queued', 'leased')
            ),
            ranked AS (
                SELECT id, state, lease_until, priority, ts,
                       ROW_NUMBER() OVER (PARTITION BY grp ORDER BY ts, id) AS rn
                FROM outstanding
            )
            SELECT id FROM ranked
            WHERE rn = 1 AND (state = 'queued' OR lease_until < ?)
            ORDER BY priority, ts, id
            LIMIT ?
        """
        with self._tx() as conn:
            ids = [
                row["id"]
                for row in conn.execute(select_sql, (to_role, stamp, limit))
            ]
            if not ids:
                return []
            until = stamp + float(lease_s)
            for message_id in ids:
                conn.execute(
                    "UPDATE messages SET state = 'leased', lease_owner = ?,"
                    " lease_until = ?, attempts = attempts + 1 WHERE id = ?",
                    (owner, until, message_id),
                )
            rows = conn.execute(
                "SELECT * FROM messages WHERE id IN (%s)"
                % ",".join("?" for _ in ids),
                tuple(ids),
            ).fetchall()
        by_id = {row["id"]: _message_row(row) for row in rows}
        return [by_id[message_id] for message_id in ids if message_id in by_id]

    def ack(self, message_id: str, owner: str) -> bool:
        """Acknowledge a leased message; repeating the same ack is a no-op."""
        stamp = time.time()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state, lease_owner FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if row is None or row["lease_owner"] != owner:
                return False
            if row["state"] == "acked":
                return True
            if row["state"] != "leased":
                return False
            conn.execute(
                "UPDATE messages SET state = 'acked', settled_at = ? WHERE id = ?",
                (stamp, message_id),
            )
        return True

    def release(
        self,
        message_id: str,
        owner: str,
        *,
        error: str = "",
        dead: bool = False,
    ) -> None:
        """Return a lease to the queue (or bury the message)."""
        stamp = time.time()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT lease_owner, trace FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if row is None or row["lease_owner"] != owner:
                return
            trace = row["trace"] or ""
            if error:
                trace = f"{trace}\n{error}" if trace else error
            state = "dead" if dead else "queued"
            conn.execute(
                "UPDATE messages SET state = ?, lease_owner = NULL,"
                " lease_until = NULL, trace = ?, settled_at = ? WHERE id = ?",
                (state, trace, stamp if dead else None, message_id),
            )

    def expire_leases(self, now: float | None = None) -> int:
        """Return expired leases to ``queued``; returns the number expired."""
        stamp = _clock(now)
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE messages SET state = 'queued', lease_owner = NULL,"
                " lease_until = NULL WHERE state = 'leased' AND lease_until < ?",
                (stamp,),
            )
            return cursor.rowcount

    def settle(self, message_id: str, *, outcome: str = "") -> None:
        """Settle a message after it has been applied."""
        with self._tx() as conn:
            _settle_message(conn, message_id, outcome)

    def counts(self, now: float | None = None) -> dict[str, int]:
        """Queued plus leased messages by destination role."""
        with self._snapshot() as conn:
            return _queue_counts(conn)

    def oldest_age_s(self, now: float | None = None) -> float:
        """Age in seconds of the oldest queued or leased message."""
        stamp = _clock(now)
        with self._snapshot() as conn:
            return _oldest_age(conn, stamp)

    def dead_count(self) -> int:
        with self._snapshot() as conn:
            return _scalar(conn, "SELECT COUNT(*) FROM messages WHERE state = 'dead'")

    def leased_count(self) -> int:
        with self._snapshot() as conn:
            return _scalar(conn, "SELECT COUNT(*) FROM messages WHERE state = 'leased'")

    # -- tickets ----------------------------------------------------------

    def create_ticket(
        self,
        *,
        origin: dict,
        mandate: dict,
        ticket_id: str = "",
        thread_key: str = "",
        stage: str = "open",
    ) -> str:
        if not isinstance(origin, dict) or not isinstance(mandate, dict):
            raise MailboxError("invalid_field", "origin and mandate must be objects")
        if stage not in STAGES:
            raise MailboxError("invalid_field", f"unknown ticket stage {stage!r}")
        ticket_id = ticket_id or _new_id("tkt")
        if not isinstance(ticket_id, str):
            raise MailboxError("invalid_field", "ticket_id must be a string")
        stamp = time.time()
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO tickets (id, origin, mandate, stage, assignments,"
                    " reports, open_asks, decisions, thread_key, created_at,"
                    " updated_at, closed_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        ticket_id,
                        _dumps(origin),
                        _dumps(mandate),
                        stage,
                        _dumps([]),
                        _dumps([]),
                        _dumps([]),
                        _dumps([]),
                        _require_str(thread_key, "thread_key"),
                        stamp,
                        stamp,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise MailboxError(
                "invalid_field", f"ticket already exists: {ticket_id!r}"
            ) from exc
        return ticket_id

    def ticket(self, ticket_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        return None if row is None else _ticket_row(row)

    def tickets(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM tickets ORDER BY created_at, id"
        ).fetchall()
        return [_ticket_row(row) for row in rows]

    def update_ticket(
        self,
        ticket_id: str,
        *,
        stage: str | None = None,
        mandate: dict | None = None,
    ) -> dict:
        updates: dict[str, Any] = {}
        if stage is not None:
            if stage not in STAGES:
                raise MailboxError("invalid_field", f"unknown ticket stage {stage!r}")
            updates["stage"] = stage
        if mandate is not None:
            if not isinstance(mandate, dict):
                raise MailboxError("invalid_field", "mandate must be an object")
            updates["mandate"] = _dumps(mandate)
        with self._tx() as conn:
            _write_ticket(conn, ticket_id, updates)
        row = self.ticket(ticket_id)
        assert row is not None
        return row

    def add_assignment(self, ticket_id: str, assignment: dict) -> int:
        """Append an assignment and return its 1-based attempt number."""
        if not isinstance(assignment, dict):
            raise MailboxError("invalid_field", "assignment must be an object")
        with self._tx() as conn:
            row = _require_ticket(conn, ticket_id)
            entries = _load_list(row["assignments"], "assignments")
            attempt = len(entries) + 1
            entry = dict(assignment)
            entry["attempt"] = attempt
            entries.append(entry)
            conn.execute(
                "UPDATE tickets SET assignments = ?, updated_at = ? WHERE id = ?",
                (_dumps(entries), time.time(), ticket_id),
            )
        return attempt

    def assignments(self, ticket_id: str) -> list[dict]:
        row = self.ticket(ticket_id)
        if row is None:
            raise MailboxError("unknown_ticket", f"no ticket {ticket_id!r}")
        return row["assignments"]

    def set_assignment_state(self, ticket_id: str, agent_id: str, state: str) -> None:
        with self._tx() as conn:
            row = _require_ticket(conn, ticket_id)
            entries = _load_list(row["assignments"], "assignments")
            changed = False
            for entry in entries:
                if isinstance(entry, dict) and entry.get("agent_id") == agent_id:
                    entry["state"] = state
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE tickets SET assignments = ?, updated_at = ? WHERE id = ?",
                    (_dumps(entries), time.time(), ticket_id),
                )

    def add_report(self, ticket_id: str, report: dict) -> None:
        if not isinstance(report, dict):
            raise MailboxError("invalid_field", "report must be an object")
        with self._tx() as conn:
            row = _require_ticket(conn, ticket_id)
            entries = _load_list(row["reports"], "reports")
            entries.append(report)
            conn.execute(
                "UPDATE tickets SET reports = ?, updated_at = ? WHERE id = ?",
                (_dumps(entries), time.time(), ticket_id),
            )

    def reports(self, ticket_id: str) -> list[dict]:
        row = self.ticket(ticket_id)
        if row is None:
            raise MailboxError("unknown_ticket", f"no ticket {ticket_id!r}")
        return row["reports"]

    def open_ask(
        self,
        ticket_id: str,
        ask_id: str,
        *,
        agent: str = "",
        message_id: str = "",
    ) -> None:
        if not isinstance(ask_id, str) or not ask_id:
            raise MailboxError("invalid_field", "ask_id is required")
        with self._tx() as conn:
            row = _require_ticket(conn, ticket_id)
            entries = _load_list(row["open_asks"], "open_asks")
            if any(
                isinstance(entry, dict) and entry.get("id") == ask_id
                for entry in entries
            ):
                return
            entries.append({
                "id": ask_id,
                "agent": agent,
                "message_id": message_id,
                "opened_at": time.time(),
            })
            conn.execute(
                "UPDATE tickets SET open_asks = ?, updated_at = ? WHERE id = ?",
                (_dumps(entries), time.time(), ticket_id),
            )

    def close_ask(self, ticket_id: str, ask_id: str) -> None:
        with self._tx() as conn:
            row = _require_ticket(conn, ticket_id)
            entries = _load_list(row["open_asks"], "open_asks")
            remaining = [
                entry for entry in entries
                if not (isinstance(entry, dict) and entry.get("id") == ask_id)
            ]
            if len(remaining) == len(entries):
                return
            conn.execute(
                "UPDATE tickets SET open_asks = ?, updated_at = ? WHERE id = ?",
                (_dumps(remaining), time.time(), ticket_id),
            )

    def is_ask_open(self, ticket_id: str, ask_id: str) -> bool:
        row = self.ticket(ticket_id)
        if row is None:
            return False
        return any(
            isinstance(entry, dict) and entry.get("id") == ask_id
            for entry in row["open_asks"]
        )

    def add_decision_ref(self, ticket_id: str, decision_id: str) -> None:
        if not isinstance(decision_id, str) or not decision_id:
            raise MailboxError("invalid_field", "decision_id is required")
        with self._tx() as conn:
            row = _require_ticket(conn, ticket_id)
            entries = _load_list(row["decisions"], "decisions")
            if decision_id in entries:
                return
            entries.append(decision_id)
            conn.execute(
                "UPDATE tickets SET decisions = ?, updated_at = ? WHERE id = ?",
                (_dumps(entries), time.time(), ticket_id),
            )

    def ticket_for_agent(self, agent_id: str) -> dict | None:
        """The newest live Ticket that carries ``agent_id`` as an assignment.

        ``done`` and ``stopped`` Tickets are finished cases, so a finished
        worker no longer resolves to them; ``None`` means no live Ticket.
        """
        if not isinstance(agent_id, str) or not agent_id:
            return None
        rows = self._conn.execute(
            "SELECT * FROM tickets WHERE stage NOT IN ('done', 'stopped')"
            " ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        for row in rows:
            ticket = _ticket_row(row)
            if any(
                isinstance(entry, dict) and entry.get("agent_id") == agent_id
                for entry in ticket["assignments"]
            ):
                return ticket
        return None

    def close_ticket(self, ticket_id: str, *, reason: str = "") -> None:
        stamp = time.time()
        with self._tx() as conn:
            _require_ticket(conn, ticket_id)
            conn.execute(
                "UPDATE tickets SET stage = 'done', closed_at = ?, updated_at = ?"
                " WHERE id = ?",
                (stamp, stamp, ticket_id),
            )

    def conclude(
        self,
        ticket_id: str,
        *,
        conversation: str,
        line: str,
        marker_key: str,
        pane_key: str = "",
    ) -> bool:
        """Emit one batch conclusion line and settle the Ticket, once.

        Checks ``marker_key`` and, in the same transaction, inserts the Outbox
        row, sets the marker, settles every queued or leased message of this
        Ticket and closes it. A repeat returns ``False`` and writes nothing.
        """
        if not isinstance(conversation, str) or not conversation:
            raise MailboxError("decision_invalid", "conversation is required")
        if not isinstance(line, str):
            raise MailboxError("decision_invalid", "line must be a string")
        if not isinstance(marker_key, str) or not marker_key:
            raise MailboxError("decision_invalid", "marker_key is required")
        stamp = time.time()
        with self._tx() as conn:
            if conn.execute(
                "SELECT 1 FROM kv WHERE key = ?", (marker_key,)
            ).fetchone() is not None:
                return False
            _require_ticket(conn, ticket_id)
            seq = _scalar(
                conn,
                "SELECT COALESCE(MAX(seq), 0) FROM outbox"
                " WHERE ticket_id = ? AND conversation = ?",
                (ticket_id, conversation),
            ) + 1
            conn.execute(
                "INSERT INTO outbox (id, ticket_id, conversation, seq, payload,"
                " state, attempts, next_at, sent_at, receipt, created_at)"
                " VALUES (?,?,?,?,?,'pending',0,0.0,NULL,'',?)",
                (
                    _new_id("out"),
                    ticket_id,
                    conversation,
                    seq,
                    _dumps(
                        {"line": line, "pane_key": str(pane_key or "")}
                        if pane_key else {"line": line}
                    ),
                    stamp,
                ),
            )
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, '1')"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (marker_key,),
            )
            rows = conn.execute(
                "SELECT id FROM messages WHERE ticket = ?"
                " AND state IN ('queued', 'leased') ORDER BY id",
                (ticket_id,),
            ).fetchall()
            for row in rows:
                _settle_message(conn, row["id"], "concluded")
            conn.execute(
                "UPDATE tickets SET stage = 'done', closed_at = ?,"
                " updated_at = ? WHERE id = ?",
                (stamp, stamp, ticket_id),
            )
        return True

    # -- decisions, effects, outbox ---------------------------------------

    def commit_decision(
        self,
        decision: dict,
        *,
        effects: Iterable[dict] = (),
        settlements: Iterable[str] = (),
        outbox: Iterable[dict] = (),
    ) -> bool:
        """Commit a decision, its effects, settlements and outbox atomically.

        Returns ``False`` (and writes nothing) when the decision id was already
        committed.
        """
        row = _validate_decision(decision)
        effect_rows = [_validate_effect(item) for item in effects]
        settlement_ids = [_validate_settlement(item) for item in settlements]
        outbox_rows = [_validate_outbox(item) for item in outbox]
        stamp = time.time()
        with self._tx() as conn:
            if conn.execute(
                "SELECT 1 FROM decisions WHERE decision_id = ?",
                (row["decision_id"],),
            ).fetchone():
                return False
            conn.execute(
                "INSERT INTO decisions (decision_id, message_id, action, ticket,"
                " reason, payload, author, confidence, state, result, ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["decision_id"],
                    row["message_id"],
                    row["action"],
                    row["ticket"],
                    row["reason"],
                    _dumps(row["payload"]),
                    row["author"],
                    row["confidence"],
                    "committed",
                    "",
                    stamp,
                ),
            )
            for effect in effect_rows:
                conn.execute(
                    "INSERT OR IGNORE INTO effects (id, decision_id, kind, key,"
                    " payload, state, attempts, next_at, last_error, claim_owner,"
                    " claim_until) VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)",
                    (
                        effect["id"] or _new_id("eff"),
                        effect["decision_id"] or row["decision_id"],
                        effect["kind"],
                        effect["key"],
                        _dumps(effect["payload"]),
                        "pending",
                        0,
                        effect["next_at"],
                        "",
                    ),
                )
            for message_id in settlement_ids:
                conn.execute(
                    "UPDATE messages SET state = 'settled', settled_at = ?,"
                    " lease_owner = NULL, lease_until = NULL WHERE id = ?",
                    (stamp, message_id),
                )
            pending: dict[tuple[str, str], int] = {}
            for item in outbox_rows:
                key = (item["ticket_id"], item["conversation"])
                if key not in pending:
                    pending[key] = _scalar(
                        conn,
                        "SELECT COALESCE(MAX(seq), 0) FROM outbox"
                        " WHERE ticket_id = ? AND conversation = ?",
                        key,
                    )
                pending[key] += 1
                conn.execute(
                    "INSERT INTO outbox (id, ticket_id, conversation, seq, payload,"
                    " state, attempts, next_at, sent_at, receipt, created_at)"
                    " VALUES (?,?,?,?,?,'pending',0,0.0,NULL,'',?)",
                    (
                        item["id"] or _new_id("out"),
                        item["ticket_id"],
                        item["conversation"],
                        pending[key],
                        _dumps(item["payload"]),
                        stamp,
                    ),
                )
        return True

    def pending_decisions(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE state = 'committed'"
            " ORDER BY ts, decision_id LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [_decision_row(row) for row in rows]

    def decision(self, decision_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        return None if row is None else _decision_row(row)

    def mark_decision(self, decision_id: str, state: str, *, result: str = "") -> None:
        if not isinstance(state, str) or not state:
            raise MailboxError("decision_invalid", "state is required")
        with self._tx() as conn:
            conn.execute(
                "UPDATE decisions SET state = ?, result = ? WHERE decision_id = ?",
                (state, result or "", decision_id),
            )

    def add_effect(
        self,
        kind: str,
        key: str,
        payload: dict,
        *,
        decision_id: str = "",
        next_at: float = 0.0,
    ) -> str:
        """Insert one effect per ``(kind, key)``; returns the existing id on repeat."""
        if not isinstance(kind, str) or not kind:
            raise MailboxError("decision_invalid", "effect kind is required")
        if not isinstance(key, str) or not key:
            raise MailboxError("decision_invalid", "effect key is required")
        if not isinstance(payload, dict):
            raise MailboxError("decision_invalid", "effect payload must be an object")
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT id FROM effects WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
            if existing is not None:
                return existing["id"]
            effect_id = _new_id("eff")
            conn.execute(
                "INSERT INTO effects (id, decision_id, kind, key, payload, state,"
                " attempts, next_at, last_error, claim_owner, claim_until)"
                " VALUES (?,?,?,?,?,'pending',0,?,'',NULL,NULL)",
                (
                    effect_id,
                    decision_id or "",
                    kind,
                    key,
                    _dumps(payload),
                    float(next_at),
                ),
            )
        return effect_id

    def effect_exists(self, kind: str, key: str) -> bool:
        """Whether an Effect with this ``(kind, key)`` idempotency key exists."""
        row = self._conn.execute(
            "SELECT 1 FROM effects WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
        return row is not None

    def pending_effects(
        self, now: float | None = None, limit: int = 20
    ) -> list[dict]:
        """Due ``pending`` Effects plus ``running`` ones whose claim expired.

        A claim outliving its owner is the daemon-died case: the row is
        returned again so ``claim_effect`` can take it over instead of the
        Effect waiting forever.
        """
        stamp = _clock(now)
        rows = self._conn.execute(
            "SELECT * FROM effects WHERE"
            " (state = 'pending' AND next_at <= ?)"
            " OR (state = 'running' AND claim_until IS NOT NULL"
            " AND claim_until < ?)"
            " ORDER BY next_at, id LIMIT ?",
            (stamp, stamp, int(limit)),
        ).fetchall()
        return [_effect_row(row) for row in rows]

    def claim_effect(
        self,
        effect_id: str,
        owner: str,
        *,
        lease_s: float = 60.0,
        now: float | None = None,
    ) -> bool:
        stamp = _clock(now)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state, claim_until FROM effects WHERE id = ?", (effect_id,)
            ).fetchone()
            if row is None or row["state"] in ("done", "dead"):
                return False
            if row["state"] == "running" and _as_float(row["claim_until"]) > stamp:
                return False
            conn.execute(
                "UPDATE effects SET state = 'running', claim_owner = ?,"
                " claim_until = ?, attempts = attempts + 1 WHERE id = ?",
                (owner, stamp + float(lease_s), effect_id),
            )
        return True

    def finish_effect(
        self,
        effect_id: str,
        *,
        state: str = "done",
        error: str = "",
        next_at: float | None = None,
    ) -> None:
        if state not in EFFECT_STATES:
            raise MailboxError("decision_invalid", f"unknown effect state {state!r}")
        with self._tx() as conn:
            conn.execute(
                "UPDATE effects SET state = ?, last_error = ?, next_at = ?,"
                " claim_owner = NULL, claim_until = NULL WHERE id = ?",
                (
                    state,
                    error or "",
                    0.0 if next_at is None else float(next_at),
                    effect_id,
                ),
            )

    def dead_letters(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM effects WHERE state = 'dead' ORDER BY id"
        ).fetchall()
        return [_effect_row(row) for row in rows]

    def outbox_put(self, *, ticket_id: str, conversation: str, payload: dict) -> str:
        if not isinstance(conversation, str) or not conversation:
            raise MailboxError("decision_invalid", "conversation is required")
        if not isinstance(payload, dict):
            raise MailboxError("decision_invalid", "payload must be an object")
        if not isinstance(ticket_id, str):
            raise MailboxError("decision_invalid", "ticket_id must be a string")
        stamp = time.time()
        with self._tx() as conn:
            seq = _scalar(
                conn,
                "SELECT COALESCE(MAX(seq), 0) FROM outbox"
                " WHERE ticket_id = ? AND conversation = ?",
                (ticket_id, conversation),
            ) + 1
            outbox_id = _new_id("out")
            conn.execute(
                "INSERT INTO outbox (id, ticket_id, conversation, seq, payload,"
                " state, attempts, next_at, sent_at, receipt, created_at)"
                " VALUES (?,?,?,?,?,'pending',0,0.0,NULL,'',?)",
                (outbox_id, ticket_id, conversation, seq, _dumps(payload), stamp),
            )
        return outbox_id

    def outbox_pending(
        self, limit: int = 50, now: float | None = None
    ) -> list[dict]:
        stamp = _clock(now)
        rows = self._conn.execute(
            "SELECT * FROM outbox WHERE state = 'pending' AND next_at <= ?"
            " ORDER BY ticket_id, conversation, seq LIMIT ?",
            (stamp, int(limit)),
        ).fetchall()
        return [_outbox_row(row) for row in rows]

    def outbox_sent(self, outbox_id: str, receipt: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE outbox SET state = 'sent', sent_at = ?, receipt = ?"
                " WHERE id = ?",
                (time.time(), receipt or "sent", outbox_id),
            )

    def outbox_retry(
        self, outbox_id: str, *, next_at: float, error: str = ""
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE outbox SET state = 'pending', next_at = ?,"
                " receipt = 'waiting', attempts = attempts + 1 WHERE id = ?",
                (float(next_at), outbox_id),
            )

    def outbox_dead(self, outbox_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE outbox SET state = 'dead', receipt = 'gone' WHERE id = ?",
                (outbox_id,),
            )

    # -- kv, router, status ------------------------------------------------

    def kv_get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row["value"]

    def kv_set(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def bind_router(self, address: str, token: str) -> None:
        if not isinstance(address, str) or not address:
            raise MailboxError("invalid_field", "router address is required")
        if not isinstance(token, str) or not token:
            raise MailboxError("invalid_field", "router token is required")
        self.kv_set(
            "router:binding",
            _dumps({
                "address": address,
                "token": token,
                "bound_at": time.time(),
            }),
        )

    def router_binding(self) -> dict | None:
        raw = self.kv_get("router:binding")
        if raw is None:
            return None
        binding = _load_dict(raw, "router:binding")
        return binding or None

    def status(self) -> dict:
        """The §18 JSON; ``running``/``pid``/``spool_lag`` are caller-filled."""
        stamp = time.time()
        with self._snapshot() as conn:
            queues = _queue_counts(conn)
            oldest = _oldest_age(conn, stamp)
            leased = _scalar(
                conn, "SELECT COUNT(*) FROM messages WHERE state = 'leased'"
            )
            dead = _scalar(
                conn,
                "SELECT (SELECT COUNT(*) FROM messages WHERE state = 'dead')"
                " + (SELECT COUNT(*) FROM effects WHERE state = 'dead')"
                " + (SELECT COUNT(*) FROM outbox WHERE state = 'dead')",
            )
            applied = _scalar(
                conn,
                "SELECT COUNT(*) FROM decisions"
                " WHERE state = 'applied' AND ts >= ?",
                (stamp - 300.0,),
            )
            row = conn.execute(
                "SELECT value FROM kv WHERE key = 'doorbell:state'"
            ).fetchone()
            doorbell = row["value"] if row is not None and row["value"] else "none"
        return {
            "running": False,
            "pid": 0,
            "schema_version": SCHEMA_VERSION,
            "queues": queues,
            "oldest_age_s": oldest,
            "leased": leased,
            "dead": dead,
            "spool_lag": 0,
            "doorbell": doorbell,
            "decisions_applied_5m": applied,
        }


# -- row decoders ---------------------------------------------------------


def _message_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["body"] = _load_dict(data.get("body"), "body")
    data["from"] = {
        "role": data.get("from_role") or "",
        "id": data.get("from_id") or "",
        "trust": data.get("trust") or "",
    }
    data["to"] = {
        "role": data.get("to_role") or "",
        "id": data.get("to_id") or "",
    }
    data["correlation"] = {
        key: data.get(key) or "" for key in CORRELATION_KEYS
    }
    owner = data.get("lease_owner")
    until = data.get("lease_until")
    data["lease"] = (
        {"owner": owner, "until": until} if owner else None
    )
    data["message_id"] = data["id"]
    return data


def _ticket_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["origin"] = _load_dict(data.get("origin"), "origin")
    data["mandate"] = _load_dict(data.get("mandate"), "mandate")
    for column in ("assignments", "reports", "open_asks", "decisions"):
        data[column] = _load_list(data.get(column), column)
    data["ticket_id"] = data["id"]
    return data


def _decision_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["payload"] = _load_dict(data.get("payload"), "payload")
    return data


def _effect_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["payload"] = _load_dict(data.get("payload"), "payload")
    return data


def _outbox_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["payload"] = _load_dict(data.get("payload"), "payload")
    return data


# -- shared internals -----------------------------------------------------


def _scalar(
    conn: sqlite3.Connection, sql: str, params: tuple | dict = ()
) -> Any:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def _queue_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {role: 0 for role in QUEUE_ROLES}
    rows = conn.execute(
        "SELECT to_role, COUNT(*) AS n FROM messages"
        " WHERE state IN ('queued', 'leased') GROUP BY to_role"
    ).fetchall()
    for row in rows:
        if row["to_role"] in counts:
            counts[row["to_role"]] = int(row["n"])
    return counts


def _oldest_age(conn: sqlite3.Connection, now: float) -> float:
    oldest = _scalar(
        conn,
        "SELECT MIN(created_at) FROM messages"
        " WHERE state IN ('queued', 'leased')",
    )
    if oldest is None:
        return 0.0
    return max(0.0, now - float(oldest))


def _require_ticket(conn: sqlite3.Connection, ticket_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        raise MailboxError("unknown_ticket", f"no ticket {ticket_id!r}")
    return row


def _write_ticket(
    conn: sqlite3.Connection, ticket_id: str, updates: dict[str, Any]
) -> None:
    _require_ticket(conn, ticket_id)
    updates = dict(updates)
    updates["updated_at"] = time.time()
    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE tickets SET {assignments} WHERE id = ?",
        (*updates.values(), ticket_id),
    )


# -- input validation -----------------------------------------------------


def _validate_decision(decision: dict) -> dict:
    if not isinstance(decision, dict):
        raise MailboxError("decision_invalid", "decision must be an object")
    decision_id = decision.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id:
        raise MailboxError("decision_invalid", "decision_id is required")
    action = decision.get("action")
    if action not in ACTIONS:
        raise MailboxError("decision_invalid", f"unknown action {action!r}")
    if "reason" not in decision:
        raise MailboxError("decision_invalid", "reason is required")
    reason = decision.get("reason")
    if not isinstance(reason, str):
        raise MailboxError("decision_invalid", "reason must be a string")
    payload = decision.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise MailboxError("decision_invalid", "payload must be an object")
    author = decision.get("author") or "router"
    if author not in ("router", "human"):
        raise MailboxError("decision_invalid", f"unknown author {author!r}")
    confidence = _as_float(decision.get("confidence"), 0.0)
    message_id = decision.get("message_id")
    ticket = decision.get("ticket")
    if message_id is None:
        message_id = ""
    if ticket is None:
        ticket = ""
    if not isinstance(message_id, str) or not isinstance(ticket, str):
        raise MailboxError(
            "decision_invalid", "message_id and ticket must be strings"
        )
    return {
        "decision_id": decision_id,
        "message_id": message_id,
        "action": action,
        "ticket": ticket,
        "reason": reason,
        "payload": payload,
        "author": author,
        "confidence": confidence,
    }


def _validate_effect(effect: dict) -> dict:
    if not isinstance(effect, dict):
        raise MailboxError("decision_invalid", "effect must be an object")
    kind = effect.get("kind")
    key = effect.get("key")
    if not isinstance(kind, str) or not kind:
        raise MailboxError("decision_invalid", "effect kind is required")
    if not isinstance(key, str) or not key:
        raise MailboxError("decision_invalid", "effect key is required")
    payload = effect.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise MailboxError("decision_invalid", "effect payload must be an object")
    effect_id = effect.get("id")
    decision_id = effect.get("decision_id")
    if effect_id is None:
        effect_id = ""
    if decision_id is None:
        decision_id = ""
    if not isinstance(effect_id, str) or not isinstance(decision_id, str):
        raise MailboxError("decision_invalid", "effect ids must be strings")
    return {
        "id": effect_id,
        "decision_id": decision_id,
        "kind": kind,
        "key": key,
        "payload": payload,
        "next_at": _as_float(effect.get("next_at"), 0.0),
    }


def _validate_settlement(message_id: Any) -> str:
    if not isinstance(message_id, str) or not message_id:
        raise MailboxError("decision_invalid", "settlement id must be a message id")
    return message_id


def _validate_outbox(item: dict) -> dict:
    if not isinstance(item, dict):
        raise MailboxError("decision_invalid", "outbox row must be an object")
    ticket_id = item.get("ticket_id")
    conversation = item.get("conversation")
    payload = item.get("payload")
    outbox_id = item.get("id")
    if ticket_id is None:
        ticket_id = ""
    if payload is None:
        payload = {}
    if outbox_id is None:
        outbox_id = ""
    if not isinstance(ticket_id, str):
        raise MailboxError("decision_invalid", "ticket_id must be a string")
    if not isinstance(conversation, str) or not conversation:
        raise MailboxError("decision_invalid", "conversation is required")
    if not isinstance(payload, dict):
        raise MailboxError("decision_invalid", "outbox payload must be an object")
    if not isinstance(outbox_id, str):
        raise MailboxError("decision_invalid", "outbox id must be a string")
    return {
        "id": outbox_id,
        "ticket_id": ticket_id,
        "conversation": conversation,
        "payload": payload,
    }

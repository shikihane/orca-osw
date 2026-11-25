"""Dispatcher: validate Decisions and apply their Effects.

Pure code. A Decision is validated, checked against the Ticket Mandate, and
committed together with its Effects, message settlements and Outbox rows in
one ``Store.commit_decision`` transaction. Execution happens afterwards in
``run_pending``/``apply_effect``, which claim an Effect, call the matching
``Hooks`` method and record the outcome.

Contract: ``docs/superpowers/plans/2026-11-14-control-plane-interfaces.md``
section 10; behaviour: spec sections 9.5, 10, 12, 13 and 17.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Protocol

from osw import log
from osw.mailbox.errors import MailboxError
from osw.mailbox.store import new_ticket_id
from osw.mandate import (
    HUMAN_SOURCE,
    MandateError,
    answer_allowed,
    check_action,
    check_open_from,
    expires_at_epoch,
    load_mandate,
)
from osw.state import logs_dir

ACTIONS = (
    "accept",
    "followup",
    "answer",
    "stop",
    "retry",
    "reject",
    "escalate",
    "noop",
    "close",
)
ANSWER_TEMPLATE = "# [osw] answer {decision_id}: {text}"
MAX_ANSWER_BYTES = 4096
DECISION_ID_RE = re.compile(r"^dec_[A-Za-z0-9_.:-]{1,120}$")
#  CR/LF and every other control character, in runs, become one space.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
MAX_ATTEMPTS = 8
MAX_BACKOFF_S = 60.0
BACKOFF_BASE_S = 2.0

# Actions keyed on ``ticket`` (spec section 12); ``reject``, ``escalate`` and
# ``noop`` carry no Ticket and are keyed on ``message.id`` instead.
_TICKET_ACTIONS = frozenset(
    {"accept", "followup", "answer", "stop", "retry", "close"}
)

_STOPPED = "stopped"
_TERMINAL_ASSIGNMENT_STATES = frozenset(
    {"done", "closed", "stopped", "failed", "lost", "finished", "exited"}
)
_STR_FIELDS = frozenset(
    {
        "provider",
        "model",
        "thinking",
        "prefix",
        "prompt",
        "text",
        "task",
        "command",
        "summary",
        "note",
        "line",
        "ask",
        "agent",
        "dispatch",
        "conversation",
        "attention",
    }
)


class Hooks(Protocol):
    """The side-effect surface the Dispatcher drives.

    These are the only calls that leave the process; each maps to one closed
    Effect kind. ``launch`` returns ``{"agent_id", "terminal", "dispatch_id"}``;
    ``answer`` receives ``payload["ask"]``, ``payload["text"]`` and the fixed
    ``payload["template"]``.
    """

    async def launch(self, ticket: dict, payload: dict) -> dict:
        ...

    async def followup(self, ticket: dict, payload: dict) -> None:
        ...

    async def answer(self, ticket: dict, payload: dict) -> None:
        ...

    async def stop(self, ticket: dict) -> None:
        ...


def validate_decision(d: dict) -> dict:
    """Return a normalized Decision or raise ``decision_invalid``.

    ``decision_id``, ``action`` and ``reason`` are always required; ``ticket``
    is required only for the actions the spec keys on a Ticket (accept,
    followup, answer, stop, retry, close). ``reject``, ``escalate`` and
    ``noop`` settle the message and are keyed on ``message.id``, so they may
    be ticketless. Known fields are type-checked, unknown top-level keys are
    ignored so a caller can carry its own bookkeeping, and payload fields keep
    their names; the action's required payload fields and the answer
    single-line rule are enforced here.
    """
    if not isinstance(d, dict):
        raise MailboxError("decision_invalid", "decision must be an object")

    decision_id = _required_text(d, "decision_id")
    if not DECISION_ID_RE.match(decision_id):
        raise MailboxError(
            "decision_invalid",
            "decision_id must match ^dec_[A-Za-z0-9_.:-]{1,120}$",
        )
    action = _required_text(d, "action")
    if action not in ACTIONS:
        raise MailboxError("decision_invalid", f"unknown action: {action}")
    ticket = _optional_text(d, "ticket")
    if not ticket and action in _TICKET_ACTIONS:
        raise MailboxError("decision_invalid", f"ticket is required for {action}")
    reason = _required_text(d, "reason")
    message_id = d.get("message_id") or ""
    if not isinstance(message_id, str):
        raise MailboxError("decision_invalid", "message_id must be a string")
    author = d.get("author", "router")
    if author not in ("router", "human"):
        raise MailboxError("decision_invalid", f"unknown author: {author!r}")
    confidence = d.get("confidence", 0.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise MailboxError("decision_invalid", "confidence must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise MailboxError("decision_invalid", "confidence outside 0..1")
    payload = d.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise MailboxError("decision_invalid", "payload must be an object")

    payload = _validate_payload(action, payload)

    return {
        "decision_id": decision_id,
        "message_id": message_id.strip(),
        "action": action,
        "ticket": ticket,
        "reason": " ".join(reason.split()),
        "payload": payload,
        "author": author,
        "confidence": float(confidence),
    }


def plan_effects(decision: dict) -> list[dict]:
    """Map one Decision to its Effect rows (``kind``/``key``/``payload``).

    Keys follow the spec section 12 table: ``ticket`` for accept/stop/close,
    ``ticket + message_id`` for followup, ``ask_id`` for answer,
    ``ticket + attempt`` for retry and ``message_id`` for reject/escalate.
    ``noop`` settles the message and queues no Effect.
    """
    d = validate_decision(decision)
    action = d["action"]
    ticket = d["ticket"]
    mid = d["message_id"]
    decision_id = d["decision_id"]
    fields = dict(d["payload"])
    base: dict[str, Any] = {
        "ticket": ticket,
        "decision_id": decision_id,
        "message_id": mid,
        "action": action,
    }

    if action == "accept":
        return [_effect("launch", ticket, {**base, **fields})]
    if action == "followup":
        return [_effect("launch", f"{ticket}:{mid or decision_id}", {**base, **fields})]
    if action == "answer":
        return [_effect("answer", str(fields.get("ask") or ""), {**base, **fields})]
    if action == "stop":
        return [_effect("stop", ticket, {**base, **fields})]
    if action == "retry":
        attempt = _attempt(fields)
        return [_effect("retry", f"{ticket}:{attempt}", {**base, **fields, "attempt": attempt})]
    if action == "reject":
        return [_effect("notify", mid or decision_id, {**base, **fields})]
    if action == "escalate":
        return [_effect("escalate", mid or decision_id, {**base, **fields})]
    if action == "noop":
        return []
    return [_effect("close_ticket", ticket, {**base, **fields})]


def commit(store, decision: dict, *, message_id: str = "") -> bool:
    """Validate, check the Mandate, and commit Decision + Effects in one go.

    Returns ``False`` for an already-committed ``decision_id`` (idempotent
    repeat) and ``True`` when the rows were written. A refusal raises the typed
    error (``mandate_denied``, ``mandate_expired``, ``close_precondition_failed``,
    ``decision_invalid``, ...) and writes nothing; an ``accept`` that opens a
    Ticket is checked against the base Mandate and ``open_from`` before the row
    exists, so a refusal leaves no orphan Ticket.

    An Effect whose ``(kind, key)`` already exists is skipped by the store, so
    the Decision is recorded afterwards: applied as ``ask_already_answered``
    for a repeated answer, ``failed`` with ``duplicate_effect`` plus one human
    notice otherwise.
    """
    d = validate_decision(decision)
    if message_id:
        d["message_id"] = message_id.strip()
    action = d["action"]
    ticket_id = d["ticket"]
    now = time.time()

    if store.decision(d["decision_id"]) is not None:
        return False

    ticket = store.ticket(ticket_id) if ticket_id else None
    try:
        if ticket is None:
            if action == "accept":
                ticket = _open_ticket(store, d, ticket_id, now)
            elif ticket_id:
                raise MailboxError("unknown_ticket", ticket_id)
            #  A ticketless noop/reject/escalate has no Ticket and no Mandate:
            #  it only settles the message and, for reject/escalate, notifies.

        if ticket is not None:
            mandate = _mandate_of(ticket)
            _check_expiry(mandate, now)
            stage = str(ticket.get("stage") or "open")
            if stage == _STOPPED and action in ("accept", "followup", "retry"):
                raise MailboxError("mandate_denied", f"ticket {ticket_id} is stopped")
            fields = dict(d["payload"])
            answer_text = str(fields.get("text") or "") if action == "answer" else ""
            check_action(
                mandate,
                action,
                now=now,
                answer_text=answer_text,
                close_authority=d["author"],
            )
            _check_providers(mandate, action, fields)
            _check_limits(store, mandate, action, fields, ticket_id)
            _check_answer_limits(mandate, d, answer_text)
            _check_close(store, ticket, action)
    except MailboxError as exc:
        if exc.code in {
            "mandate_denied",
            "mandate_expired",
            "close_precondition_failed",
            "ask_not_open",
        }:
            _emit(
                store,
                exc.code,
                message=exc.message,
                data={
                    "decision_id": d["decision_id"],
                    "action": action,
                    "ticket": ticket_id,
                },
            )
        raise

    #  A ticketless Decision queues no Effect: its only side effect is the
    #  outbox notice (reject/escalate) committed below, and Effects are keyed
    #  on a Ticket that does not exist.
    effects = plan_effects(d) if ticket is not None else []
    #  ``commit_decision`` skips an Effect whose ``(kind, key)`` already exists
    #  and reports only the decision-level repeat, so probe the keys first and
    #  record the skipped ones after the commit (the Decision row must exist
    #  before it can be marked); a second Decision with the same key is never
    #  left to vanish silently.
    duplicates = [
        (effect["kind"], effect["key"])
        for effect in effects
        if store.effect_exists(effect["kind"], effect["key"])
    ]
    outbox = _outbox_rows(store, d)
    settlements = [d["message_id"]] if d["message_id"] else []
    written = bool(
        store.commit_decision(
            d,
            effects=effects,
            settlements=settlements,
            outbox=outbox,
        )
    )
    if written:
        _emit(
            store,
            "router_decision",
            data={
                "decision_id": d["decision_id"],
                "action": action,
                "ticket": ticket_id,
                "effects": [effect["kind"] for effect in effects],
                "outbox": len(outbox),
            },
        )
        if not effects:
            #  A Decision with no Effect (noop, ticketless reject/escalate) is
            #  applied by the commit itself; record that so the ledger and
            #  `decisions_applied_5m` do not keep it "committed" forever.
            _mark(store, d["decision_id"], "applied", "settled")
            _emit(
                store,
                "router_applied",
                data={
                    "decision_id": d["decision_id"],
                    "action": action,
                    "ticket": ticket_id,
                    "result": "settled",
                },
            )
        elif duplicates:
            _record_duplicates(store, d, ticket_id, duplicates)
    return written


# ---------------------------------------------------------------------------
# super fan-out batches


_BATCH_TERMINAL = frozenset({"done", "stopped", "lost"})


def open_batch(
    store,
    *,
    root,
    origin: dict,
    tasks: int,
    thread_key: str = "",
    now: float | None = None,
) -> str:
    """Open one Ticket covering ``tasks`` parallel workers; return its id.

    The fan-out is one Ticket carrying the validated base Mandate. Its only
    Decision is an audit ``accept`` by the human: the caller spawns the
    workers and records one assignment each, so no launch Effect is planned.
    """
    mandate = load_mandate(Path(root))
    if isinstance(tasks, bool) or not isinstance(tasks, int) or tasks < 1:
        raise MailboxError("decision_invalid", "a batch needs at least one task")
    limit = int(mandate["max_workers"])
    if tasks > limit:
        raise MandateError(
            "mandate_denied",
            f"批量派发 {tasks} 个 worker 超过 Mandate 上限 {limit}",
        )
    _check_expiry(mandate, time.time() if now is None else float(now))
    ticket_id = new_ticket_id("batch")
    store.create_ticket(
        origin=dict(origin),
        mandate=mandate,
        ticket_id=ticket_id,
        thread_key=thread_key or ticket_id,
        stage="open",
    )
    decision = {
        "decision_id": new_ticket_id("dec"),
        "action": "accept",
        "author": "human",
        "ticket": ticket_id,
        "reason": f"super fan-out of {tasks} tasks",
        "payload": {"tasks": tasks},
        "confidence": 1.0,
    }
    store.commit_decision(decision)
    store.mark_decision(decision["decision_id"], "applied", result="batch_opened")
    return ticket_id


def seal_batch(store, ticket_id: str) -> None:
    """Mark a batch working and sealed: its reports become one conclusion."""
    store.update_ticket(ticket_id, stage="working")
    store.kv_set(f"batch:sealed:{ticket_id}", "1")


def batch_ready(store, ticket_id: str) -> bool:
    """Whether a sealed batch has at least one assignment and all are terminal.

    Terminal means ``done``, ``stopped`` or ``lost``: a worker the daemon
    declared lost can never report again, so it must not block the batch.
    """
    if not store.kv_get(f"batch:sealed:{ticket_id}"):
        return False
    assignments = store.assignments(ticket_id)
    if not assignments:
        return False
    return all(
        str(entry.get("state") or "") in _BATCH_TERMINAL
        for entry in assignments
        if isinstance(entry, dict)
    )


def conclude_batch(store, ticket_id: str, line: str) -> bool:
    """Emit the one conclusion line of a batch into its origin conversation.

    Returns ``True`` only for the call that wrote it; a repeat is ``False``.
    A Ticket whose origin names no conversation never concludes.
    """
    ticket = store.ticket(ticket_id)
    if ticket is None:
        raise MailboxError("unknown_ticket", ticket_id)
    origin = ticket.get("origin")
    conversation = ""
    pane_key = ""
    if isinstance(origin, dict):
        conversation = str(origin.get("id") or "")
        pane_key = str(origin.get("pane_key") or "")
    if not conversation:
        return False
    return store.conclude(
        ticket_id,
        conversation=conversation,
        line=line,
        marker_key=f"batch:concluded:{ticket_id}",
        pane_key=pane_key,
    )


async def run_pending(
    store,
    hooks,
    *,
    now: float | None = None,
    owner: str = "",
    limit: int = 20,
) -> list[dict]:
    """Claim due Effects, apply each, and return one result row per Effect.

    ``(kind, key)`` makes application idempotent; an Effect left ``running`` by
    a daemon that died is returned by the store once its claim expires and is
    reclaimed here.
    """
    at = time.time() if now is None else now
    claim_owner = owner or "dispatcher"
    results: list[dict] = []
    for effect in store.pending_effects(now=at, limit=limit):
        effect_id = str(effect.get("id") or "")
        if not effect_id:
            continue
        if not store.claim_effect(effect_id, claim_owner, now=at):
            continue
        claimed = dict(effect)
        claimed["attempts"] = int(effect.get("attempts") or 0) + 1
        claimed["_now"] = at
        results.append(await apply_effect(store, claimed, hooks, owner=claim_owner))
    return results


async def apply_effect(store, effect: dict, hooks, *, owner: str = "") -> dict:
    """Execute one claimed Effect and record its durable outcome.

    An Effect whose hook raises goes back to ``pending`` with
    ``min(60, 2**attempts)`` seconds of backoff; at ``MAX_ATTEMPTS`` it becomes
    ``dead`` and a human notice is enqueued.
    """
    effect_id = str(effect.get("id") or "")
    kind = str(effect.get("kind") or "")
    decision_id = str(effect.get("decision_id") or "")
    payload = dict(effect.get("payload") or {})
    attempts = _attempts(effect)
    decision = store.decision(decision_id) if decision_id else None
    ticket_id = str(payload.get("ticket") or (decision or {}).get("ticket") or "")
    ticket = store.ticket(ticket_id) if ticket_id else None

    if ticket is None:
        return _dead(
            store,
            effect,
            ticket_id,
            "unknown_ticket",
            attempts,
            decision,
        )

    try:
        result = await _execute(store, kind, ticket, payload, effect, decision, hooks)
    except Exception as exc:  # noqa: BLE001 - one Effect must never kill the loop
        error = _error_code(exc)
        _emit(
            store,
            "mailbox_delivery_failed",
            message=error,
            data={
                "effect_id": effect_id,
                "kind": kind,
                "ticket": ticket_id,
                "attempts": attempts,
            },
        )
        if attempts >= MAX_ATTEMPTS:
            return _dead(store, effect, ticket_id, error, attempts, decision)
        next_at = _apply_now(effect) + min(MAX_BACKOFF_S, float(2**attempts))
        _finish(store, effect_id, "pending", error=error, next_at=next_at)
        return _result(
            effect,
            ticket_id,
            "pending",
            error=error,
            attempts=attempts,
        )

    state = "done"
    error = str(result.get("result") or "")
    _finish(store, effect_id, state, error=error)
    if decision is not None and decision_id:
        _mark(store, decision_id, "applied", error)
    _emit(
        store,
        "router_applied",
        message=error,
        data={
            "decision_id": decision_id,
            "effect_id": effect_id,
            "kind": kind,
            "ticket": ticket_id,
            "result": error,
            "owner": owner,
        },
    )
    return _result(
        effect,
        ticket_id,
        state,
        result=error,
        attempts=attempts,
    )


# ---------------------------------------------------------------------------
# validation helpers


def _required_text(d: dict, key: str) -> str:
    value = d.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MailboxError("decision_invalid", f"{key} is required")
    return value.strip()


def _optional_text(d: dict, key: str) -> str:
    """A field that may be absent or empty, but must be a string when present."""
    value = d.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MailboxError("decision_invalid", f"{key} must be a string")
    return value.strip()


def _validate_payload(action: str, payload: dict) -> dict:
    """Return a normalized copy of ``payload``; raise on a wrong shape.

    An answer's text is flattened first: CR/LF and other control characters
    become single spaces, so the template stays one line and the length cap is
    measured on what is actually sent.
    """
    fields = dict(payload)
    for key, value in fields.items():
        if key in _STR_FIELDS and value is not None and not isinstance(value, str):
            raise MailboxError("decision_invalid", f"payload.{key} must be a string")
    if "attempt" in fields:
        _attempt(fields)
    if action == "answer":
        ask = fields.get("ask")
        if not isinstance(ask, str) or not ask.strip():
            raise MailboxError("decision_invalid", "answer requires payload.ask")
        text = fields.get("text")
        if not isinstance(text, str):
            raise MailboxError("decision_invalid", "answer requires payload.text")
        text = _single_line(text)
        if not text.strip():
            raise MailboxError("decision_invalid", "answer requires payload.text")
        fields["text"] = text
    if action in ("reject", "escalate"):
        line = fields.get("line")
        if isinstance(line, str) and ("\n" in line or "\r" in line):
            raise MailboxError("decision_invalid", f"{action} line must be single line")
    return fields


def _single_line(text: str) -> str:
    """Replace control characters (CR/LF included) with single spaces."""
    return CONTROL_RE.sub(" ", text)


def _attempt(fields: dict) -> int:
    attempt = fields.get("attempt", 0)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise MailboxError("decision_invalid", "payload.attempt must be a non-negative int")
    return attempt


# ---------------------------------------------------------------------------
# mandate helpers


def _root(store) -> Path:
    root = getattr(store, "root", None)
    return Path(root) if root else Path.cwd()


def _open_ticket(store, decision: dict, ticket_id: str, now: float) -> dict:
    """Create the missing Ticket an ``accept`` opens and return its record.

    The base Mandate and its ``open_from`` source are checked *before* the row
    exists, so a refused accept leaves no orphan Ticket behind; the stored
    Mandate is then re-checked with the same helpers once it does.
    """
    origin = _origin(store, decision)
    mandate = load_mandate(_root(store))
    _check_opening(store, mandate, decision, origin, now)
    store.create_ticket(
        origin=origin,
        mandate=mandate,
        ticket_id=ticket_id,
        thread_key=str(origin.get("thread_key") or ""),
        stage="open",
    )
    ticket = store.ticket(ticket_id)
    if ticket is None:  # pragma: no cover - store contract guarantees it
        raise MailboxError("unknown_ticket", ticket_id)
    _check_open_source(_mandate_of(ticket), origin)
    return ticket


def _check_opening(
    store, mandate: dict, decision: dict, origin: dict, now: float
) -> None:
    """The base-Mandate checks an ``accept`` must pass before it opens a Ticket."""
    action = decision["action"]
    fields = dict(decision["payload"])
    _check_expiry(mandate, now)
    check_action(mandate, action, now=now)
    _check_providers(mandate, action, fields)
    _check_limits(store, mandate, action, fields, decision["ticket"])
    _check_open_source(mandate, origin)


def _check_open_source(mandate: dict, origin: dict) -> None:
    source = _open_source(origin)
    if not check_open_from(mandate, source):
        raise MailboxError(
            "mandate_denied",
            f"source {source or '<human>'!r} may not open a Ticket",
        )


def _open_source(origin: dict) -> str:
    """The ``open_from`` source a Ticket origin represents.

    A human sender is the empty human source and is always allowed. A Decision
    that carries no message sender (a direct Router call) also stays on the
    human source: no foreign system triggered it. Anything else must be listed
    in the Mandate's ``open_from``.
    """
    role = str(origin.get("role") or "")
    if role in ("", "human"):
        return HUMAN_SOURCE
    return role


def _record_duplicates(
    store, decision: dict, ticket_id: str, duplicates: list[tuple[str, str]]
) -> None:
    """Record a planned Effect that ``(kind, key)`` already had.

    Two distinct Decisions must never disappear silently. A repeated answer is
    the benign case (the ask was answered once already), so it is applied with
    ``ask_already_answered`` and no retry; any other skipped Effect marks the
    Decision ``failed`` with ``duplicate_effect`` and queues one human notice.
    """
    decision_id = decision["decision_id"]
    action = decision["action"]
    hard = [(kind, key) for kind, key in duplicates if kind != "answer"]
    if not hard:
        _mark(store, decision_id, "applied", "ask_already_answered")
        _emit(
            store,
            "router_applied",
            data={
                "decision_id": decision_id,
                "action": action,
                "ticket": ticket_id,
                "result": "ask_already_answered",
                "duplicate_effect": f"answer:{duplicates[0][1]}",
            },
        )
        return

    _mark(store, decision_id, "failed", "duplicate_effect")
    kind, key = hard[0]
    line = _notice_line(
        "duplicate_effect",
        f"decision={decision_id} effect={kind}:{key}"
        f" ticket={ticket_id or '-'}",
    )
    try:
        store.outbox_put(
            ticket_id=ticket_id,
            conversation="human",
            payload={
                "line": line,
                "kind": "notice",
                "attention": "human",
                "decision_id": decision_id,
                "effect": f"{kind}:{key}",
            },
        )
    except Exception:  # pragma: no cover - notice is best effort
        pass
    _emit(
        store,
        "duplicate_effect",
        message=line,
        data={
            "decision_id": decision_id,
            "action": action,
            "ticket": ticket_id,
            "effect": f"{kind}:{key}",
        },
    )


def _mandate_of(ticket: dict) -> dict:
    mandate = ticket.get("mandate")
    if not isinstance(mandate, dict):
        raise MailboxError("mandate_invalid", "ticket has no mandate")
    return mandate


def _check_expiry(mandate: dict, now: float) -> None:
    expires = expires_at_epoch(mandate)
    if expires is not None and expires <= now:
        raise MailboxError("mandate_expired", "ticket mandate expired")


def _check_providers(mandate: dict, action: str, fields: dict) -> None:
    if action not in ("accept", "retry"):
        return
    provider = str(fields.get("provider") or "")
    if not provider:
        return
    allowed = mandate.get("providers") or []
    if provider not in allowed:
        raise MailboxError("mandate_denied", f"provider {provider} not in mandate")


def _check_limits(
    store,
    mandate: dict,
    action: str,
    fields: dict,
    ticket_id: str,
) -> None:
    if action not in ("accept", "retry"):
        return
    max_workers = mandate.get("max_workers")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 0:
        raise MailboxError("mandate_invalid", "max_workers must be a non-negative int")
    active = _active_assignments(store, ticket_id)
    if action == "retry":
        attempt = _attempt(fields)
        budget = mandate.get("max_retries")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise MailboxError("mandate_invalid", "max_retries must be a non-negative int")
        if attempt >= budget:
            raise MailboxError(
                "mandate_denied",
                f"retry attempt {attempt} exceeds budget {budget}",
            )
        if len(active) >= max_workers:
            raise MailboxError(
                "mandate_denied",
                f"max_workers {max_workers} reached",
            )
    elif not active and max_workers <= 0:
        raise MailboxError("mandate_denied", "max_workers is zero")


def _check_answer_limits(mandate: dict, decision: dict, text: str) -> None:
    if decision["action"] != "answer":
        return
    if not answer_allowed(mandate, text):
        raise MailboxError("mandate_denied", "answer mode does not allow this text")
    template = ANSWER_TEMPLATE.format(decision_id=decision["decision_id"], text=text)
    if len(template.encode("utf-8")) > MAX_ANSWER_BYTES:
        raise MailboxError(
            "mandate_denied",
            f"answer template exceeds {MAX_ANSWER_BYTES} bytes",
        )


def _check_close(store, ticket: dict, action: str) -> None:
    if action != "close":
        return
    if _has_open_ask(ticket):
        raise MailboxError("close_precondition_failed", "ticket has an open ask")
    if _has_running(store, str(ticket.get("id") or "")):
        raise MailboxError("close_precondition_failed", "ticket has a running assignment")


# ---------------------------------------------------------------------------
# outbox helpers


def _outbox_rows(store, decision: dict) -> list[dict]:
    action = decision["action"]
    fields = decision["payload"]
    ticket_id = decision["ticket"]
    decision_id = decision["decision_id"]
    if action == "escalate":
        line = _notice_line(
            "escalate",
            fields.get("line") or _notice_body(decision),
        )
        conversation = str(fields.get("conversation") or "human")
        return [
            {
                "ticket_id": ticket_id,
                "conversation": conversation,
                "payload": {
                    "line": line,
                    "kind": "notice",
                    "attention": "human",
                    "decision_id": decision_id,
                },
            }
        ]
    if action == "reject":
        line = _notice_line(
            "reject",
            fields.get("line") or _notice_body(decision),
        )
        conversation = str(
            fields.get("conversation")
            or _sender_conversation(store, decision["message_id"])
            or "human"
        )
        return [
            {
                "ticket_id": ticket_id,
                "conversation": conversation,
                "payload": {
                    "line": line,
                    "kind": "notice",
                    "attention": "sender",
                    "decision_id": decision_id,
                },
            }
        ]
    return []


def _notice_body(decision: dict) -> str:
    """The default notice body; a ticketless notice names its message instead."""
    if decision["ticket"]:
        scope = f"ticket={decision['ticket']}"
    else:
        scope = f"message={decision['message_id'] or decision['decision_id']}"
    return f"{scope} decision={decision['decision_id']} reason={decision['reason']}"


def _notice_line(kind: str, text: str) -> str:
    body = " ".join(str(text).split())
    line = f"# [osw] attention {kind} {body}".strip()
    return line


def _sender_conversation(store, message_id: str) -> str:
    if not message_id:
        return "human"
    try:
        message = store.get_message(message_id) or {}
    except Exception:  # pragma: no cover - store read must never break commit
        return "human"
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    ident = str(sender.get("id") or message.get("from_id") or "")
    if ident.startswith(("terminal:", "session:")):
        return ident
    return "human"


def _origin(store, decision: dict) -> dict:
    message_id = decision["message_id"]
    origin: dict[str, Any] = {
        "message_id": message_id,
        "decision_id": decision["decision_id"],
        "source": decision["author"],
        "thread_key": "",
    }
    if not message_id:
        origin["thread_key"] = decision["ticket"]
        return origin
    try:
        message = store.get_message(message_id) or {}
    except Exception:  # pragma: no cover - best effort
        message = {}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    origin["role"] = str(sender.get("role") or message.get("from_role") or "")
    origin["id"] = str(sender.get("id") or message.get("from_id") or "")
    origin["text"] = str(body.get("text") or "")
    origin["thread_key"] = str(message.get("thread_key") or decision["ticket"])
    return origin


# ---------------------------------------------------------------------------
# effect execution


async def _execute(
    store,
    kind: str,
    ticket: dict,
    payload: dict,
    effect: dict,
    decision: dict | None,
    hooks,
) -> dict:
    ticket_id = str(ticket.get("id") or payload.get("ticket") or "")
    decision_id = str(effect.get("decision_id") or "")
    action = str(payload.get("action") or "")

    if kind == "launch":
        if action == "followup":
            await hooks.followup(ticket, payload)
            return {"result": "sent"}
        active = _active_assignments(store, ticket_id)
        if active:
            reuse = active[-1]
            forwarded = {
                **payload,
                "agent_id": str(reuse.get("agent_id") or ""),
                "terminal": str(reuse.get("terminal") or ""),
                "reused": True,
            }
            await hooks.followup(ticket, forwarded)
            return {"result": "reused", "agent_id": forwarded["agent_id"]}
        result = await hooks.launch(ticket, payload)
        _record_assignment(store, ticket_id, result)
        return {"result": "launched", "agent_id": str((result or {}).get("agent_id") or "")}

    if kind == "retry":
        result = await hooks.launch(ticket, payload)
        _record_assignment(store, ticket_id, result)
        return {"result": "retried", "agent_id": str((result or {}).get("agent_id") or "")}

    if kind == "answer":
        ask_id = str(payload.get("ask") or effect.get("key") or "")
        if not ask_id or not store.is_ask_open(ticket_id, ask_id):
            return {"result": "ask_not_open"}
        text = str(payload.get("text") or "")
        forwarded = {
            **payload,
            "ask": ask_id,
            "text": text,
            "template": ANSWER_TEMPLATE.format(decision_id=decision_id, text=text),
        }
        await hooks.answer(ticket, forwarded)
        #  One answer closes the ask: a later answer for the same id is
        #  recorded as ``ask_already_answered`` instead of being sent twice.
        store.close_ask(ticket_id, ask_id)
        return {"result": "answered"}

    if kind == "stop":
        if str(ticket.get("stage") or "") == _STOPPED:
            return {"result": "already_stopped"}
        await hooks.stop(ticket)
        for assignment in store.assignments(ticket_id):
            if str(assignment.get("state") or "") == "running":
                store.set_assignment_state(
                    ticket_id,
                    str(assignment.get("agent_id") or ""),
                    "stopped",
                )
        store.update_ticket(ticket_id, stage=_STOPPED)
        return {"result": "stopped"}

    if kind == "close_ticket":
        if _has_open_ask(ticket) or _has_running(store, ticket_id):
            raise MailboxError("close_precondition_failed", ticket_id)
        reason = str(payload.get("reason") or (decision or {}).get("reason") or "")
        store.close_ticket(ticket_id, reason=reason)
        return {"result": "closed"}

    if kind in ("escalate", "notify"):
        return {"result": "queued"}

    raise MailboxError("decision_invalid", f"unknown effect kind: {kind}")


def _record_assignment(store, ticket_id: str, result: dict | None) -> None:
    result = result if isinstance(result, dict) else {}
    agent_id = str(result.get("agent_id") or "")
    if not agent_id:
        raise MailboxError("decision_invalid", "launch returned no agent_id")
    known = {
        str(assignment.get("agent_id") or "")
        for assignment in store.assignments(ticket_id)
    }
    if agent_id not in known:
        store.add_assignment(
            ticket_id,
            {
                "agent_id": agent_id,
                "terminal": str(result.get("terminal") or ""),
                "dispatch_id": str(result.get("dispatch_id") or ""),
                "state": "running",
            },
        )
    store.set_assignment_state(ticket_id, agent_id, "running")
    store.update_ticket(ticket_id, stage="working")


# ---------------------------------------------------------------------------
# small store helpers


def _active_assignments(store, ticket_id: str) -> list[dict]:
    try:
        assignments = store.assignments(ticket_id)
    except Exception:  # pragma: no cover - store contract
        return []
    return [
        assignment
        for assignment in assignments
        if str(assignment.get("state") or "running") not in _TERMINAL_ASSIGNMENT_STATES
    ]


def _has_running(store, ticket_id: str) -> bool:
    try:
        assignments = store.assignments(ticket_id)
    except Exception:  # pragma: no cover - store contract
        return False
    return any(str(a.get("state") or "") == "running" for a in assignments)


def _has_open_ask(ticket: dict) -> bool:
    asks = ticket.get("open_asks")
    if not asks:
        return False
    if isinstance(asks, dict):
        for value in asks.values():
            if isinstance(value, dict):
                if str(value.get("state") or "open") in ("closed", "answered", "done"):
                    continue
            elif value is False or value is None:
                continue
            return True
        return False
    if isinstance(asks, list):
        for value in asks:
            if isinstance(value, dict):
                if str(value.get("state") or "open") in ("closed", "answered", "done"):
                    continue
            return True
        return False
    return True


def _attempts(effect: dict) -> int:
    try:
        attempts = int(effect.get("attempts") or 0)
    except (TypeError, ValueError):  # pragma: no cover - store contract
        attempts = 0
    return max(1, attempts)


def _apply_now(effect: dict) -> float:
    """Backoff base: ``run_pending``'s logical clock when it supplied one."""
    try:
        return float(effect.get("_now"))
    except (TypeError, ValueError):
        return time.time()


def _effect(kind: str, key: str, payload: dict) -> dict:
    return {"kind": kind, "key": key, "payload": payload}


def _result(
    effect: dict,
    ticket_id: str,
    state: str,
    *,
    result: str = "",
    error: str = "",
    attempts: int = 0,
) -> dict:
    return {
        "id": str(effect.get("id") or ""),
        "decision_id": str(effect.get("decision_id") or ""),
        "kind": str(effect.get("kind") or ""),
        "key": str(effect.get("key") or ""),
        "ticket": ticket_id,
        "state": state,
        "result": result,
        "error": error,
        "attempts": attempts,
    }


def _finish(
    store,
    effect_id: str,
    state: str,
    *,
    error: str = "",
    next_at: float | None = None,
) -> None:
    if not effect_id:
        return
    store.finish_effect(effect_id, state=state, error=error, next_at=next_at)


def _mark(store, decision_id: str, state: str, result: str = "") -> None:
    try:
        store.mark_decision(decision_id, state, result=result)
    except Exception:  # pragma: no cover - bookkeeping must not break apply
        return


def _dead(store, effect: dict, ticket_id: str, error: str, attempts: int, decision) -> dict:
    effect_id = str(effect.get("id") or "")
    kind = str(effect.get("kind") or "")
    decision_id = str(effect.get("decision_id") or "")
    _finish(store, effect_id, "dead", error=error)
    if decision_id and decision is not None:
        _mark(store, decision_id, "failed", error)
    line = _notice_line(
        "dead_letter",
        f"effect={effect_id} kind={kind} ticket={ticket_id} error={error}",
    )
    try:
        store.outbox_put(
            ticket_id=ticket_id,
            conversation="human",
            payload={
                "line": line,
                "kind": "notice",
                "attention": "human",
                "effect_id": effect_id,
                "decision_id": decision_id,
            },
        )
    except Exception:  # pragma: no cover - notice is best effort
        pass
    _emit(
        store,
        "mailbox_dead_letter",
        message=error,
        data={"effect_id": effect_id, "kind": kind, "ticket": ticket_id},
    )
    return _result(
        effect,
        ticket_id,
        "dead",
        error=error,
        attempts=attempts,
    )


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", "")
    if isinstance(code, str) and code:
        return code
    return " ".join(str(exc).split()) or exc.__class__.__name__


def _emit(store, event: str, *, message: str = "", data: dict | None = None) -> None:
    root = getattr(store, "root", None)
    if not root:
        return
    try:
        log.emit_event(
            logs_dir(Path(root)),
            component="dispatcher",
            event=event,
            message=message,
            data=data or {},
        )
    except Exception:  # pragma: no cover - events never affect behavior
        return

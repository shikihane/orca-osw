"""Delivery: the ordered Outbox (spec §13).

Messages to one ``ticket_id + conversation`` leave in ``seq`` order. A failed
send is retried with 2 s doubling to 60 s and becomes a dead letter after
:data:`MAX_ATTEMPTS`. Every send ends in one receipt: ``sent`` (delivered),
``waiting`` (scheduled for another attempt) or ``gone`` (dead letter).

The conversation address is transport, not routing: ``terminal:<handle>`` or
``session:<id>``. The caller supplies ``notify(conversation, line)``; it may be
a plain function or a coroutine function.

A failed send to a ``terminal:`` address gets one extra chance: when the
hooks object offers ``rebind_conversation(conversation, payload)`` and answers
with a different, non-empty address, the same line is sent there once. The
hook is optional and its failures are ignored, so a mailbox without it keeps
the plain retry/back-off/dead-letter behaviour.

A process-local address (the headless ``local:<pid>`` form) names a pid, not a
conversation: :func:`is_deliverable` refuses it, so a producer that would queue
a notice there can see it is not a target before it writes an Outbox row that
could only dead-letter.
"""

from __future__ import annotations

import inspect
import time
from typing import Any, Callable

RETRY_BASE_S = 2.0
RETRY_MAX_S = 60.0
MAX_ATTEMPTS = 8

SENT = "sent"
WAITING = "waiting"
GONE = "gone"

Notify = Callable[[str, str], Any]

#: Address prefixes that name a process rather than a conversation. Nothing can
#: be delivered there: an Outbox row would retry and then dead-letter.
UNDELIVERABLE_PREFIXES = ("local:",)

#: Scan every pending row, not only the due ones: a conversation's earlier
#: row may be in backoff and must still hold its later rows back.
_ALL_PENDING = 1e18


def retry_delay(attempts: int) -> float:
    """Seconds to wait after the given 1-based failed attempt count."""
    attempts = max(1, int(attempts))
    return min(RETRY_MAX_S, RETRY_BASE_S * (2 ** (attempts - 1)))


def is_deliverable(address: str) -> bool:
    """Whether a notification may be sent to *address* at all.

    A ``local:<pid>`` address is a headless sender's process, not a
    conversation a ``notify`` can reach, so it is never a delivery target.
    """
    address = str(address or "").strip()
    if not address:
        return False
    return not address.startswith(UNDELIVERABLE_PREFIXES)


def conversation_address(target: dict) -> str:
    """Transport address of a conversation target.

    ``{"terminal": "term_x"}`` -> ``"terminal:term_x"``;
    ``{"session": "<id>"}`` -> ``"session:<id>"``. An already-prefixed
    ``address`` is returned unchanged, so a stored binding needs no
    conversion.
    """
    if isinstance(target, str):
        return _normalize(str(target))
    if not isinstance(target, dict):
        return ""
    terminal = str(target.get("terminal") or "")
    if terminal:
        return _normalize(terminal, "terminal")
    session = str(target.get("session") or "")
    if session:
        return _normalize(session, "session")
    address = str(target.get("address") or target.get("id") or "")
    return _normalize(address)


async def deliver_outbox(
    store: Any,
    notify: Notify,
    *,
    hooks: Any = None,
    limit: int = 50,
    now: float | None = None,
) -> list[dict]:
    """Send due outbox rows, in order per ``(ticket_id, conversation)``.

    A failure retries that row (backoff) or dead-letters it, and stops that
    conversation for this pass: a later ``seq`` never overtakes an earlier
    one. Other conversations are independent and keep flowing.
    """
    now = time.time() if now is None else now
    limit = max(1, int(limit))
    notify, hooks = _notify_owner(notify, hooks)
    scan_limit = max(limit * 4, 200)
    rows = store.outbox_pending(limit=scan_limit, now=_ALL_PENDING) or []

    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (
            str(row.get("ticket_id") or ""),
            str(row.get("conversation") or ""),
        )
        grouped.setdefault(key, []).append(row)

    outcomes: list[dict] = []
    for key in sorted(grouped):
        for row in sorted(grouped[key], key=_seq):
            if _next_at(row) > now:
                # Not due: this conversation waits, later seq included.
                break
            outcome = await _deliver_one(store, notify, row, now, hooks)
            outcomes.append(outcome)
            if outcome["state"] != SENT:
                break
            if len(outcomes) >= limit:
                return outcomes
    return outcomes


async def _deliver_one(
    store: Any, notify: Notify, row: dict, now: float, hooks: Any = None
) -> dict:
    row_id = str(row.get("id") or "")
    conversation = str(row.get("conversation") or "")
    payload = row.get("payload")
    line = _line(payload)
    outcome = {
        "id": row_id,
        "ticket_id": str(row.get("ticket_id") or ""),
        "conversation": conversation,
        "seq": _seq(row),
        "attempts": int(row.get("attempts") or 0),
        "line": line,
    }
    try:
        await _send(notify, hooks, conversation, line, payload)
    except Exception as exc:  # one bad send never kills the loop
        attempts = int(row.get("attempts") or 0) + 1
        error = str(exc) or exc.__class__.__name__
        if attempts >= MAX_ATTEMPTS:
            store.outbox_dead(row_id)
            return {
                **outcome,
                "state": "dead",
                "receipt": GONE,
                "attempts": attempts,
                "next_at": 0.0,
                "error": error,
            }
        next_at = now + retry_delay(attempts)
        store.outbox_retry(row_id, next_at=next_at, error=error)
        return {
            **outcome,
            "state": "waiting",
            "receipt": WAITING,
            "attempts": attempts,
            "next_at": next_at,
            "error": error,
        }
    store.outbox_sent(row_id, SENT)
    return {
        **outcome,
        "state": SENT,
        "receipt": SENT,
        "sent_at": now,
        "next_at": 0.0,
        "error": "",
    }


def _notify_owner(notify: Any, hooks: Any) -> tuple[Notify, Any]:
    """Split the send callable from the object that may own a rebind hook.

    A hooks object with a ``notify`` method is accepted directly; a bound
    ``notify`` method already names its owner, so the usual daemon call needs
    no extra argument.
    """
    if not callable(notify):
        inner = getattr(notify, "notify", None)
        if not callable(inner):
            raise TypeError("notify must be callable")
        if hooks is None:
            hooks = notify
        notify = inner
    if hooks is None:
        hooks = getattr(notify, "__self__", None)
    return notify, hooks


async def _send(
    notify: Notify, hooks: Any, conversation: str, line: str, payload: Any
) -> None:
    """Send one line, rebinding a dead ``terminal:`` address once."""
    try:
        await _call(notify, conversation, line)
    except Exception:
        if not conversation.startswith("terminal:"):
            raise
        rebound = await _rebind(hooks, conversation, payload)
        if not rebound:
            raise
        await _call(notify, rebound, line)


async def _call(notify: Notify, conversation: str, line: str) -> None:
    result = notify(conversation, line)
    if inspect.isawaitable(result):
        await result


async def _rebind(hooks: Any, conversation: str, payload: Any) -> str:
    """Fresh address from the hooks, or ``""`` when there is no rebind."""
    hook = None
    if hooks is not None:
        hook = getattr(hooks, "rebind_conversation", None)
    if not callable(hook):
        return ""
    try:
        result = hook(conversation, payload if isinstance(payload, dict) else {})
        if inspect.isawaitable(result):
            result = await result
    except Exception:  # an optional hook never fails a delivery
        return ""
    address = _normalize(str(result or "").strip())
    return "" if address == conversation else address


def _line(payload: Any) -> str:
    """The single line one outbox row carries to its conversation."""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    for key in ("line", "text", "message", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _seq(row: dict) -> int:
    try:
        return int(row.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def _next_at(row: dict) -> float:
    try:
        return float(row.get("next_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _normalize(address: str, prefix: str = "") -> str:
    address = address.strip()
    if not address:
        return ""
    if address.startswith("terminal:") or address.startswith("session:"):
        return address
    if address.startswith("orca_session_id:"):
        return "session:" + address.split(":", 1)[1]
    if ":" in address:  # an opaque address (e.g. a headless fallback)
        return address
    if prefix:
        return f"{prefix}:{address}"
    return f"terminal:{address}"

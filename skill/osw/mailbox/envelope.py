"""The envelope: the only shape producers hand the Mailbox.

Everything a message says travels in one canonical dict (design §7). This
module owns the shape, its validation, the trust attached to an author's
text, the priority floor, the 1 KiB decision card (design §9.3) and the
exact-command grammar the fast path uses to recognise a human command.

The Mailbox treats every body as data; ``from.trust`` is derived from the
author role, never granted by the caller.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from .errors import MailboxError

VERSION = 1

KINDS = ("request", "followup", "answer", "ask", "report", "blocked", "status",
         "stop", "ack", "notice", "timer", "system")
FROM_ROLES = ("human", "worker", "watcher", "system", "router", "timer")
TO_ROLES = ("router", "worker", "human", "broadcast", "dead")
REPORT_OUTCOMES = ("progress", "waiting", "ready", "blocked", "failed",
                   "dropped")

TRUSTS = ("trusted", "untrusted")
ATTENTIONS = ("fyi", "must_answer", "human")

MAX_BODY_BYTES = 16 * 1024
MAX_CARD_BYTES = 1024
MAX_SUMMARY_CHARS = 200

CORRELATION_KEYS = ("ticket", "agent", "dispatch", "ask", "decision")
CANONICAL_KEYS = ("v", "id", "ts", "from", "to", "kind", "correlation",
                  "thread_key", "reply_to", "priority", "ttl_s", "dedupe_key",
                  "body", "state", "attempts", "lease", "trace")
CARD_KEYS = ("message_id", "kind", "from", "trust", "intent", "attention",
             "priority", "summary", "tickets", "detail_hint")

# Roles whose authored text is trusted. A worker's text is data.
TRUSTED_ROLES = frozenset(FROM_ROLES) - {"worker"}

# Report outcomes a human must look at; progress is information only. The
# fast path owns the real classification; this is only the card's floor.
_TERMINAL_OUTCOMES = ("ready", "failed", "dropped", "blocked")

_ID_HEX_BYTES = 3
_ID_STAMP = "%Y%m%dT%H%M%S"

EXACT_COMMAND_RE = re.compile(r"^osw\s+(new|use|stop|status|list|logs)\b")
EXACT_COMMAND_FORBIDDEN = frozenset(";&|><`$\n\r")


# ---------------------------------------------------------------------------
# Identity of a message
# ---------------------------------------------------------------------------

def new_id(prefix: str = "msg", *, now: datetime | None = None) -> str:
    """A fresh message id: ``<prefix>_YYYYMMDDTHHMMSSfffZ_<6 hex>`` (UTC)."""
    moment = _utc(now)
    stamp = moment.strftime(_ID_STAMP) + f"{moment.microsecond // 1000:03d}Z"
    return f"{prefix}_{stamp}_{secrets.token_hex(_ID_HEX_BYTES)}"


def _utc(now: datetime | None = None) -> datetime:
    """``now`` in UTC; a naive value is read as UTC."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso_ts(moment: datetime) -> str:
    """``2026-11-14T13:15:00.123Z``, the envelope's ``ts`` form."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + (
        f".{moment.microsecond // 1000:03d}Z")


# ---------------------------------------------------------------------------
# Trust and priority
# ---------------------------------------------------------------------------

def trust_for(role: str) -> str:
    """The trust a role's authored text carries; workers are untrusted."""
    if not isinstance(role, str) or role not in FROM_ROLES:
        raise MailboxError(
            "unknown_role",
            f"role {role!r} is not one of {list(FROM_ROLES)}",
        )
    return "trusted" if role in TRUSTED_ROLES else "untrusted"


def default_priority(env: dict) -> int:
    """The priority floor: human 0, ask/blocked 1, report 2, rest 3."""
    source = env.get("from") if isinstance(env, dict) else None
    role = source.get("role") if isinstance(source, dict) else ""
    kind = env.get("kind") if isinstance(env, dict) else ""
    if role == "human":
        return 0
    if kind in ("ask", "blocked"):
        return 1
    if kind == "report":
        return 2
    return 3


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_envelope(raw: dict, *, now: datetime | None = None) -> dict:
    """Return the canonical envelope for ``raw``, filling mailbox-owned fields.

    Raises :class:`MailboxError` with ``invalid_envelope``, ``unknown_kind``,
    ``unknown_role``, ``invalid_priority``, ``invalid_field`` or
    ``body_too_large``. Validation is idempotent: validating a validated
    envelope changes nothing.
    """
    if not isinstance(raw, dict):
        raise MailboxError("invalid_envelope", "an envelope must be an object")

    version = raw.get("v", VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or \
            version != VERSION:
        raise MailboxError(
            "invalid_envelope",
            f"unsupported envelope version {version!r}; this build writes {VERSION}",
        )

    env = dict(raw)
    env["v"] = VERSION
    env["id"] = _id(raw.get("id"), now=now)
    env["ts"] = _ts(raw.get("ts"), now=now)
    env["from"] = _from(raw.get("from"))
    env["to"] = _to(raw.get("to"))
    env["kind"] = _kind(raw.get("kind"))
    env["correlation"] = _correlation(raw.get("correlation"))
    env["thread_key"] = _optional(raw.get("thread_key"), "thread_key")
    env["reply_to"] = _optional(raw.get("reply_to"), "reply_to")
    env["dedupe_key"] = _optional(raw.get("dedupe_key"), "dedupe_key")
    env["body"] = _body(raw.get("body"), kind=env["kind"])
    if raw.get("priority") is None:
        env["priority"] = default_priority(env)
    else:
        env["priority"] = _priority(raw["priority"])
    env["ttl_s"] = _ttl(raw.get("ttl_s"))
    env["state"] = _state(raw.get("state"))
    env["attempts"] = _attempts(raw.get("attempts"))
    env["lease"] = _lease(raw.get("lease"))
    env["trace"] = _optional(raw.get("trace"), "trace")
    return _canonical(env)


def _canonical(env: dict) -> dict:
    """The canonical key order, then any extra keys the caller carried."""
    ordered = {key: env[key] for key in CANONICAL_KEYS}
    ordered.update({k: v for k, v in env.items() if k not in ordered})
    return ordered


def _id(value: Any, *, now: datetime | None = None) -> str:
    if value is None:
        return new_id(now=now)
    return _required(value, "id")


def _ts(value: Any, *, now: datetime | None = None) -> str:
    if value is None:
        return _iso_ts(_utc(now))
    if not isinstance(value, str) or not value.strip():
        raise MailboxError("invalid_envelope", "ts must be an ISO-8601 string")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MailboxError(
            "invalid_envelope", f"ts is not ISO-8601: {value!r}") from exc
    return _iso_ts(_utc(parsed))


def _endpoint(value: Any, roles: tuple[str, ...], name: str) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise MailboxError("invalid_envelope", f"{name} must be an object")
    role = value.get("role")
    if not isinstance(role, str) or not role.strip():
        raise MailboxError("invalid_envelope", f"{name}.role is required")
    role = role.strip().lower()
    if role not in roles:
        raise MailboxError(
            "unknown_role",
            f"{name}.role {role!r} is not one of {list(roles)}",
        )
    return role, _optional(value.get("id"), f"{name}.id")


def _from(value: Any) -> dict:
    role, ident = _endpoint(value, FROM_ROLES, "from")
    return {"role": role, "id": ident, "trust": _trust(role, value.get("trust"))}


def _to(value: Any) -> dict:
    role, ident = _endpoint(value, TO_ROLES, "to")
    return {"role": role, "id": ident}


def _trust(role: str, requested: Any) -> str:
    """The role's floor; a caller may ask for stricter, never looser."""
    floor = trust_for(role)
    if requested is None:
        return floor
    if not isinstance(requested, str):
        raise MailboxError("invalid_envelope", "from.trust must be a string")
    value = requested.strip().lower()
    if value == "untrusted":
        return "untrusted"
    # "trusted" (or any unknown word) is granted by the role, not the caller.
    return floor


def _kind(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MailboxError("invalid_envelope", "kind is required")
    kind = value.strip().lower()
    if kind not in KINDS:
        raise MailboxError(
            "unknown_kind", f"kind {kind!r} is not one of {list(KINDS)}")
    return kind


def _correlation(value: Any) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise MailboxError("invalid_envelope", "correlation must be an object")
    return {
        key: _optional(value.get(key), f"correlation.{key}")
        for key in CORRELATION_KEYS
    }


def _body(value: Any, *, kind: str) -> Any:
    body = {} if value is None else value
    try:
        encoded = json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise MailboxError(
            "invalid_envelope", f"body is not JSON-serializable: {exc}") from exc
    size = len(encoded.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise MailboxError(
            "body_too_large",
            f"body is {size} bytes; the cap is {MAX_BODY_BYTES}",
        )
    if kind == "report" and isinstance(body, dict):
        outcome = body.get("outcome")
        if outcome not in (None, ""):
            if not isinstance(outcome, str) or outcome not in REPORT_OUTCOMES:
                raise MailboxError(
                    "invalid_envelope",
                    f"report.outcome {outcome!r} is not one of "
                    f"{list(REPORT_OUTCOMES)}",
                )
    return body


def _priority(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MailboxError(
            "invalid_priority", f"priority must be an integer 0..3, got {value!r}")
    if not 0 <= value <= 3:
        raise MailboxError(
            "invalid_priority", f"priority {value} is outside 0..3")
    return value


def _ttl(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise MailboxError(
            "invalid_field", f"ttl_s must be a non-negative integer, got {value!r}")
    if value < 0:
        raise MailboxError("invalid_field", f"ttl_s {value} is negative")
    return value


def _state(value: Any) -> str:
    if value is None:
        return "queued"
    return _required(value, "state")


def _attempts(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MailboxError(
            "invalid_envelope", f"attempts must be a non-negative integer, got {value!r}")
    return value


def _lease(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MailboxError("invalid_envelope", "lease must be an object or null")
    return value


def _required(value: Any, name: str) -> str:
    """A non-empty identifier string."""
    if not isinstance(value, str) or not value.strip():
        raise MailboxError("invalid_envelope", f"{name} must be a non-empty string")
    return value.strip()


def _optional(value: Any, name: str) -> str:
    """An optional string field; absent or null reads as ``""``."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MailboxError("invalid_envelope", f"{name} must be a string")
    return value.strip()


# ---------------------------------------------------------------------------
# Decision card (design §9.3)
# ---------------------------------------------------------------------------

def card(envelope: dict, *, ticket: dict | None = None,
         summary: str = "") -> dict:
    """The 1 KiB summary of one envelope that ``wait`` hands the Router.

    ``intent`` and ``attention`` come from the fast path when it annotated the
    envelope; otherwise they fall back to the kind's floor. The summary is
    clamped to 200 characters and then truncated until the whole card
    serializes within :data:`MAX_CARD_BYTES` — no key is ever dropped.
    """
    source = envelope.get("from")
    if not isinstance(source, dict):
        source = {}
    role = source.get("role") if isinstance(source.get("role"), str) else ""
    ident = source.get("id") if isinstance(source.get("id"), str) else ""
    message_id = str(envelope.get("id") or "")
    kind = str(envelope.get("kind") or "")
    parts = {
        "message_id": message_id,
        "kind": kind,
        "from": f"{role}:{ident}" if ident else role,
        "trust": _card_trust(source, role),
        "intent": _intent(envelope, kind),
        "attention": _attention(envelope, kind, role),
        "priority": _card_priority(envelope),
        "tickets": _card_tickets(ticket),
        "detail_hint": f"osw mailbox show {message_id}" if message_id else "",
    }
    text = summary if isinstance(summary, str) else ""
    if not text.strip():
        text = summary_of(envelope)
    return _assemble(parts, _fit_summary(parts, text))


def summary_of(envelope: dict, *, limit: int = MAX_SUMMARY_CHARS) -> str:
    """One line of body text: ``body.summary``, else ``body.text``, else body."""
    body = envelope.get("body") if isinstance(envelope, dict) else None
    text = ""
    if isinstance(body, dict):
        for key in ("summary", "text"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
    elif isinstance(body, str):
        text = body
    line = text.strip().splitlines()[0] if text.strip() else ""
    return " ".join(line.split())[:limit]


def _card_trust(source: dict, role: str) -> str:
    trust = source.get("trust")
    if isinstance(trust, str) and trust.strip().lower() in TRUSTS:
        return trust.strip().lower()
    return "trusted" if role in TRUSTED_ROLES else "untrusted"


def _intent(envelope: dict, kind: str) -> str:
    given = envelope.get("intent")
    if isinstance(given, str) and given.strip():
        return given.strip()
    return kind


def _attention(envelope: dict, kind: str, role: str) -> str:
    given = envelope.get("attention")
    if isinstance(given, str) and given.strip() in ATTENTIONS:
        return given.strip()
    return _attention_floor(envelope, kind, role)


def _attention_floor(envelope: dict, kind: str, role: str) -> str:
    if kind in ("ask", "blocked"):
        return "must_answer"
    if kind in ("request", "followup") and role == "human":
        return "must_answer"
    if kind == "report":
        body = envelope.get("body")
        outcome = body.get("outcome") if isinstance(body, dict) else ""
        return "must_answer" if outcome in _TERMINAL_OUTCOMES else "fyi"
    return "fyi"


def _card_priority(envelope: dict) -> int:
    value = envelope.get("priority")
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3:
        return value
    return default_priority(envelope)


def _card_tickets(ticket: dict | None) -> list[dict]:
    if not isinstance(ticket, dict) or not ticket:
        return []
    ident = (ticket.get("id") or ticket.get("ticket_id")
             or ticket.get("ticket") or "")
    stage = ticket.get("stage") or ""
    return [{"id": str(ident), "stage": str(stage)}]


def _assemble(parts: dict, summary: str) -> dict:
    assembled = dict(parts)
    assembled["summary"] = summary
    return {key: assembled[key] for key in CARD_KEYS}


def _card_bytes(parts: dict, summary: str) -> int:
    encoded = json.dumps(_assemble(parts, summary), ensure_ascii=False)
    return len(encoded.encode("utf-8"))


def _fit_summary(parts: dict, text: str) -> str:
    if _card_bytes(parts, text) <= MAX_CARD_BYTES:
        return text
    clipped = text[:MAX_SUMMARY_CHARS]
    truncated = "…"
    for size in range(len(clipped), 0, -1):
        candidate = clipped[:size]
        if size < len(clipped):
            candidate += truncated
        if _card_bytes(parts, candidate) <= MAX_CARD_BYTES:
            return candidate
    if _card_bytes(parts, "") <= MAX_CARD_BYTES:
        return ""
    raise MailboxError(
        "invalid_field",
        f"card for {parts.get('message_id')!r} exceeds {MAX_CARD_BYTES} bytes "
        "with an empty summary",
    )


# ---------------------------------------------------------------------------
# Fast-path command grammar
# ---------------------------------------------------------------------------

def is_exact_command(text: str) -> bool:
    """True for one bare ``osw new|use|stop|status|list|logs`` command line.

    The fast path only recognises a command that carries no shell
    metacharacter and no newline, so a command body can never smuggle a
    second command.
    """
    if not isinstance(text, str) or not text:
        return False
    if any(char in EXACT_COMMAND_FORBIDDEN for char in text):
        return False
    return bool(EXACT_COMMAND_RE.match(text))

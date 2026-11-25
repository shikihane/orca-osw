"""Fast path: deterministic classification and bookkeeping for the mailbox.

Design: ``docs/superpowers/specs/2026-11-14-osw-control-plane-design.md`` §8.
Every rule is pure code with a replay fixture under
``tests/fixtures/fastpath/<rule>.json``. The fast path only classifies a
message and derives the bookkeeping or the human-command Decision it implies;
it never launches a worker, answers one, retries, closes a Ticket or chooses a
provider. Judgment stays with the Router.

Rules are evaluated in order and the first match wins. ``ctx`` carries the
facts the rules may read: ``now`` (ISO timestamp), ``tickets`` (rows from the
store), ``doorbell`` (kv state) and ``queued_router`` (count of queued router
cards, including the one under evaluation). ``ctx["counts"]`` and
``ctx["dead"]`` are optional extras used only to print a status line.
``ctx["agent_ticket"]`` is the one store-backed fact: :func:`apply` resolves
``correlation.agent`` through ``Store.ticket_for_agent`` when neither the
correlation ticket nor the thread key matches, and hands the row to the rules,
which stay pure. Nothing here launches, replies, answers a worker, closes a
Ticket or chooses a provider; a human command becomes a Decision the Dispatcher
plans and applies, and everything else is bookkeeping.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from osw.mailbox.envelope import default_priority, is_exact_command

Result = dict[str, Any]

#: Human exact command -> the Decision action it produces.
COMMAND_ACTIONS = {
    "new": "accept",
    "use": "followup",
    "stop": "stop",
    "status": "noop",
    "list": "noop",
    "logs": "noop",
}

#: Report outcome -> the assignment state that report finalizes.
ASSIGNMENT_STATES = {"ready": "done", "failed": "done", "dropped": "stopped"}

REPORT_RESULT_OUTCOMES = ("ready", "failed", "dropped")
REPORT_PROGRESS_OUTCOMES = ("progress", "waiting")
NOTICE_KINDS = ("notice", "ack", "timer", "system")
ASK_ROLES = ("worker", "watcher")


# --------------------------------------------------------------------------
# small readers


def _role(env: dict, side: str) -> str:
    return str((env.get(side) or {}).get("role") or "")


def _body(env: dict) -> dict:
    body = env.get("body")
    return body if isinstance(body, dict) else {}


def _corr(env: dict, key: str) -> str:
    return str((env.get("correlation") or {}).get(key) or "")


def _message_id(env: dict) -> str:
    return str(env.get("id") or "")


def _address(env: dict) -> str:
    """Transport address of the sender, usable as an Outbox conversation."""
    sender = env.get("from") or {}
    return str(sender.get("id") or sender.get("role") or "")


def _epoch(value: Any) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# --------------------------------------------------------------------------
# result assembly


def _wake(ctx: dict) -> str:
    """Doorbell policy: a queued card rings unless one is already pending."""
    if int(ctx.get("queued_router") or 0) > 0 and ctx.get("doorbell") != "pending":
        return "doorbell"
    return ""


def _result(
    env: dict,
    ctx: dict,
    *,
    intent: str,
    attention: str,
    reason: str,
    priority: int | None = None,
    wake: str | None = None,
    decision: dict | None = None,
    bookkeeping: list[dict] | None = None,
) -> Result:
    return {
        "intent": intent,
        "attention": attention,
        "priority": default_priority(env) if priority is None else int(priority),
        "wake": _wake(ctx) if wake is None else wake,
        "decision": decision,
        "bookkeeping": list(bookkeeping or ()),
        "reason": reason,
    }


# --------------------------------------------------------------------------
# ticket attachment


def _match_ticket(env: dict, ctx: dict) -> dict | None:
    """The Ticket this message belongs to.

    ``correlation.ticket`` first, then ``thread_key``, then the Ticket
    :func:`apply` resolved from ``correlation.agent``. The watcher names only
    the agent (``correlation.agent`` plus ``thread_key`` = the agent id when
    its record has no ticket id), so a worker's report or ask carries no ticket
    id that matches: the agent's assignment is the join.
    """
    tickets = [t for t in (ctx.get("tickets") or ()) if isinstance(t, dict)]
    ticket_id = _corr(env, "ticket")
    if ticket_id:
        found = next((t for t in tickets if t.get("id") == ticket_id), None)
        if found is not None:
            return found
    thread_key = str(env.get("thread_key") or "")
    if thread_key:
        found = next((t for t in tickets if t.get("thread_key") == thread_key), None)
        if found is not None:
            return found
    agent_ticket = ctx.get("agent_ticket")
    return agent_ticket if isinstance(agent_ticket, dict) else None


def _agent_ticket(store: Any, env: dict, ctx: dict) -> dict | None:
    """Resolve ``correlation.agent`` to its Ticket, or ``None``.

    Only consulted when neither the correlation ticket nor the thread key
    matches a Ticket: that is exactly the watcher-produced shape, where the
    agent id is the sole correlation and the Ticket keeps the agent in its
    assignments. ``apply`` puts the row in ``ctx`` so the rules stay pure.
    """
    if _match_ticket(env, ctx) is not None:
        return None
    agent = _corr(env, "agent")
    if not agent:
        return None
    return store.ticket_for_agent(agent)


def _attach_ops(
    env: dict,
    ctx: dict,
    *,
    stage: str,
    report: dict | None = None,
    ask: str = "",
    agent: str = "",
) -> list[dict]:
    """Attach to the matching Ticket and derive its stage; [] when none."""
    ticket = _match_ticket(env, ctx)
    if ticket is None:
        return []
    ticket_id = str(ticket.get("id") or "")
    attach: dict = {
        "op": "attach",
        "ticket": ticket_id,
        "message_id": _message_id(env),
    }
    if report is not None:
        attach["report"] = report
    if ask:
        attach["ask"] = ask
        if agent:
            attach["agent"] = agent
    return [attach, {"op": "stage", "ticket": ticket_id, "stage": stage}]


def _report(env: dict) -> dict:
    body = _body(env)
    return {
        "message_id": _message_id(env),
        "outcome": str(body.get("outcome") or ""),
        "report_key": str(env.get("dedupe_key") or ""),
        "body": body,
    }


def _status_line(ctx: dict) -> str:
    counts = ctx.get("counts") or {}
    pending = int(counts.get("router", ctx.get("queued_router") or 0) or 0)
    human = int(counts.get("human") or 0)
    worker = int(counts.get("worker") or 0)
    dead = int(ctx.get("dead") or counts.get("dead") or 0)
    return (
        f"# [osw] status pending={pending} human={human} "
        f"worker={worker} dead={dead}"
    )


# --------------------------------------------------------------------------
# rules, in evaluation order


class Rule:
    """One deterministic fast-path rule."""

    name = ""

    def apply(self, env: dict, ctx: dict) -> Result | None:
        raise NotImplementedError


class ExpireTTL(Rule):
    """An envelope past its ``ttl_s`` is dropped and settled as expired."""

    name = "expire_ttl"

    def apply(self, env: dict, ctx: dict) -> Result | None:
        ttl_s = int(env.get("ttl_s") or 0)
        if ttl_s <= 0:
            return None
        sent = _epoch(env.get("ts"))
        now = _epoch(ctx.get("now"))
        if sent is None or now is None or now - sent < ttl_s:
            return None
        return _result(
            env,
            ctx,
            intent="expire",
            attention="fyi",
            reason=f"ttl {ttl_s}s expired",
            bookkeeping=[{"op": "expire", "message_id": _message_id(env)}],
        )


class HumanCommand(Rule):
    """A trusted human's exact command becomes a Decision authored by human."""

    name = "human_command"

    def apply(self, env: dict, ctx: dict) -> Result | None:
        if _role(env, "from") != "human" or env.get("kind") != "request":
            return None
        if (env.get("from") or {}).get("trust") != "trusted":
            return None
        command = str(_body(env).get("command") or "").strip()
        if not is_exact_command(command):
            return None
        action = COMMAND_ACTIONS[command.split()[1].lower()]
        decision = {
            "decision_id": _corr(env, "decision") or f"dec_{_message_id(env)}",
            "message_id": _message_id(env),
            "action": action,
            "ticket": _corr(env, "ticket"),
            "reason": "human command",
            "payload": {"command": command},
            "author": "human",
            "confidence": 1.0,
        }
        return _result(
            env,
            ctx,
            intent="command",
            attention="fyi",
            priority=0,
            reason="human command",
            decision=decision,
        )


class HumanRequest(Rule):
    """A human request always needs an answer."""

    name = "human_request"

    def apply(self, env: dict, ctx: dict) -> Result | None:
        if _role(env, "from") != "human" or env.get("kind") != "request":
            return None
        return _result(
            env,
            ctx,
            intent="request",
            attention="must_answer",
            priority=0,
            reason="human request",
        )


class Ask(Rule):
    """A worker or watcher question waits on an answer and parks the Ticket."""

    name = "ask"

    def apply(self, env: dict, ctx: dict) -> Result | None:
        if env.get("kind") != "ask" or _role(env, "from") not in ASK_ROLES:
            return None
        return _result(
            env,
            ctx,
            intent="answer",
            attention="must_answer",
            reason="worker question",
            bookkeeping=_attach_ops(
                env,
                ctx,
                stage="waiting_answer",
                ask=_corr(env, "ask"),
                agent=_corr(env, "agent"),
            ),
        )


class Blocked(Rule):
    """A blocked screen is escalated; the Ticket moves to ``blocked``."""

    name = "blocked"

    def apply(self, env: dict, ctx: dict) -> Result | None:
        if env.get("kind") != "blocked":
            return None
        return _result(
            env,
            ctx,
            intent="resolve",
            attention="must_answer",
            reason="blocked screen",
            bookkeeping=_attach_ops(env, ctx, stage="blocked"),
        )


class ReportResult(Rule):
    """A finished turn is reviewed (or stopped when the turn was dropped)."""

    name = "report_result"

    def apply(self, env: dict, ctx: dict) -> Result | None:
        if env.get("kind") != "report":
            return None
        outcome = str(_body(env).get("outcome") or "")
        if outcome not in REPORT_RESULT_OUTCOMES:
            return None
        stage = "stopped" if outcome == "dropped" else "review"
        return _result(
            env,
            ctx,
            intent="accept_result",
            attention="must_answer",
            reason=f"report {outcome}",
            bookkeeping=_attach_ops(
                env, ctx, stage=stage, report=_report(env)
            ),
        )


class ReportProgress(Rule):
    """A progress report is bookkeeping: park the Ticket at ``working``."""

    name = "report_progress"

    def apply(self, env: dict, ctx: dict) -> Result | None:
        if env.get("kind") != "report":
            return None
        outcome = str(_body(env).get("outcome") or "")
        if outcome not in REPORT_PROGRESS_OUTCOMES:
            return None
        ops = _attach_ops(env, ctx, stage="working", report=_report(env))
        if outcome == "progress" and ops:
            ops.append(
                {
                    "op": "settle",
                    "message_id": _message_id(env),
                    "outcome": outcome,
                }
            )
        return _result(
            env,
            ctx,
            intent="progress",
            attention="fyi",
            reason=f"report {outcome}",
            bookkeeping=ops,
        )


class StatusRequest(Rule):
    """A status request is answered by one outbox notice carrying the counts."""

    name = "status_request"

    def apply(self, env: dict, ctx: dict) -> Result | None:
        if env.get("kind") != "status" or _role(env, "to") != "router":
            return None
        message_id = _message_id(env)
        outbox = {
            "op": "outbox",
            "effect": "notify",
            "key": f"status:{message_id}",
            "payload": {
                "line": _status_line(ctx),
                "conversation": _address(env),
            },
        }
        return _result(
            env,
            ctx,
            intent="answer_status",
            attention="fyi",
            reason="status request",
            bookkeeping=[outbox],
        )


class Notice(Rule):
    """Notices are read at leisure and never ring the doorbell."""

    name = "notice"

    def apply(self, env: dict, ctx: dict) -> Result | None:
        kind = str(env.get("kind") or "")
        if kind not in NOTICE_KINDS:
            return None
        return _result(
            env,
            ctx,
            intent="notice",
            attention="fyi",
            wake="",
            reason=f"{kind} notice",
        )


class Wake(Rule):
    """Fallback: unclassified cards still ring the doorbell when one is due."""

    name = "wake"

    def apply(self, env: dict, ctx: dict) -> Result | None:
        wake = _wake(ctx)
        return _result(
            env,
            ctx,
            intent=str(env.get("kind") or "unknown"),
            attention="fyi",
            wake=wake,
            reason="doorbell" if wake else "no matching rule",
        )


RULES: list[Rule] = [
    ExpireTTL(),
    HumanCommand(),
    HumanRequest(),
    Ask(),
    Blocked(),
    ReportResult(),
    ReportProgress(),
    StatusRequest(),
    Notice(),
    Wake(),
]

RULE_BY_NAME = {rule.name: rule for rule in RULES}


# --------------------------------------------------------------------------
# pipeline


def evaluate(env: dict, ctx: dict | None = None) -> Result:
    """Classify one envelope; the first matching rule wins."""
    context = dict(ctx or {})
    for rule in RULES:
        result = rule.apply(env, context)
        if result is not None:
            return result
    raise AssertionError("fastpath.Wake always matches")  # pragma: no cover


def _finalize_assignment(
    store: Any, ticket_id: str, report: dict, env: dict | None
) -> None:
    """Settle the reporting worker's assignment from the report outcome.

    The agent is the only identity a watcher report carries, so it comes from
    ``correlation.agent``. A ready or failed turn is finished (``done``), a
    dropped one is ``stopped``; a progress or waiting report settles nothing.
    """
    state = ASSIGNMENT_STATES.get(str(report.get("outcome") or ""))
    agent = _corr(env or {}, "agent")
    if state and agent:
        store.set_assignment_state(ticket_id, agent, state)


def _outbox_row(op: dict) -> tuple[str, dict]:
    """The conversation and payload of one ``outbox`` bookkeeping op."""
    payload = dict(op.get("payload") or {})
    conversation = str(op.get("conversation") or "")
    if not conversation:
        conversation = str(payload.pop("conversation", "") or "")
    return conversation, payload


def _sealed_batch(store: Any, ticket_id: str) -> bool:
    """Whether this Ticket is a sealed fan-out batch (one conclusion instead).

    The marker is written by ``dispatcher.seal_batch``; an unreadable store or
    a plain Ticket is simply not a batch, and the ordinary rules apply.
    """
    try:
        return bool(store.kv_get(f"batch:sealed:{ticket_id}"))
    except Exception:
        return False


def _apply_op(store: Any, op: dict, env: dict | None = None) -> None:
    name = op.get("op")
    if name == "attach":
        report = op.get("report")
        ticket_id = op["ticket"]
        if report is not None:
            store.add_report(ticket_id, report)
            _finalize_assignment(store, ticket_id, report, env)
            # A sealed batch turns its reports into one conclusion, so the
            # report itself must not become a Router card (and must not ring
            # the doorbell): settle it as soon as it is attached.
            message_id = str(op.get("message_id") or "")
            if message_id and _sealed_batch(store, ticket_id):
                store.settle(message_id, outcome="attached")
        ask = op.get("ask")
        if ask:
            store.open_ask(
                ticket_id,
                ask,
                agent=op.get("agent", ""),
                message_id=op.get("message_id", ""),
            )
    elif name == "stage":
        # A sealed batch keeps "working" until the daemon concludes it; the
        # per-report "review" stage would hide it from that scan.
        if str(op.get("stage") or "") == "review" \
                and _sealed_batch(store, op["ticket"]):
            return
        store.update_ticket(op["ticket"], stage=op["stage"])
    elif name == "settle":
        store.settle(op["message_id"], outcome=op.get("outcome", ""))
    elif name == "expire":
        store.settle(op["message_id"], outcome="expired")
    elif name == "outbox":
        # A status answer is a bookkeeping notice, not an Effect: an Effect
        # keyed on a message has no Ticket to anchor it, so it would only
        # dead-letter. The outbox row carries the line to the asker.
        conversation, payload = _outbox_row(op)
        store.outbox_put(
            ticket_id=str(op.get("ticket") or ""),
            conversation=conversation,
            payload=payload,
        )
    else:
        raise ValueError(f"unknown fastpath bookkeeping op: {name}")


def _commit_decision(store: Any, decision: dict, env: dict) -> bool:
    """Commit a human-command Decision through the Dispatcher.

    Imported lazily: the fast path must stay importable on its own (direct
    mode), and the Dispatcher stays the only planner and applier of Decision
    effects, so an exact human command is planned exactly like any other
    Decision instead of being written half-way by the fast path.
    """
    from osw import dispatcher

    return dispatcher.commit(store, decision, message_id=_message_id(env))


def apply(store: Any, env: dict, ctx: dict | None = None) -> dict:
    """Evaluate and perform the bookkeeping through the store, nothing else.

    Returns the :func:`evaluate` result plus ``committed`` (the Decision was
    new) and ``applied`` (the bookkeeping op names performed).
    """
    context = dict(ctx or {})
    #  The store is the only source of the agent join; an inherited value could
    #  attach a later message to an older Ticket.
    context.pop("agent_ticket", None)
    agent_ticket = _agent_ticket(store, env, context)
    if agent_ticket is not None:
        context["agent_ticket"] = agent_ticket
    result = evaluate(env, context)
    decision = result["decision"]
    committed = False
    if decision is not None:
        committed = _commit_decision(store, decision, env)
    applied: list[str] = []
    for op in result["bookkeeping"]:
        _apply_op(store, op, env)
        applied.append(str(op.get("op") or ""))
    return {**result, "committed": committed, "applied": applied}

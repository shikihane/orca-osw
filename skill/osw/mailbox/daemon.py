"""The Mailbox daemon: one local process that owns the control plane.

A tick is fixed and ordered: ingest the spool, run the fast path over the new
messages, poll the optional source adapters, apply pending Decisions through
the Dispatcher, deliver the Outbox, expire stale leases, ring the doorbell,
then write ``status.json``. The stop side file ``daemon.stop`` ends the loop.
Every unit is isolated: one exception becomes a logged event, never a dead
daemon.

The daemon is the only writer of mailbox state. It is local-only (spec §15):
producers write envelopes to the spool, the spool is the only door in.

:class:`CLIHooks` is the production Dispatcher hook set. It shells out to the
OSW CLI (``new``/``use``/``stop``) and answers worker questions through
``orca orchestration reply``. It is the only place that builds those argv
lists.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio

from osw import dispatcher, fastpath
from osw.log import emit_event
from osw.orca_cli import orchestration_send, run_orca, terminal_send
from osw.process import hidden_subprocess_kwargs
from osw.state import logs_dir, pid_is_running

from . import adapters, spool
from .delivery import conversation_address, deliver_outbox, is_deliverable
from .envelope import validate_envelope
from .errors import MailboxError
from .paths import (
    db_path,
    ensure_mailbox_dirs,
    ingested_dir,
    lock_path,
    spool_dir,
    status_path,
    stop_path,
)
from .store import Store

DOORBELL_LINE = (
    '# [osw] mail pending={count} run="osw mailbox wait --as router --timeout 0"'
)
ATTENTION_LINE = (
    '# [osw] attention router-unreachable pending={count}'
    ' run="osw mailbox wait --timeout 0"'
)
DOORBELL_RE_RING_S = 120.0
DOORBELL_STATE_KEY = "doorbell:state"
DOORBELL_AT_KEY = "doorbell:at"
DOORBELL_RINGS_KEY = "doorbell:rings"

CREATED_RE = re.compile(r"Created\s+(\S+)\s+on terminal\s+(\S+)")
DISPATCH_RE = re.compile(r"dispatch=(\S+)")

CLI_TIMEOUT_S = 300.0

#: Inherited identity markers a hook subprocess must not carry: a Router shell
#: would otherwise make its own ``new``/``use``/``stop`` refuse with
#: ``router_forbidden``, and a daemon started from one would pass it on.
STRIPPED_ENV_KEYS = ("OSW_ROLE", "OSW_ROLE_TOKEN", "ORCA_TERMINAL_HANDLE")

#: Ticket stages in which a Ticket no longer wants a conversation woken.
CLOSED_STAGES = ("done", "stopped")


# ---------------------------------------------------------------------------
# Lock and status files


def acquire_lock(root: Path) -> dict:
    """Take the daemon lock; a dead pid is reclaimed, a live one refuses.

    Returns ``{"pid", "started_at"}``. A lock whose recorded pid is alive is a
    foreign daemon: :class:`MailboxError` ``mailbox_locked``.
    """
    root = Path(root)
    ensure_mailbox_dirs(root)
    path = lock_path(root)
    payload = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    for _ in range(5):
        existing = _read_lock(root)
        if existing is not None:
            pid = _lock_pid(existing)
            if pid and pid_is_running(pid):
                _event(
                    root,
                    "mailbox_locked",
                    level="WARNING",
                    message=f"mailbox daemon already running (pid {pid})",
                    data={"pid": pid},
                )
                raise MailboxError(
                    "mailbox_locked",
                    f"mailbox daemon already running (pid {pid})",
                )
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        try:
            handle = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644,
            )
        except FileExistsError:
            time.sleep(0.01)
            continue
        except OSError as exc:
            raise MailboxError("mailbox_unavailable", f"cannot write lock: {exc}")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        return payload
    raise MailboxError("mailbox_locked", "could not take the mailbox lock")


def release_lock(root: Path) -> None:
    """Release our lock; leave a live foreign lock untouched."""
    path = lock_path(Path(root))
    existing = _read_lock(Path(root))
    if existing is not None:
        pid = _lock_pid(existing)
        if pid and pid_is_running(pid) and pid != os.getpid():
            return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def is_running(root: Path) -> tuple[bool, int]:
    """``(running, pid)`` from the lock file; a dead pid is not running."""
    existing = _read_lock(Path(root))
    if existing is None:
        return False, 0
    pid = _lock_pid(existing)
    if pid and pid_is_running(pid):
        return True, pid
    return False, 0


def read_status(root: Path) -> dict | None:
    """The last ``status.json`` or ``None`` when absent or unreadable."""
    try:
        raw = status_path(Path(root)).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_status(root: Path, store: Store) -> dict:
    """Persist the §18 status document from the store and the own lock."""
    root = Path(root)
    try:
        data = dict(store.status() or {})
    except Exception as exc:  # a broken status write must not stop the loop
        _event(root, "mailbox_status_failed", level="ERROR", message=str(exc))
        data = {}
    running, pid = is_running(root)
    if not running:
        # We are the writer and hold the lock even if the pid probe missed.
        running, pid = True, os.getpid()
    data.update({"running": running, "pid": pid})
    try:
        data["spool_lag"] = spool.pending_count(root)
    except Exception:
        data["spool_lag"] = 0
    try:
        _write_json(status_path(root), data)
    except OSError as exc:
        _event(root, "mailbox_status_failed", level="ERROR", message=str(exc))
    return data


# ---------------------------------------------------------------------------
# Doorbell


def router_notify(
    store: Store, root: Path, *, now: float | None = None
) -> None:
    """Ring the Router once, suppress the second, then wake a human.

    One doorbell may be pending: while it is, nothing more is sent until
    :data:`DOORBELL_RE_RING_S` has passed (the lease-expiry signal). The
    second ring waits another interval; after that a human attention line is
    enqueued once, addressed to the Ticket origin when that is a real
    conversation.

    A ring with no deliverable address is never queued: an Outbox row there
    could only dead-letter. It logs ``doorbell_skipped`` once per interval
    instead, and the ring sequence still advances so the human fallback stays
    reachable.
    """
    now = time.time() if now is None else now
    try:
        counts = store.counts(now=now) or {}
    except Exception:
        return
    pending = int(counts.get("router") or 0)
    state = str(store.kv_get(DOORBELL_STATE_KEY) or "none")
    if pending <= 0:
        store.kv_set(DOORBELL_STATE_KEY, "none")
        return
    if state == "human":
        return
    rings = _as_int(store.kv_get(DOORBELL_RINGS_KEY))
    at = _as_float(store.kv_get(DOORBELL_AT_KEY))
    if state != "pending":
        rings = 0  # consumed or never rung: a fresh sequence
    if rings == 0:
        _ring_or_skip(
            store,
            root,
            _router_address(store),
            DOORBELL_LINE,
            pending,
            now,
            at,
            rings=1,
            reason="no deliverable router binding; doorbell not sent",
        )
        return
    if now - at < DOORBELL_RE_RING_S:
        return
    if rings == 1:
        _ring_or_skip(
            store,
            root,
            _router_address(store),
            DOORBELL_LINE,
            pending,
            now,
            at,
            rings=2,
            reason="no deliverable router binding; doorbell not sent",
        )
        return
    delivered = _ring_or_skip(
        store,
        root,
        _attention_address(store) or _router_address(store),
        ATTENTION_LINE,
        pending,
        now,
        at,
        rings=rings,
        reason="no deliverable attention address; doorbell not sent",
    )
    if not delivered:
        return
    store.kv_set(DOORBELL_STATE_KEY, "human")
    _event(
        root,
        "doorbell_attention",
        message="doorbell unanswered; human attention enqueued",
        data={"pending": pending},
    )


def _ring_or_skip(
    store: Store,
    root: Path,
    address: str,
    template: str,
    pending: int,
    now: float,
    at: float,
    *,
    rings: int,
    reason: str,
) -> bool:
    """Queue one ring, or record the undeliverable attempt once per interval.

    Returns whether a ring was queued. A skip still advances the ring sequence
    so the next interval can try the next step (and the human fallback).
    """
    if address and is_deliverable(address):
        _ring(store, root, address, template, pending, now, rings=rings)
        return True
    if now - at < DOORBELL_RE_RING_S:
        return False
    store.kv_set(DOORBELL_STATE_KEY, "pending")
    store.kv_set(DOORBELL_AT_KEY, str(now))
    store.kv_set(DOORBELL_RINGS_KEY, str(rings))
    _event(
        root,
        "doorbell_skipped",
        level="WARNING",
        message=reason,
        data={"pending": pending, "rings": rings},
    )
    return False


def _ring(
    store: Store,
    root: Path,
    address: str,
    template: str,
    pending: int,
    now: float,
    *,
    rings: int,
) -> None:
    line = template.format(count=pending)
    store.outbox_put(
        ticket_id="", conversation=address, payload={"line": line},
    )
    store.kv_set(DOORBELL_STATE_KEY, "pending")
    store.kv_set(DOORBELL_AT_KEY, str(now))
    store.kv_set(DOORBELL_RINGS_KEY, str(rings))
    _event(
        root,
        "doorbell_sent",
        message="doorbell enqueued",
        data={"pending": pending, "rings": rings, "conversation": address},
    )


def _router_address(store: Store) -> str:
    """The Router's transport, or ``""`` when it is not a notify target."""
    try:
        binding = store.router_binding()
    except Exception:
        return ""
    if not isinstance(binding, dict):
        return ""
    raw = str(
        binding.get("address")
        or binding.get("transport")
        or binding.get("id")
        or ""
    )
    address = _normalize_address(raw)
    return address if is_deliverable(address) else ""


def _attention_address(store: Store) -> str:
    """The conversation a human fallback wakes: a live Ticket's origin.

    A headless Router binding names a process, not a conversation, so the
    attention line goes to whoever opened a Ticket instead: its origin
    transport, when that is a real ``terminal:``/``session:`` address.
    """
    try:
        tickets = store.tickets() or []
    except Exception:
        return ""
    for ticket in sorted(tickets, key=_ticket_updated, reverse=True):
        if not isinstance(ticket, dict):
            continue
        if str(ticket.get("stage") or "") in CLOSED_STAGES:
            continue
        address = _origin_address(ticket)
        if _is_transport(address):
            return address
    return ""


def _origin_address(ticket: dict) -> str:
    """The Ticket origin's transport conversation, or ``""``."""
    origin = ticket.get("origin") if isinstance(ticket, dict) else None
    if not isinstance(origin, dict):
        return ""
    transport = origin.get("transport")
    address = ""
    if isinstance(transport, (dict, str)):
        address = conversation_address(transport)
    if not address:
        address = conversation_address(str(origin.get("id") or ""))
    if not address:
        address = conversation_address(str(origin.get("terminal") or ""))
    return address


def _is_transport(address: str) -> bool:
    """A real conversation address, not a role, pid or opaque marker."""
    for prefix in ("terminal:", "session:"):
        if address.startswith(prefix) and address[len(prefix):].strip():
            return True
    return False


def _ticket_updated(ticket: dict) -> float:
    return _as_float(ticket.get("updated_at") or ticket.get("created_at"))


def _normalize_address(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("terminal:") or raw.startswith("session:"):
        return raw
    if raw.startswith("orca_session_id:"):
        return "session:" + raw.split(":", 1)[1]
    if ":" in raw:  # an opaque address (a headless ``local:<pid>`` is one)
        return raw
    return f"terminal:{raw}"


# ---------------------------------------------------------------------------
# The loop


async def run(
    root: Path,
    *,
    once: bool = False,
    poll_s: float = 1.0,
    hooks: Any | None = None,
) -> None:
    """Own the mailbox until the stop file appears (or one tick with *once*).

    ``hooks`` is the Dispatcher hook set; production passes nothing and gets
    :class:`CLIHooks`. Tests inject a fake so no Orca is needed.
    """
    root = Path(root)
    ensure_mailbox_dirs(root)
    acquire_lock(root)
    store: Store | None = None
    _event(root, "mailbox_started", message="mailbox daemon started")
    try:
        store = Store(db_path(root))
        try:
            store.root = root
        except Exception:
            pass
        active = hooks if hooks is not None else CLIHooks(root)
        owner = f"mailbox:{os.getpid()}"
        while True:
            try:
                await tick(root, store, active, owner=owner)
            except Exception as exc:
                _event(
                    root,
                    "mailbox_tick_failed",
                    level="ERROR",
                    message=str(exc),
                )
            if once:
                break
            if stop_path(root).exists():
                break
            await anyio.sleep(poll_s)
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        try:
            stop_path(root).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        release_lock(root)
        _event(root, "mailbox_stopped", message="mailbox daemon stopped")


async def tick(
    root: Path,
    store: Store,
    hooks: Any,
    *,
    now: float | None = None,
    owner: str = "",
) -> None:
    """One ordered pass of the daemon (spec §12)."""
    root = Path(root)
    now = time.time() if now is None else now
    owner = owner or f"mailbox:{os.getpid()}"

    _ingest_and_fastpath(root, store, now)

    _run_adapters(root, now)

    try:
        await dispatcher.run_pending(store, hooks, now=now, owner=owner)
    except Exception as exc:
        _event(root, "mailbox_dispatch_failed", level="ERROR", message=str(exc))

    notify = getattr(hooks, "notify", None)
    if callable(notify):
        try:
            outcomes = await deliver_outbox(store, notify, now=now)
        except Exception as exc:
            _event(
                root, "mailbox_delivery_failed", level="ERROR", message=str(exc)
            )
            outcomes = []
        _report_delivery(root, outcomes)

    try:
        expired = store.expire_leases(now=now)
    except Exception as exc:
        _event(root, "mailbox_lease_expired", level="WARNING", message=str(exc))
    else:
        if expired:
            _event(
                root,
                "mailbox_lease_expired",
                message="expired leases returned to the queue",
                data={"count": int(expired)},
            )

    try:
        router_notify(store, root, now=now)
    except Exception as exc:
        _event(root, "doorbell_failed", level="ERROR", message=str(exc))

    try:
        write_status(root, store)
    except Exception as exc:
        _event(root, "mailbox_status_failed", level="ERROR", message=str(exc))


def _run_adapters(root: Path, now: float) -> None:
    """Poll the optional source adapters; one failure never stops the tick.

    Adapters are optional: no config, or no enabled adapter with targets, is
    simply no work. Their envelopes land in the spool and are ingested by the
    next tick, exactly like any other producer's.
    """
    try:
        counts = adapters.run_once(root, now=now)
    except Exception as exc:
        _event(root, "mailbox_adapters_failed", level="ERROR", message=str(exc))
        return
    produced = 0
    for count in (counts or {}).values():
        produced += _as_int((count or {}).get("envelopes"))
    if produced:
        _event(
            root,
            "mailbox_adapters",
            message="adapter envelopes written to the spool",
            data={"count": produced},
        )


def _report_delivery(root: Path, outcomes: list[dict]) -> None:
    for outcome in outcomes:
        state = outcome.get("state")
        if state == "dead":
            _event(
                root,
                "mailbox_dead_letter",
                level="WARNING",
                message="outbox row dead-lettered",
                data={
                    "outbox": outcome.get("id"),
                    "conversation": outcome.get("conversation"),
                    "error": outcome.get("error"),
                },
            )
        elif state == "waiting":
            _event(
                root,
                "mailbox_delivery_failed",
                level="WARNING",
                message="outbox delivery failed; retry scheduled",
                data={
                    "outbox": outcome.get("id"),
                    "attempts": outcome.get("attempts"),
                    "next_at": outcome.get("next_at"),
                    "error": outcome.get("error"),
                },
            )


def _ingest_and_fastpath(root: Path, store: Store, now: float) -> None:
    """Ingest the spool, then run the fast path over the newly ingested ids.

    The snapshot of pending names is taken immediately before ``ingest``, so
    every file ingest can claim is a candidate; a file written after this
    listing stays pending for the next tick.
    """
    names = _pending_names(root)
    try:
        counts = spool.ingest(root, store)
    except Exception as exc:
        _event(root, "mailbox_ingest_failed", level="ERROR", message=str(exc))
        return
    if counts.get("ingested"):
        _event(
            root,
            "mailbox_ingested",
            message="spool envelopes ingested",
            data={"count": int(counts["ingested"])},
        )
    if counts.get("duplicates"):
        _event(
            root,
            "mailbox_duplicate",
            message="duplicate envelopes ignored",
            data={"count": int(counts["duplicates"])},
        )
    if counts.get("rejected"):
        _event(
            root,
            "mailbox_rejected",
            level="WARNING",
            message="invalid envelopes rejected",
            data={"count": int(counts["rejected"])},
        )
    if not names:
        return
    ctx = _fastpath_context(store, now)
    for name in names:
        message_id = name[: -len(".json")]
        if not message_id:
            continue
        try:
            row = store.get_message(message_id)
        except Exception:
            continue
        if not row:
            continue
        # Only a fresh, untouched message is fast-pathed; a duplicate spool
        # file whose message already progressed is never re-applied.
        if row.get("state") != "queued" or int(row.get("attempts") or 0):
            continue
        envelope = _claimed_envelope(root, name)
        if envelope is None:
            continue
        try:
            fastpath.apply(store, envelope, ctx)
        except Exception as exc:
            _event(
                root,
                "mailbox_fastpath_failed",
                level="WARNING",
                message=str(exc),
                data={"message": message_id},
            )


def _pending_names(root: Path) -> list[str]:
    """Pending envelope file names, cheaply listed; ``.tmp`` never counts."""
    try:
        return sorted(
            path.name
            for path in spool_dir(root).iterdir()
            if path.is_file() and path.name.endswith(".json")
        )
    except OSError:
        return []


def _claimed_envelope(root: Path, name: str) -> dict | None:
    """The validated envelope of one spool file, wherever ingest put it."""
    for path in (spool_dir(root) / name, ingested_dir(root) / name):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:
            return validate_envelope(raw)
        except MailboxError:
            return None
    return None


def _fastpath_context(store: Store, now: float) -> dict:
    try:
        tickets = store.tickets()
    except Exception:
        tickets = []
    try:
        doorbell = store.kv_get(DOORBELL_STATE_KEY) or "none"
    except Exception:
        doorbell = "none"
    try:
        counts = store.counts(now=now) or {}
    except Exception:
        counts = {}
    try:
        dead = store.dead_count()
    except Exception:
        dead = 0
    return {
        "now": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "tickets": tickets,
        "doorbell": doorbell,
        "queued_router": int(counts.get("router") or 0),
        "counts": counts,
        "dead": dead,
    }


# ---------------------------------------------------------------------------
# Production hooks


def hook_env() -> dict[str, str]:
    """The environment a hook subprocess runs in: ours, minus our identity.

    ``OSW_ROLE``/``OSW_ROLE_TOKEN`` would make the child OSW CLI refuse its own
    ``new``/``use``/``stop`` with ``router_forbidden`` (and dead-letter the
    launch), and ``ORCA_TERMINAL_HANDLE`` would name our terminal as the
    caller's. The child gets neither.
    """
    env = dict(os.environ)
    for key in STRIPPED_ENV_KEYS:
        env.pop(key, None)
    return env


class CLIHooks:
    """Dispatcher hooks that run the OSW CLI and Orca reply as subprocesses."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.env = hook_env()

    async def launch(self, ticket: dict, payload: dict) -> dict:
        payload = payload or {}
        provider = str(
            payload.get("provider")
            or _ticket_field(ticket, "provider")
            or "claude"
        )
        prompt = _prompt(payload, ticket)
        if not prompt:
            raise RuntimeError("launch needs a prompt")
        argv = self._osw_argv("new", provider)
        argv += _option("--prefix", payload.get("prefix"))
        argv += _option("--model", payload.get("model"))
        argv += _option("--thinking", payload.get("thinking"))
        argv += _notify_args(ticket)
        argv.append(prompt)
        text = await self._run_cli(argv)
        return _parse_created(text)

    async def followup(self, ticket: dict, payload: dict) -> None:
        payload = payload or {}
        target = str(
            payload.get("agent_id")
            or payload.get("target")
            or _ticket_target(ticket)
        )
        if not target:
            raise RuntimeError("followup needs an agent id or terminal handle")
        prompt = _prompt(payload, ticket)
        if not prompt:
            raise RuntimeError("followup needs a prompt")
        argv = self._osw_argv("use")
        argv += _notify_args(ticket)
        argv += [target, prompt]
        await self._run_cli(argv)

    async def stop(self, ticket: dict) -> None:
        target = _ticket_target(ticket)
        if not target:
            raise RuntimeError("stop needs an agent id")
        await self._run_cli(self._osw_argv("stop", target))

    async def answer(self, ticket: dict, payload: dict) -> None:
        payload = payload or {}
        ask = str(payload.get("ask") or payload.get("ask_id") or "")
        # The Dispatcher hands us the fixed template; it is the only
        # OSW-authored worker input, so prefer it over the raw text.
        text = str(
            payload.get("template")
            or payload.get("text")
            or payload.get("answer")
            or ""
        )
        if not ask or not text:
            raise RuntimeError("answer needs an ask id and text")
        await run_orca(
            "orchestration", "reply", "--id", ask, "--body", text,
        )

    async def notify(self, conversation: str, line: str) -> None:
        """Send one single-line notification to a transport address.

        A transport conversation is ``terminal:<handle>`` or
        ``session:<id>``. A logical role (``human``/``router``) is relayed
        through the registered Router session, the only live channel a
        notification may use (spec §14).
        """
        address = str(conversation or "")
        if address.startswith("terminal:"):
            await terminal_send(address.split(":", 1)[1], line)
            return
        if address.startswith("session:"):
            session_id = address.split(":", 1)[1]
            await orchestration_send(
                f"orca_session_id:{session_id}", "# [osw] mail", line,
            )
            return
        if address in ("", "human", "router"):
            relay = self._router_address()
            if not relay:
                raise RuntimeError("no router binding to relay a human notice")
            await self.notify(relay, line)
            return
        raise RuntimeError(f"unknown conversation: {address}")

    def _router_address(self) -> str:
        from .paths import db_path
        from .store import Store

        try:
            store = Store(db_path(self.root))
            try:
                binding = store.router_binding()
            finally:
                store.close()
        except Exception:
            return ""
        if not isinstance(binding, dict):
            return ""
        return _normalize_address(
            str(binding.get("address") or binding.get("transport") or "")
        )

    def _osw_argv(self, *args: str) -> list[str]:
        return [
            sys.executable,
            str(self.root / "skill" / "osw.py"),
            *args,
        ]

    async def _run_cli(self, argv: list[str]) -> str:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            **hidden_subprocess_kwargs(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), CLI_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"osw command timed out: {' '.join(argv[2:])}"
            )
        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace").strip()
        if proc.returncode != 0:
            detail = err or out.strip()
            raise RuntimeError(
                f"osw exited {proc.returncode}: {detail[:500]}"
            )
        return out


def _parse_created(text: str) -> dict:
    match = CREATED_RE.search(text or "")
    if not match:
        raise RuntimeError(
            "could not parse 'Created' from osw new output: "
            f"{text[-300:]}"
        )
    dispatch = DISPATCH_RE.search(text or "")
    return {
        "agent_id": match.group(1),
        "terminal": match.group(2),
        "dispatch_id": dispatch.group(1) if dispatch else "",
    }


def _option(flag: str, value: Any) -> list[str]:
    text = "" if value is None else str(value)
    return [flag, text] if text else []


def _notify_args(ticket: dict) -> list[str]:
    terminal = _origin_terminal(ticket)
    return ["--caller-terminal", terminal] if terminal else ["--no-notify"]


def _origin_terminal(ticket: dict) -> str:
    """The ticket origin's terminal transport, or "".

    Ticket origin is ``{"role", "id", "transport", "text"}``; transport is
    either ``{"terminal": "<handle>"}`` or an adress string.
    """
    if not isinstance(ticket, dict):
        return ""
    origin = ticket.get("origin")
    if not isinstance(origin, dict):
        return ""
    transport = origin.get("transport")
    if isinstance(transport, dict):
        terminal = str(transport.get("terminal") or "")
        if terminal:
            return _bare_terminal(terminal)
    if isinstance(transport, str) and transport.startswith("terminal:"):
        return _bare_terminal(transport)
    # Dispatcher tickets carry the sender's envelope id: "terminal:<handle>".
    origin_id = str(origin.get("id") or "")
    if origin_id.startswith("terminal:"):
        return _bare_terminal(origin_id)
    return _bare_terminal(str(origin.get("terminal") or ""))


def _bare_terminal(value: str) -> str:
    value = value.strip()
    if value.startswith("terminal:"):
        return value.split(":", 1)[1]
    return "" if value.startswith("session:") else value


def _prompt(payload: dict, ticket: dict) -> str:
    for key in ("prompt", "text", "task", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    origin = ticket.get("origin") if isinstance(ticket, dict) else None
    if isinstance(origin, dict):
        for key in ("text", "prompt"):
            value = origin.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _ticket_field(ticket: dict, key: str) -> str:
    if not isinstance(ticket, dict):
        return ""
    value = ticket.get(key)
    if isinstance(value, str) and value:
        return value
    for assignment in reversed(_assignments(ticket)):
        if isinstance(assignment, dict):
            value = assignment.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _ticket_target(ticket: dict) -> str:
    if not isinstance(ticket, dict):
        return ""
    for assignment in reversed(_assignments(ticket)):
        if isinstance(assignment, dict):
            for key in ("agent_id", "terminal"):
                value = assignment.get(key)
                if isinstance(value, str) and value:
                    return value
    for key in ("agent_id", "terminal"):
        value = ticket.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _assignments(ticket: dict) -> list:
    rows = ticket.get("assignments") if isinstance(ticket, dict) else None
    return rows if isinstance(rows, list) else []


# ---------------------------------------------------------------------------
# Small helpers


def _read_lock(root: Path) -> dict | None:
    try:
        raw = lock_path(root).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _lock_pid(lock: dict) -> int:
    try:
        return int(lock.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _event(
    root: Path,
    event: str,
    *,
    level: str = "INFO",
    message: str = "",
    data: dict | None = None,
) -> None:
    try:
        emit_event(
            logs_dir(Path(root)),
            component="mailbox",
            event=event,
            level=level,
            message=message,
            data=data or {},
        )
    except Exception:
        pass

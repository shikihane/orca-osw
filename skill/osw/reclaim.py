"""Terminal reclamation: which settled agent's pane may be closed.

`new` and `revive` give every agent its own Orca terminal, and a finished
agent's pane holds nothing its record does not: the record keeps the
provider session id, so `revive` resumes that session in a fresh pane, and
the report and handoff files keep the turn's evidence.

The watcher never closes a pane: a finished turn keeps its terminal until a
human or the caller runs `osw del <agent> --close` (the completion line
carries that command) or `osw sweep`, which applies this policy to whatever
is already open (`--force` widens it to failed turns). Closing a pane means
closing it explicitly, and the policy is:

* only a pane OSW created (`origin == "new"`) is reclaimable,
* only a settled record qualifies (`done`; `--force` adds `error`/`lost`),
* a `use`/`enroll` record adopted a pane a human owns,
* a supervised record's terminal belongs to Orca's dispatch plane,
* a `guard`/`revive` turn is a pane a human just reopened.

One policy (`decision`) and one closer (`close_if_open`) are shared by
`osw sweep` and `del --close`, so records stay the only thing deleted.
"""

from __future__ import annotations

from pathlib import Path

from . import hosts
from .orca_cli import OrcaError, terminal_close, terminal_list

# Only a pane OSW created itself may be reclaimed.
CREATED_ORIGINS = frozenset({"new"})
SETTLED_STATES = frozenset({"done", "error", "lost"})


def decision(
    agent: dict, *, human_turn: bool = False, force: bool = False,
) -> tuple[bool, str]:
    """May this agent's pane be closed? ``(allowed, reason)``; ``""`` allows.

    ``force`` widens the settled set to failed turns (``osw sweep --force``).
    ``human_turn`` marks a ``guard``/``revive`` turn, whose pane a human
    reopened on purpose.
    """
    state = str(agent.get("state") or "")
    if state not in SETTLED_STATES:
        return False, "not_settled"
    if state != "done" and not force:
        return False, "not_clean"
    if str(agent.get("origin") or "") not in CREATED_ORIGINS:
        return False, "adopted_pane"
    if str(agent.get("launch_mode") or "") == "supervised":
        # Orca owns a dispatch terminal: `del --close` releases the worker
        # first, and closing the pane behind Orca's bookkeeping is not ours.
        return False, "supervised"
    if human_turn:
        return False, "human_turn"
    if not str(agent.get("terminal") or "") \
            and not str(agent.get("pane_key") or agent.get("paneKey") or ""):
        return False, "no_terminal"
    return True, ""


def pane_terminal(terminals: list[dict], pane_key: str) -> dict | None:
    """The listed terminal currently occupying ``pane_key``, if any."""
    if not pane_key:
        return None
    return next(
        (t for t in terminals
         if f"{t.get('tabId')}:{t.get('leafId')}" == pane_key and t.get("handle")),
        None,
    )


def open_terminal(terminals: list[dict], agent: dict) -> dict | None:
    """The listed terminal for this record: pane identity first, handle second.

    Handles die with the Orca runtime, so the stable tabId:leafId is asked
    first; a record without a pane key falls back to its handle.
    """
    pane = str(agent.get("pane_key") or agent.get("paneKey") or "")
    handle = str(agent.get("terminal") or "")
    found = pane_terminal(terminals, pane)
    if found is not None:
        return found
    if not handle:
        return None
    return next((t for t in terminals if t.get("handle") == handle), None)


async def list_open(
    root: Path, host: hosts.Host | None = None,
) -> list[dict]:
    """Terminals Orca has in this project's worktree on ``host``."""
    host = host or hosts.local_host()
    return await terminal_list(host.worktree_selector(str(root)), host=host)


async def close_if_open(
    root: Path,
    agent: dict,
    host: hosts.Host | None = None,
    terminals: list[dict] | None = None,
) -> bool:
    """Close the record's terminal if it is still listed; True if closed.

    A listing failure raises ``OrcaError``; a pane that is already gone is
    not an error, so the caller can treat this as idempotent. ``terminals``
    is a pre-listed snapshot, for a caller sweeping many records at once.
    """
    host = host or hosts.local_host()
    if terminals is None:
        terminals = await list_open(root, host)
    terminal = open_terminal(terminals, agent)
    if terminal is None:
        return False
    await terminal_close(str(terminal["handle"]), host=host)
    return True


__all__ = [
    "CREATED_ORIGINS", "SETTLED_STATES", "close_if_open", "decision",
    "list_open", "open_terminal", "pane_terminal",
]

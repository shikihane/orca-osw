"""Which Orca pane dispatched this command, when the environment is silent.

A dispatcher running as an OMP/DSH agent pane calls OSW through its Bash
tool: stdin and stdout are pipes, and Orca exports neither
``ORCA_TERMINAL_HANDLE`` nor ``ORCA_AGENT_SESSION_ID`` into that child.
The record then carries no caller, and the completion notice is dropped.

Orca's hook server, however, keeps the last status every agent pane
reported, and that cache names the pane, its worktree, its agent type and
its provider session. The cache is read once per process generation by
``hookcache``; this module only filters it, so inference costs no Orca
round-trip and no terminal listing.

The cache also contains the panes OSW created for its own workers, so the
inference removes every recorded pane. The answer is returned only when
exactly one candidate is left: a wrong caller sends the completion line to
the wrong pane, which is worse than leaving the caller unresolved and
keeping the existing ``--caller-terminal`` requirement.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import hookcache, hosts, state


def _normalize(path: object) -> str:
    """Compare worktree paths the way the filesystem does."""
    return os.path.normcase(os.path.normpath(str(path)))


def _pane_key(entry: object) -> str:
    """The stable ``tabId:leafId`` identity of a cache entry."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(
            entry.get("paneKey") or entry.get("pane_key") or ""
        ).strip()
    return ""


def _worktree(entry: object, pane_key: str) -> str:
    """The worktree path a cache entry belongs to, or ""."""
    if isinstance(entry, dict):
        for key in ("worktree_path", "worktreePath", "worktreeId"):
            if key in entry:
                value = str(entry.get(key) or "")
                # Orca keys worktrees as ``<uuid>::<absolute path>``.
                if "::" in value:
                    value = value.split("::", 1)[1]
                return value
    try:
        return str(hookcache.worktree_path(pane_key) or "")
    except Exception:
        return ""


def _agent_type(entry: object, pane_key: str) -> str:
    if isinstance(entry, dict):
        for key in ("agent_type", "agentType"):
            if key in entry:
                return str(entry.get(key) or "").strip()
    try:
        return str(hookcache.agent_type(pane_key) or "").strip()
    except Exception:
        return ""


def _session_id(entry: object, pane_key: str) -> str:
    if isinstance(entry, dict):
        for key in ("session_id", "sessionId", "providerSessionId"):
            if key in entry:
                return str(entry.get(key) or "").strip()
        session = entry.get("providerSession")
        if isinstance(session, dict):
            return str(session.get("id") or "").strip()
    try:
        return str(hookcache.session_id(pane_key) or "").strip()
    except Exception:
        return ""


def _handle(entry: object) -> str:
    if isinstance(entry, dict):
        return str(entry.get("handle") or "")
    return ""


async def infer_caller_pane(
    root: Path, host: hosts.Host | None = None,
) -> dict:
    """The one Orca agent pane in this worktree that is not an OSW agent.

    Returns ``{"handle": str, "pane_key": str, "title": str}``; ``{}`` when
    the answer would be a guess. ``handle`` is empty unless the cache entry
    carries one; callers that need a handle resolve it from ``pane_key``.
    Read-only: no record writes, never raises ``OrcaError``.
    """
    try:
        target = _normalize(root)
        # A pane OSW created is never the dispatcher, whichever machine it
        # runs on. Records live locally, so one read covers every host.
        osw_panes = {
            str(agent.get("pane_key") or agent.get("paneKey") or "")
            for agent in state.list_agents(root).values()
        }
        osw_panes.discard("")
        cache = hookcache.entries()
        # The cache maps paneKey -> entry; a bare iterable of entries (or of
        # pane keys) is accepted too, so the caller never assumes one shape.
        stream = cache.items() if isinstance(cache, dict) else (
            (None, entry) for entry in cache
        )
        candidates: list[dict] = []
        seen: set[str] = set()
        for keyed, entry in stream:
            pane_key = _pane_key(entry) or str(keyed or "").strip()
            if not pane_key or pane_key in seen:
                continue
            seen.add(pane_key)
            if _normalize(_worktree(entry, pane_key)) != target:
                continue
            if pane_key in osw_panes:
                continue
            agent_type = _agent_type(entry, pane_key)
            session_id = _session_id(entry, pane_key)
            if not agent_type or not session_id:
                continue
            candidates.append({
                "handle": _handle(entry),
                "pane_key": pane_key,
                "title": f"{agent_type} {session_id}",
            })
    except Exception:  # inference is best-effort, never fatal
        return {}
    return candidates[0] if len(candidates) == 1 else {}


__all__ = ["infer_caller_pane"]

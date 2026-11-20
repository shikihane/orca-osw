"""Orca's agent-hook cache: which pane runs which agent, and where.

Orca's hook server persists the last status every agent pane reported.
Older releases kept that in ``last-status.json`` (``version: 2``, keyed by
``paneKey``); Orca 1.4.265 replaced it with ``last-status.db``:

    meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)   -- schema_version
    rows(key TEXT PRIMARY KEY, seq INTEGER NOT NULL, value TEXT NOT NULL)
        key    "entry<tabId>:<leafId>"
        value  one JSON object: paneKey, tabId, worktreeId ("<uuid>::<path>"),
               providerSession{key, id, transcriptPath?},
               payload{state, agentType, ...}, receivedAt, ...

Neither file is a public Orca interface, so a cache OSW cannot read
(missing, corrupt, unknown ``schema_version``) reads as empty and this
module falls back to the legacy JSON sibling rather than raising.

Caching rule: one process opens the database at most once per cache
generation. A generation is the pair ``(st_mtime_ns, st_size)`` of the
file, keyed by its path, so repeated lookups against an unchanged file —
the common case for a watcher polling thirty agents — cost a ``stat``
and a dict lookup, never a new connection or query. Thirty watchers on
one machine therefore cost thirty opens at most per file change, one
each. Nothing here locks, writes, or shells out to the ``orca`` CLI.

Sources of truth, in the order `entries` tries them:

1. the SQLite cache (``last-status.db``) for any ``schema_version`` this
   module knows (``>= SQLITE_SCHEMA_VERSION``);
2. the legacy ``last-status.json`` beside it, ``version == 2`` only.

A ``.json`` path names the legacy file explicitly and skips the database.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# The layout Orca 1.4.265 writes. Both tables OSW reads have only gained
# columns since, so this value and anything newer are accepted; anything
# older (or unparseable) reads as empty and the caller falls back.
SQLITE_SCHEMA_VERSION = 202

# The JSON cache older Orca releases wrote, and the `version` value its
# entries are keyed under.
LEGACY_JSON_VERSION = 2


@dataclass(frozen=True)
class _Generation:
    """One parsed cache, pinned to the file states it was read from."""

    database: tuple[int, int] | None
    database_used: bool
    legacy: tuple[int, int] | None
    entries: dict[str, dict]


# path -> the generation read from that path. At most one entry per path;
# `reset_cache` drops them all.
_CACHE: dict[str, _Generation] = {}


def default_path() -> Path:
    """Default location of Orca's SQLite hook cache."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "orca" / "agent-hooks" / "last-status.db"


def entries(path: Path | None = None) -> dict[str, dict]:
    """Every pane Orca last recorded, keyed by ``paneKey``.

    Empty when neither cache is readable. The returned mapping is a copy;
    the entry dicts inside it are the parsed ones, treated as read-only.
    """
    return dict(_entries(path))


def session_id(pane_key: str, path: Path | None = None) -> str:
    """The provider session id Orca recorded for a pane, or ""."""
    entry = _entry(pane_key, path)
    if not entry:
        return ""
    session = entry.get("providerSession")
    value = session.get("id") if isinstance(session, dict) else None
    if isinstance(value, str) and value:
        return value
    # A binding-only row carries the agent's presence instead of a
    # status report: {"agentPresence": {"agent": "omp", "session": id}}.
    presence = entry.get("agentPresence")
    session = presence.get("session") if isinstance(presence, dict) else None
    return session if isinstance(session, str) else ""


def agent_type(pane_key: str, path: Path | None = None) -> str:
    """The agent type ("omp", "dsh", ...) of a pane, lowercased, or ""."""
    entry = _entry(pane_key, path)
    payload = entry.get("payload") if entry else None
    value = payload.get("agentType") if isinstance(payload, dict) else None
    return value.lower() if isinstance(value, str) else ""


def worktree_path(pane_key: str, path: Path | None = None) -> str:
    """The worktree a pane belongs to, or "".

    Orca keys worktrees as ``<uuid>::<absolute path>``; the path is
    everything after the first separator, untouched (Windows remotes
    report ``E:\\bit\\x`` and ``E:/bit/x`` in different releases).
    """
    entry = _entry(pane_key, path)
    value = entry.get("worktreeId") if entry else None
    if not isinstance(value, str) or "::" not in value:
        return ""
    return value.split("::", 1)[1]


def legacy_path(path: Path | None = None) -> Path:
    """The legacy JSON cache a lookup for `path` falls back to."""
    return _sources(path)[1]


def reset_cache() -> None:
    """Forget every parsed generation, so the next lookup re-reads."""
    _CACHE.clear()


def _entry(pane_key: str, path: Path | None) -> dict | None:
    """The cached entry for one pane, without copying the mapping."""
    if not pane_key:
        return None
    return _entries(path).get(pane_key)


def _entries(path: Path | None) -> dict[str, dict]:
    """The cached mapping itself, reused until a file changes."""
    database, legacy = _sources(path)
    key = str(database or legacy)
    database_sig = _signature(database)
    cached = _CACHE.get(key)
    if cached is not None and cached.database == database_sig:
        if cached.database_used:
            return cached.entries
        if cached.legacy == _signature(legacy):
            return cached.entries

    loaded = _read_sqlite(database) if database_sig is not None else None
    if loaded is not None:
        generation = _Generation(database_sig, True, None, loaded)
    else:
        legacy_sig = _signature(legacy)
        generation = _Generation(
            database_sig,
            False,
            legacy_sig,
            _read_legacy(legacy) if legacy_sig is not None else {},
        )
    _CACHE[key] = generation
    return generation.entries


def _sources(path: Path | None) -> tuple[Path | None, Path]:
    """The (sqlite, legacy json) pair a lookup reads, for `path`."""
    if path is None:
        database = default_path()
        return database, database.with_suffix(".json")
    candidate = Path(path)
    if candidate.suffix.lower() == ".json":
        return None, candidate
    return candidate, candidate.with_suffix(".json")


def _signature(path: Path | None) -> tuple[int, int] | None:
    """`(mtime_ns, size)` of `path`, or None when it cannot be stat'ed."""
    if path is None:
        return None
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _uri(path: Path) -> str:
    """A read-only SQLite URI for `path`."""
    try:
        return path.resolve().as_uri() + "?mode=ro"
    except ValueError:
        return "file:" + path.as_posix() + "?mode=ro"


def _read_sqlite(path: Path) -> dict[str, dict] | None:
    """Every entry in the SQLite cache, or None when it is unusable.

    None means "fall back to the legacy JSON"; ``{}`` means this cache is
    readable and authoritative but holds nothing.
    """
    try:
        connection = sqlite3.connect(_uri(path), uri=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        try:
            version = int(row[0]) if row else -1
        except (TypeError, ValueError):
            return None
        if version < SQLITE_SCHEMA_VERSION:
            return None
        found: dict[str, dict] = {}
        for (value,) in connection.execute(
            "SELECT value FROM rows WHERE key LIKE 'entry%'"
        ):
            entry = _parse_entry(value)
            if entry is not None:
                found[str(entry.get("paneKey"))] = entry
        return found
    except (sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        connection.close()


def _parse_entry(value: object) -> dict | None:
    """One row's JSON object, or None when it is not a usable entry."""
    if not isinstance(value, str):
        return None
    try:
        entry = json.loads(value)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    pane_key = entry.get("paneKey")
    if not isinstance(pane_key, str) or not pane_key:
        return None
    return entry


def _read_legacy(path: Path) -> dict[str, dict]:
    """Entries of a legacy JSON cache, empty for anything else."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict) or data.get("version") != LEGACY_JSON_VERSION:
        return {}
    cached = data.get("entries")
    if not isinstance(cached, dict):
        return {}
    return {
        pane_key: entry
        for pane_key, entry in cached.items()
        if isinstance(pane_key, str) and isinstance(entry, dict)
    }


__all__ = [
    "LEGACY_JSON_VERSION",
    "SQLITE_SCHEMA_VERSION",
    "agent_type",
    "default_path",
    "entries",
    "legacy_path",
    "reset_cache",
    "session_id",
    "worktree_path",
]

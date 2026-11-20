from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from osw import hookcache, hosts


# Orca's hook server persists the last status each agent reported. Older
# releases wrote one JSON document (`{version, entries: {<paneKey>:
# {providerSession: {id, transcriptPath?}, payload, receivedAt, ...}}}`);
# Orca 1.4.265 replaced it with the SQLite cache `hookcache` reads. Neither
# is a public Orca interface, so any version OSW does not understand reads
# as "no session".
LAST_STATUS_VERSION = hookcache.LEGACY_JSON_VERSION

# `meta.schema_version` of the SQLite cache; anything at or above it is
# read, anything below it falls back to the JSON document above.
HOOK_CACHE_SCHEMA_VERSION = hookcache.SQLITE_SCHEMA_VERSION

# Orca's launch-time identity database, beside the status cache: one row
# per agent launch linking the Orca session id to the provider's own.
IDENTITIES_FILENAME = "agent-identities.db"

# path -> ((mtime_ns, size), {orca_session_id: provider_session_id}).
_LINKED_CACHE: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}

# Where a POSIX remote keeps the same cache, relative to the host home.
# Orca's own directory casing moved between releases (and its XDG data
# dir is the third shape), so every observed layout is probed in order.
SESSION_CACHE_CANDIDATES = (
    ".config/orca/agent-hooks/last-status.json",
    ".config/Orca/agent-hooks/last-status.json",
    ".local/share/orca/agent-hooks/last-status.json",
)

# A Windows remote keeps Orca's hook cache under the roaming profile, so
# its layout is a single path and no POSIX candidate can match there.
WINDOWS_SESSION_CACHE_CANDIDATES = (
    "AppData/Roaming/orca/agent-hooks/last-status.json",
)


def session_cache_candidates(host: hosts.Host) -> tuple[str, ...]:
    """The cache layouts to probe on `host`, by its platform."""
    if host.is_windows:
        return WINDOWS_SESSION_CACHE_CANDIDATES
    return SESSION_CACHE_CANDIDATES


def last_status_path() -> Path:
    """Default location of Orca's agent-hook status cache."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "orca" / "agent-hooks" / "last-status.json"


def parse_last_status(text: str, pane_key: str) -> str:
    """The provider session id in a status cache, or "" when absent.

    Empty for unparseable JSON, a version OSW does not understand, a
    pane with no entry (an agent only gets one after its first status
    report), or an entry without a session id.
    """
    if not pane_key:
        return ""
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(data, dict) or data.get("version") != LAST_STATUS_VERSION:
        return ""
    entries = data.get("entries")
    entry = entries.get(pane_key) if isinstance(entries, dict) else None
    session = entry.get("providerSession") if isinstance(entry, dict) else None
    session_id = session.get("id") if isinstance(session, dict) else None
    return session_id if isinstance(session_id, str) else ""


def session_for_pane(
    pane_key: str, path: Path | None = None, orca_session_id: str = "",
) -> str:
    """Return the provider session id Orca recorded for a pane, or "".

    Three sources, in the order Orca itself layers them. The per-pane
    status cache (SQLite, then the legacy JSON) answers for the agents
    that report status there — omp does, pi and claude do not. For those
    the identity link is the only record: `agent-identities.db` maps the
    pane's `orcaSessionId` to its provider session id, and the caller
    passes that id in from `worktree ps`. Empty when no source answers.
    """
    session = hookcache.session_id(pane_key, path)
    if session:
        return session
    if orca_session_id:
        linked = linked_session(orca_session_id, path)
        if linked:
            return linked
    try:
        text = hookcache.legacy_path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    return parse_last_status(text, pane_key)


def identities_path(path: Path | None = None) -> Path:
    """Orca's identity database, beside the status cache."""
    anchor = path or hookcache.default_path()
    return anchor.with_name(IDENTITIES_FILENAME)


def linked_session(orca_session_id: str, path: Path | None = None) -> str:
    """The provider session id linked to an Orca session id, or "".

    `agent-identities.db` is a plain SQLite file with one row per agent
    launch (`provider_links`: execution_host, wsl_distro, agent,
    provider_session_id, locator, orca_session_id). Like the status
    cache it is not a public interface, so a missing, unreadable or
    unexpected database reads as "no link". One read per file change is
    cached for the whole process.
    """
    if not orca_session_id:
        return ""
    target = identities_path(path)
    try:
        info = target.stat()
    except OSError:
        return ""
    signature = (info.st_mtime_ns, info.st_size)
    key = str(target)
    cached = _LINKED_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1].get(orca_session_id, "")
    links: dict[str, str] = {}
    try:
        connection = sqlite3.connect(
            target.resolve().as_uri() + "?mode=ro", uri=True
        )
    except (sqlite3.Error, ValueError):
        return ""
    try:
        for orca_id, provider_id in connection.execute(
            "SELECT orca_session_id, provider_session_id FROM provider_links"
        ):
            if isinstance(orca_id, str) and isinstance(provider_id, str):
                links[orca_id] = provider_id
    except sqlite3.Error:
        return ""
    finally:
        connection.close()
    _LINKED_CACHE[key] = (signature, links)
    return links.get(orca_session_id, "")



async def session_for_agent(
    host: hosts.Host, pane_key: str, root: Path | None = None,
) -> str:
    """The provider session id `host` recorded for one of its panes, or "".

    One entry point for the CLI's stop/revive/enroll paths, so no call
    site re-decides how to read a cache. A local host reads the status
    file directly, where absence is the ordinary "no session yet". A
    remote host goes through `session_on_host`: a missing cache there
    raises `HostError(remote_session_unknown)` naming the paths probed,
    which call sites surface as-is.
    """
    if host.is_local:
        return session_for_pane(pane_key)
    return await session_on_host(host, pane_key, root)


async def session_cache_path(host: hosts.Host, root: Path | None = None) -> str:
    """The status cache location on this host.

    Probe order is fixed and the winner is remembered on the host record,
    so a remote pays for the search once and every later call is local
    bookkeeping. `root` is the repo holding the host registry; without
    it the result is not persisted.
    """
    if host.session_cache:
        return host.session_cache
    if host.is_local:
        # Both caches live side by side locally; the SQLite one is the
        # current shape, so it wins when it exists.
        database = hookcache.default_path()
        return str(database if database.exists() else last_status_path())
    tried = [
        host.join(host.home, rel) for rel in session_cache_candidates(host)
    ]
    for path in tried:
        if await host.exists(path):
            host.session_cache = path
            if root is not None:
                hosts.save_host(root, host)
            return path
    raise hosts.HostError(
        hosts.REMOTE_SESSION_UNKNOWN,
        f"{host.name}: no Orca agent-hook status cache; looked for "
        + ", ".join(tried),
        host=host.name,
    )


async def session_on_host(
    host: hosts.Host, pane_key: str, root: Path | None = None,
) -> str:
    """The provider session id that host recorded for a pane, or "".

    Remote transport failures are named errors: a missing cache means
    the workspace has no resolvable session, not "no session yet".
    """
    path = await session_cache_path(host, root)
    if host.is_local:
        # Local absence is the ordinary first-launch case, which
        # `session_for_pane` already reads as "no session yet".
        return session_for_pane(pane_key, path=Path(path))
    try:
        raw = await host.read_file(path)
    except hosts.HostError as exc:
        raise hosts.HostError(
            hosts.REMOTE_SESSION_UNKNOWN,
            f"{host.name}: cannot read session cache {path}",
            host=host.name,
            detail=exc.message,
        ) from exc
    return parse_last_status(raw.decode("utf-8", errors="replace"), pane_key)

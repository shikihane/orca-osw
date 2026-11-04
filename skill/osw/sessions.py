from __future__ import annotations

import json
import os
from pathlib import Path

from osw import hosts


# Orca's hook server persists the last status each agent reported, keyed
# by paneKey: `{version, entries: {<paneKey>: {providerSession: {id,
# transcriptPath?}, payload, receivedAt, ...}}}`. This is not a public
# Orca interface, so any other version reads as "no session".
LAST_STATUS_VERSION = 2

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


def session_for_pane(pane_key: str, path: Path | None = None) -> str:
    """Return the provider session id Orca recorded for a pane, or "".

    Empty when the file is missing or unreadable, its version is not the
    one OSW understands, or the pane has no entry.
    """
    try:
        text = (path or last_status_path()).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    return parse_last_status(text, pane_key)


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
        return str(last_status_path())
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

from __future__ import annotations

import json
import os
from pathlib import Path


# Orca's hook server persists the last status each agent reported, keyed
# by paneKey: `{version, entries: {<paneKey>: {providerSession: {id,
# transcriptPath?}, payload, receivedAt, ...}}}`. This is not a public
# Orca interface, so any other version reads as "no session".
LAST_STATUS_VERSION = 2


def last_status_path() -> Path:
    """Default location of Orca's agent-hook status cache."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "orca" / "agent-hooks" / "last-status.json"


def session_for_pane(pane_key: str, path: Path | None = None) -> str:
    """Return the provider session id Orca recorded for a pane, or "".

    Empty when the file is missing or unreadable, its version is not the
    one OSW understands, or the pane has no entry (an agent only gets
    one after its first status report).
    """
    if not pane_key:
        return ""
    try:
        data = json.loads((path or last_status_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict) or data.get("version") != LAST_STATUS_VERSION:
        return ""
    entries = data.get("entries")
    entry = entries.get(pane_key) if isinstance(entries, dict) else None
    session = entry.get("providerSession") if isinstance(entry, dict) else None
    session_id = session.get("id") if isinstance(session, dict) else None
    return session_id if isinstance(session_id, str) else ""

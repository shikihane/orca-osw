from __future__ import annotations

import shutil
import subprocess

# Candidate agent CLIs to probe for on PATH. This is a *scan list*,
# not configuration — nothing here ends up in state.json unless the
# operator explicitly assigns it to a tier.
KNOWN_AGENT_CLIS = [
    "claude",
    "codex",
    "pi",
    "gemini",
    "aider",
    "goose",
    "opencode",
    "cursor-agent",
    "qwen",
    "copilot",
]

_VERSION_TIMEOUT = 10


def scan_agent_clis(probe_version: bool = True) -> list[dict]:
    """Scan PATH for known agent CLIs.

    Returns a list of {"name", "path", "version"} for every CLI found.
    Version probing is best-effort (`<cli> --version`, short timeout).
    """
    found: list[dict] = []
    for name in KNOWN_AGENT_CLIS:
        path = shutil.which(name)
        if not path:
            continue
        entry = {"name": name, "path": path, "version": ""}
        if probe_version:
            entry["version"] = _probe_version(path)
        found.append(entry)
    return found


def _probe_version(path: str) -> str:
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            timeout=_VERSION_TIMEOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    output = (result.stdout or result.stderr or "").strip()
    return output.splitlines()[0][:80] if output else ""

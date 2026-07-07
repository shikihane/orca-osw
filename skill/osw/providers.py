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

# Suggested tier entries per provider, shown as a menu during
# `init --interactive`. These are CANDIDATES only — nothing is written
# unless the user explicitly picks one, and a custom-command escape
# hatch always exists.
PROVIDER_PRESETS: dict[str, list[dict]] = {
    "claude": [
        {"name": "claude-opus", "command": "claude --model opus"},
        {"name": "claude-sonnet", "command": "claude --model sonnet"},
        {"name": "claude-haiku", "command": "claude --model haiku"},
    ],
    "codex": [
        {"name": "codex-high", "command": "codex -c model_reasoning_effort=high"},
        {"name": "codex-medium", "command": "codex -c model_reasoning_effort=medium"},
        {"name": "codex-low", "command": "codex -c model_reasoning_effort=low"},
    ],
    "pi": [
        {"name": "pi-deepseek", "command": "pi --model deepseek"},
        {"name": "pi-kimi", "command": "pi --model kimi"},
    ],
}


def preset_options(found: list[dict]) -> list[dict]:
    """Build the selectable entries for the detected CLIs.

    CLIs with known presets contribute their variants; anything else
    contributes its bare command.
    """
    options: list[dict] = []
    for cli in found:
        presets = PROVIDER_PRESETS.get(cli["name"])
        if presets:
            options.extend(presets)
        else:
            options.append({"name": cli["name"], "command": cli["name"]})
    return options


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

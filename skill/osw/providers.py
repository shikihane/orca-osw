from __future__ import annotations

import re
import shutil
import subprocess

# Candidate agent CLIs to probe for on PATH. This is a *scan list*,
# not configuration — nothing ends up in state.json unless the
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

_PROBE_TIMEOUT = 15


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
            output = _run_capture([path, "--version"])
            entry["version"] = output.splitlines()[0][:80] if output else ""
        found.append(entry)
    return found


# ---------------------------------------------------------------------------
# Variant discovery — ask each CLI what it supports, never hardcode
# ---------------------------------------------------------------------------

def probe_variants(name: str, path: str | None = None) -> list[dict]:
    """Discover model variants by querying the CLI itself.

    Returns [{"name", "command"}] built from the CLI's own output
    (pi --list-models, claude --help aliases, ...). CLIs without a
    discovery interface return [] — the operator supplies arguments
    manually for those.
    """
    exe = path or shutil.which(name)
    if exe is None:
        return []
    if name == "pi":
        return parse_pi_models(_run_capture([exe, "--list-models"]))
    if name == "claude":
        return parse_claude_aliases(_run_capture([exe, "--help"]))
    return []


def parse_pi_models(output: str) -> list[dict]:
    """Parse the `pi --list-models` table into variant entries.

    Rows only count after the header line — anything else (error
    messages, banners) is ignored.
    """
    variants: list[dict] = []
    in_table = False
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "provider":
            in_table = True
            continue
        if not in_table or len(parts) < 2:
            continue
        provider, model = parts[0], parts[1]
        variants.append({
            "name": f"pi-{model}",
            "command": f"pi --model {provider}/{model}",
        })
    return variants


_CLAUDE_ALIAS_RE = re.compile(
    r"--model[\s\S]{0,400}?alias[\s\S]{0,200}?\(e\.g\.\s*([^)]*)\)"
)


def parse_claude_aliases(help_text: str) -> list[dict]:
    """Extract model aliases from claude --help's --model description."""
    match = _CLAUDE_ALIAS_RE.search(help_text)
    if not match:
        return []
    aliases = re.findall(r"'([a-z0-9.\-]+)'", match.group(1))
    return [
        {"name": f"claude-{a}", "command": f"claude --model {a}"}
        for a in aliases
        if not a.startswith("claude-")  # skip full-name examples
    ]


def _run_capture(args: list[str], timeout: int = _PROBE_TIMEOUT) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or result.stderr or "").strip()

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from osw.process import hidden_subprocess_kwargs

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
    """Discover model variants by querying the CLI or its local config.

    Returns [{"name", "command"}], never empty for an installed CLI:
    the CLI's bare command (its own defaults) is always a selectable
    option, so the operator picks instead of typing.
    """
    exe = path or shutil.which(name)
    if exe is None:
        return []
    bare = [{"name": f"{name}-default", "command": name}]
    if name == "pi":
        return parse_pi_models(_run_capture([exe, "--list-models"])) or bare
    if name == "claude":
        return parse_claude_aliases(_run_capture([exe, "--help"])) or bare
    if name == "codex":
        return bare + codex_variants(_read_codex_config())
    return bare


# The valid values of codex's model_reasoning_effort config key.
# These are CLI protocol constants (like its sandbox modes), not model
# data — codex exposes no way to enumerate them at runtime.
CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def _read_codex_config() -> dict:
    """Read the user's ~/.codex/config.toml (real per-machine data)."""
    path = Path.home() / ".codex" / "config.toml"
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def codex_variants(config: dict) -> list[dict]:
    """Build codex entries: the configured model x reasoning efforts."""
    model = str(config.get("model") or "").strip()
    label = model or "model"
    variants = []
    for effort in CODEX_EFFORTS:
        command = f"codex -c model_reasoning_effort={effort}"
        if model:
            command += f' -m {model}'
        variants.append({
            "name": f"codex-{label}-{effort}",
            "command": command,
        })
    return variants


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
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or result.stderr or "").strip()

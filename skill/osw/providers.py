from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
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
    "omp",
    "kimi",
    "gemini",
    "aider",
    "goose",
    "opencode",
    "cursor-agent",
    "qwen",
    "copilot",
]

_PROBE_TIMEOUT = 15


def _quote_command_arg(value: str) -> str:
    return subprocess.list2cmdline([value])


SUPPORTED_PROVIDERS = ("claude", "codex", "pi", "omp", "kimi")
PROVIDER_AUTONOMY_FLAGS = {
    "claude": ("--dangerously-skip-permissions",),
    "codex": ("--dangerously-bypass-approvals-and-sandbox",),
    "pi": ("--approve",),
    "omp": ("--auto-approve",),
    "kimi": ("--yolo",),
}


class ProviderError(ValueError):
    pass


def build_provider_command(
    provider: str,
    model: str | None = None,
    thinking: str | None = None,
) -> str:
    """Build the provider CLI command OSW runs in an Orca terminal.

    Never includes the task prompt: the watcher sends the prompt as a
    separate, observable turn once the TUI is ready.
    """
    provider = provider.strip()
    model = (model or "").strip() or None
    thinking = (thinking or "").strip() or None
    if provider not in SUPPORTED_PROVIDERS:
        expected = ", ".join(SUPPORTED_PROVIDERS)
        raise ProviderError(
            f"unsupported provider '{provider}' (expected one of: {expected})"
        )

    args = [provider, *PROVIDER_AUTONOMY_FLAGS[provider]]
    if provider == "codex":
        if thinking:
            args += ["-c", f"model_reasoning_effort={thinking}"]
        if model:
            args += ["-m", model]
    elif provider == "kimi":
        # kimi has no thinking CLI flag: effort lives per-model in its
        # config.toml, so a value here would be silently unusable.
        if thinking:
            raise ProviderError(
                "provider 'kimi' does not accept --thinking; "
                "effort is configured per model alias in kimi's config.toml"
            )
        if model:
            args += ["--model", model]
    else:
        if model:
            args += ["--model", model]
        if thinking:
            flag = "--effort" if provider == "claude" else "--thinking"
            args += [flag, thinking]
    return " ".join(_quote_command_arg(arg) for arg in args)


def ensure_workspace_trust(provider: str, root: Path) -> str:
    """Pre-record workspace trust so the provider TUI skips its
    interactive "trust this directory?" prompt on first launch.

    Best-effort and idempotent: returns a short note of what was done
    ("" for providers without a known trust store), and never raises —
    a failed write just means the prompt appears as before.
    """
    try:
        if provider == "claude":
            return _trust_claude(root)
        if provider == "codex":
            return _trust_codex(root)
        if provider == "kimi":
            return _trust_kimi(root)
    except OSError:
        return "trust pre-record failed"
    return ""


def _trust_claude(root: Path) -> str:
    """claude keeps per-project trust in ~/.claude.json as
    projects["<posix path>"].hasTrustDialogAccepted."""
    path = Path.home() / ".claude.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    projects = data.setdefault("projects", {})
    entry = projects.setdefault(root.as_posix(), {})
    if entry.get("hasTrustDialogAccepted") is True:
        return "already trusted"
    entry["hasTrustDialogAccepted"] = True
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return "trusted"


def _trust_codex(root: Path) -> str:
    """codex keeps trust in ~/.codex/config.toml as
    [projects."<windows path>"] trust_level = "trusted"."""
    path = Path.home() / ".codex" / "config.toml"
    key = str(root).replace("\\", "\\\\").replace('"', '\\"')
    try:
        with path.open("rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        config = {}
    projects = config.get("projects")
    if isinstance(projects, dict):
        entry = projects.get(str(root))
        if isinstance(entry, dict) and entry.get("trust_level") == "trusted":
            return "already trusted"
    with path.open("a", encoding="utf-8") as f:
        f.write(f'\n[projects."{key}"]\ntrust_level = "trusted"\n')
    return "trusted"


def _trust_kimi(root: Path) -> str:
    """kimi keeps trust in ~/.kimi-code/workspace-trust/ as
    wd_<basename-lower>_<sha256(posix root)[:12]> JSON files."""
    posix_root = root.as_posix()
    digest = hashlib.sha256(posix_root.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9._-]+", "-", root.name.lower()).strip("-") or "root"
    path = Path.home() / ".kimi-code" / "workspace-trust" / f"wd_{slug}_{digest}"
    if path.exists():
        return "already trusted"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"root": posix_root, "trustedAt": int(time.time() * 1000)}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return "trusted"


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
    when discovery finds nothing, the CLI's bare command (its own
    defaults) is the selectable fallback, so the operator picks
    instead of typing. codex always lists bare first because its
    discovered variants pin a reasoning effort, never the defaults.
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
    if name == "omp":
        return parse_omp_models(
            _run_capture([exe, "models", "--json"])
        ) or bare
    if name == "kimi":
        return kimi_variants(_read_kimi_config()) or bare
    return bare


# The valid values of codex's model_reasoning_effort config key.
# These are CLI protocol constants (like its sandbox modes), not model
# data — codex exposes no way to enumerate them at runtime.
CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def _read_codex_config() -> dict:
    """Read the user's ~/.codex/config.toml (real per-machine data)."""
    try:
        path = Path.home() / ".codex" / "config.toml"
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, RuntimeError, tomllib.TOMLDecodeError):
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


def _read_kimi_config() -> dict:
    """Read the user's ~/.kimi-code/config.toml (real per-machine data)."""
    try:
        path = Path.home() / ".kimi-code" / "config.toml"
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, RuntimeError, tomllib.TOMLDecodeError):
        return {}


def kimi_variants(config: dict) -> list[dict]:
    """Build kimi entries from the model aliases in its config.toml.

    Aliases look like "kimi-code/k3"; the command needs the full alias,
    the display name only the tail segment — unless tails collide
    across providers, in which case the full alias keeps names unique.
    """
    models = config.get("models")
    if not isinstance(models, dict):
        return []
    aliases = [
        alias for alias, spec in models.items()
        if alias and isinstance(spec, dict)
    ]
    tails = [alias.split("/")[-1] for alias in aliases]
    variants = []
    for alias, tail in zip(aliases, tails):
        label = tail if tails.count(tail) == 1 else alias.replace("/", "-")
        variants.append({
            "name": f"kimi-{label}",
            "command": f"kimi --model {_quote_command_arg(alias)}",
        })
    return variants


def parse_omp_models(output: str) -> list[dict]:
    """Parse the machine-readable catalog from `omp models --json`."""
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []

    variants: list[dict] = []
    seen: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            continue
        selector = model.get("selector")
        if not isinstance(selector, str):
            continue
        selector = selector.strip()
        if not selector or selector in seen:
            continue
        label = re.sub(r"[^a-zA-Z0-9._-]+", "-", selector).strip("-")
        if not label:
            continue
        seen.add(selector)
        variants.append({
            "name": f"omp-{label}",
            "command": f"omp --model {_quote_command_arg(selector)}",
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

"""Deterministic readiness gate (design §5.0) and frame hygiene (§5.7).

Pure functions only: the watcher owns timing, polling and side effects.
The gate turns a rendered terminal frame into one of three verdicts:

- ``ready``     — the TUI sits at an idle composer and would accept input;
- ``not_ready`` — the frame is usable but shows startup or a busy turn;
- ``api_retry`` — the agent is waiting on its upstream API and will retry
  on its own; the pane is alive but a send now would just queue or stall,
  so the caller tolerates it (waits for recovery) instead of sending or
  failing;
- ``abstain``   — no verdict is possible (no rendered frame, provider
  without a predicate); the caller keeps today's behavior.

Only frames captured through ``run_orca`` (stdout bytes decoded as
UTF-8) are judged; a frame whose ``source`` is not ``screen`` abstains
(design §5.0.1).
"""

from __future__ import annotations

import hashlib
import re

GATE_PROVIDERS = ("claude", "kimi", "dsh")  # providers with a deterministic predicate
COMPOSER_WAIT_MS = 5000              # bounded composer wait, then today's behavior
GATE_POLL_SECS = 0.5                 # screen re-read interval during that wait
GATE_TAIL_LINES = 15                 # the predicate reads only the frame bottom

# Claude's status line while a turn runs; the composer stays visible
# mid-turn, so this marker is what separates busy from idle. Matches
# e.g. "✢ Proofing… (2m 55s · ↓ 4.1k tokens · thought for 1s)".
CLAUDE_WORKING_RE = re.compile(
    r"^\S\s+\S[^\n]*…\s*\((?:\d+h\s*)?(?:\d+m\s*)?\d+s\b", re.MULTILINE)

# Kept in sync with watcher.BLOCKING_DIALOG_MARKERS (avoids an import cycle).
DIALOG_MARKERS = ("Trust this folder",)

# Kept in sync with watcher.READY_PREVIEW_MARKERS.
KIMI_PREVIEW_MARKERS = ("one will be created on your first message",)

# DSH-TUI draws its launchpad composer before the harness finishes
# booting, and a prompt sent in that window is typed into the box and
# then dropped (observed: the text echoed once, Enter cleared it, no
# turn and no session started). The footer names the boot in the UI's
# own language.
DSH_BOOT_MARKERS = ("正在启动 DSH", "Starting DSH")

# Upstream-API retry notice (claude: "Waiting for API response · will
# retry · check your network"). The pane is alive and recovers on its
# own: a send now would queue or stall and the caller mistakes it for a
# dead pane. Provider-agnostic — any frame showing one of these lines is
# judged `api_retry`, whatever the provider.
API_RETRY_MARKERS = ("Waiting for API response", "will retry")

_RULE_RE = re.compile(r"^─{20,}$")
_FURNITURE_RE = re.compile(r"^[\s─━│┃╭╮╰╯┌┐└┘═▀▄█▌▐]+$")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def sanitize_frame(lines: list[str]) -> list[str]:
    """Per-line frame hygiene (design §5.7, steps 1–3).

    1. GBK mojibake repair, only for lines carrying lone surrogates:
       ``line.encode("gbk", "surrogateescape").decode("utf-8")``; any
       failure keeps the original line (defense layer only — the
       production path already decodes bytes as UTF-8).
    2. Remaining lone surrogates become U+FFFD.
    3. C0/C1 control characters and DEL are stripped (``\\t`` survives).

    Layout lines survive: the predicate needs the composer sandwich,
    and raw storage keeps layout for relabeling and replay.
    """
    cleaned = []
    for raw in lines:
        text = str(raw)
        if _SURROGATE_RE.search(text):
            try:
                text = text.encode("gbk", "surrogateescape").decode("utf-8")
            except (UnicodeError, LookupError):
                pass
        text = _SURROGATE_RE.sub("\ufffd", text)
        cleaned.append(_CONTROL_RE.sub("", text))
    return cleaned


def gate_verdict(provider: str, frame: dict) -> tuple[str, str]:
    """Verdict and reason for one ``terminal_read(screen=True)`` body.

    ``frame`` is the full JSON response (``result.terminal`` read here).
    """
    term = frame.get("result", {}).get("terminal", {})
    source = term.get("source")
    tail = term.get("tail")
    if source != "screen" or not isinstance(tail, list) or not tail:
        return "abstain", "no_usable_frame"
    lines = sanitize_frame([str(line) for line in tail])[-GATE_TAIL_LINES:]
    if any(marker in line for line in lines for marker in API_RETRY_MARKERS):
        return "api_retry", "upstream_api_retrying"
    if provider not in GATE_PROVIDERS:
        return "abstain", "provider_without_predicate"
    if provider == "claude":
        return _claude_verdict(lines)
    if provider == "dsh":
        return _dsh_verdict(lines)
    return _kimi_verdict(lines)


def _claude_verdict(lines: list[str]) -> tuple[str, str]:
    for line in lines:
        if any(marker in line for marker in DIALOG_MARKERS):
            return "not_ready", "dialog"
    composer = None
    for index, line in enumerate(lines):
        text = line.strip()
        if text == "❯" or text.startswith("❯ "):
            if (index > 0 and index + 1 < len(lines)
                    and _RULE_RE.match(lines[index - 1].strip())
                    and _RULE_RE.match(lines[index + 1].strip())):
                composer = index
                break
    if composer is None:
        return "not_ready", "composer_missing"
    if any(CLAUDE_WORKING_RE.search(line) for line in lines):
        return "not_ready", "turn_running"
    return "ready", "composer_visible"


def _dsh_verdict(lines: list[str]) -> tuple[str, str]:
    """DSH-TUI's composer sits in a box, so the box is the predicate.

    The chat view renders `╭───╮` / `⌸ ❯ …` / `╰───╯` and the launchpad
    `╭───╮` / `│ ❯ …` / `╰───╯`; the placeholder text after the glyph is
    localized, so the sandwich — not the wording — is what counts. The
    whale title and the status bar are up long before the composer
    mounts, so neither proves readiness on its own.
    """
    if any(marker in line for line in lines for marker in DSH_BOOT_MARKERS):
        return "not_ready", "harness_starting"
    for index, line in enumerate(lines):
        if "❯" not in line or index == 0 or index + 1 >= len(lines):
            continue
        if (lines[index - 1].strip().startswith("╭")
                and lines[index + 1].strip().startswith("╰")):
            return "ready", "composer_visible"
    return "not_ready", "composer_missing"


def _kimi_verdict(lines: list[str]) -> tuple[str, str]:
    for line in lines:
        text = "".join(ch for ch in line if ch.isprintable()).strip()
        if text == ">":
            return "ready", "composer_visible"
    for line in lines:
        if any(marker in line for marker in KIMI_PREVIEW_MARKERS):
            return "ready", "preview_marker"
    return "not_ready", "composer_missing"


def normalized_hash(lines: list[str]) -> str:
    """Letters-only line hash for frame-change detection (design §5.1).

    Same normalization idea as the echo-surface redraw check: strip
    everything that is not a word character plus digits and underscores,
    so spinner glyphs, counters and padding churn do not register as a
    change.
    """
    normalized = "\n".join(
        re.sub(r"[^\w]|[\d_]", "", line) for line in lines
    )
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()


def jev_payload(lines: list[str], max_bytes: int = 8192) -> str:
    """Jev-bound window (design §5.7 step 4).

    Drops pure furniture lines, collapses repeated blank lines, and
    keeps at most the bottom ``max_bytes`` of UTF-8 text. Never used for
    the predicate or for raw storage, which keep layout.
    """
    kept: list[str] = []
    prev_blank = False
    for line in lines:
        if _FURNITURE_RE.match(line):
            continue
        blank = not line.strip()
        if blank and prev_blank:
            continue
        kept.append(line)
        prev_blank = blank
    data = "\n".join(kept).encode("utf-8", "replace")
    if len(data) <= max_bytes:
        return data.decode("utf-8", "replace")
    return data[-max_bytes:].decode("utf-8", "ignore")

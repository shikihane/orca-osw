"""Blocking-screen discovery and handling policy (plan §2.2).

Pure functions plus one machine-wide lock; nothing here sends keys. The
watcher wires these together:

  detect:    is the pane blocked? Orca's `agentWait.reason` first (it
             names the dialog outright), else the Jev `ready_state`
             probabilities of `blocked_dialog` / `blocked_startup`
  classify:  which kind of blocker — the kind Orca named, else a
             confident Jev `blocker_kind` answer, else "other"
  decide:    act / shadow / notify for (provider, kind) under the Jev
             mode; only whitelisted pairs are ever acted on, and only in
             `enforce`
  lock:      one update per provider per machine at a time

The whitelist only takes a (provider, kind) pair whose screen has a
recorded sample under tests/fixtures/frames/<provider>/<kind>.json, and
a forbidden kind or a delete/payment screen is never handled
automatically, whatever the whitelist says.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from osw.jev import BLOCKER_KINDS, JEV_NOTIFY_PROB, READY_STARTUP_PROB

KINDS = BLOCKER_KINDS

# Orca's own name for the wait (`agentWait.reason`) -> blocker kind. Only
# reasons observed on a real pane are listed: `agent-update-prompt` is
# what `orca terminal send` reported for codex's update dialog
# (docs/research/2026-10-11-v2-v3-v5-session-mapping.md).
AGENT_WAIT_KINDS = {
    "agent-update-prompt": "update_prompt",
}

# A Jev `blocker_kind` answer below this probability classifies as
# "other": an acted-on kind must not rest on a guess.
BLOCKER_KIND_PROB = 0.8

# (provider, kind) -> key sequence. Keys are symbolic: "enter", "esc",
# "interrupt". Every entry needs a recorded screen sample.
#
# codex update dialog ("✨ Update available! … 1. Update now / 2. Skip",
# default "Update now"): Enter takes the default and runs the update.
# Orca answers plain `terminal send` to this dialog with
# `agent_prompt_blocked`; the research got through only with
# `--text "" --enter --interrupt`, which the sender must reproduce.
WHITELIST: dict[tuple[str, str], tuple[str, ...]] = {
    ("codex", "update_prompt"): ("enter",),
}

# Never handled automatically: logging in or granting permissions beyond
# the launch's own autonomy flags is the human's call.
FORBIDDEN_KINDS = frozenset({"auth", "permission"})

# Deletion and payment screens are never handled automatically either,
# whatever kind they were classified as.
FORBIDDEN_TEXT_RE = re.compile(
    r"\b(delet\w*|remov\w*|uninstall\w*|eras\w*|wip\w*|purge\w*|"
    r"purchas\w*|pay|payment\w*|paid|billing|bill|buy|subscri\w*|"
    r"credit card|upgrade to|upgrade your plan)\b"
    r"|删除|移除|卸载|清除|付费|支付|购买|订阅|充值",
    re.IGNORECASE,
)

DECISIONS = ("act", "shadow", "notify")

UPDATE_LOCK_TTL_SECS = 900  # a holder that crashed frees the lock after this


@dataclass(frozen=True)
class Detection:
    """A blocked pane: where the signal came from and what it named.

    `kind` is "" when the signal does not name the blocker (Jev
    `ready_state`, or an `agentWait.reason` OSW does not know); `classify`
    settles it then. `p` is the ready_state probability, None for Orca.
    """

    source: str   # "agent_wait" | "ready_state"
    kind: str
    signal: str   # the agentWait reason or the ready_state class
    p: float | None


def _agent_wait_reason(observation: dict | None) -> str:
    if not isinstance(observation, dict):
        return ""
    wait = observation.get("agentWait")
    if isinstance(wait, dict):
        return str(wait.get("reason") or "")
    return str(wait or "")


def _probability(answer: dict | None, cls: str) -> float:
    probabilities = (answer or {}).get("probabilities") or {}
    try:
        return float(probabilities.get(cls) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def detect(observation: dict | None, ready_answer: dict | None) -> Detection | None:
    """Whether the pane is blocked, or None.

    `observation` carries Orca's `agentWait` (a reason string, or an
    object with `reason`); `ready_answer` is an `ask_ready` result.
    """
    reason = _agent_wait_reason(observation)
    if reason:
        return Detection("agent_wait", AGENT_WAIT_KINDS.get(reason, ""), reason, None)
    for cls, threshold in (("blocked_dialog", JEV_NOTIFY_PROB),
                           ("blocked_startup", READY_STARTUP_PROB)):
        p = _probability(ready_answer, cls)
        if p >= threshold:
            return Detection("ready_state", "", cls, p)
    return None


def classify(detection: Detection, blocker_answer: dict | None) -> str:
    """The blocker kind: Orca's name, else a confident Jev answer, else "other"."""
    if detection.kind:
        return detection.kind
    choice = str((blocker_answer or {}).get("choice") or "")
    if choice in KINDS and _probability(blocker_answer, choice) >= BLOCKER_KIND_PROB:
        return choice
    return "other"


def forbidden(kind: str, frame_text: str = "") -> bool:
    """True when this blocker must never be handled automatically."""
    return kind in FORBIDDEN_KINDS or bool(FORBIDDEN_TEXT_RE.search(frame_text))


def decide(provider: str, kind: str, mode: str, frame_text: str = "") -> str:
    """"act", "shadow" (record what would be done) or "notify".

    `off` only notifies; `shadow` records the whitelisted keys without
    sending them; `enforce` sends them. Anything outside the whitelist,
    forbidden, or under an unknown mode only notifies.
    """
    if forbidden(kind, frame_text) or (provider, kind) not in WHITELIST:
        return "notify"
    if mode == "enforce":
        return "act"
    if mode == "shadow":
        return "shadow"
    return "notify"


# ---------------------------------------------------------------------------
# Machine-wide update lock: one updating pane per provider
# ---------------------------------------------------------------------------

def update_lock_dir() -> Path:
    """User-level lock directory, shared by every project on the machine."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "state")
    return Path(base) / "osw" / "locks"


def update_lock_path(provider: str, directory: Path | None = None) -> Path:
    return (directory or update_lock_dir()) / f"update-{provider}.lock"


def _lock_expired(path: Path, now: float, ttl_secs: float) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data["expires_at"]) < now
    except FileNotFoundError:
        return True
    except (OSError, ValueError, KeyError, TypeError):
        # Unreadable (e.g. a holder died mid-write): judge by file age.
        try:
            return path.stat().st_mtime + ttl_secs < now
        except FileNotFoundError:
            return True


def _create_lock(path: Path, owner: str, now: float, ttl_secs: float) -> bool:
    try:
        with path.open("x", encoding="utf-8") as f:
            json.dump({
                "owner": owner,
                "pid": os.getpid(),
                "acquired_at": now,
                "expires_at": now + ttl_secs,
            }, f)
        return True
    except FileExistsError:
        return False


def acquire_update_lock(
    provider: str,
    owner: str = "",
    *,
    directory: Path | None = None,
    ttl_secs: float = UPDATE_LOCK_TTL_SECS,
) -> bool:
    """Take the machine-wide update lock for `provider`; False if held.

    The lock file itself is the lock (`open(..., "x")`). An expired lock
    — its holder crashed or never released — is broken by renaming it
    aside first, so of several contenders only the one whose rename
    succeeds may re-create it; a lock that turns out to have been
    refreshed in between is put back.
    """
    owner = owner or f"pid:{os.getpid()}"
    path = update_lock_path(provider, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if _create_lock(path, owner, now, ttl_secs):
        return True
    if not _lock_expired(path, now, ttl_secs):
        return False
    aside = path.with_name(f"{path.name}.stale.{uuid.uuid4().hex}")
    try:
        os.rename(path, aside)
    except OSError:
        return False  # another contender broke it first
    if not _lock_expired(aside, now, ttl_secs):
        # Lost a race: what we moved is a fresh lock. Restore it.
        try:
            os.rename(aside, path)
        except OSError:
            aside.unlink(missing_ok=True)
        return False
    aside.unlink(missing_ok=True)
    return _create_lock(path, owner, now, ttl_secs)


def release_update_lock(
    provider: str,
    owner: str = "",
    *,
    directory: Path | None = None,
) -> bool:
    """Release the lock if `owner` holds it; True when a lock was removed."""
    owner = owner or f"pid:{os.getpid()}"
    path = update_lock_path(provider, directory)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("owner") != owner:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True

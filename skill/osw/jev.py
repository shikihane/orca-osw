"""Jev (TypeSafe System One) integration: mode, calls, storage, status.

Design reference: ``docs/research/2026-10-09-jev-integration-design.md``
(§4.2/§4.3/§5.1/§5.5/§7). Everything here is fail-open: any failure
degrades to the deterministic path, records ``jev_degraded``, and never
raises into the watcher. The SDK is optional (``typesafe-sdk``); when it
is absent or the key is missing every call degrades cleanly.

The config file is the single source of the mode (Q3): no environment
variable toggles Jev — ``TYPESAFE_API_KEY``/``TYPESAFE_BASE_URL`` only
carry credentials.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import anyio

from osw.log import emit_event, get_logger
from osw.ready_gate import jev_payload, sanitize_frame
from osw.state import logs_dir, state_dir

log = get_logger("jev")

JEV_MODEL = "jev-1.13.0"           # pinned; aliases drift (design §4.3)
JEV_TIMEOUT_MS = 3500              # single-call total budget (outer fail_after)
JEV_ENFORCEABLE_PROVIDERS = ("claude", "omp")  # whitelist for enforce_providers
READY_STARTUP_PROB = 0.7           # hold trigger on P(blocked_startup)
JEV_NOTIFY_PROB = 0.8              # advisory notice threshold
HOLD_BUDGET_MS = 30000             # total hold budget
HOLD_MIN_REASK_MS = 5000           # min interval between hold re-asks
HOLD_FALSE_HOLD_CAP_MS = 15000     # static-frame give-up (named honestly)
HOLD_MIN_REMAINING_MS = 15000      # never start/continue a hold below this
JEV_WINDOW_BYTES = 8192            # bottom-of-frame window sent to Jev

MODES = ("off", "shadow", "enforce")

READY_QUESTION_ID = "ready_state"

# Design §5.5: option order keeps `ready_composer` away from first place
# (the model leans toward the first option); the offline eval re-tests
# with the alternate order.
READY_CRITERIA = {
    "blocked_startup": (
        "The TUI is still starting or initializing; pasted input would be "
        "queued, rendered late, or lost."),
    "blocked_dialog": (
        "A modal dialog, first-run question, trust prompt, login or sign-in "
        "screen, or confirmation is up; pasted input would be swallowed or "
        "go to the wrong place."),
    "working": (
        "The agent is mid-turn; pasted input would be queued or interleaved "
        "rather than start a new turn."),
    "shell_prompt": (
        "A plain shell prompt is shown; the agent TUI is not running."),
    "api_retry": (
        "The agent is waiting on its upstream API and will retry on its own "
        "(for example a line like 'Waiting for API response · will retry · "
        "check your network'); the TUI is alive but cannot start a turn yet."),
    "other": (
        "Anything else, including frames too garbled to interpret."),
    "ready_composer": (
        "The TUI is fully started and an idle input composer is accepting "
        "keyboard input; pasted text would land in the composer and submit "
        "normally."),
}

READY_INSTRUCTIONS = (
    "The `screen` field is the rendered frame of a terminal running an "
    "interactive coding-agent TUI (provider: `provider`; the word `unknown` "
    "means the provider is not known). An orchestrator is about to paste a "
    "task prompt into this terminal and press Enter. Decide what the "
    "terminal is showing right now.")

BLOCKER_QUESTION_ID = "blocker_kind"

# Plan §2.2: what is blocking the composer. Same ordering rule as
# READY_CRITERIA: the only option an orchestrator may act on
# (`update_prompt`, whitelisted per provider in osw.blockers) goes last,
# with `other` right before it, so the model's first-option lean favors
# the kinds that are only ever reported.
BLOCKER_CRITERIA = {
    "auth": (
        "A login, sign-in, API key, or authorization screen asks the user "
        "to authenticate."),
    "permission": (
        "The agent asks for permission or approval to run a command, edit "
        "files, or use a tool."),
    "trust_folder": (
        "A first-run question asks whether to trust this folder or "
        "workspace."),
    "announcement": (
        "A what's-new, release-notes, tips, or welcome screen waits to be "
        "dismissed."),
    "rate_limit": (
        "A usage-limit, quota, or rate-limit message says the agent cannot "
        "continue until later or until the plan changes."),
    "crashed_to_shell": (
        "The agent exited or crashed and a plain shell prompt or error "
        "trace is shown instead of the TUI."),
    "other": (
        "Any other blocking screen, including frames too garbled to "
        "interpret."),
    "update_prompt": (
        "The CLI announces that a newer version is available and asks "
        "whether to update now or skip."),
}
BLOCKER_KINDS = tuple(BLOCKER_CRITERIA)

BLOCKER_INSTRUCTIONS = (
    "The `screen` field is the rendered frame of a terminal running an "
    "interactive coding-agent TUI (provider: `provider`; the word `unknown` "
    "means the provider is not known). Something on this screen keeps the "
    "agent's input composer from accepting a task prompt. Decide what kind "
    "of screen is blocking it.")


class JevUnavailable(Exception):
    """Raised internally when a call cannot be attempted (fail-open)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Mode and configuration (single source: config.json)
# ---------------------------------------------------------------------------

def resolve_mode(root: Path) -> str:
    """Effective mode: the global switch, and nothing else.

    `OSW_JEV_MODE` (`off|shadow|enforce`) is the only control; absent or
    invalid means `off`. Set it machine-wide (e.g. `setx OSW_JEV_MODE
    enforce` on Windows); `root` stays in the signature for callers.
    """
    mode = os.environ.get("OSW_JEV_MODE", "").strip().lower()
    return mode if mode in MODES else "off"


def resolve_enforce_providers(root: Path) -> tuple[str, ...]:
    """Providers allowed to hold: the enforceable whitelist, globally.

    There is no per-project whitelist anymore; under a global `enforce`
    the full white list applies on every project.
    """
    if resolve_mode(root) == "enforce":
        return tuple(JEV_ENFORCEABLE_PROVIDERS)
    return ()


def enforce_active(root: Path, provider: str) -> bool:
    """True when holds are allowed for this provider right now (§5.1.1)."""
    return (resolve_mode(root) == "enforce"
            and provider in resolve_enforce_providers(root))


# ---------------------------------------------------------------------------
# Credentials and environment status
# ---------------------------------------------------------------------------

def api_key() -> str:
    return os.environ.get("TYPESAFE_API_KEY", "").strip()


def masked_key() -> str:
    key = api_key()
    if not key:
        return "(absent)"
    if len(key) <= 12:
        return "***"
    return f"{key[:6]}…{key[-4:]}"


def base_url() -> str:
    return (os.environ.get("TYPESAFE_BASE_URL", "").strip()
            or "https://api.typesafe.ai")


def sdk_status() -> tuple[bool, str]:
    """(importable, version) for the optional typesafe-sdk dependency."""
    try:
        import typesafe_sdk
    except Exception:
        return False, ""
    return True, str(getattr(typesafe_sdk, "__version__", "") or "unknown")


def proxy_set() -> bool:
    return any(
        os.environ.get(name)
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    )


def ensure_proxy_env() -> bool:
    """Adopt the machine's runtime proxy when standard variables are absent.

    Orca terminals on this host export `PI_PROXY` (and
    `DOCKER_SANDBOXES_PROXY`) but not the `HTTPS_PROXY`/`HTTP_PROXY`
    variables httpx honors; without this every Jev call degrades with the
    regional-block status. Standard variables always win; the adopted
    value applies to this process only.
    """
    if proxy_set():
        return True
    for name in ("PI_PROXY", "DOCKER_SANDBOXES_PROXY"):
        value = os.environ.get(name, "").strip()
        if value:
            os.environ.setdefault("HTTPS_PROXY", value)
            os.environ.setdefault("HTTP_PROXY", value)
            return True
    return False


def check_endpoint(timeout: float = 15.0) -> dict:
    """Live probe for ``osw.py jev status --check`` (free, not billed).

    ``GET {base}/v1/models`` distinguishes a valid key (200) from an
    invalid one (401) and from network/proxy failures; 403 is reported
    as-is (edge/permission, see design N11).
    """
    import urllib.error
    import urllib.request

    ensure_proxy_env()
    url = base_url().rstrip("/") + "/v1/models"
    headers = {}
    key = api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {
                "ok": True,
                "status": int(getattr(response, "status", 200)),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "status": int(exc.code),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "reason": "auth_failed" if exc.code == 401 else (
                "permission_or_edge" if exc.code == 403 else f"http_{exc.code}"),
        }
    except Exception as exc:  # network, proxy, DNS
        return {
            "ok": False,
            "status": 0,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "reason": f"network: {exc}",
        }


# ---------------------------------------------------------------------------
# Raw storage (§7): one JSON per call, ensure_ascii=True
# ---------------------------------------------------------------------------

def jev_dir(root: Path) -> Path:
    return state_dir(root) / "jev"


def write_raw(root: Path, agent_id: str, point: str, payload: dict) -> Path:
    """Persist one raw judgment record; never raises on lone surrogates.

    ``ensure_ascii=True`` cannot raise ``UnicodeEncodeError`` on a stray
    surrogate (N10), so storage survives even if a frame slipped past
    sanitization.
    """
    directory = jev_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    path = directory / f"{agent_id}_{point}_{stamp}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=1), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Event helpers (jev_* events never carry frame text)
# ---------------------------------------------------------------------------

def emit_jev_degraded(
    root: Path, agent_id: str, mode: str, reason: str, point: str,
) -> None:
    emit_event(
        logs_dir(root),
        component="jev",
        event="jev_degraded",
        level="WARNING",
        agent_id=agent_id,
        message=f"Jev degraded ({reason}); deterministic path governs",
        data={"reason": reason, "point": point, "mode": mode},
    )


# ---------------------------------------------------------------------------
# The ready judgment (design §5.1.1, §5.5)
# ---------------------------------------------------------------------------

def _build_choice(instructions: str, criteria: dict):
    try:
        from typesafe_sdk import Choice
    except Exception as exc:
        raise JevUnavailable("no_sdk") from exc
    return Choice(instructions=instructions, criteria=dict(criteria))


def _build_ready_question():
    return _build_choice(READY_INSTRUCTIONS, READY_CRITERIA)


def _build_blocker_question():
    return _build_choice(BLOCKER_INSTRUCTIONS, BLOCKER_CRITERIA)


def _build_client():
    ensure_proxy_env()
    key = api_key()
    if not key:
        raise JevUnavailable("no_key")
    try:
        from typesafe_sdk import AsyncTypeSafeClient
    except Exception:
        raise JevUnavailable("no_sdk")
    url = os.environ.get("TYPESAFE_BASE_URL", "").strip()
    try:
        return AsyncTypeSafeClient(api_key=key, base_url=url) if url \
            else AsyncTypeSafeClient(api_key=key)
    except Exception as exc:
        raise JevUnavailable(f"client_init: {exc}")


def startup_should_hold(answer: dict) -> bool:
    probabilities = answer.get("probabilities") or {}
    try:
        return float(probabilities.get("blocked_startup") or 0.0) \
            >= READY_STARTUP_PROB
    except (TypeError, ValueError):
        return False


def notify_class(answer: dict) -> tuple[str, float] | None:
    """(class, probability) when an advisory notice is warranted (§5.1.4)."""
    probabilities = answer.get("probabilities") or {}
    for cls in ("blocked_dialog", "shell_prompt"):
        try:
            p = float(probabilities.get(cls) or 0.0)
        except (TypeError, ValueError):
            continue
        if p >= JEV_NOTIFY_PROB:
            return cls, p
    return None


async def _ask_choice(
    root: Path,
    agent_id: str,
    provider: str,
    frame_body: dict,
    mode: str,
    *,
    point: str,
    question_id: str,
    criteria: dict,
    build_question,
    message: str,
    record_extra: dict,
    event_extra,
) -> dict | None:
    """One Choice judgment over a ``--screen`` frame; ``None`` = degraded.

    Shared by every Jev question: reads only ``result.terminal`` of the
    frame, sends the sanitized bottom window (≤JEV_WINDOW_BYTES) plus the
    provider, persists the raw record (``<agent>_<point>_<ts>.json``) and
    emits ``jev_call`` before returning ``{choice, probabilities,
    confidence}``. Every failure is fail-open: ``jev_degraded`` with the
    reason, then ``None``.
    """
    term = frame_body.get("result", {}).get("terminal", {})
    tail = term.get("tail")
    if not isinstance(tail, list) or not tail:
        emit_jev_degraded(root, agent_id, mode, "no_frame", point)
        return None
    screen = jev_payload(sanitize_frame([str(line) for line in tail]),
                         JEV_WINDOW_BYTES)
    payload_state = {"screen": screen, "provider": provider or "unknown"}
    questions = {question_id: None}  # replaced below; kept for the record
    started = time.monotonic()
    try:
        # Client first: a missing key is the more actionable degradation
        # (`no_key`), and the SDK import is checked right after.
        client = _build_client()
        question = build_question()
        questions[question_id] = str(criteria)  # readable record
        with anyio.fail_after(JEV_TIMEOUT_MS / 1000):
            async with client:
                response = await client.system_one(
                    state=payload_state,
                    questions={question_id: question},
                )
        answer = response.choices[question_id]
        probabilities = dict(getattr(answer, "probabilities", {}) or {})
        result = {
            "choice": getattr(answer, "choice", ""),
            "probabilities": probabilities,
            "confidence": getattr(answer, "confidence", None),
        }
        latency_ms = int((time.monotonic() - started) * 1000)
        input_tokens = None
        usage = getattr(response, "usage", None)
        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", None)
        model = str(getattr(response, "model", "") or JEV_MODEL)
        record = {
            "state": payload_state,
            "questions": questions,
            "response": {
                "choice": result["choice"],
                "probabilities": probabilities,
                "confidence": result["confidence"],
            },
            "model": model,
            "latency_ms": latency_ms,
            "mode": mode,
            **record_extra,
        }
        try:
            request_file = write_raw(root, agent_id, point, record)
        except OSError:
            request_file = None
        emit_event(
            logs_dir(root),
            component="jev",
            event="jev_call",
            agent_id=agent_id,
            message=message,
            data={
                "point": point,
                "mode": mode,
                "model": model,
                "request_file": str(request_file) if request_file else "",
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "verdict": result["choice"],
                **event_extra(probabilities),
            },
        )
        return result
    except JevUnavailable as exc:
        emit_jev_degraded(root, agent_id, mode, exc.reason, point)
        return None
    except TimeoutError:
        emit_jev_degraded(root, agent_id, mode, "timeout", point)
        return None
    except BaseException as exc:
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            emit_jev_degraded(root, agent_id, mode, "cancelled", point)
            raise
        reason = "rate_limited" if "429" in str(exc) else "api_error"
        emit_jev_degraded(root, agent_id, mode, reason, point)
        return None


async def ask_ready(
    root: Path,
    agent_id: str,
    provider: str,
    frame_body: dict,
    mode: str,
    gate_label: str,
) -> dict | None:
    """One ``ready_state`` judgment; ``None`` means degraded (fail-open).

    The raw record is
    ``{state, questions, response, model, latency_ms, mode, gate_verdict}``.
    """
    return await _ask_choice(
        root, agent_id, provider, frame_body, mode,
        point="ready",
        question_id=READY_QUESTION_ID,
        criteria=READY_CRITERIA,
        build_question=_build_ready_question,
        message="ready judgment",
        record_extra={"gate_verdict": gate_label},
        event_extra=lambda p: {
            "p_startup": p.get("blocked_startup"),
            "p_dialog": p.get("blocked_dialog"),
            "p_shell": p.get("shell_prompt"),
        },
    )


async def ask_blocker(
    root: Path,
    agent_id: str,
    provider: str,
    frame_body: dict,
    mode: str,
) -> dict | None:
    """One ``blocker_kind`` judgment (plan §2.2); ``None`` = degraded.

    Classifies what keeps the composer blocked; acting on the answer is
    `osw.blockers`' decision, never this call's. Raw records are stored
    as ``<agent>_blocker_<ts>.json``.
    """
    return await _ask_choice(
        root, agent_id, provider, frame_body, mode,
        point="blocker",
        question_id=BLOCKER_QUESTION_ID,
        criteria=BLOCKER_CRITERIA,
        build_question=_build_blocker_question,
        message="blocker judgment",
        record_extra={},
        event_extra=lambda p: {
            "p_choice": max(
                (v for v in p.values() if isinstance(v, (int, float))),
                default=None,
            ),
        },
    )

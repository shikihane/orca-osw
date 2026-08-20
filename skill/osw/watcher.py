"""Per-agent background supervisor.

One watcher process per dispatched task, spawned detached by `osw new`
/ `osw use` and gone when the task is settled. No daemon: Orca itself
is the source of truth (`worktree ps` reports each recognized agent
TUI's state/prompt/tool), the watcher just polls it and drives one
shared flow for `new` and `use`:

  ready:            wait until the pane can accept a prompt
                    (`worktree ps` "done" for tracked panes,
                    `tui-idle` otherwise, ready-screen preview markers
                    or stable output silence for TUIs Orca never
                    reports idle)
  phase "task":     send the prompt, wait until Orca reports the
                    agent's turn is done
  phase "handoff":  send the fixed wrap-up instruction, wait again,
                    then verify the handoff markdown exists

An idle observation is never proof that a turn completed: completion
always requires task-correlated evidence (a "done" state that started
after the prompt was sent and is newer than the pane's pre-send state,
or new output followed by stable silence for untracked CLIs).

A tracked pane must also acknowledge the prompt within a receipt
window (report the turn "working", finish it, or echo the prompt text
in ps); no acknowledgement means something other than the agent's
composer swallowed the input (login screen, update dialog, crash
remains) — the turn fails closed as "no_receipt" and the caller is
notified, instead of idling to the hard timeout.

Independently of ps tracking, every sent prompt's echo is verified
against the terminal's scrollback (never the preview tail: a
full-screen TUI's bottom lines are its composer box, where the
submitted message can never appear). The check first gauges whether
the read can actually reach the pane's retained buffer — a pane
exposing only its current screenful (e.g. an alternate-screen TUI)
has no echo surface, and judging there is a guaranteed false miss.
A missing echo on a usable surface alarms the caller but never
triggers a resend — a false miss would otherwise inject a duplicate
prompt into a working agent's input queue.

CLIs Orca does not recognize (no `agents` entry in `worktree ps`)
fall back to lastOutputAt idle detection. That fallback never counts
the prompt's own terminal echo as output, and the quick tui-idle exit
is reserved for panes that demonstrably worked on this turn before
falling out of tracking — a pane Orca never tracked (e.g. one spawned
so recently that ps has no entry yet) must wait out the slow
stable-silence window instead.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import anyio

from osw.log import emit_event, enable_file_logging, get_logger, setup_logging
from osw.orca_cli import (
    OrcaError,
    terminal_list,
    terminal_read,
    terminal_send,
    terminal_show,
    terminal_wait,
    worktree_ps,
)
from osw.state import (
    StateWriteError,
    logs_dir,
    new_handoff_path,
    read_agent,
    write_agent,
    write_report,
)

log = get_logger("watcher")

POLL_SECS = 2.0                    # worktree ps poll interval
READY_TIMEOUT_MS = 600_000         # max wait for the agent TUI to come up
READY_IDLE_STABLE_MS = 30_000      # ready-phase fallback: silent this long = ready
READY_PREVIEW_SILENCE_MS = 10_000  # composer marker also needs this much silence
FALLBACK_IDLE_STABLE_MS = 60_000   # untracked CLI: silent this long = turn over
HARD_TIMEOUT_SECS = 3600           # give up on the whole task after this
STUCK_NOTIFY_SECS = 300            # tracked agent frozen this long -> tell caller
CLOCK_SKEW_MS = 5_000              # tolerance comparing our clock vs Orca's
TRACK_LOST_IDLE_WAIT_MS = 1_000    # quick TUI idle check before slow fallback
RECEIPT_TIMEOUT_MS = 90_000        # tracked pane must acknowledge the prompt

# Fixed, machine-generated wrap-up instruction (single line: multi-line
# text gets truncated at newlines by orca.CMD --text on Windows). This
# is the only self-authored text OSW ever injects into a worker.
HANDOFF_TEMPLATE = (
    "Task wrap-up: the task above is finished. Write a concise English "
    "handoff document to {path} (markdown) covering: 1) what you did, "
    "2) key findings and decisions, 3) files you modified, 4) what "
    "remains or is blocked. If any of your background tasks are still "
    "running, wait for them to finish and fold their results into the "
    "handoff before writing the file. Create the file even if the task "
    "was trivial, then stop; do not start any new work."
)

# kimi paints this status badge while its own background bash tasks are
# still running; the TUI itself sits idle then, so output-silence alone
# would mistake "waiting on background tasks" for "turn finished".
BACKGROUND_TASK_BADGE_RE = re.compile(r"\[\d+ tasks? running\]")

# Terminal-preview strings proving a kimi TUI has finished starting and
# sits at its composer. Orca neither tracks kimi in `worktree ps` nor
# emits tui-idle for it, so these give the ready-wait a fast path that
# pure output silence cannot. Plain ASCII survives the preview's mangled
# box-drawing glyphs on Windows; matched against `terminal show`'s
# preview tail, with output silence and the slow stable window as
# fallbacks when a future kimi version rewords them.
READY_PREVIEW_MARKERS = (
    "one will be created on your first message",  # fresh-session screen
)

# Ready-screen signatures that are painted permanently, not just on a
# fresh-session screen — so unlike the markers above they must be gated
# on a short output-silence window (a busy TUI is never silent, and a
# freshly spawned TUI gets a moment to finish booting before the send).
# Claude Code v2 exposes neither a ps entry nor tui-idle nor a composer
# line in the preview: the bottom line is its status bar, which carries
# the model string ("claude-fable-5", "claude-sonnet-4", ...). Without
# this marker every fresh dispatch pays the full READY_IDLE_STABLE_MS
# silence wait while the TUI sits ready.
READY_SCREEN_MARKERS = (
    "claude-",
)

# A modal first-run dialog is the classic prompt swallow: the composer
# is not what receives keyboard input while one of these is up, so a
# pane showing one is never ready no matter how silent its output is.
BLOCKING_DIALOG_MARKERS = (
    "Trust this folder",
)

# Post-send prompt verification: after the send, the terminal's
# scrollback is re-read and must contain the prompt's echo. The
# preview tail cannot serve here: on a full-screen TUI those bottom
# lines are permanently occupied by the composer box and status line,
# so the submitted message — rendered in the conversation area above —
# never enters that window, and judging by it produces a guaranteed
# false miss on every prompt. `terminal read` preserves raw text
# (CJK included), so the prompt's normalized prefix is the primary
# match unit, with its first sufficiently long ASCII island as a
# mangling-proof fallback. Both units are bounded prefixes, because a
# TUI may clip the echoed line at the terminal width (kimi renders a
# long submission as one clipped line ending in an ellipsis). A prompt
# carrying neither unit in sufficient length cannot be verified and
# the check abstains.
#
# The read itself is only checked for usability first: panes whose TUI
# runs on the alternate screen (or that Orca otherwise exposes only as
# the current screenful) return a handful of lines while their own
# cursors report a far larger retained buffer. There is no echo
# surface to judge by there — some panes render the conversation into
# real scrollback, some never do — so the check abstains instead of
# producing another guaranteed false miss.
PROMPT_ECHO_DELAY_SECS = 3.0
PROMPT_ECHO_MIN_CHARS = 8
PROMPT_ECHO_PREFIX_CHARS = 24
PROMPT_ECHO_ISLAND_CHARS = 16
PROMPT_ECHO_READ_LINES = 400
PROMPT_ECHO_MIN_SURFACE_LINES = 16  # below this, thinness cannot be judged
PROMPT_ECHO_SURFACE_MIN_RATIO = 4   # tail must cover >= 1/4 of reachable lines


def _prompt_echo_units(text: str) -> list[str]:
    """Bounded substrings of `text` a submitted-turn echo must contain."""
    flat = one_line(text)
    units = []
    prefix = flat[:PROMPT_ECHO_PREFIX_CHARS].rstrip()
    if len(prefix) >= PROMPT_ECHO_MIN_CHARS:
        units.append(prefix)
    for island in re.findall(r"[\x21-\x7e]+", flat):
        if len(island) >= PROMPT_ECHO_MIN_CHARS:
            units.append(island[:PROMPT_ECHO_ISLAND_CHARS])
            break
    return units


def _composer_visible(preview: str) -> bool:
    """True when the preview ends at an empty `>` composer line.

    kimi keeps the composer painted even mid-turn, so callers must gate
    this on a minimum output-silence window.
    """
    for line in preview.splitlines():
        text = "".join(ch for ch in line if ch.isprintable()).strip()
        if text == ">":
            return True
    return False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def one_line(text: str, max_len: int = 0) -> str:
    """Collapse all whitespace runs into single spaces (Windows: any
    newline sent through orca.CMD --text truncates the message)."""
    flat = " ".join((text or "").split())
    if max_len and len(flat) > max_len:
        flat = flat[: max_len - 3] + "..."
    return flat


def format_completion_report(
    agent_id: str, terminal: str, report_file: str,
    handoff_file: str = "", summary: str = "",
) -> str:
    """Single-line completion notification sent to the caller terminal.

    Single-line because newlines get mangled through orca.CMD --text on
    Windows; the leading '#' keeps it inert in a shell. Full data lives
    in the JSON report and the handoff markdown.
    """
    message = f"# [osw] task-finished agent={agent_id} terminal={terminal}"
    if report_file:
        message += f" report={report_file}"
    if handoff_file:
        message += f" handoff={handoff_file}"
    if summary:
        message += f" summary={one_line(summary, 160)}"
    return message


# ---------------------------------------------------------------------------
# Orca observation helpers
# ---------------------------------------------------------------------------

async def _pane_key(handle: str) -> str | None:
    """Map a terminal handle to worktree ps's agent paneKey (tabId:leafId)."""
    try:
        data = await terminal_show(handle)
    except OrcaError:
        return None
    term = data.get("result", {}).get("terminal", {})
    tab, leaf = term.get("tabId"), term.get("leafId")
    if tab and leaf:
        return f"{tab}:{leaf}"
    return None


async def _ps_entry(pane_key: str) -> dict | None:
    """Find the Orca agent-state entry for a pane, if Orca tracks it."""
    for wt in await worktree_ps():
        for entry in wt.get("agents") or []:
            if entry.get("paneKey") == pane_key:
                return entry
    return None


# ---------------------------------------------------------------------------
# Watcher state machine
# ---------------------------------------------------------------------------

class Watcher:
    def __init__(self, root: Path, agent_id: str) -> None:
        self.root = root
        self.agent = read_agent(root, agent_id)
        self.agent_id = agent_id
        self.handle = self.agent["terminal"]
        self.deadline = time.monotonic() + HARD_TIMEOUT_SECS
        self.pane: str | None = self.agent.get("pane_key") or None
        self._state_dirty = False
        self._state_storage_error: StateWriteError | None = None

    def _update(self, **fields) -> None:
        changed = False
        for key, value in fields.items():
            if self.agent.get(key) != value:
                self.agent[key] = value
                changed = True
        if changed:
            self.agent["updated_at"] = now_iso()
            self._state_dirty = True
        self._flush_state()

    def _flush_state(self) -> bool:
        if not self._state_dirty:
            return True
        try:
            write_agent(self.root, self.agent)
        except StateWriteError as exc:
            if self._state_storage_error is None:
                self._state_storage_error = exc
                log.error(
                    "watcher(%s): state storage failed: %s",
                    self.agent_id,
                    exc,
                )
                self._event(
                    "state_storage_error",
                    level="ERROR",
                    message=str(exc),
                    data={"path": str(exc.path)},
                )
            return False

        self._state_dirty = False
        if self._state_storage_error is not None:
            previous = self._state_storage_error
            self._state_storage_error = None
            log.info("watcher(%s): state storage recovered", self.agent_id)
            self._event(
                "state_storage_recovered",
                data={"path": str(previous.path)},
            )
        return True

    def _event(
        self,
        event: str,
        *,
        level: str = "INFO",
        message: str = "",
        data: dict | None = None,
    ) -> None:
        emit_event(
            logs_dir(self.root),
            component="watcher",
            event=event,
            level=level,
            agent_id=self.agent_id,
            terminal=self.handle,
            phase=str(self.agent.get("phase") or ""),
            message=message,
            data=data,
        )

    async def _notify(self, line: str) -> None:
        caller = self.agent.get("caller_terminal")
        if not caller:
            # Never drop a completion notification silently: record an
            # ERROR event so the gap is visible in `osw logs`.
            log.warning(
                "notify skipped (agent=%s): no caller_terminal recorded",
                self.agent_id,
            )
            self._event(
                "notify_skipped",
                level="ERROR",
                message="no caller_terminal recorded; notification dropped",
                data={"line": line},
            )
            return
        try:
            await terminal_send(caller, line)
        except OrcaError as exc:
            log.warning("notify failed (caller=%s): %s", caller, exc)
            self._event(
                "notify_failed",
                level="ERROR",
                message=f"failed to notify caller terminal: {exc}",
                data={"caller_terminal": caller, "line": line},
            )

    async def _rebind_terminal(self) -> bool:
        """Refresh a runtime-scoped handle using the pane's stable identity."""
        if not self.pane:
            return False
        try:
            terminals = await terminal_list(f"path:{self.root}")
        except OrcaError:
            return False
        for terminal in terminals:
            tab = terminal.get("tabId")
            leaf = terminal.get("leafId")
            handle = terminal.get("handle")
            if handle and f"{tab}:{leaf}" == self.pane:
                previous = self.handle
                self.handle = str(handle)
                self._update(terminal=self.handle)
                self._event(
                    "terminal_rebound",
                    data={"previous": previous, "current": self.handle},
                )
                return True
        return False

    async def _wait_tui_idle(
        self, timeout_ms: int = TRACK_LOST_IDLE_WAIT_MS
    ) -> bool:
        try:
            await terminal_wait(self.handle, "tui-idle", timeout_ms=timeout_ms)
            return True
        except OrcaError as exc:
            if exc.code != "terminal_handle_stale" \
                    or not await self._rebind_terminal():
                return False
        try:
            await terminal_wait(self.handle, "tui-idle", timeout_ms=timeout_ms)
            return True
        except OrcaError:
            return False

    async def _terminal_snapshot(self) -> tuple[int, str]:
        """(lastOutputAt, preview) for the pane, from one terminal show."""
        try:
            data = await terminal_show(self.handle)
        except OrcaError as exc:
            if exc.code != "terminal_handle_stale" \
                    or not await self._rebind_terminal():
                raise
            data = await terminal_show(self.handle)
        term = data.get("result", {}).get("terminal", {})
        return int(term.get("lastOutputAt") or 0), str(term.get("preview") or "")

    async def _last_output_at(self) -> int:
        return (await self._terminal_snapshot())[0]

    async def _echo_surface(
        self, limit: int
    ) -> tuple[list[str], int | None] | None:
        """(tail lines, retained line count) for the pane.

        The retained count comes from the read cursors
        (latestCursor - oldestCursor) and is None when the response
        carries no cursors. None overall when the terminal cannot be
        read.
        """
        try:
            data = await terminal_read(self.handle, limit=limit)
        except OrcaError as exc:
            if exc.code != "terminal_handle_stale" \
                    or not await self._rebind_terminal():
                return None
            try:
                data = await terminal_read(self.handle, limit=limit)
            except OrcaError:
                return None
        term = data.get("result", {}).get("terminal", {})
        tail = term.get("tail")
        if not isinstance(tail, list):
            return None
        retained = None
        try:
            retained = max(
                0, int(term["latestCursor"]) - int(term["oldestCursor"])
            )
        except (KeyError, TypeError, ValueError):
            pass
        return [str(line) for line in tail], retained

    async def _send_terminal(self, text: str) -> None:
        try:
            await terminal_send(self.handle, text)
            return
        except OrcaError as exc:
            if exc.code != "terminal_handle_stale" \
                    or not await self._rebind_terminal():
                raise
        await terminal_send(self.handle, text)

    async def _verify_prompt_echo(self, text: str) -> bool | None:
        """Re-read the terminal scrollback and check the sent prompt's
        echo is there.

        Returns True/False, or None when no verdict is possible: the
        prompt carries no match unit long enough to be trustworthy, the
        terminal cannot be read, or the read cannot reach the pane's
        retained buffer (no usable echo surface).
        """
        units = _prompt_echo_units(text)
        if not units:
            return None
        # Two looks before judging: the TUI may need a moment to render
        # the submitted message into the scrollback.
        for _ in range(2):
            await anyio.sleep(PROMPT_ECHO_DELAY_SECS)
            surface = await self._echo_surface(PROMPT_ECHO_READ_LINES)
            if surface is None:
                return None
            tail, retained = surface
            reachable = min(PROMPT_ECHO_READ_LINES, retained) \
                if retained is not None else len(tail)
            if (reachable >= PROMPT_ECHO_MIN_SURFACE_LINES
                    and len(tail) * PROMPT_ECHO_SURFACE_MIN_RATIO < reachable):
                self._event(
                    "prompt_echo_unverifiable",
                    message="terminal read exposes only a fraction of the "
                            "retained buffer; no usable echo surface",
                    data={"returned": len(tail), "retained": retained},
                )
                return None
            haystack = one_line(
                "".join(
                    ch if ch.isprintable() or ch.isspace() else " "
                    for ch in " ".join(tail)
                )
            )
            if any(unit in haystack for unit in units):
                return True
        return False

    async def run(self) -> None:
        self._update(watcher_pid=os.getpid(), state="starting")
        self._event("watcher_started", data={"pid": os.getpid()})
        log.info(
            "watcher(%s): started  terminal=%s  pid=%d",
            self.agent_id, self.handle, os.getpid(),
        )

        self._event("ready_wait_started")
        ready_source = await self._wait_ready()
        if ready_source is None:
            log.error("watcher(%s): ready-wait timed out", self.agent_id)
            self._event(
                "ready_wait_failed",
                level="ERROR",
                message="terminal never became ready",
            )
            await self._finalize("error", "ready_failed", error="ready timeout")
            return
        self._event("ready_observed", data={"source": ready_source})
        log.info(
            "watcher(%s): ready (source=%s)  pane=%s",
            self.agent_id, ready_source, self.pane or "(unknown)",
        )

        prompt = one_line(self.agent.get("prompt") or "")
        if not prompt:
            await self._finalize("done", "no_prompt")
            return

        # ---- Phase 1: the task itself ----
        source = await self._run_turn(prompt, phase="task")
        if source in ("timeout", "terminal_lost", "send_failed", "no_receipt"):
            await self._finalize("error", source)
            return
        log.info("watcher(%s): task turn done (source=%s)", self.agent_id, source)

        # ---- Phase 2: handoff document ----
        handoff = new_handoff_path(self.root, self.agent_id)
        self._update(handoff_path=str(handoff))
        instruction = HANDOFF_TEMPLATE.format(path=handoff)
        for attempt in (1, 2):
            handoff_source = await self._run_turn(instruction, phase="handoff")
            if handoff_source in (
                "timeout", "terminal_lost", "send_failed", "no_receipt",
            ):
                await self._finalize("error", handoff_source)
                return
            if _handoff_ok(handoff):
                break
            log.warning(
                "watcher(%s): handoff file missing after attempt %d",
                self.agent_id, attempt,
            )

        if _handoff_ok(handoff):
            await self._finalize("done", source)
        else:
            self._update(handoff_path="")
            await self._finalize("done", "handoff_missing")

    async def _wait_ready(self) -> str | None:
        """Wait until the worker terminal can accept the next prompt.

        Pane state comes first: a pane Orca tracks is ready when
        `worktree ps` reports it "done" (such a pane can be ready even
        while `terminal wait --for tui-idle` would hang, e.g. Claude's
        agents-awaiting-input UI). `tui-idle` is the fallback for panes
        Orca does not track — but Orca only emits tui-idle for TUIs it
        recognizes, so a TUI it neither tracks nor recognizes (e.g.
        kimi, Claude Code v2) is judged on its terminal preview
        instead: a known ready-screen marker, or a composer / status-bar
        signature plus a short silence window, means ready now;
        otherwise output must stay silent for READY_IDLE_STABLE_MS. Readiness is never treated as task
        completion: the task prompt is always sent and observed as its
        own turn afterwards.

        Returns the readiness source ("ps_idle" / "tui_idle" /
        "preview_marker" / "output_idle"), or None on timeout.
        """
        deadline = time.monotonic() + READY_TIMEOUT_MS / 1000
        dialog_notified = False
        while time.monotonic() < deadline:
            if self.pane is None:
                self.pane = await _pane_key(self.handle)
                if self.pane:
                    self._update(pane_key=self.pane)
                    self._event("pane_detected", data={"pane_key": self.pane})
            entry = None
            if self.pane:
                try:
                    entry = await _ps_entry(self.pane)
                except OrcaError:
                    entry = None
            if entry is not None:
                # Tracked pane: "done" means ready; anything else (a
                # turn still running on an adopted pane) means wait.
                if entry.get("state") == "done":
                    return "ps_idle"
            else:
                if await self._wait_tui_idle():
                    return "tui_idle"
                try:
                    last, preview = await self._terminal_snapshot()
                except OrcaError:
                    last, preview = 0, ""
                # A modal dialog (e.g. kimi's first-run "Trust this
                # folder?") means the composer is not the input target:
                # silence here is a blocked screen, never readiness.
                if any(m in preview for m in BLOCKING_DIALOG_MARKERS):
                    if not dialog_notified:
                        dialog_notified = True
                        self._event(
                            "blocking_dialog_detected",
                            level="ERROR",
                            message="modal dialog is up; "
                                    "the prompt would be swallowed",
                        )
                        await self._notify(
                            f"# [osw] agent-dialog agent={self.agent_id}"
                            f" terminal={self.handle}"
                            " (a modal dialog is blocking the agent; "
                            "answer it directly in that terminal)"
                        )
                elif last:
                    silent_ms = time.time() * 1000 - last
                    if any(marker in preview for marker in READY_PREVIEW_MARKERS):
                        return "preview_marker"
                    if silent_ms >= READY_PREVIEW_SILENCE_MS and (
                            _composer_visible(preview)
                            or any(m in preview for m in READY_SCREEN_MARKERS)):
                        return "preview_marker"
                    if silent_ms >= READY_IDLE_STABLE_MS:
                        return "output_idle"
            await anyio.sleep(POLL_SECS)
        return None

    async def _pane_done_state_started(self) -> int:
        """stateStartedAt of the pane's current "done" entry, or 0.

        Captured before a prompt is sent: a pre-existing "done" state
        can fall inside CLOCK_SKEW_MS of the send and must never be
        mistaken for completion of the turn that is about to start.
        """
        if self.pane is None:
            return 0
        try:
            entry = await _ps_entry(self.pane)
        except OrcaError:
            return 0
        if entry is not None and entry.get("state") == "done":
            return int(entry.get("stateStartedAt") or 0)
        return 0

    async def _run_turn(self, text: str, phase: str) -> str:
        """Send one prompt and wait for the turn to complete.

        Returns the completion source: "ps_done", "tui_idle",
        "fallback_idle", or one of the failure modes
        "send_failed"/"timeout"/"terminal_lost"/"no_receipt".
        """
        sent_at_ms = time.time() * 1000
        baseline_state_started = await self._pane_done_state_started()
        try:
            await self._send_terminal(text)
        except OrcaError as exc:
            log.error("watcher(%s): send failed: %s", self.agent_id, exc)
            self._event(
                "turn_send_failed",
                level="ERROR",
                message=str(exc),
                data={"phase": phase},
            )
            return "send_failed"
        self._update(state="working", phase=phase, tool_name="")
        self._event(
            "turn_sent",
            data={"phase": phase, "chars": len(text)},
        )
        # Belt-and-suspenders against a swallowed prompt (modal dialog,
        # login screen): the text must be visible in the terminal's
        # scrollback afterwards. A miss only alarms — it never resends:
        # resent text lands in a working agent's input queue whenever
        # the miss is false (a full-screen TUI whose echo the check
        # cannot see made every miss false), and genuine non-receipt
        # already fails closed in the turn supervision below.
        echoed = await self._verify_prompt_echo(text)
        if echoed is False:
            log.warning(
                "watcher(%s): prompt not visible in terminal scrollback",
                self.agent_id,
            )
            self._event(
                "prompt_echo_missing",
                level="ERROR",
                message="prompt not visible in terminal scrollback",
                data={"phase": phase},
            )
            await self._notify(
                f"# [osw] prompt-echo-unverified agent={self.agent_id}"
                f" terminal={self.handle}"
                " (prompt may have been swallowed; check that terminal)"
            )
        source = await self._wait_turn_done(
            sent_at_ms, baseline_state_started, sent_text=text
        )
        self._event(
            "turn_finished",
            data={"phase": phase, "source": source},
        )
        return source

    async def _wait_turn_done(
        self, sent_at_ms: float, baseline_state_started: int = 0,
        sent_text: str = "",
    ) -> str:
        try:
            baseline_out = await self._last_output_at()
        except OrcaError:
            baseline_out = 0
        started = False
        entry_seen = False   # Orca showed a ps entry for this pane this turn
        saw_working = False  # ...and reported it "working" at least once
        receipt = False      # evidence the CLI actually took our prompt
        sent_flat = one_line(sent_text)
        echo_absorbed = False
        misses = 0
        activity_observed = False
        ask_notified = False
        stall_notified = False
        background_defer_notified = False

        while time.monotonic() < self.deadline:
            await anyio.sleep(POLL_SECS)
            self._flush_state()

            # Receipt check: a pane Orca tracks must acknowledge the
            # prompt (turn working, fresh done, or the prompt echoed in
            # ps) within the receipt window — otherwise the input was
            # swallowed by something that is not the agent's composer
            # (login screen, update dialog, crash remains): fail closed
            # now and tell the caller, instead of idling to the hard
            # timeout.
            if (entry_seen and not receipt
                    and time.time() * 1000 - sent_at_ms > RECEIPT_TIMEOUT_MS):
                log.error(
                    "watcher(%s): prompt never acknowledged by the pane",
                    self.agent_id,
                )
                self._event(
                    "prompt_receipt_missing",
                    level="ERROR",
                    message="tracked pane never acknowledged the prompt",
                )
                await self._notify(
                    f"# [osw] prompt-not-received agent={self.agent_id}"
                    f" terminal={self.handle}"
                    " (input may have been swallowed; check that terminal)"
                )
                return "no_receipt"

            if not echo_absorbed:
                # The prompt we just sent advances lastOutputAt all by
                # itself when the terminal renders it; fold that echo
                # into the baseline so it never counts as agent output.
                echo_absorbed = True
                try:
                    baseline_out = max(
                        baseline_out, await self._last_output_at()
                    )
                except OrcaError:
                    pass

            entry = None
            if self.pane is not None:
                try:
                    entry = await _ps_entry(self.pane)
                except OrcaError:
                    continue

            if entry is not None:
                entry_seen = True
                misses = 0
                if not receipt and sent_flat:
                    # Auxiliary receipt only: ps may mangle non-ASCII
                    # prompt text on Windows, so a match counts but a
                    # mismatch proves nothing.
                    reported = one_line(entry.get("prompt") or "")
                    if reported and (
                        reported == sent_flat
                        or reported.startswith(sent_flat[:32])
                    ):
                        receipt = True
                state = entry.get("state")
                state_started = int(entry.get("stateStartedAt") or 0)
                if (state == "done"
                        and state_started >= sent_at_ms - CLOCK_SKEW_MS
                        and state_started > baseline_state_started):
                    self._event(
                        "task_done_observed",
                        data={"source": "ps_done",
                              "state_started_at": state_started},
                    )
                    return "ps_done"
                if state == "working":
                    saw_working = True
                    receipt = True
                    if not activity_observed:
                        activity_observed = True
                        self._event(
                            "task_activity_observed",
                            data={"source": "ps_working"},
                        )
                    tool = entry.get("toolName") or ""
                    self._update(tool_name=tool)
                    if tool == "AskUserQuestion" and not ask_notified:
                        ask_notified = True
                        await self._notify(
                            f"# [osw] agent-waiting agent={self.agent_id}"
                            f" tool=AskUserQuestion terminal={self.handle}"
                            " (answer it directly in that terminal)"
                        )
                    updated = int(entry.get("updatedAt") or 0)
                    frozen_ms = time.time() * 1000 - updated
                    if (updated and frozen_ms > STUCK_NOTIFY_SECS * 1000
                            and not stall_notified):
                        stall_notified = True
                        await self._notify(
                            f"# [osw] agent-stalled agent={self.agent_id}"
                            f" terminal={self.handle}"
                            f" no-activity={int(frozen_ms / 1000)}s"
                        )
                continue

            if entry_seen:
                # Orca tracked this pane and the entry vanished: the
                # terminal died, or the agent TUI exited to a shell.
                # Give transient ps blips a grace period first.
                misses += 1
                if misses < 3:
                    continue
                if misses == 3:
                    log.warning(
                        "watcher(%s): agent no longer tracked by Orca, "
                        "falling back to idle detection", self.agent_id,
                    )

            # No ps entry for this pane (never tracked, or tracking
            # lost): idle heuristics on lastOutputAt. Keep polling ps
            # above regardless — Orca may simply not have registered a
            # freshly spawned pane yet, and real evidence beats these
            # heuristics the moment it appears.
            try:
                last, preview = await self._terminal_snapshot()
            except OrcaError:
                return "terminal_lost"
            if last > baseline_out:
                started = True
                if not activity_observed:
                    activity_observed = True
                    self._event(
                        "task_activity_observed", data={"source": "output"},
                    )
            # The quick tui-idle exit needs task-correlated ps evidence:
            # this pane demonstrably worked on this turn and then fell
            # out of tracking (e.g. the TUI exited when done). A pane
            # Orca never tracked gets no shortcut — it must ride the
            # slow stable-silence path below.
            if entry_seen and saw_working and started \
                    and await self._wait_tui_idle():
                self._event(
                    "task_done_observed", data={"source": "tui_idle"},
                )
                return "tui_idle"
            if started and (time.time() * 1000 - last) >= FALLBACK_IDLE_STABLE_MS:
                # An idle TUI is not a finished turn while the agent's
                # own background tasks are still running: kimi parks at
                # the composer with a "[N task(s) running]" badge and
                # waits for their completion notifications. Defer
                # completion until the badge clears.
                if BACKGROUND_TASK_BADGE_RE.search(preview):
                    if not background_defer_notified:
                        background_defer_notified = True
                        self._event("background_tasks_defer_completion")
                    continue
                self._event(
                    "task_done_observed", data={"source": "fallback_idle"},
                )
                return "fallback_idle"

        return "timeout"

    async def _finalize(
        self, final_state: str, source: str, error: str = ""
    ) -> None:
        finished_at = now_iso()

        last_msg = ""
        if self.pane:
            try:
                entry = await _ps_entry(self.pane)
                if entry:
                    last_msg = entry.get("lastAssistantMessage") or ""
            except OrcaError:
                pass

        report_payload = {
            "version": 3,
            "event": "task_finished",
            "agent_id": self.agent_id,
            "terminal": self.handle,
            "state": final_state,
            "completion_source": source,
            "prompt": self.agent.get("prompt", ""),
            "created_at": self.agent.get("created_at"),
            "finished_at": finished_at,
            "handoff_path": self.agent.get("handoff_path") or "",
            "last_assistant_message": last_msg,
            "error": error,
        }
        report_path = write_report(self.root, self.agent_id, report_payload)
        log.info("watcher(%s): report written to %s", self.agent_id, report_path)
        self._event(
            "report_written",
            data={"report": str(report_path), "state": final_state, "source": source},
        )

        self._update(
            state=final_state,
            phase="",
            completion_source=source,
            report_file=str(report_path),
            finished_at=finished_at,
        )

        if final_state == "error":
            line = (
                f"# [osw] task-error agent={self.agent_id}"
                f" terminal={self.handle} reason={source}"
                f" report={report_path}"
            )
        else:
            line = format_completion_report(
                self.agent_id, self.handle, str(report_path),
                handoff_file=self.agent.get("handoff_path") or "",
                summary=last_msg,
            )
        await self._notify(line)
        self._event(
            "watcher_finished",
            level="ERROR" if final_state == "error" else "INFO",
            message=error,
            data={"state": final_state, "source": source},
        )
        log.info("watcher(%s): finished  state=%s source=%s",
                 self.agent_id, final_state, source)


def _handoff_ok(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Process entry point (invoked as `osw.py watch <agent_id>`)
# ---------------------------------------------------------------------------

async def run_watcher(root: Path, agent_id: str) -> None:
    await Watcher(root, agent_id).run()


def main(root: Path, agent_id: str) -> None:
    setup_logging()
    enable_file_logging(logs_dir(root))
    enable_file_logging(logs_dir(root), prefix=agent_id)
    try:
        anyio.run(run_watcher, root, agent_id)
    except StateWriteError as exc:
        log.exception("watcher(%s): state storage failed", agent_id)
        try:
            agent = read_agent(root, agent_id)
            emit_event(
                logs_dir(root),
                component="watcher",
                event="state_storage_error",
                level="ERROR",
                agent_id=agent_id,
                terminal=str(agent.get("terminal") or ""),
                message=str(exc),
                data={"path": str(exc.path)},
            )
        except OSError:
            pass
        raise
    except Exception:
        log.exception("watcher(%s): crashed", agent_id)
        try:
            agent = read_agent(root, agent_id)
            emit_event(
                logs_dir(root),
                component="watcher",
                event="watcher_crashed",
                level="ERROR",
                agent_id=agent_id,
                terminal=str(agent.get("terminal") or ""),
                message="unhandled watcher exception",
            )
        except OSError:
            pass
        try:
            agent = read_agent(root, agent_id)
            agent["state"] = "error"
            agent["completion_source"] = "watcher_crash"
            agent["updated_at"] = now_iso()
            write_agent(root, agent)
        except OSError:
            pass
        raise

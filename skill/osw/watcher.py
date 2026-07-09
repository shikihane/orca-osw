"""Per-agent background supervisor.

One watcher process per dispatched task, spawned detached by `osw new`
/ `osw use` and gone when the task is settled. No daemon: Orca itself
is the source of truth (`worktree ps` reports each recognized agent
TUI's state/prompt/tool), the watcher just polls it and drives a
two-phase completion flow:

  phase "task":     send the prompt, wait until Orca reports the
                    agent's turn is done
  phase "handoff":  send the fixed wrap-up instruction, wait again,
                    then verify the handoff markdown exists

CLIs Orca does not recognize (no `agents` entry in `worktree ps`)
fall back to lastOutputAt idle detection.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import anyio

from osw.log import emit_event, enable_file_logging, get_logger, setup_logging
from osw.orca_cli import (
    OrcaError,
    terminal_read,
    terminal_send,
    terminal_show,
    terminal_wait,
    worktree_ps,
)
from osw.state import (
    logs_dir,
    new_handoff_path,
    read_agent,
    write_agent,
    write_report,
)

log = get_logger("watcher")

POLL_SECS = 2.0                    # worktree ps poll interval
READY_TIMEOUT_MS = 600_000         # max wait for the agent TUI to come up
FALLBACK_IDLE_STABLE_MS = 60_000   # untracked CLI: silent this long = turn over
HARD_TIMEOUT_SECS = 3600           # give up on the whole task after this
STUCK_NOTIFY_SECS = 300            # tracked agent frozen this long -> tell caller
CLOCK_SKEW_MS = 5_000              # tolerance comparing our clock vs Orca's
TRACK_LOST_IDLE_WAIT_MS = 1_000    # quick TUI idle check before slow fallback

# Fixed, machine-generated wrap-up instruction (single line: multi-line
# text gets truncated at newlines by orca.CMD --text on Windows). This
# is the only self-authored text OSW ever injects into a worker.
HANDOFF_TEMPLATE = (
    "Task wrap-up: the task above is finished. Write a concise English "
    "handoff document to {path} (markdown) covering: 1) what you did, "
    "2) key findings and decisions, 3) files you modified, 4) what "
    "remains or is blocked. Create the file even if the task was "
    "trivial, then stop; do not start any new work."
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def one_line(text: str, max_len: int = 0) -> str:
    """Collapse all whitespace runs into single spaces (Windows: any
    newline sent through orca.CMD --text truncates the message)."""
    flat = " ".join((text or "").split())
    if max_len and len(flat) > max_len:
        flat = flat[: max_len - 3] + "..."
    return flat


def scrub_tail(lines: list[str], limit: int = 40) -> list[str]:
    """Drop TUI redraw garbage from a terminal tail.

    Agent TUIs redraw their status bar every spinner frame; Orca's line
    capture concatenates those frames into multi-KB junk lines, all
    carrying the "esc to interrupt" hint. Content lines are re-emitted
    clean once they scroll out of the active area, so dropping the
    dirty ones loses nothing.
    """
    kept: list[str] = []
    blank = False
    for line in lines:
        if "esc to interrupt" in line:
            continue
        stripped = line.rstrip()
        if not stripped:
            if blank:
                continue
            blank = True
        else:
            blank = False
        kept.append(stripped)
    return kept[-limit:]


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

async def _last_output_at(handle: str) -> int:
    data = await terminal_show(handle)
    term = data.get("result", {}).get("terminal", {})
    return int(term.get("lastOutputAt") or 0)


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


async def _tui_idle(handle: str, timeout_ms: int = TRACK_LOST_IDLE_WAIT_MS) -> bool:
    try:
        await terminal_wait(handle, "tui-idle", timeout_ms=timeout_ms)
        return True
    except OrcaError:
        return False


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
        self.pane: str | None = None

    def _update(self, **fields) -> None:
        changed = False
        for key, value in fields.items():
            if self.agent.get(key) != value:
                self.agent[key] = value
                changed = True
        if changed:
            self.agent["updated_at"] = now_iso()
            write_agent(self.root, self.agent)

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
            return
        try:
            await terminal_send(caller, line)
        except OrcaError as exc:
            log.warning("notify failed (caller=%s): %s", caller, exc)

    async def run(self) -> None:
        self._update(watcher_pid=os.getpid(), state="starting")
        self._event("watcher_started", data={"pid": os.getpid()})
        log.info(
            "watcher(%s): started  terminal=%s  pid=%d",
            self.agent_id, self.handle, os.getpid(),
        )

        task_started_on_launch = bool(self.agent.get("task_started_on_launch"))
        try:
            self._event("ready_wait_started")
            ready_timeout = (
                HARD_TIMEOUT_SECS * 1000
                if task_started_on_launch else READY_TIMEOUT_MS
            )
            await terminal_wait(self.handle, "tui-idle", timeout_ms=ready_timeout)
            self._event("ready_wait_finished")
        except OrcaError as exc:
            log.error("watcher(%s): ready-wait failed: %s", self.agent_id, exc)
            self._event(
                "ready_wait_failed",
                level="ERROR",
                message=str(exc),
            )
            await self._finalize("error", "ready_failed", error=str(exc))
            return

        self.pane = await _pane_key(self.handle)
        if self.pane:
            self._update(pane_key=self.pane)
            self._event("pane_detected", data={"pane_key": self.pane})
        log.info("watcher(%s): pane=%s", self.agent_id, self.pane or "(unknown)")

        prompt = one_line(self.agent.get("prompt") or "")
        if not prompt:
            await self._finalize("done", "no_prompt")
            return

        # ---- Phase 1: the task itself ----
        if task_started_on_launch:
            source = "launch_tui_idle"
            self._update(state="working", phase="task", tool_name="")
            self._event(
                "turn_finished",
                data={"phase": "task", "source": source},
            )
        else:
            source = await self._run_turn(prompt, phase="task")
        if source in ("timeout", "terminal_lost", "send_failed"):
            await self._finalize("error", source)
            return
        log.info("watcher(%s): task turn done (source=%s)", self.agent_id, source)

        # ---- Phase 2: handoff document ----
        handoff = new_handoff_path(self.root, self.agent_id)
        self._update(handoff_path=str(handoff))
        instruction = HANDOFF_TEMPLATE.format(path=handoff)
        for attempt in (1, 2):
            handoff_source = await self._run_turn(instruction, phase="handoff")
            if handoff_source in ("timeout", "terminal_lost", "send_failed"):
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

    async def _run_turn(self, text: str, phase: str) -> str:
        """Send one prompt and wait for the turn to complete.

        Returns the completion source: "ps_done", "fallback_idle", or
        one of the failure modes "send_failed"/"timeout"/"terminal_lost".
        """
        sent_at_ms = time.time() * 1000
        try:
            await terminal_send(self.handle, text)
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
        source = await self._wait_turn_done(sent_at_ms)
        self._event(
            "turn_finished",
            data={"phase": phase, "source": source},
        )
        return source

    async def _wait_turn_done(self, sent_at_ms: float) -> str:
        try:
            baseline_out = await _last_output_at(self.handle)
        except OrcaError:
            baseline_out = 0
        started = False
        tracked = self.pane is not None
        misses = 0
        ask_notified = False
        stall_notified = False

        while time.monotonic() < self.deadline:
            await anyio.sleep(POLL_SECS)

            entry = None
            if tracked:
                try:
                    entry = await _ps_entry(self.pane)
                except OrcaError:
                    continue

            if entry is not None:
                misses = 0
                state = entry.get("state")
                state_started = int(entry.get("stateStartedAt") or 0)
                if state == "done" and state_started >= sent_at_ms - CLOCK_SKEW_MS:
                    return "ps_done"
                if state == "working":
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

            if tracked:
                # Orca tracked this pane but the entry vanished: the
                # terminal died, or the agent TUI exited to a shell.
                misses += 1
                if misses < 3:
                    continue
                try:
                    current_out = await _last_output_at(self.handle)
                except OrcaError:
                    return "terminal_lost"
                if current_out > baseline_out:
                    started = True
                    baseline_out = current_out
                else:
                    started = False
                    baseline_out = current_out
                log.warning(
                    "watcher(%s): agent no longer tracked by Orca, "
                    "falling back to idle detection", self.agent_id,
                )
                if await _tui_idle(self.handle):
                    return "tui_idle"
                tracked = False
                continue

            # Untracked CLI (e.g. pi): idle heuristics on lastOutputAt.
            try:
                last = await _last_output_at(self.handle)
            except OrcaError:
                return "terminal_lost"
            if last > baseline_out:
                started = True
            if started and (time.time() * 1000 - last) >= FALLBACK_IDLE_STABLE_MS:
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

        # The terminal tail is evidence, not content: only captured when
        # something went wrong and there is no handoff to read instead.
        output_tail: list[str] = []
        terminal_status = ""
        if final_state == "error" or source == "handoff_missing":
            try:
                data = await terminal_read(self.handle, limit=100)
                term = data.get("result", {}).get("terminal", {})
                output_tail = scrub_tail(term.get("tail") or [])
                terminal_status = term.get("status", "")
            except OrcaError as exc:
                log.warning(
                    "watcher(%s): failed to capture tail: %s", self.agent_id, exc,
                )

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
            "terminal_status": terminal_status,
            "output_tail": output_tail,
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

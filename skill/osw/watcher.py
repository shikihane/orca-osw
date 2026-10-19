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
submitted message can never appear). The check pages past the
newest screenful when the read cursors say more is retained, then
judges only surfaces that can actually host a conversation — a tail
reaching just a fraction of the retained buffer, or one that is only
a full-screen TUI's repaint frames, has no echo surface, and judging
there is a guaranteed false miss, so the check abstains instead.
A missing echo on a usable surface alarms the caller only when ps
does not track the pane: on a tracked pane the ps receipt window is
authoritative, and a tracked alternate-screen TUI (Claude Code) never
renders its conversation into scrollback, so every miss there is
false by construction. The alarm never triggers a resend either way —
a false miss would otherwise inject a duplicate prompt into a working
agent's input queue.

Orca's send-time effect confirmation is held to the same standard:
`agent_prompt_stalled` means the write landed but Orca saw no fresh
idle->working edge within its short window — typical for a prompt
arriving at a TUI that is already "working" (a fresh Claude Code
session still on its startup turn) — and so is a split send whose
composer-echo wait elapsed. The turn proceeds to the receipt window,
which is authoritative: like the echo miss, an unconfirmed send
alarms the caller only on a pane ps does not track. Only genuine
write failures (`terminal_not_writable`, unrecoverable stale handle,
a permission modal swallowing the input) fail the send.

CLIs Orca does not recognize (no `agents` entry in `worktree ps`)
fall back to lastOutputAt idle detection. That fallback never counts
the prompt's own terminal echo as output, and the quick tui-idle exit
is reserved for panes that demonstrably worked on this turn before
falling out of tracking — a pane Orca never tracked (e.g. one spawned
so recently that ps has no entry yet) must wait out the slow
stable-silence window instead.

A record carrying `follow_turn: true` takes over the turn already
running in the pane instead: no ready wait, no prompt, no handoff —
just the task-phase completion detection, then the report and the
caller notification.
"""

from __future__ import annotations

import functools
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import anyio

from osw.log import emit_event, enable_file_logging, get_logger, setup_logging
from osw.orca_cli import (
    OrcaError,
    orchestration_inbox,
    orchestration_worker_retain,
    orchestration_worker_show,
    terminal_interrupt,
    terminal_list,
    terminal_read,
    terminal_send,
    terminal_send_dialog_enter,
    terminal_send_escape,
    terminal_show,
    terminal_wait,
    worker_asks,
    worker_settlement,
    worktree_ps,
)
from osw.ready_gate import (
    COMPOSER_WAIT_MS,
    GATE_POLL_SECS,
    GATE_PROVIDERS,
    gate_verdict,
    normalized_hash,
    sanitize_frame,
)
from osw import blockers
from osw.jev import (
    HOLD_BUDGET_MS,
    HOLD_FALSE_HOLD_CAP_MS,
    HOLD_MIN_REASK_MS,
    HOLD_MIN_REMAINING_MS,
    ask_blocker,
    ask_ready,
    enforce_active,
    notify_class,
    resolve_mode,
    startup_should_hold,
)
from osw.sessions import session_for_pane
from osw.state import (
    StateWriteError,
    clear_stop_request,
    logs_dir,
    new_handoff_path,
    read_agent,
    read_stop_request,
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
SUPERVISED_POLL_SECS = 3.0         # orchestration worker-show poll interval
ASK_INBOX_LIMIT = 100              # newest inbox rows scanned for worker asks
ASK_TEXT_MAX = 160                 # ask summary length in the notify line

# Blocking screens (plan §2.2; policy in osw.blockers). A send Orca
# refuses with `agent_prompt_blocked` never reached the agent, so it is
# retried after the blocker is handled and the pane is ready again — at
# most BLOCKED_SEND_LIMIT times, BLOCKED_RETRY_SECS apart.
BLOCKED_SEND_LIMIT = 5
BLOCKED_RETRY_SECS = 20.0
BLOCKER_EXCERPT_CHARS = 120        # screen excerpt in the blocked notice
BLOCKER_EXCERPT_LINES = 6          # bottom non-empty lines it is cut from

# Symbolic whitelist keys -> name of the Orca send that presses them
# (resolved at call time).
BLOCKER_KEY_SENDERS = {
    "enter": "terminal_send_dialog_enter",
    "esc": "terminal_send_escape",
    "interrupt": "terminal_interrupt",
}
HANDOFF_FALLBACK_TIMEOUT_SECS = 600.0  # wrap-up fallback: wait for the file
HANDOFF_FALLBACK_POLL_SECS = 2.0   # wrap-up fallback: file stability cadence

# Two-stage send for renderGate providers (design §12.4-1): Orca's
# agent-prompt send waits for a cursor marker current Claude Code never
# emits, so every claude/codex prompt pays the gate's full 8s timeout.
# Affected providers instead write the text *without* Enter (raw path,
# no gate), confirm it reached the composer via a bounded --screen poll,
# then send Enter alone. A confirmation timeout still sends the Enter —
# "force submit at deadline", the same semantics as Orca's gate, with
# the cap at 2s instead of 8s — and the text is never resent.
SPLIT_SEND_PROVIDERS = ("claude",)   # codex shares the gate; its TUI is unverified here
SPLIT_SEND_CONFIRM_SECS = 2.0        # bounded composer-echo wait after the paste
SPLIT_SEND_POLL_SECS = 0.5           # --screen poll interval inside that wait

# Completion attribution guards (design §1.5): a fresh "done" is only
# accepted after it survives all three — it must not belong to a foreign
# agent session reporting states under this pane (prompt attribution),
# it must persist across two polls, and the pane's output must have been
# silent for COMPLETION_SILENCE_MS. Together they keep a nested
# session's done (or any flickering done) from ending a turn that is
# still running.
COMPLETION_SILENCE_MS = 3_000

# A stopped turn (`osw stop`, plan §1.8) is over once ps reports it
# interrupted or done, or the pane has been output-silent this long:
# claude and kimi stop for real, but ps keeps their pre-interrupt
# "working" snapshot, so silence is the only signal there.
STOP_SILENCE_MS = 5_000

# Fixed, machine-generated wrap-up instruction (single line: multi-line
# text gets truncated at newlines by orca.CMD --text on Windows). This
# and RESUME_TEMPLATE are the only self-authored texts OSW ever injects
# into a worker.
HANDOFF_TEMPLATE = (
    "Task wrap-up: the task above is finished. Write a concise English "
    "handoff document to {path} (markdown) covering: 1) what you did, "
    "2) key findings and decisions, 3) files you modified, 4) what "
    "remains or is blocked. If any of your background tasks are still "
    "running, wait for them to finish and fold their results into the "
    "handoff before writing the file. Create the file even if the task "
    "was trivial, then stop; do not start any new work."
)

# Task prompt of a revived session whose interrupted task should go on
# (record flag `resume_turn`, plan §1.7).
RESUME_TEMPLATE = "continue"

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
# A default `terminal read` exposes only the pane's newest screenful,
# even when its cursors span a far larger retained buffer (kimi's
# waiting screen reads back as a single line). When the cursors say
# more is retained than was returned, the check pages the real
# scrollback with an explicit cursor before judging — judging on the
# screenful alone produced false misses on full-screen TUIs.
#
# Even the paged tail is judged for usability before the match runs
# (see _unusable_echo_surface): a surface that cannot host the
# conversation makes a miss meaningless and a hit worse than
# meaningless — on repaint frames the only matchable text is a
# swallowed prompt still sitting in the repainted composer box, which
# must not count as delivery. Either unusable shape means abstain.
PROMPT_ECHO_DELAY_SECS = 3.0
PROMPT_ECHO_MIN_CHARS = 8
PROMPT_ECHO_PREFIX_CHARS = 24
PROMPT_ECHO_ISLAND_CHARS = 16
PROMPT_ECHO_READ_LINES = 400
PROMPT_ECHO_MIN_SURFACE_LINES = 16  # below this, thinness cannot be judged
PROMPT_ECHO_SURFACE_MIN_RATIO = 4   # tail must cover >= 1/4 of reachable lines
PROMPT_ECHO_REDRAW_MIN_LINES = 32   # redraw check only on tails this long
PROMPT_ECHO_REDRAW_MIN_REPEAT = 3.0  # avg line repeats this much = frames


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


def _send_accepted(data: dict) -> bool:
    """Whether Orca's send receipt accepted a raw input write.

    `result.send.accepted` is a negative-only signal (design §12.3):
    hosts that predate the field accepted the legacy raw input, so a
    missing field counts as accepted. Only an explicit `False` means
    the write did not land.
    """
    result = data.get("result")
    send = result.get("send") if isinstance(result, dict) else None
    if not isinstance(send, dict):
        return True
    return send.get("accepted") is not False


def _find_agent_wait(value, depth: int = 0):
    """The first `agentWait` value anywhere in an Orca error payload."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        if value.get("agentWait"):
            return value["agentWait"]
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = _find_agent_wait(child, depth + 1)
        if found:
            return found
    return None


def _blocked_observation(exc: OrcaError) -> dict:
    """Observation for `blockers.detect` from an `agent_prompt_blocked`
    send error. Orca blocked the prompt even when it names no wait, so
    the error code itself stands in as an (unnamed) reason."""
    wait = _find_agent_wait(exc.payload)
    return {"agentWait": wait or {"reason": exc.code or "agent_prompt_blocked"}}


def _dialog_only(answer: dict | None) -> dict | None:
    """A ready_state answer reduced to its `blocked_dialog` probability.

    A booting TUI is the normal `blocked_startup` case and has its own
    bounded hold; only a dialog judgment is evidence of a blocking
    screen at a ready release.
    """
    if not answer:
        return None
    p = (answer.get("probabilities") or {}).get("blocked_dialog")
    return {"probabilities": {"blocked_dialog": p}} if p is not None else None


def _shares_run(a: str, b: str, width: int = 12) -> bool:
    """Whether `a` and `b` share any `width`-char substring (stepped 4)."""
    if len(a) < width or len(b) < width:
        return False
    for start in range(0, len(a) - width + 1, 4):
        if a[start:start + width] in b:
            return True
    return False


def _foreign_done(entry: dict, sent_flat: str) -> bool:
    """Whether a fresh "done" belongs to a foreign agent session.

    A nested agent launched inside this pane with the pane's inherited
    ORCA_* hook environment reports its own states to Orca under this
    pane (design §1.5). The tell is a ps `prompt` that is ours neither
    verbatim nor by receipt prefix. Only pure-ASCII ps prompts are
    judged: a CJK prompt may read back mangled on Windows, and a
    partially mangled echo still shares long runs with what we sent —
    text sharing no 12-char run with our prompt is what proves a
    different session. Anything unreadable fails open to "not foreign";
    the persistence and silence guards still apply on their own.
    """
    reported = one_line(str(entry.get("prompt") or ""))
    if not reported or not sent_flat or not reported.isascii():
        return False
    if reported == sent_flat or reported.startswith(sent_flat[:32]):
        return False
    return not _shares_run(reported, sent_flat)


def _unusable_echo_surface(tail: list[str], retained: int | None) -> str | None:
    """Why `tail` cannot host a conversation echo, or None if it can.

    Two shapes of pane have no echo surface, and judging the echo on
    either is a guaranteed false miss:

    "thin" — the read reaches only a fraction of the pane's retained
    buffer: the TUI runs on the alternate screen (or Orca otherwise
    exposes just the current screenful), so the conversation, if it is
    rendered anywhere at all, is out of reach. Too short a reachable
    window cannot be judged for thinness — a genuinely short surface
    (trust dialog, fresh pane) stays usable so a miss there still
    alarms.

    "redraw" — the tail is just a full-screen TUI's repaint frames:
    Claude Code mid-turn repaints its whole screen (spinner, status
    bar, composer hint, path) into the scrollback every frame, so
    hundreds of retained lines hold the same few distinct contents
    once per frame. A real conversation consists of lines seen once;
    frames make the average line repeat many times. Letters-only
    normalization (strip everything that is not a Unicode letter)
    collapses the frame-to-frame churn first — the spinner glyph
    cycles (✶ ✢ ✽ * ·), counters change digits, padding follows the
    counter width, and Windows mangles non-ASCII into stray
    surrogates. Lines that normalize to nothing (box-drawing rules,
    digit-only rows, progress bars) are screen furniture, not
    content, and are dropped so they can neither inflate the ratio on
    a conversation nor hide a flood; a long tail of furniture alone
    is itself redraw-only. Only a long tail is judged this way at
    all: a short one is thin but genuine (see "thin" above).
    """
    reachable = min(PROMPT_ECHO_READ_LINES, retained) \
        if retained is not None else len(tail)
    if (reachable >= PROMPT_ECHO_MIN_SURFACE_LINES
            and len(tail) * PROMPT_ECHO_SURFACE_MIN_RATIO < reachable):
        return "thin"
    if len(tail) >= PROMPT_ECHO_REDRAW_MIN_LINES:
        lines = [
            re.sub(r"[^\w]|[\d_]", "", line) for line in tail if line.strip()
        ]
        lines = [line for line in lines if line]
        if not lines:
            return "redraw"
        if len(lines) / len(set(lines)) >= PROMPT_ECHO_REDRAW_MIN_REPEAT:
            return "redraw"
    return None


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


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO timestamp (naive means UTC); None if unparseable."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def one_line(text: str, max_len: int = 0) -> str:
    """Collapse all whitespace runs into single spaces (Windows: any
    newline sent through orca.CMD --text truncates the message)."""
    flat = " ".join((text or "").split())
    if max_len and len(flat) > max_len:
        flat = flat[: max_len - 3] + "..."
    return flat


def format_completion_report(
    agent_id: str, terminal: str, report_file: str,
    handoff_file: str = "", summary: str = "", outcome: str = "",
    warning: str = "",
) -> str:
    """Single-line completion notification sent to the caller terminal.

    Single-line because newlines get mangled through orca.CMD --text on
    Windows; the leading '#' keeps it inert in a shell. Full data lives
    in the JSON report and the handoff markdown.
    """
    message = f"# [osw] task-finished agent={agent_id} terminal={terminal}"
    if outcome:
        message += f" outcome={one_line(outcome, 40)}"
    if warning:
        message += f" warning={one_line(warning, 40)}"
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
        self._gate_since: float | None = None
        self._gate_last: tuple[str, str] | None = None
        self._gate_call_pending = False
        self._hold: dict | None = None
        self._ready_deadline = 0.0
        self._notified_classes: set[str] = set()
        self._task_group = None
        self._stop_at = ""  # stop request time, once observed
        # Blocking-screen handling (plan §2.2), per watcher run:
        self._blockers_acted: set[str] = set()     # kinds keys went out for
        self._blockers_stopped: set[str] = set()   # kinds no longer automated
        self._blocker_notices: set[str] = set()    # notices already sent
        self._blocker_shadowed: set[str] = set()   # shadow actions recorded
        self._blocker_last: tuple[str, str] | None = None
        self._update_lock_held = False
        # Tool activity (plan §3.1): per turn, from ps `toolName` while
        # the turn is working; the task turn's verdict goes in the report.
        self._turn_tracked = False
        self._turn_tools_seen = False
        self._task_turn_ran = False
        self._task_tools_observed: bool | None = None

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

    def _discard_stale_stop_request(self) -> None:
        """Drop a stop request older than this run's record.

        `osw stop` writes the sidecar only while a watcher runs, but a
        watcher that died before finalizing leaves it behind; it must
        not interrupt the next task dispatched on this record. A request
        written after the record was created belongs to this run (it
        may land before the watcher process is up) and is kept.
        """
        requested = read_stop_request(self.root, self.agent_id)
        if not requested:
            return
        requested_at = _parse_iso(requested)
        created_at = _parse_iso(str(self.agent.get("created_at") or ""))
        if requested_at is None or created_at is None \
                or requested_at >= created_at:
            return
        try:
            clear_stop_request(self.root, self.agent_id)
        except OSError:
            pass
        self._event(
            "stale_stop_request_discarded",
            data={"requested_at": requested,
                  "created_at": self.agent.get("created_at")},
        )

    def _stop_requested(self) -> bool:
        """Whether `osw stop` asked to interrupt this agent.

        Polls the `.stop` sidecar (the CLI must not write the record
        while the watcher owns it) and mirrors the request time into the
        record once.
        """
        if self._stop_at:
            return True
        requested = read_stop_request(self.root, self.agent_id)
        if not requested:
            return False
        self._stop_at = requested
        self._update(stop_requested_at=requested)
        self._event("stop_request_observed", data={"requested_at": requested})
        log.info("watcher(%s): stop requested at %s", self.agent_id, requested)
        return True

    async def _stop_settled(self) -> str:
        """Why the stopped turn is over ("" while it still runs)."""
        entry = None
        if self.pane is not None:
            try:
                entry = await _ps_entry(self.pane)
            except OrcaError:
                entry = None
        if entry is not None:
            if entry.get("interrupted") is True:
                return "ps_interrupted"
            if entry.get("state") == "done":
                return "ps_done"
        if await self._output_silent(STOP_SILENCE_MS):
            return "output_silent"
        return ""

    # ---- blocking screens (plan §2.2) -----------------------------------

    @staticmethod
    def _blocker_screen(frame: dict | None) -> tuple[str, str]:
        """(sanitized frame text, one-line excerpt) of a --screen body."""
        term = (frame or {}).get("result", {}).get("terminal", {})
        tail = term.get("tail") if isinstance(term, dict) else None
        if not isinstance(tail, list):
            return "", ""
        lines = sanitize_frame([str(line) for line in tail])
        kept = [line.strip() for line in lines if line.strip()]
        excerpt = one_line(
            " | ".join(kept[-BLOCKER_EXCERPT_LINES:]), BLOCKER_EXCERPT_CHARS)
        return "\n".join(lines), excerpt

    async def _notify_blocked(
        self, kind: str, excerpt: str, note: str = "", stopped: bool = False,
    ) -> bool:
        """One `# [osw] blocked` notice per blocker (plus one when
        automatic handling stops). True when a notice went out."""
        key = f"{kind}:stopped" if stopped else kind
        if key in self._blocker_notices \
                or (not stopped and f"{kind}:stopped" in self._blocker_notices):
            return False
        self._blocker_notices.add(key)
        if stopped:
            note = note or "still blocked after automatic handling; stopped"
        self._event(
            "blocker_notified",
            data={"kind": kind, "stopped": stopped, "note": note},
        )
        line = (f"# [osw] blocked agent={self.agent_id} kind={kind}"
                f" terminal={self.handle}")
        if note:
            line += f" ({note})"
        line += f" screen={excerpt or '(unreadable)'}"
        await self._notify(line)
        return True

    async def _send_blocker_keys(self, keys: list[str]) -> None:
        for key in keys:
            sender = globals()[BLOCKER_KEY_SENDERS[key]]
            try:
                await sender(self.handle)
            except OrcaError as exc:
                if exc.code != "terminal_handle_stale" \
                        or not await self._rebind_terminal():
                    raise
                await sender(self.handle)

    def _release_update_lock(self, reason: str) -> None:
        if not self._update_lock_held:
            return
        provider = str(self.agent.get("provider") or "")
        try:
            blockers.release_update_lock(provider, self.agent_id)
        except OSError:
            pass
        self._update_lock_held = False
        self._event(
            "blocker_lock_released",
            data={"provider": provider, "reason": reason},
        )

    async def _check_blocker(
        self,
        observation: dict | None,
        ready_answer: dict | None,
        frame: dict | None,
        *,
        act: str = "send",
        point: str = "",
    ) -> str:
        """Handle a blocking screen, if there is one (plan §2.2).

        Discovery and policy live in `osw.blockers`: Orca's agentWait
        first, else the ready_state answer; the kind from Orca, else a
        `blocker_kind` judgment (never under mode off); then the mode
        decides. `act` says what an `act` decision may do here: "send"
        presses the whitelisted keys; "defer" leaves them to the
        foreground send path (a background judgment must never type);
        "notify" reports instead (the prompt is already in flight).

        Returns "none", "acted", "waiting" (update lock held elsewhere),
        "shadow", "deferred", "notified", or "stopped" (still blocked
        after acting, or the keys could not be sent — automatic handling
        of that kind is off for the rest of this run).
        """
        detection = blockers.detect(observation, ready_answer)
        if detection is None:
            return "none"
        provider = str(self.agent.get("provider") or "")
        mode = resolve_mode(self.root)
        if frame is None:
            frame = await self._read_gate_frame()
        text, excerpt = self._blocker_screen(frame)
        answer = None
        if not detection.kind and mode != "off" and frame is not None:
            answer = await ask_blocker(
                self.root, self.agent_id, provider, frame, mode)
        kind = blockers.classify(detection, answer)
        decision = blockers.decide(provider, kind, mode, text)
        if kind in self._blockers_stopped:
            decision = "notify"
        if self._blocker_last != (kind, decision):
            self._blocker_last = (kind, decision)
            self._event(
                "blocker_detected",
                data={"source": detection.source, "signal": detection.signal,
                      "p": detection.p, "kind": kind, "decision": decision,
                      "point": point},
            )
        keys = list(blockers.WHITELIST.get((provider, kind), ()))

        if decision == "shadow":
            if kind not in self._blocker_shadowed:
                self._blocker_shadowed.add(kind)
                self._event(
                    "blocker_shadow_action",
                    data={"kind": kind, "keys": keys, "point": point},
                )
            # Plan §3.2: nothing is pressed, so the guardian must hear
            # about the blocker exactly as under notify.
            await self._notify_blocked(kind, excerpt)
            return "shadow"
        if decision == "notify":
            await self._notify_blocked(kind, excerpt)
            return "notified"

        if act != "send":
            self._event(
                "blocker_action_deferred",
                data={"kind": kind, "keys": keys, "point": point,
                      "reason": ("background_judgment" if act == "defer"
                                 else "prompt_in_flight")},
            )
            if act == "notify":
                await self._notify_blocked(kind, excerpt)
                return "notified"
            return "deferred"
        if kind in self._blockers_acted:
            # Re-judged after acting and still blocked: stop automating.
            self._blockers_stopped.add(kind)
            self._release_update_lock("still_blocked")
            self._event(
                "blocker_still_blocked",
                level="WARNING",
                data={"kind": kind, "keys": keys, "point": point},
            )
            await self._notify_blocked(kind, excerpt, stopped=True)
            return "stopped"
        if kind == "update_prompt" and not self._update_lock_held:
            if not blockers.acquire_update_lock(provider, self.agent_id):
                if await self._notify_blocked(
                        kind, excerpt,
                        note="another pane is applying this update; waiting"):
                    self._event("blocker_lock_busy", data={"provider": provider})
                return "waiting"
            self._update_lock_held = True
            self._event("blocker_lock_acquired", data={"provider": provider})
        try:
            await self._send_blocker_keys(keys)
        except OrcaError as exc:
            self._blockers_stopped.add(kind)
            self._release_update_lock("action_failed")
            self._event(
                "blocker_action_failed",
                level="ERROR",
                message=str(exc),
                data={"kind": kind, "keys": keys, "point": point},
            )
            await self._notify_blocked(
                kind, excerpt, note="sending the keys failed; stopped",
                stopped=True)
            return "stopped"
        self._blockers_acted.add(kind)
        self._event(
            "blocker_action",
            data={"kind": kind, "keys": keys, "source": detection.source,
                  "signal": detection.signal, "point": point},
        )
        return "acted"

    async def _terminal_object(self) -> dict | None:
        try:
            data = await terminal_show(self.handle)
        except OrcaError:
            return None
        term = data.get("result", {}).get("terminal", {})
        return term if isinstance(term, dict) else None

    async def _blocker_probe(self, point: str) -> None:
        """One blocker look at a pane that stopped responding (no
        receipt, stall). The prompt is already in flight, so this only
        reports: pressing keys now could act on a working agent."""
        provider = str(self.agent.get("provider") or "")
        frame = await self._read_gate_frame()
        observation = await self._terminal_object()
        mode = resolve_mode(self.root)
        answer = None
        if mode != "off" and frame is not None:
            answer = await ask_ready(
                self.root, self.agent_id, provider, frame, mode, point)
        await self._check_blocker(
            observation, answer, frame, act="notify", point=point)

    def _note_turn_tools(self, phase: str) -> bool | None:
        """Tool activity of the turn that just ended (plan §3.1).

        True when ps reported a tool while the turn was working, False
        when ps tracked the pane but never did, None when ps never
        tracked it (nothing could be observed). The task turn's value
        is kept for the report.
        """
        if self._turn_tools_seen:
            tools: bool | None = True
        elif self._turn_tracked:
            tools = False
        else:
            tools = None
        if phase == "task":
            self._task_turn_ran = True
            self._task_tools_observed = tools
        return tools

    def _record_session(self) -> None:
        """Record the pane's provider session id after a turn settles.

        Best effort: Orca's hook cache is not a public interface, so an
        empty answer or any failure leaves the record (and the turn)
        exactly as it was.
        """
        if not self.pane:
            return
        try:
            session_id = session_for_pane(self.pane)
        except Exception as exc:
            self._event(
                "session_lookup_failed",
                level="WARNING",
                message=str(exc),
                data={"pane_key": self.pane},
            )
            return
        if session_id:
            self._update(session_id=session_id)

    def _notify_target(self) -> tuple[str, str]:
        """(terminal, pane_key) to notify, resolved fresh on every call.

        A guardian that is an OSW agent is stored by id only and read
        from its own record each time, so its current handle is used; an
        inline guardian carries its terminal and pane key. Without a
        resolvable guardian the caller terminal (no pane key) is used.
        """
        guardian = self.agent.get("guardian")
        if isinstance(guardian, dict) and guardian.get("agent_id"):
            guardian_id = str(guardian["agent_id"])
            try:
                record = read_agent(self.root, guardian_id)
            except (OSError, ValueError):
                record = None
            if record and record.get("terminal"):
                return str(record["terminal"]), str(record.get("pane_key") or "")
            self._event(
                "guardian_unresolved",
                level="WARNING",
                message="guardian agent record unreadable; "
                        "falling back to caller terminal",
                data={"guardian": guardian_id},
            )
        elif isinstance(guardian, dict) and guardian.get("terminal"):
            return str(guardian["terminal"]), str(guardian.get("pane_key") or "")
        return str(self.agent.get("caller_terminal") or ""), ""

    async def _rebind_notify_target(self, pane_key: str) -> str:
        """Current handle of the notify target's pane, or ""."""
        try:
            terminals = await terminal_list(f"path:{self.root}")
        except OrcaError:
            return ""
        for terminal in terminals:
            handle = terminal.get("handle")
            if handle and \
                    f"{terminal.get('tabId')}:{terminal.get('leafId')}" == pane_key:
                return str(handle)
        return ""

    async def _notify(self, line: str) -> None:
        target, pane_key = self._notify_target()
        if not target:
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
            await terminal_send(target, line)
            return
        except OrcaError as exc:
            failure = exc
        # Handles die with the Orca runtime; the pane key does not.
        # Rebind once and retry once.
        if failure.code == "terminal_handle_stale" and pane_key:
            rebound = await self._rebind_notify_target(pane_key)
            if rebound:
                self._event(
                    "notify_target_rebound",
                    data={"previous": target, "current": rebound},
                )
                guardian = self.agent.get("guardian")
                if isinstance(guardian, dict) and not guardian.get("agent_id"):
                    # Inline guardian lives in this record; a guardian
                    # agent's record belongs to its own watcher.
                    self._update(guardian={**guardian, "terminal": rebound})
                target = rebound
                try:
                    await terminal_send(target, line)
                    return
                except OrcaError as exc:
                    failure = exc
        log.warning("notify failed (caller=%s): %s", target, failure)
        self._event(
            "notify_failed",
            level="ERROR",
            message=f"failed to notify caller terminal: {failure}",
            data={"caller_terminal": target, "line": line},
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

    async def _terminal_read(self, limit: int, cursor: str | None = None) -> dict:
        try:
            return await terminal_read(self.handle, limit=limit, cursor=cursor)
        except OrcaError as exc:
            if exc.code != "terminal_handle_stale" \
                    or not await self._rebind_terminal():
                raise
            return await terminal_read(self.handle, limit=limit, cursor=cursor)

    async def _echo_surface(
        self, limit: int
    ) -> tuple[list[str], int | None] | None:
        """(tail lines, retained line count) for the pane.

        The retained count comes from the read cursors
        (latestCursor - oldestCursor) and is None when the response
        carries no cursors. None overall when the terminal cannot be
        read.

        A default read exposes only the pane's newest screenful (kimi's
        waiting screen can be a single line) while its cursors still
        span the whole retained buffer — so when the cursors say more
        is retained than was returned, page the real scrollback with an
        explicit cursor before judging. Judging on the screenful alone
        is what produced false `prompt_echo_missing` alarms on full-
        screen TUIs.
        """
        try:
            data = await self._terminal_read(limit)
        except OrcaError:
            return None
        term = data.get("result", {}).get("terminal", {})
        tail = term.get("tail")
        if not isinstance(tail, list):
            return None
        retained = None
        try:
            oldest = int(term["oldestCursor"])
            latest = int(term["latestCursor"])
            retained = max(0, latest - oldest)
        except (KeyError, TypeError, ValueError):
            oldest = latest = None
        if retained is not None and len(tail) < min(limit, retained):
            # Paging can only help while the default read returned
            # fewer lines than both the ask and the buffer; a full
            # read is already the newest `limit` lines and a paged
            # read from latest - limit would return the same window.
            start = max(oldest, latest - limit)
            try:
                paged = await self._terminal_read(limit, cursor=str(start))
            except OrcaError:
                paged = None
            if paged is not None:
                paged_tail = paged.get("result", {}).get(
                    "terminal", {}).get("tail")
                if isinstance(paged_tail, list) \
                        and len(paged_tail) > len(tail):
                    tail = paged_tail
        return [str(line) for line in tail], retained

    async def _send_terminal(self, text: str) -> bool:
        """Send `text`; True when the send was confirmed as delivered.

        Providers behind Orca's renderGate (SPLIT_SEND_PROVIDERS) take
        the two-stage raw send: the gate never settles on current Claude
        Code builds, so the legacy single-shot path costs a fixed ~8s on
        every prompt (design §12.4-1). Everyone else keeps the legacy
        path unchanged.

        Orca's send-time confirmation watches the TUI for a fresh
        non-working -> working edge within a short window. A TUI that is
        already "working" when the prompt lands (a fresh Claude Code
        session still on its startup turn) accepts the prompt without a
        new edge, so Orca reports `agent_prompt_stalled` even though the
        write succeeded. That is a delivery-unconfirmed signal, not a
        write failure: return False and let the receipt window below
        judge. Genuine write failures still raise.
        """
        provider = str(self.agent.get("provider") or "")
        if provider in SPLIT_SEND_PROVIDERS:
            return await self._split_send(text)
        try:
            data = await terminal_send(self.handle, text)
        except OrcaError as exc:
            if exc.code == "agent_prompt_stalled":
                return False
            if exc.code != "terminal_handle_stale" \
                    or not await self._rebind_terminal():
                raise
            data = await terminal_send(self.handle, text)
        if _send_accepted(data) is False:
            raise OrcaError(
                "terminal send refused (accepted=false)",
                returncode=1, code="send_not_accepted",
            )
        return True

    async def _split_send(self, text: str) -> bool:
        """Two-stage send for renderGate providers (design §12.4-1).

        Stage 1 writes the text without Enter (raw `sendTerminal` path,
        no renderGate); stage 2 waits a bounded SPLIT_SEND_CONFIRM_SECS
        for the composer echo; stage 3 sends Enter alone. On a
        confirmation timeout the Enter is still sent and False is
        returned — delivery is then judged by the receipt window, and
        because the text is never resent, a slow render can at worst
        submit an empty composer, never double-submit the prompt.
        """
        try:
            first = await terminal_send(self.handle, text, enter=False)
        except OrcaError as exc:
            if exc.code != "terminal_handle_stale" \
                    or not await self._rebind_terminal():
                raise
            first = await terminal_send(self.handle, text, enter=False)
        if _send_accepted(first) is False:
            raise OrcaError(
                "terminal send refused (accepted=false)",
                returncode=1, code="send_not_accepted",
            )
        confirmed = await self._await_composer_echo(text)
        # Enter-only write: a stale handle retries just the Enter, never
        # a second paste of the text.
        try:
            await terminal_send(self.handle, "", enter=True)
        except OrcaError as exc:
            if exc.code != "terminal_handle_stale" \
                    or not await self._rebind_terminal():
                raise
            await terminal_send(self.handle, "", enter=True)
        return confirmed

    async def _await_composer_echo(self, text: str) -> bool:
        """Bounded wait for the pasted text to reach the composer.

        Reads the rendered frame (`--screen`) every SPLIT_SEND_POLL_SECS
        until the sent prefix, the prompt's ASCII island, or Claude
        Code's `[Pasted text` collapse marker is visible, or
        SPLIT_SEND_CONFIRM_SECS elapses. A failing read shortens the
        wait instead of blocking the Enter that follows.
        """
        units = _prompt_echo_units(text)
        deadline = time.monotonic() + SPLIT_SEND_CONFIRM_SECS
        while True:
            try:
                data = await self._terminal_read_screen()
            except OrcaError:
                return False
            term = data.get("result", {}).get("terminal", {})
            tail = term.get("tail")
            if isinstance(tail, list):
                hay = "".join(
                    ch for ch in " ".join(str(line) for line in tail)
                    if ch.isprintable() and not ch.isspace()
                )
                if "[Pastedtext" in hay:
                    return True
                if any("".join(unit.split()) in hay for unit in units):
                    return True
            if time.monotonic() >= deadline:
                return False
            await anyio.sleep(SPLIT_SEND_POLL_SECS)

    async def _terminal_read_screen(self) -> dict:
        try:
            return await terminal_read(self.handle, screen=True)
        except OrcaError as exc:
            if exc.code != "terminal_handle_stale" \
                    or not await self._rebind_terminal():
                raise
            return await terminal_read(self.handle, screen=True)

    async def _verify_prompt_echo(self, text: str) -> bool | None:
        """Re-read the terminal scrollback and check the sent prompt's
        echo is there.

        Returns True/False, or None when no verdict is possible: the
        prompt carries no match unit long enough to be trustworthy, the
        terminal cannot be read, or the surface cannot host a
        conversation echo — a read reaching only a fraction of the
        retained buffer, or repaint frames only (see
        _unusable_echo_surface).
        """
        units = _prompt_echo_units(text)
        if not units:
            return None
        # Two looks before judging: the TUI may need a moment to render
        # the submitted message into the scrollback. The last look's
        # usability verdict wins: a surface that turned unusable can
        # give no verdict, while one that turned usable and still
        # misses is a genuine miss.
        unusable = None
        for _ in range(2):
            await anyio.sleep(PROMPT_ECHO_DELAY_SECS)
            surface = await self._echo_surface(PROMPT_ECHO_READ_LINES)
            if surface is None:
                return None
            tail, retained = surface
            # Usability before the match: on an unusable surface the
            # only matchable text is a swallowed prompt still sitting
            # in the repainted composer box — a hit there must not
            # count as delivery any more than a miss counts as loss.
            unusable = _unusable_echo_surface(tail, retained)
            if unusable is not None:
                continue
            # Whitespace-insensitive matching: a wrapped echo breaks
            # the prompt across visual lines (CJK is double-width, so a
            # 24-char prefix straddles a wrap on any terminal narrower
            # than ~48 columns), and the line join would otherwise
            # inject spaces mid-unit and produce a guaranteed false
            # miss. Stripping all whitespace from both sides makes the
            # wrap points irrelevant.
            haystack = "".join(
                ch for ch in " ".join(tail)
                if ch.isprintable() and not ch.isspace()
            )
            if any("".join(unit.split()) in haystack for unit in units):
                return True
        if unusable is not None:
            self._event(
                "prompt_echo_unverifiable",
                message=(
                    "terminal read exposes only a fraction of the "
                    "retained buffer; no usable echo surface"
                    if unusable == "thin" else
                    "scrollback holds only repaint frames; "
                    "no usable echo surface"
                ),
                data={"reason": unusable, "returned": len(tail),
                      "retained": retained},
            )
            return None
        return False

    async def run(self) -> None:
        async with anyio.create_task_group() as task_group:
            self._task_group = task_group
            await self._run_turns()

    async def _run_turns(self) -> None:
        self._update(watcher_pid=os.getpid(), state="starting")
        self._event("watcher_started", data={"pid": os.getpid()})
        log.info(
            "watcher(%s): started  terminal=%s  pid=%d",
            self.agent_id, self.handle, os.getpid(),
        )
        self._discard_stale_stop_request()

        if self.agent.get("launch_mode") == "supervised":
            await self._run_supervised()
            return

        if self.agent.get("follow_turn"):
            await self._run_follow()
            return

        # A resumed session (`osw revive`) continues its interrupted task
        # with the fixed RESUME_TEMPLATE; the record keeps the original
        # prompt for the report.
        if self.agent.get("resume_turn"):
            prompt = RESUME_TEMPLATE
        else:
            prompt = one_line(self.agent.get("prompt") or "")

        # Ready, then the task send. A send Orca refused because a dialog
        # owns the input (plan §2.2) never reached the agent: once the
        # blocker is handled the pane is waited ready again and the
        # prompt sent again, a bounded number of times.
        blocked_sends = 0
        while True:
            self._event("ready_wait_started")
            ready_source = await self._wait_ready()
            if ready_source == "stop_requested":
                # Stopped before the prompt went out: never send it.
                await self._finalize("error", "interrupted")
                return
            if ready_source is None:
                busy = await self._pane_alive()
                reason = "pane_busy" if busy else "ready_failed"
                error = ("agent still busy at the ready deadline"
                         if busy else "ready timeout")
                log.error("watcher(%s): ready-wait timed out (%s)",
                          self.agent_id, reason)
                self._event(
                    "ready_wait_failed",
                    level="ERROR",
                    message=("terminal never became ready; pane still busy"
                             if busy else "terminal never became ready"),
                )
                await self._finalize("error", reason, error=error)
                return
            # Ready again: an update this watcher applied is over.
            self._release_update_lock("ready_restored")
            self._event("ready_observed", data={"source": ready_source})
            log.info(
                "watcher(%s): ready (source=%s)  pane=%s",
                self.agent_id, ready_source, self.pane or "(unknown)",
            )
            if not prompt:
                await self._finalize("done", "no_prompt")
                return

            # ---- Phase 1: the task itself ----
            source = await self._run_turn(prompt, phase="task")
            if source != "blocked" or blocked_sends >= BLOCKED_SEND_LIMIT:
                break
            blocked_sends += 1
            await anyio.sleep(BLOCKED_RETRY_SECS)
        if source == "interrupted" or self._stop_requested():
            # A stopped task gets no handoff turn.
            await self._finalize("error", "interrupted")
            return
        if source in ("blocked", "blocked_stopped"):
            await self._finalize("error", "blocked")
            return
        if source in ("timeout", "terminal_lost", "send_failed", "no_receipt"):
            await self._finalize("error", source)
            return
        log.info("watcher(%s): task turn done (source=%s)", self.agent_id, source)

        # ---- Phase 2: handoff document ----
        # The handoff send carries no Jev judgment (G6) but still passes
        # the zero-cost Phase 0 gate.
        await self._phase0_ready_wait()
        handoff = new_handoff_path(self.root, self.agent_id)
        self._update(handoff_path=str(handoff))
        instruction = HANDOFF_TEMPLATE.format(path=handoff)
        for attempt in (1, 2):
            # The retry resends byte-identical text: attempt 1's echo
            # is still in the scrollback, so a verified echo on
            # attempt 2 cannot prove this send was received.
            handoff_source = await self._run_turn(
                instruction, phase="handoff", echo_receipt=attempt == 1,
            )
            if handoff_source == "interrupted" or self._stop_requested():
                if not _handoff_ok(handoff):
                    self._update(handoff_path="")
                await self._finalize("error", "interrupted")
                return
            if handoff_source in ("blocked", "blocked_stopped"):
                if not _handoff_ok(handoff):
                    self._update(handoff_path="")
                await self._finalize("error", "blocked")
                return
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

    # ---- follow: take over the turn already running in the pane ---------

    async def _run_follow(self) -> None:
        """Supervise a turn this watcher did not start (`follow_turn`).

        Nothing is sent: no ready wait, no prompt, no handoff turn. The
        takeover moment stands in for the send time and the ps prompt
        seen at takeover for the sent text, so the completion guards
        (foreign done, persistence, silence) apply unchanged. There is
        no receipt or echo to verify for a prompt someone else typed.
        A pane ps already reports "done" at takeover has no turn left
        to follow: waiting for the next one would judge its new prompt
        foreign and park the watcher until the hard deadline.
        """
        if self.pane is None:
            self.pane = await _pane_key(self.handle)
            if self.pane:
                self._update(pane_key=self.pane)
                self._event("pane_detected", data={"pane_key": self.pane})
        followed_at_ms = time.time() * 1000
        entry = None
        if self.pane:
            try:
                entry = await _ps_entry(self.pane)
            except OrcaError:
                entry = None
        ps_state = str((entry or {}).get("state") or "")
        self._event("follow_started", data={"ps_state": ps_state or "untracked"})
        if ps_state == "done":
            self._record_session()
            if self._stop_requested():
                await self._finalize("error", "interrupted")
                return
            await self._finalize("done", "already_done")
            return
        self._update(state="working", phase="task", tool_name="")
        # A pane with no done at takeover has no done baseline: the
        # running turn's done is the first one to come.
        source = await self._wait_turn_done(
            followed_at_ms, 0,
            sent_text=str((entry or {}).get("prompt") or ""),
            receipt_hint=True, absorb_echo=False,
        )
        self._record_session()
        tools = self._note_turn_tools("task")
        self._event("turn_finished", data={
            "phase": "task", "source": source, "tools_observed": tools})
        if source == "interrupted" or self._stop_requested():
            await self._finalize("error", "interrupted")
            return
        if source in ("timeout", "terminal_lost"):
            await self._finalize("error", source)
            return
        await self._finalize("done", source)

    # ---- supervised completion (Orca orchestration) ---------------------

    async def _relay_worker_asks(self, dispatch_id: str) -> None:
        """Tell the guardian about the worker's unanswered `ask` and
        escalation messages, once per message id (plan §2.3).

        The ids are recorded in `notified_asks` before notifying, so a
        restarted watcher (e.g. after `guard`) never repeats one. An
        inbox failure is transient, like a worker-show hiccup.
        """
        try:
            messages = await orchestration_inbox(limit=ASK_INBOX_LIMIT)
        except OrcaError as exc:
            log.warning("watcher(%s): inbox failed: %s", self.agent_id, exc)
            return
        pane = self.pane or str(self.agent.get("pane_key") or "")
        notified = [str(i) for i in (self.agent.get("notified_asks") or [])]
        for message in worker_asks(messages, dispatch_id, self.handle, pane):
            msg_id = str(message["id"])
            if msg_id in notified:
                continue
            notified = [*notified, msg_id]
            self._update(notified_asks=notified)
            text = one_line(
                str(message.get("body") or message.get("subject") or ""),
                ASK_TEXT_MAX,
            )
            await self._notify(
                f"# [osw] ask agent={self.agent_id} message={msg_id}"
                f' reply="orca orchestration reply --id {msg_id} --body <text>"'
                f" text={text}"
            )
            self._event(
                "worker_ask_notified",
                data={"message_id": msg_id, "type": message.get("type", "")},
            )

    async def _run_supervised(self) -> None:
        """Completion for workers OSW launched through Orca orchestration.

        The launch phase already waited for terminal readiness and
        injected the task spec, and the worker reports `worker_done`
        itself, so the watcher polls Orca's Dispatch lifecycle instead of
        reading terminal output. That is the only signal that works for
        a TUI whose status bar repaints about once a second: every
        output-silence heuristic would wait forever.
        """
        dispatch_id = str(self.agent.get("dispatch_id") or "")
        if not dispatch_id:
            await self._finalize("error", "supervised_missing_dispatch")
            return
        self._update(state="working", phase="task")
        await self._supervised_launch_judgment()
        self._event(
            "supervised_wait_started", data={"dispatch_id": dispatch_id},
        )
        outcome = ""
        while time.monotonic() < self.deadline:
            await anyio.sleep(SUPERVISED_POLL_SECS)
            # Record a stop as soon as it lands; the Dispatch settling
            # is still what ends the turn.
            self._stop_requested()
            try:
                data = await orchestration_worker_show(dispatch_id)
                settlement = worker_settlement(data)
            except OrcaError as exc:
                # Transient CLI/runtime hiccup: the deadline still rules.
                log.warning(
                    "watcher(%s): worker-show failed: %s",
                    self.agent_id, exc,
                )
                continue
            if settlement["settled"]:
                outcome = settlement["outcome"]
                break
            await self._relay_worker_asks(dispatch_id)
            if settlement["agent_wait"] \
                    and "agent_wait" not in self._notified_classes:
                self._notified_classes.add("agent_wait")
                await self._notify(
                    f"# [osw] agent-waiting agent={self.agent_id}"
                    f" terminal={self.handle}"
                    f" wait={one_line(settlement['agent_wait'], 80)}"
                    " (answer it directly in that terminal)"
                )
            observed = int(settlement["last_observed_at"] or 0)
            if observed and "stall" not in self._notified_classes:
                frozen_ms = time.time() * 1000 - observed
                if frozen_ms > STUCK_NOTIFY_SECS * 1000:
                    self._notified_classes.add("stall")
                    await self._notify(
                        f"# [osw] agent-stalled agent={self.agent_id}"
                        f" terminal={self.handle}"
                        f" no-activity={int(frozen_ms / 1000)}s"
                    )
        else:
            self._event(
                "supervised_wait_timeout",
                level="ERROR",
                data={"dispatch_id": dispatch_id},
            )
            await self._finalize("error", "timeout")
            return

        succeeded = outcome in ("succeeded", "completed")
        self._update(orchestration_outcome=outcome, phase="handoff")
        self._event(
            "supervised_settled",
            data={"dispatch_id": dispatch_id, "outcome": outcome},
        )
        self._record_session()
        log.info(
            "watcher(%s): worker settled (outcome=%s)",
            self.agent_id, outcome,
        )

        # The terminal stays live and owned by OSW until `osw del
        # --close` releases it; record that next-owner decision with
        # Orca so the Dispatch accounting settles too.
        try:
            await orchestration_worker_retain(dispatch_id)
            self._event(
                "supervised_retained", data={"dispatch_id": dispatch_id},
            )
        except OrcaError as exc:
            self._event(
                "supervised_retain_failed",
                level="WARNING",
                message=exc.message,
            )

        if self._stop_requested():
            # A stopped worker gets no wrap-up turn either.
            await self._finalize("error", "interrupted")
            return

        handoff = Path(str(self.agent.get("handoff_path") or "")) \
            if self.agent.get("handoff_path") \
            else new_handoff_path(self.root, self.agent_id)
        self._update(handoff_path=str(handoff))
        if not _handoff_ok(handoff):
            await self._supervised_handoff_fallback(handoff)

        if _handoff_ok(handoff):
            await self._finalize(
                "done" if succeeded else "error",
                "orchestration" if succeeded else "orchestration_failed",
            )
        else:
            self._update(handoff_path="")
            await self._finalize(
                "done" if succeeded else "error",
                "handoff_missing" if succeeded else "orchestration_failed",
            )

    async def _supervised_launch_judgment(self) -> None:
        """One Jev look at a supervised launch (operator override 2026-10-11).

        Orca already injected the task, so nothing can be held; the
        judgment is advisory and recorded, and a `blocked_startup`
        verdict raises the early warning for the agent-readiness flake
        observed on 2026-10-10 (the pane boots but readiness never fires).
        """
        mode = resolve_mode(self.root)
        if mode not in ("shadow", "enforce"):
            return
        provider = str(self.agent.get("provider") or "")
        try:
            frame = await self._read_gate_frame()
        except OrcaError:
            frame = None
        if frame is None:
            return
        term = frame.get("result", {}).get("terminal", {})
        if not (term.get("source") == "screen"
                and isinstance(term.get("tail"), list) and term.get("tail")):
            return
        if enforce_active(self.root, provider):
            answer = await ask_ready(
                self.root, self.agent_id, provider, frame,
                "enforce", "supervised_launch")
            await self._handle_answer(answer, provider)
            if answer is not None and startup_should_hold(answer):
                self._event(
                    "jev_veto",
                    data={"action": "advisory", "reason": "blocked_startup",
                          "held_ms": 0, "calls": 1},
                )
                await self._notify(
                    f"# [osw] jev-notice agent={self.agent_id}"
                    f" terminal={self.handle} class=blocked_startup"
                    " (supervised launch; judgment recorded, nothing blocked)"
                )
        else:
            self._spawn_shadow(provider, frame, "supervised_launch")

    async def _supervised_handoff_fallback(self, handoff: Path) -> None:
        """The worker settled without writing the contract file.

        Send the fixed wrap-up turn and judge by the file — non-empty and
        size-stable across polls — never by output silence.
        """
        self._event("supervised_handoff_missing", level="WARNING")
        log.warning(
            "watcher(%s): handoff missing at settlement; sending wrap-up",
            self.agent_id,
        )
        try:
            await self._send_terminal(HANDOFF_TEMPLATE.format(path=handoff))
        except OrcaError as exc:
            self._event(
                "supervised_wrapup_send_failed",
                level="WARNING",
                message=exc.message,
            )
            return
        deadline = min(
            self.deadline,
            time.monotonic() + HANDOFF_FALLBACK_TIMEOUT_SECS,
        )
        last_size = -1
        stable = 0
        while time.monotonic() < deadline:
            await anyio.sleep(HANDOFF_FALLBACK_POLL_SECS)
            if not _handoff_ok(handoff):
                continue
            size = handoff.stat().st_size
            if size == last_size:
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
                last_size = size

    def _emit_gate_verdict(
        self, verdict: str, provider: str, source: str, reason: str,
    ) -> None:
        key = (verdict, reason)
        if key == self._gate_last:
            return
        self._gate_last = key
        self._event(
            "gate_verdict",
            data={"verdict": verdict, "provider": provider,
                  "source": source, "reason": reason},
        )

    async def _read_gate_frame(self) -> dict | None:
        try:
            return await self._terminal_read_screen()
        except OrcaError:
            return None

    async def _api_retry_wait(self, data: dict) -> None:
        """Tolerate an upstream-API retry (own verdict class).

        The pane is alive and recovers on its own, so nothing is sent,
        failed, or held: notify once, drop any pending composer wait and
        re-arm the ready clock (the hard deadline still caps it). The
        first recovery is judged by the normal gate on the next poll.
        """
        self._gate_since = None
        self._gate_call_pending = False
        self._ready_deadline = min(
            self.deadline, time.monotonic() + READY_TIMEOUT_MS / 1000)
        if "api_retry" in self._notified_classes:
            return
        self._notified_classes.add("api_retry")
        self._event("gate_api_retry", data=data)
        await self._notify(
            f"# [osw] agent-retrying agent={self.agent_id}"
            f" terminal={self.handle}"
            " (upstream API is retrying; still waiting, no prompt sent)"
        )

    async def _gate_veto(self, source: str) -> bool:
        """Deterministic gate + Jev veto at a ready release point.

        Design §5.0/§5.1. Returns True to veto this release (the caller
        re-enters the outer loop); False releases. Every release source
        reaches this gate (including `tui_idle`), and since the operator
        override of 2026-10-11 Jev is consulted on `ready` verdicts too —
        advisory there, never a hold.

        Order of decisions:
        1. an active hold runs its own ordered checks (§5.1.2);
        2. otherwise the deterministic verdict drives the Phase 0 flow:
           `ready`/`abstain` release, `not_ready` starts the bounded
           composer wait (≤COMPOSER_WAIT_MS, then releases with today's
           behavior — never until the ready deadline, N3);
        3. Jev is consulted only when the gate abstains or first says
           `not_ready` (§5.1.1): shadow fires a background judgment,
           enforce calls synchronously and may start a hold.
        """
        provider = str(self.agent.get("provider") or "")
        mode = resolve_mode(self.root)
        frame: dict | None = None
        if provider in GATE_PROVIDERS or mode != "off":
            frame = await self._read_gate_frame()
        frame_usable = False
        if frame is not None:
            term = frame.get("result", {}).get("terminal", {})
            frame_usable = (
                term.get("source") == "screen"
                and isinstance(term.get("tail"), list)
                and bool(term.get("tail"))
            )
        if provider in GATE_PROVIDERS:
            if frame is None:
                verdict, reason = "abstain", "read_failed"
            else:
                verdict, reason = gate_verdict(provider, frame)
            self._emit_gate_verdict(verdict, provider, source, reason)
        else:
            verdict, reason = "abstain", "provider_without_predicate"
            self._emit_gate_verdict(verdict, provider, source, reason)

        # ---- upstream API retry: tolerate, never send/fail/hold ----
        if verdict == "api_retry":
            await self._api_retry_wait({"reason": reason, "source": source})
            return True

        if self._hold is not None:
            return await self._hold_check(source, provider, frame, verdict)

        # ---- Phase 0 composer wait ----
        wait_veto = False
        if verdict == "ready":
            if self._gate_since is not None:
                waited_ms = int(
                    (time.monotonic() - self._gate_since) * 1000)
                self._gate_since = None
                self._gate_call_pending = False
                self._event(
                    "gate_released",
                    data={"reason": "composer_visible",
                          "waited_ms": waited_ms},
                )
        elif verdict == "abstain":
            self._gate_since = None
            self._gate_call_pending = False
        else:  # not_ready
            now = time.monotonic()
            if self._gate_since is None:
                self._gate_since = now
                self._gate_call_pending = True
            waited_ms = int((now - self._gate_since) * 1000)
            if waited_ms >= COMPOSER_WAIT_MS:
                self._gate_since = None
                self._event(
                    "gate_released",
                    data={"reason": "wait_expired", "waited_ms": waited_ms},
                )
            else:
                wait_veto = True

        # ---- Jev consultation (§5.1.1; operator override 2026-10-11) ----
        # Jev is consulted on every gate invocation, including a `ready`
        # verdict: leaving the typical path at zero calls made the mode a
        # no-op in practice. A ready verdict can never start a hold (the
        # deterministic predicate stays authoritative for release); its
        # judgment is advisory and recorded.
        if (mode in ("shadow", "enforce") and frame_usable
                and (verdict in ("ready", "abstain")
                     or (verdict == "not_ready" and self._gate_call_pending))):
            self._gate_call_pending = False
            if enforce_active(self.root, provider):
                answer = await ask_ready(
                    self.root, self.agent_id, provider, frame,
                    "enforce", reason)
                await self._handle_answer(answer, provider)
                if answer is not None \
                        and str(answer.get("choice") or "") == "api_retry":
                    await self._api_retry_wait(
                        {"reason": "jev_choice", "source": source})
                    return True
                # A dialog judgment is checked as a blocking screen
                # (plan §2.2). Keys sent, or another pane holding the
                # update lock, keep the prompt back; anything else stays
                # advisory and releases as before.
                blocked = await self._check_blocker(
                    None, _dialog_only(answer), frame, point="ready")
                if blocked in ("acted", "waiting"):
                    return True
                if (verdict != "ready"
                        and answer is not None
                        and startup_should_hold(answer)
                        and self._hold_budget_ok()):
                    self._start_hold(frame)
                    return True
            else:
                self._spawn_shadow(provider, frame, reason)

        if wait_veto and self._hold is None:
            return True
        return False

    def _hold_budget_ok(self) -> bool:
        """Enough ready budget left to start/continue a hold (§5.1.2)."""
        return (self._ready_deadline - time.monotonic()
                >= HOLD_MIN_REMAINING_MS / 1000)

    def _start_hold(self, frame: dict) -> None:
        term = frame.get("result", {}).get("terminal", {})
        lines = [str(line) for line in (term.get("tail") or [])]
        now = time.monotonic()
        self._hold = {
            "since": now,
            "last_ask_at": now,
            "last_norm_hash": normalized_hash(lines),
            "static_since": now,
            "calls": 1,
        }
        self._event(
            "jev_veto",
            data={"action": "held", "reason": "blocked_startup",
                  "held_ms": 0, "calls": 1},
        )

    def _release_hold(self, reason: str, action: str = "released") -> None:
        hold = self._hold or {}
        held_ms = int(
            (time.monotonic() - hold.get("since", time.monotonic())) * 1000)
        calls = int(hold.get("calls", 0))
        self._hold = None
        self._event(
            "jev_veto",
            data={"action": action, "reason": reason,
                  "held_ms": held_ms, "calls": calls},
        )

    async def _hold_check(
        self, source: str, provider: str, frame: dict | None, verdict: str,
    ) -> bool:
        """Ordered hold checks (§5.1.2); True keeps holding."""
        hold = self._hold
        assert hold is not None
        now = time.monotonic()
        # 1. deterministic fast path
        if verdict == "ready":
            self._release_hold("fast_path")
            return False
        # 2. mode / provider hot-read (kill switch mid-hold)
        if not enforce_active(self.root, provider):
            self._release_hold("mode_off")
            return False
        # 3. re-ask only on fresh semantic content, ≥HOLD_MIN_REASK_MS apart
        if frame is not None:
            term = frame.get("result", {}).get("terminal", {})
            lines = [str(line) for line in (term.get("tail") or [])]
            norm = normalized_hash(lines)
            if norm != hold["last_norm_hash"]:
                hold["last_norm_hash"] = norm
                hold["static_since"] = now
                if (now - hold["last_ask_at"]) >= HOLD_MIN_REASK_MS / 1000:
                    answer = await ask_ready(
                        self.root, self.agent_id, provider, frame,
                        "enforce", "hold_reask")
                    hold["last_ask_at"] = now
                    hold["calls"] += 1
                    if answer is None or not startup_should_hold(answer):
                        self._release_hold("reask_clear")
                        return False
            if (norm == hold["last_norm_hash"]
                    and now - hold["static_since"]
                    >= HOLD_FALSE_HOLD_CAP_MS / 1000):
                # "Static" does not mean startup finished — the loading
                # screen is static. This cap is the false-hold give-up.
                self._release_hold("false_hold_cap")
                return False
        # 4. budget: min(HOLD_BUDGET_MS, ready_deadline − HOLD_MIN_REMAINING_MS)
        budget = min(
            HOLD_BUDGET_MS / 1000,
            max(0.0, self._ready_deadline - now
                - HOLD_MIN_REMAINING_MS / 1000),
        )
        if now - hold["since"] >= budget:
            self._release_hold("forced", action="forced")
            return False
        return True

    async def _handle_answer(self, answer: dict | None, provider: str) -> None:
        """Advisory notices only (§5.1.4): one per class per watcher."""
        if answer is None:
            return
        hit = notify_class(answer)
        if hit is None:
            return
        cls, p = hit
        if cls in self._notified_classes:
            return
        self._notified_classes.add(cls)
        self._event(
            "jev_notice",
            data={"class": cls, "p": p, "provider": provider},
        )
        await self._notify(
            f"# [osw] jev-notice agent={self.agent_id}"
            f" terminal={self.handle} class={cls} p={p:.2f}"
            " (advisory; prompt was sent as usual)"
        )

    def _spawn_shadow(
        self, provider: str, frame: dict, gate_label: str,
    ) -> None:
        """Background shadow judgment (§5.1.5): never blocks the release."""
        group = self._task_group
        if group is None:
            return
        group.start_soon(
            functools.partial(
                self._shadow_ready_call, provider, frame, gate_label))

    async def _shadow_ready_call(
        self, provider: str, frame: dict, gate_label: str,
    ) -> None:
        try:
            answer = await ask_ready(self.root, self.agent_id, provider, frame,
                                     "shadow", gate_label)
        except Exception:
            return  # ask_ready recorded the degradation itself
        # The ready release did not wait for this judgment, so it may only
        # record or report a blocking screen; keys are left to the
        # foreground send path, which Orca blocks on such a dialog.
        try:
            await self._check_blocker(
                None, _dialog_only(answer), frame, act="defer",
                point="ready_shadow")
        except OrcaError:
            pass

    async def _phase0_ready_wait(self) -> None:
        """Phase 0 gate before a send without Jev judgment (handoff, G6):
        bounded composer wait, then proceed regardless."""
        provider = str(self.agent.get("provider") or "")
        if provider not in GATE_PROVIDERS:
            return
        started = time.monotonic()
        deadline = started + COMPOSER_WAIT_MS / 1000
        while True:
            frame = await self._read_gate_frame()
            if frame is None:
                return
            verdict, reason = gate_verdict(provider, frame)
            self._emit_gate_verdict(verdict, provider, "handoff", reason)
            if verdict not in ("not_ready", "api_retry"):
                return
            if time.monotonic() >= deadline:
                self._event(
                    "gate_released",
                    data={"reason": "wait_expired",
                          "waited_ms": int(
                              (time.monotonic() - started) * 1000)},
                )
                return
            await anyio.sleep(GATE_POLL_SECS)

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
        otherwise output must stay silent for READY_IDLE_STABLE_MS. A
        tracked pane that ps keeps reporting as "working" re-arms the
        ready wait instead of failing as "never ready" — a worker still
        finishing previous work is busy, not broken; the hard deadline
        still bounds the wait. Readiness is never treated as task
        completion: the task prompt is always sent and observed as its
        own turn afterwards.

        Returns the readiness source ("ps_idle" / "tui_idle" /
        "preview_marker" / "output_idle"), "stop_requested" when a stop
        request landed first, or None on timeout.
        """
        self._ready_deadline = time.monotonic() + READY_TIMEOUT_MS / 1000
        dialog_notified = False
        idle_ignored = False
        busy_notified = False
        while time.monotonic() < self._ready_deadline:
            if self._stop_requested():
                return "stop_requested"
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
                    if await self._gate_veto("ps_idle"):
                        await anyio.sleep(GATE_POLL_SECS)
                        continue
                    return "ps_idle"
                if entry.get("state") == "working":
                    # A pane mid-turn is proof of life, not a stuck
                    # startup: keep waiting (capped by the watcher's
                    # hard deadline) instead of failing a live agent as
                    # "never ready" after READY_TIMEOUT_MS.
                    self._ready_deadline = min(
                        self.deadline,
                        time.monotonic() + READY_TIMEOUT_MS / 1000,
                    )
                    if not busy_notified:
                        busy_notified = True
                        self._event(
                            "ready_wait_busy",
                            message="pane is mid-turn; extending the "
                                    "ready wait",
                        )
                        await self._notify(
                            f"# [osw] agent-busy agent={self.agent_id}"
                            f" terminal={self.handle}"
                            " (still finishing previous work; the new"
                            " prompt goes out when it becomes ready)"
                        )
            else:
                if await self._wait_tui_idle():
                    if await self._gate_veto("tui_idle"):
                        await anyio.sleep(GATE_POLL_SECS)
                        continue
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
                        if await self._gate_veto("preview_marker"):
                            await anyio.sleep(GATE_POLL_SECS)
                            continue
                        return "preview_marker"
                    if silent_ms >= READY_PREVIEW_SILENCE_MS and (
                            _composer_visible(preview)
                            or any(m in preview for m in READY_SCREEN_MARKERS)):
                        if await self._gate_veto("preview_marker"):
                            await anyio.sleep(GATE_POLL_SECS)
                            continue
                        return "preview_marker"
                    if silent_ms >= READY_IDLE_STABLE_MS and self._blockers_acted:
                        # After keys were pressed on a blocking dialog,
                        # silence is no proof of an agent: codex exits
                        # to its shell after applying an update. Only
                        # agent evidence (ps, tui-idle, markers) may
                        # release; otherwise the ready deadline rules.
                        if not idle_ignored:
                            idle_ignored = True
                            self._event(
                                "ready_output_idle_ignored",
                                message="silent pane after a dialog action; "
                                        "waiting for agent evidence",
                            )
                    elif silent_ms >= READY_IDLE_STABLE_MS:
                        if await self._gate_veto("output_idle"):
                            await anyio.sleep(GATE_POLL_SECS)
                            continue
                        return "output_idle"
            await anyio.sleep(POLL_SECS)
        return None

    async def _pane_alive(self) -> bool:
        """Best-effort proof of life at the ready deadline: ps still
        reports the pane working, or the terminal produced output in
        the last minute. Distinguishes "agent still busy" from a dead
        or stuck startup when the ready wait times out."""
        if self.pane is not None:
            try:
                entry = await _ps_entry(self.pane)
            except OrcaError:
                entry = None
            if entry is not None and entry.get("state") == "working":
                return True
        try:
            last = await self._last_output_at()
        except OrcaError:
            return False
        return bool(last) and time.time() * 1000 - last < 60_000

    async def _output_silent(self, quiet_ms: int) -> bool:
        """Whether the pane has been output-silent for `quiet_ms`.

        An unreadable lastOutputAt counts as not silent: completion is
        then deferred instead of accepted on blind faith.
        """
        try:
            last = await self._last_output_at()
        except OrcaError:
            return False
        return bool(last) and time.time() * 1000 - last >= quiet_ms

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

    async def _run_turn(
        self, text: str, phase: str, echo_receipt: bool = True,
    ) -> str:
        """Send one prompt and wait for the turn to complete.

        Returns the completion source: "ps_done", "tui_idle",
        "fallback_idle", "interrupted" (a stop request ended the turn),
        "blocked" (Orca refused the send because a dialog owns the
        input; retry after ready), "blocked_stopped" (and automatic
        handling gave up), or one of the failure modes
        "send_failed"/"timeout"/"terminal_lost"/"no_receipt".

        `echo_receipt=False` keeps a verified scrollback echo from
        counting as prompt receipt: a resend of byte-identical text
        (the handoff retry) matches the previous attempt's echo just
        as well, so the echo proves nothing about this send.

        Orca's send-time effect confirmation may report
        `agent_prompt_stalled` for a prompt that did land (the TUI was
        already "working" when it arrived, so no fresh idle->working
        edge fired within Orca's window). That is not a send failure:
        the turn proceeds to the receipt window below, which fails
        closed as "no_receipt" if the prompt truly never arrived.
        """
        sent_at_ms = time.time() * 1000
        baseline_state_started = await self._pane_done_state_started()
        try:
            confirmed = await self._send_terminal(text)
        except OrcaError as exc:
            if exc.code == "agent_prompt_blocked":
                # Orca refused the write: a dialog owns the input. The
                # prompt never reached the agent, so after the blocker
                # is handled the caller may safely send it again.
                self._event(
                    "turn_send_blocked",
                    level="WARNING",
                    message=str(exc),
                    data={"phase": phase},
                )
                outcome = await self._check_blocker(
                    _blocked_observation(exc), None, None, point="send")
                return "blocked_stopped" if outcome == "stopped" else "blocked"
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
        if not confirmed:
            # The write landed but its confirmation was not observed:
            # Orca's effect confirmation timed out (agent_prompt_stalled
            # — a TUI still on its startup turn accepted the prompt
            # without a fresh idle->working edge), or the split send's
            # bounded composer-echo wait elapsed before a long paste
            # rendered. Not a send failure. Same policy as the echo
            # miss below: on a ps-tracked pane the receipt window is
            # authoritative and a genuinely swallowed prompt fails
            # closed as no_receipt with its own notification, so this
            # is only logged; a pane ps does not track has no receipt
            # window, so the caller is told there.
            if self.pane is None:
                log.warning(
                    "watcher(%s): prompt written but delivery unconfirmed",
                    self.agent_id,
                )
                self._event(
                    "prompt_delivery_unconfirmed",
                    level="WARNING",
                    message=(
                        "send confirmation not observed (agent_prompt_"
                        "stalled or split-send composer echo missed); "
                        "pane is not ps-tracked"
                    ),
                    data={"phase": phase},
                )
                await self._notify(
                    f"# [osw] prompt-delivery-unconfirmed agent={self.agent_id}"
                    f" terminal={self.handle}"
                    " (send confirmation not observed; prompt was written,"
                    " check that terminal)"
                )
            else:
                log.info(
                    "watcher(%s): prompt written but delivery unconfirmed; "
                    "receipt window authoritative",
                    self.agent_id,
                )
                self._event(
                    "prompt_delivery_unconfirmed",
                    message=(
                        "send confirmation not observed (agent_prompt_"
                        "stalled or split-send composer echo missed); "
                        "pane is ps-tracked, so the receipt window is "
                        "authoritative"
                    ),
                    data={"phase": phase},
                )
        # Belt-and-suspenders against a swallowed prompt (modal dialog,
        # login screen): the text must be visible in the terminal's
        # scrollback afterwards. A miss only alarms — it never resends:
        # resent text lands in a working agent's input queue whenever
        # the miss is false (a full-screen TUI whose echo the check
        # cannot see made every miss false), and genuine non-receipt
        # already fails closed in the turn supervision below.
        #
        # The alarm is reserved for panes ps does not track. On a
        # tracked pane the ps receipt window below is authoritative —
        # and for a tracked alternate-screen TUI (Claude Code) the
        # conversation never enters the scrollback at all: every miss
        # there is false by construction, while a genuine swallow still
        # fails closed as no_receipt. Early in such a turn the repaint
        # frames have not yet flooded the retained buffer, so the
        # redraw-surface abstention cannot save the verdict either.
        echoed = await self._verify_prompt_echo(text)
        if echoed is False:
            log.warning(
                "watcher(%s): prompt not visible in terminal scrollback",
                self.agent_id,
            )
            if self.pane is None:
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
            else:
                self._event(
                    "prompt_echo_missing",
                    message="prompt not visible in terminal scrollback; "
                            "pane is ps-tracked, so the receipt window "
                            "below is authoritative",
                    data={"phase": phase},
                )
        # A verified scrollback echo is direct evidence the submitted
        # prompt reached the TUI's conversation — stronger than the ps
        # prompt-text match, which mangles CJK on Windows. Count it as
        # receipt up front so a ps that stays blind to the new turn
        # (stale "done") cannot fail a delivered prompt closed.
        source = await self._wait_turn_done(
            sent_at_ms, baseline_state_started, sent_text=text,
            receipt_hint=echo_receipt and echoed is True,
        )
        self._record_session()
        tools = self._note_turn_tools(phase)
        self._event(
            "turn_finished",
            data={"phase": phase, "source": source, "tools_observed": tools},
        )
        return source

    async def _wait_turn_done(
        self, sent_at_ms: float, baseline_state_started: int = 0,
        sent_text: str = "", receipt_hint: bool = False,
        absorb_echo: bool = True,
    ) -> str:
        """Wait for the turn that started at `sent_at_ms` to complete.

        `absorb_echo=False` is for a followed turn: nothing was sent, so
        the first output after the baseline is the agent's own.

        Also records, for the caller, whether ps tracked the pane this
        turn (`_turn_tracked`) and reported a tool while it was working
        (`_turn_tools_seen`); a done row's `toolName` may be the previous
        turn's and never counts.
        """
        self._turn_tracked = False
        self._turn_tools_seen = False
        try:
            baseline_out = await self._last_output_at()
        except OrcaError:
            baseline_out = 0
        started = False
        entry_seen = False   # Orca showed a ps entry for this pane this turn
        saw_working = False  # ...and reported it "working" at least once
        # receipt: evidence the CLI actually took our prompt. A verified
        # scrollback echo already proves that; ps evidence adds to it.
        receipt = receipt_hint
        sent_flat = one_line(sent_text)
        echo_absorbed = not absorb_echo
        pending_done_started = 0  # a done must be observed twice to count
        misses = 0
        activity_observed = False
        ask_notified = False
        stall_notified = False
        background_defer_notified = False

        while time.monotonic() < self.deadline:
            await anyio.sleep(POLL_SECS)
            self._flush_state()

            # A stopped turn ends on the first sign the agent is no
            # longer working; normal completion evidence is moot then.
            if self._stop_requested():
                reason = await self._stop_settled()
                if reason:
                    self._event("stop_settled", data={"reason": reason})
                    return "interrupted"
                continue

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
                # Name what swallowed it, if a blocking screen did.
                await self._blocker_probe("receipt")
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
                self._turn_tracked = True
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
                    # Completion guards (design §1.5): a foreign session
                    # reporting under this pane must not end our turn,
                    # the done must persist across two polls, and the
                    # pane's output must have gone quiet first.
                    if _foreign_done(entry, sent_flat):
                        if pending_done_started != state_started:
                            pending_done_started = state_started
                            self._event(
                                "foreign_done_ignored",
                                message=(
                                    "fresh done belongs to another agent "
                                    "session in this pane (prompt mismatch);"
                                    " ignored"
                                ),
                                data={"state_started_at": state_started},
                            )
                        continue
                    if pending_done_started != state_started:
                        pending_done_started = state_started
                        continue
                    if not await self._output_silent(COMPLETION_SILENCE_MS):
                        continue
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
                    if tool:
                        self._turn_tools_seen = True
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
                        await self._blocker_probe("stall")
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
        # Never leave the machine-wide update lock to its expiry.
        self._release_update_lock("finalize")

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
            "jev": {"mode": resolve_mode(self.root)},
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
        if self.agent.get("launch_mode") == "supervised":
            report_payload.update({
                "launch_mode": "supervised",
                "dispatch_id": self.agent.get("dispatch_id", ""),
                "task_id": self.agent.get("task_id", ""),
                "orchestration_outcome": self.agent.get(
                    "orchestration_outcome", ""
                ),
            })
        following = bool(self.agent.get("follow_turn"))
        resuming = bool(self.agent.get("resume_turn"))
        if following:
            report_payload["follow_turn"] = True
        if resuming:
            report_payload["resume_turn"] = True
        if self._stop_at:
            report_payload["stop_requested_at"] = self._stop_at
        # A task turn that ps tracked but never saw use a tool did no
        # visible work (plan §3.1): flag it, but it stays `done` and is
        # never resent — the handoff and workspace are the evidence.
        warning = ""
        if self._task_turn_ran:
            report_payload["tools_observed"] = self._task_tools_observed
            if final_state == "done" and self._task_tools_observed is False:
                warning = "no_tool_activity"
                self._event(
                    "no_tool_activity",
                    level="WARNING",
                    message="task turn finished without any observed tool call",
                )
        report_path = write_report(self.root, self.agent_id, report_payload)
        log.info("watcher(%s): report written to %s", self.agent_id, report_path)
        self._event(
            "report_written",
            data={"report": str(report_path), "state": final_state, "source": source},
        )

        settled = {
            "state": final_state,
            "phase": "",
            "completion_source": source,
            "report_file": str(report_path),
            "finished_at": finished_at,
        }
        if following:
            # The takeover is consumed: a later watcher on this record
            # must run its own turn, not follow again.
            settled["follow_turn"] = False
        if resuming:
            # Likewise the resume: the next task on this record sends
            # its own prompt, not RESUME_TEMPLATE.
            settled["resume_turn"] = False
        self._update(**settled)
        # The stop request (if any) is settled with this run.
        try:
            clear_stop_request(self.root, self.agent_id)
        except OSError:
            pass

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
                outcome=str(self.agent.get("orchestration_outcome") or ""),
                warning=warning,
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

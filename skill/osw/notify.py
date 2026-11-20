"""Completion notices that never reached their caller.

A finished turn whose record has no usable caller terminal or chat
session is recorded in the event stream as `notify_skipped` and its
line never leaves `logs/events.jsonl`. This module reads that stream
back: a notice is pending when the turn is terminal, no
`notify_delivered` or `completion_notify_suppressed` event at or after
its `finished_at` exists, and the operator has not acked its key.
Records that predate this tracking are acked once by
`reconcile_legacy`, so the list holds real gaps only.

Read-only apart from the ack store, which `ack` and `reconcile_legacy`
alone write; the watcher stays the only writer of agent records.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from osw import state
from osw.log import read_events
from osw.state import logs_dir
from osw.watcher import format_completion_report

ACKED_RELATIVE = "notify/acked.json"

TERMINAL_STATES = ("done", "error", "lost")

# Events that carry the exact text the watcher would have sent.
CAPTURE_EVENTS = (
    "notify_skipped",
    "notify_failed",
    "completion_notify_suppressed",
)

DELIVERED_EVENT = "notify_delivered"
SUPPRESSED_EVENT = "completion_notify_suppressed"

# Any event the watcher writes about one turn's notice, delivered or not.
NOTIFY_EVENTS = (DELIVERED_EVENT, *CAPTURE_EVENTS)

LEGACY_MARKER_RELATIVE = "notify/legacy_reconciled.json"


def _now_iso() -> str:
    """Current UTC timestamp in the watcher's format."""
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value) -> datetime | None:
    """Parse an ISO timestamp (naive means UTC); None if unparseable."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _at_or_after(ts, floor) -> bool:
    """Whether `ts` is at or after `floor`; unparseable values never are."""
    stamp, base = _parse_ts(ts), _parse_ts(floor)
    if stamp is None or base is None:
        return False
    return stamp >= base


def notice_key(agent: dict) -> str:
    """`<agent_id>@<finished_at>`, or "" when either is missing."""
    agent_id = str(agent.get("agent_id") or "")
    finished_at = str(agent.get("finished_at") or "")
    return f"{agent_id}@{finished_at}" if agent_id and finished_at else ""


def delivered(events: list[dict], agent: dict) -> bool:
    """Whether this turn's notice reached a delivery channel.

    A `notify_delivered` event means a line was sent. A
    `completion_notify_suppressed` event means the Mailbox daemon was
    running and took the turn's conclusion: the mailbox is a delivery
    channel, and the spool report it writes is that channel's durable
    copy. Either event counts only at or after the turn's
    `finished_at`; an earlier one belongs to a previous turn.
    """
    agent_id = str(agent.get("agent_id") or "")
    finished_at = str(agent.get("finished_at") or "")
    if not agent_id or not finished_at:
        return False
    for event in events:
        if event.get("agent_id") != agent_id:
            continue
        if event.get("event") not in (DELIVERED_EVENT, SUPPRESSED_EVENT):
            continue
        if _at_or_after(event.get("ts"), finished_at):
            return True
    return False


def _captured_line(events: list[dict], agent: dict) -> str | None:
    """Newest captured notice text for this turn, or None."""
    agent_id = str(agent.get("agent_id") or "")
    finished_at = str(agent.get("finished_at") or "")
    best: str | None = None
    best_ts = None
    for event in events:
        if event.get("agent_id") != agent_id:
            continue
        if event.get("event") not in CAPTURE_EVENTS:
            continue
        if not _at_or_after(event.get("ts"), finished_at):
            continue
        line = (event.get("data") or {}).get("line")
        if not isinstance(line, str) or not line:
            continue
        stamp = _parse_ts(event.get("ts"))
        if best is None or (stamp is not None
                            and (best_ts is None or stamp >= best_ts)):
            best, best_ts = line, stamp
    return best


def _line_field(line: str, name: str) -> str:
    """One `name=value` field out of a completion line, or ""."""
    marker = f" {name}="
    start = line.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    tail = line[start:]
    if name == "summary":
        cut = tail.find(" cleanup=")
        if cut >= 0:
            tail = tail[:cut]
    return tail.strip()


def _newest_report(root: Path, agent_id: str) -> dict | None:
    """Newest report JSON for `agent_id`, or None."""
    directory = state.reports_dir(root)
    if not directory.exists():
        return None
    paths = sorted(directory.glob(f"{agent_id}_*.json"))
    for path in reversed(paths):
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _report_warning(report: dict | None, final_state: str) -> str:
    """The warning the watcher would have attached to this turn."""
    if not report:
        return ""
    if report.get("prompt_truncated_at"):
        return "prompt_truncated"
    if final_state == "done" and report.get("tools_observed") is False:
        return "no_tool_activity"
    return ""


def format_line(
    agent: dict, report: dict | None, captured: str | None
) -> str:
    """Notice text for one finished turn.

    A captured line is the exact historical text and wins; otherwise
    one is built in the watcher's completion shape from the record and
    the turn's report.
    """
    if captured:
        return captured
    final_state = str(agent.get("state") or "")
    summary = ""
    if report:
        summary = str(report.get("last_assistant_message") or "")
    return format_completion_report(
        str(agent.get("agent_id") or ""),
        str(agent.get("terminal") or ""),
        str(agent.get("report_file") or ""),
        handoff_file=str(agent.get("handoff_path") or ""),
        summary=summary,
        outcome=str(agent.get("orchestration_outcome") or ""),
        warning=_report_warning(report, final_state),
    )


def _acked_path(root: Path) -> Path:
    return state.state_dir(root) / ACKED_RELATIVE


def _read_acked(root: Path) -> dict[str, str]:
    """The ack store; a missing or corrupt file reads as empty."""
    path = _acked_path(root)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    acked = data.get("acked") if isinstance(data, dict) else None
    if not isinstance(acked, dict):
        return {}
    return {str(k): str(v) for k, v in acked.items()}


def pending(root: Path) -> list[dict]:
    """Finished turns whose notice never reached a caller, newest first."""
    events = read_events(logs_dir(root))
    acked = _read_acked(root)
    items: list[dict] = []
    for agent in state.list_agents(root).values():
        key = notice_key(agent)
        if not key:
            continue
        final_state = str(agent.get("state") or "")
        if final_state not in TERMINAL_STATES:
            continue
        if key in acked or delivered(events, agent):
            continue
        captured = _captured_line(events, agent)
        report = _newest_report(root, str(agent["agent_id"]))
        summary = ""
        if report:
            summary = str(report.get("last_assistant_message") or "")
        if not summary and captured:
            summary = _line_field(captured, "summary")
        items.append({
            "agent_id": str(agent.get("agent_id") or ""),
            "state": final_state,
            "terminal": str(agent.get("terminal") or ""),
            "completion_source": str(agent.get("completion_source") or ""),
            "finished_at": str(agent.get("finished_at") or ""),
            "handoff_path": str(agent.get("handoff_path") or ""),
            "report_file": str(agent.get("report_file") or ""),
            "summary": summary,
            "warning": _report_warning(report, final_state),
            "key": key,
            "line": format_line(agent, report, captured),
        })
    items.sort(key=lambda item: (item["finished_at"], item["agent_id"]),
               reverse=True)
    return items


def pending_count(root: Path) -> int:
    """Number of pending notices."""
    return len(pending(root))


def ack(root: Path, keys: Iterable[str]) -> int:
    """Mark notice keys acked; returns how many were newly acked."""
    if isinstance(keys, str):
        keys = [keys]
    wanted = [str(key) for key in keys if key]
    acked = _read_acked(root)
    fresh = [key for key in dict.fromkeys(wanted) if key not in acked]
    if not fresh:
        return 0
    stamp = _now_iso()
    for key in fresh:
        acked[key] = stamp
    state.write_json_atomic(_acked_path(root), {"acked": acked})
    return len(fresh)


def reconcile_legacy(root: Path) -> int:
    """Ack finished turns that predate notice tracking; returns how many.

    A record finished before this feature existed carries no delivery
    evidence at all, and absence of evidence is not evidence of a drop.
    Exactly the terminal records with no notify-related event of any
    kind are acked; one with a recorded failure stays pending. Runs
    once — the marker makes later calls no-ops — and never writes an
    agent record, which the watcher alone owns.
    """
    marker = state.state_dir(root) / LEGACY_MARKER_RELATIVE
    if marker.exists():
        return 0
    events = read_events(logs_dir(root))
    tracked = {
        str(event.get("agent_id") or "")
        for event in events
        if event.get("event") in NOTIFY_EVENTS
    }
    acked = _read_acked(root)
    stamp = _now_iso()
    fresh: list[str] = []
    for agent in state.list_agents(root).values():
        agent_id = str(agent.get("agent_id") or "")
        key = notice_key(agent)
        if not key or agent_id in tracked or key in acked:
            continue
        if str(agent.get("state") or "") not in TERMINAL_STATES:
            continue
        acked[key] = f"{stamp} reconcile_legacy"
        fresh.append(key)
    if fresh:
        state.write_json_atomic(_acked_path(root), {"acked": acked})
    state.write_json_atomic(
        marker, {"reconciled": len(fresh), "at": stamp},
    )
    return len(fresh)

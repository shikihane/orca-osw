from __future__ import annotations

import re

FORCED_HANDOFF_PROMPT: str = (
    "/handoff 请为刚完成的任务生成交接文件。"
    "文件名必须以 HANDOFF_ 开头，以 .md 结尾。"
    "完成后明确回复文件路径。"
)

_HANDOFF_FILENAME_RE = re.compile(r"HANDOFF_[\w\-\.]+\.md")


def extract_handoff_filename(text: str) -> str | None:
    """Search text for a HANDOFF_*.md filename and return the first match.

    Handles bare filenames, filenames embedded in a path (only the
    filename portion is returned), and filenames inline within other
    text. Returns None if no match is found.
    """
    match = _HANDOFF_FILENAME_RE.search(text)
    if match is None:
        return None
    return match.group(0)


def format_completion_report(agent_id: str, terminal: str, report_file: str) -> str:
    """Format the single-line completion notification.

    Must stay single-line: newlines get mangled when passed through
    orca.CMD as a --text argument on Windows. Full structured data
    lives in the JSON report file; the notification only points to it.

    Leading '#' keeps the line inert when the caller terminal is a
    shell (PowerShell/bash treat it as a comment instead of executing
    it); agent TUIs just see it as plain text.
    """
    message = f"# [osw] task-finished agent={agent_id} terminal={terminal}"
    if report_file:
        message += f" report={report_file}"
    return message


def format_handoff_failed_message() -> str:
    """Format the message used when a handoff filename could not be confirmed."""
    return "子任务已停止，但未能确认 HANDOFF_*.md 文件名。请检查 agent 输出。"

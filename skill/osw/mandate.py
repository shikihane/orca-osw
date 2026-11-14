"""The Mandate: the code-checked limits a Ticket carries (design §11).

``skill/osw/mandate.json`` is committed and human-owned; changing it requires
a commit. The Dispatcher reads the committed ``HEAD`` version; a Router that
opens a Ticket may only narrow it. Every check here is pure code: an unknown
field, a wrong type, a widening request or an expired document is refused with
one typed error, never repaired silently.

Operator-facing reasons are Chinese because the person editing the mandate and
reading ``mandate_denied`` events is the operator of this workspace.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from osw.mailbox.errors import MailboxError
from osw.process import hidden_subprocess_kwargs

MANDATE_RELATIVE_PATH = "skill/osw/mandate.json"
GIT_TIMEOUT_S = 5

ACTIONS = (
    "accept", "followup", "answer", "stop", "retry", "reject", "escalate",
    "noop", "close",
)
ANSWER_MODES = ("none", "options_only", "free_text")
ANSWER_RANK = {mode: rank for rank, mode in enumerate(ANSWER_MODES)}
CLOSE_AUTHORITIES = ("router", "human")
FIELDS = (
    "actions", "answer", "max_retries", "max_workers", "providers",
    "open_from", "close", "expires_at",
)
OPTIONS_ONLY_MAX_CHARS = 512
FREE_TEXT_MAX_CHARS = 4096
HUMAN_SOURCE = ""


class MandateError(MailboxError):
    """One typed mandate failure: ``mandate_invalid``/``_missing``/``_denied``
    /``_expired``."""


def _fail(code: str, reason: str) -> MandateError:
    return MandateError(code, reason)


def _str_list(value: object, field: str, *, allowed: tuple[str, ...] = ()) -> list[str]:
    if not isinstance(value, list):
        raise _fail("mandate_invalid", f"Mandate 字段 {field} 必须是字符串数组")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _fail("mandate_invalid", f"Mandate 字段 {field} 只能包含非空字符串")
        if allowed and item not in allowed:
            raise _fail("mandate_invalid", f"Mandate 字段 {field} 含未知取值: {item}")
        if item in items:
            raise _fail("mandate_invalid", f"Mandate 字段 {field} 含重复取值: {item}")
        items.append(item)
    return items


def _budget(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail("mandate_invalid", f"Mandate 字段 {field} 必须是非负整数")
    if value < 0:
        raise _fail("mandate_invalid", f"Mandate 字段 {field} 不能为负")
    return value


def _choice(value: object, field: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _fail(
            "mandate_invalid",
            f"Mandate 字段 {field} 必须是 {' / '.join(allowed)} 之一",
        )
    return value


def _parse_epoch(text: str) -> float:
    """Parse an ISO-8601 instant; a naive value is UTC."""
    if not isinstance(text, str) or not text.strip():
        raise _fail("mandate_invalid", "Mandate 字段 expires_at 必须是非空 ISO-8601 时间或空串")
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise _fail("mandate_invalid", f"Mandate 字段 expires_at 不是合法时间: {text}") from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _expiry(value: object, field: str = "expires_at") -> str:
    if not isinstance(value, str):
        raise _fail("mandate_invalid", f"Mandate 字段 {field} 必须是字符串")
    if value == "":
        return ""
    _parse_epoch(value)
    return value


def _fields(doc: object) -> dict:
    """Check the closed key set and per-field types; return normalized values."""
    if not isinstance(doc, dict):
        raise _fail("mandate_invalid", "Mandate 必须是 JSON 对象")
    unknown = sorted(set(doc) - set(FIELDS))
    if unknown:
        raise _fail("mandate_invalid", f"Mandate 含未知字段: {', '.join(unknown)}")
    missing = [field for field in FIELDS if field not in doc]
    if missing:
        raise _fail("mandate_invalid", f"Mandate 缺少字段: {', '.join(missing)}")
    return {
        "actions": _str_list(doc["actions"], "actions", allowed=ACTIONS),
        "answer": _choice(doc["answer"], "answer", ANSWER_MODES),
        "max_retries": _budget(doc["max_retries"], "max_retries"),
        "max_workers": _budget(doc["max_workers"], "max_workers"),
        "providers": _str_list(doc["providers"], "providers"),
        "open_from": _str_list(doc["open_from"], "open_from"),
        "close": _choice(doc["close"], "close", CLOSE_AUTHORITIES),
        "expires_at": _expiry(doc["expires_at"]),
    }


def validate(doc: dict) -> dict:
    """Return the normalized Mandate; any unknown key or wrong type raises."""
    return _fields(doc)


def _copy(mandate: dict) -> dict:
    return {
        key: list(value) if isinstance(value, list) else value
        for key, value in mandate.items()
    }


def _widened(field: str) -> MandateError:
    return _fail("mandate_denied", f"请求会放宽 Mandate 限制: {field}")


def narrow(base: dict, requested: dict | None = None) -> dict:
    """Return ``base`` narrowed by ``requested``; widening is refused."""
    base = validate(base)
    if requested is None:
        return _copy(base)
    if not isinstance(requested, dict):
        raise _fail("mandate_invalid", "收窄请求必须是 JSON 对象")
    partial = _fields({**base, **requested}) if requested else _copy(base)
    for field in ("actions", "providers", "open_from"):
        if field in requested:
            extra = [item for item in partial[field] if item not in base[field]]
            if extra:
                raise _widened(f"{field}={extra[0]}")
    for field in ("max_retries", "max_workers"):
        if field in requested and partial[field] > base[field]:
            raise _widened(f"{field}={partial[field]}")
    if "answer" in requested and ANSWER_RANK[partial["answer"]] > ANSWER_RANK[base["answer"]]:
        raise _widened(f"answer={partial['answer']}")
    if "close" in requested and partial["close"] != base["close"]:
        raise _widened(f"close={partial['close']}")
    if "expires_at" in requested:
        wanted, limit = partial["expires_at"], base["expires_at"]
        if wanted == "" and limit != "":
            raise _widened("expires_at")
        if limit != "" and _parse_epoch(wanted) > _parse_epoch(limit):
            raise _widened(f"expires_at={wanted}")
    return validate(partial)


def expires_at_epoch(mandate: dict) -> float | None:
    """The expiry as a Unix epoch, or ``None`` when the mandate never expires."""
    text = mandate["expires_at"]
    return _parse_epoch(text) if text else None


def answer_allowed(mandate: dict, text: str) -> bool:
    """Whether ``text`` fits the mandate's answer mode (one line, bounded)."""
    if not isinstance(text, str):
        return False
    if "\n" in text or "\r" in text:
        return False
    body = text.strip()
    if not body or len(text) > FREE_TEXT_MAX_CHARS:
        return False
    mode = mandate["answer"]
    if mode == "none":
        return False
    if mode == "free_text":
        return True
    if mode != "options_only" or len(text) > OPTIONS_ONLY_MAX_CHARS:
        return False
    if body.startswith("-"):
        return True
    head = body.split(")", 1)
    return len(head) == 2 and head[0].isdigit()


def check_action(
    mandate: dict,
    action: str,
    *,
    now: float | None = None,
    answer_text: str = "",
    close_authority: str = "",
) -> None:
    """Refuse a Decision action outside the mandate; returns ``None`` if allowed."""
    mandate = validate(mandate)
    deadline = expires_at_epoch(mandate)
    if deadline is not None and deadline <= (time.time() if now is None else now):
        raise _fail(
            "mandate_expired",
            f"Mandate 已于 {mandate['expires_at']} 过期",
        )
    if action not in mandate["actions"]:
        raise _fail("mandate_denied", f"动作 {action} 不在 Mandate 授权内")
    if action == "answer":
        if mandate["answer"] == "none":
            raise _fail("mandate_denied", "Mandate 的 answer 模式为 none，不允许回答")
        if not answer_allowed(mandate, answer_text):
            raise _fail(
                "mandate_denied",
                f"回答不符合 answer 模式 {mandate['answer']} 的长度或格式限制",
            )
    if action == "close" and mandate["close"] == "human" and close_authority != "human":
        raise _fail("mandate_denied", "close 权限属于 human，当前来源无权关闭")


def check_open_from(mandate: dict, source: str) -> bool:
    """Whether ``source`` may open a Ticket; ``""`` is the human, always allowed."""
    if not isinstance(source, str):
        return False
    if source == HUMAN_SOURCE:
        return True
    return source in mandate["open_from"]


def _committed(root: Path, ref: str) -> str | None:
    """``git -C root show <ref>:skill/osw/mandate.json``, ``None`` when absent."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "show", f"{ref}:{MANDATE_RELATIVE_PATH}"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_S,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout


def load_mandate(root: Path, *, ref: str = "HEAD") -> dict:
    """Load the committed mandate, falling back to the working-tree file."""
    root = Path(root)
    text = _committed(root, ref)
    if text is None:
        path = root / MANDATE_RELATIVE_PATH
        if not path.is_file():
            raise _fail("mandate_missing", f"缺少 Mandate 文件: {MANDATE_RELATIVE_PATH}")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise _fail("mandate_missing", f"无法读取 Mandate 文件: {exc}") from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _fail("mandate_invalid", f"Mandate 不是合法 JSON: {exc}") from exc
    return validate(doc)

"""Optional source adapters: local files and processes into spool envelopes.

Design: spec §6.2; interfaces: plan §11.

Four adapters are independent of one another and of the store:

- ``log_file`` watches configured files and reports new lines;
- ``crash_file`` watches configured files for crash signatures (and for a
  newly appearing crash file);
- ``build_result`` watches configured build logs for a failed build;
- ``process_exit`` polls configured pids and reports an exit.

Each adapter owns its read positions in
``mailbox_dir/adapters/<name>.json``, so rotation (a different file identity)
or truncation (the file shrank) restarts only that adapter's position. One
poll aggregates every match per thread into a single digest envelope, written
through :func:`osw.mailbox.spool.write_envelope`; identical digests are not
re-emitted. An adapter that raises emits exactly one ``adapter_failed`` event
through :func:`osw.log.emit_event`, disables itself in its state file and
leaves the others polling. A disabled adapter stays disabled until an operator
clears ``disabled`` in its state file.

Adapters never touch the store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from osw.log import emit_event
from osw.state import logs_dir

from .paths import adapters_dir, mailbox_dir
from .spool import write_envelope

ADAPTERS = ("log_file", "crash_file", "build_result", "process_exit")

CONFIG_FILENAME = "adapters.json"
STATE_VERSION = 1

MAX_READ_BYTES = 1 << 20
MAX_DIGEST_SEEN = 32
SUMMARY_CHARS = 400

# Windows keeps a live process at STILL_ACTIVE; anything else is an exit code.
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5

CRASH_RE = re.compile(
    r"(?i)(?:panic|segmentation fault|segfault|sigsegv|sigabrt|sigbus|"
    r"core dumped|out of memory|outofmemory|\boom\b|oom[- ]kill|"
    r"fatal error|traceback \(most recent call last\))"
)

BUILD_FAILURE_RE = re.compile(
    r"(?i)(?:build|compile|compilation).{0,40}(?:failed|failure|error)"
    r"|^FAILED\b|\bnpm ERR!|\bmake(?:\[\d+\])?: \*\*\*"
    r"|\bexit code [1-9]\d*\b"
)


# ---------------------------------------------------------------------------
# Config and per-adapter state
# ---------------------------------------------------------------------------


def config_path(root: Path) -> Path:
    """``mailbox_dir/adapters.json``; the file is optional."""
    return mailbox_dir(root) / CONFIG_FILENAME


def state_path(root: Path, name: str) -> Path:
    """``mailbox_dir/adapters/<name>.json``."""
    return adapters_dir(root) / f"{name}.json"


def load_config(root: Path) -> dict:
    """Read the optional adapter config; a broken file reads as empty."""
    try:
        raw = config_path(root).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def load_state(root: Path, name: str) -> dict:
    """Read one adapter's state; a missing or broken file starts empty."""
    try:
        raw = state_path(root, name).read_text(encoding="utf-8")
    except OSError:
        return {"version": STATE_VERSION}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {"version": STATE_VERSION}
    if not isinstance(doc, dict):
        return {"version": STATE_VERSION}
    doc.setdefault("version", STATE_VERSION)
    return doc


def save_state(root: Path, name: str, state: dict) -> None:
    """Atomically persist one adapter's state (``os.replace``)."""
    path = state_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    state.setdefault("version", STATE_VERSION)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Tail reading: one read position per adapter, rotation and truncation aware
# ---------------------------------------------------------------------------


def _identity(st: os.stat_result) -> list[int]:
    """A file's identity: (device, inode), or ctime where inodes are missing."""
    inode = int(getattr(st, "st_ino", 0) or 0)
    if inode:
        return [int(getattr(st, "st_dev", 0) or 0), inode]
    return [0, int(getattr(st, "st_ctime_ns", 0) or 0)]


def _read_new_lines(
    path: Path,
    entry: dict,
    *,
    limit: int = MAX_READ_BYTES,
) -> tuple[list[str], dict, bool]:
    """Read complete new lines from ``path`` and return the next entry.

    Returns ``(lines, entry, restarted)``. A different file identity (rotation)
    or a shrunken file (truncation) restarts the position at 0, so old bytes
    are never replayed as new. A trailing partial line is left for the next
    poll; a path that does not exist yet yields no lines and keeps its entry.
    """
    try:
        st = path.stat()
    except OSError:
        return [], entry, False

    identity = _identity(st)
    offset = int(entry.get("offset") or 0)
    restarted = bool(entry) and (
        entry.get("identity") != identity or st.st_size < offset
    )
    if restarted:
        offset = 0

    next_entry = {"offset": offset, "identity": identity, "size": st.st_size}
    if st.st_size <= offset:
        return [], next_entry, restarted

    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(min(st.st_size - offset, limit))

    parts = chunk.split(b"\n")
    pending = parts.pop()
    if pending and b"\n" not in chunk and len(chunk) >= limit:
        # One unterminated line longer than the cap: consume it truncated
        # rather than blocking the position forever.
        parts.append(pending)
        pending = b""

    lines = [part.decode("utf-8", "replace").rstrip("\r") for part in parts]
    consumed = offset + sum(len(part) + 1 for part in parts)
    next_entry["offset"] = consumed
    return lines, next_entry, restarted


# ---------------------------------------------------------------------------
# Digest envelopes
# ---------------------------------------------------------------------------


def _fingerprint(name: str, event: str, summary: str) -> str:
    raw = f"{name}\0{event}\0{summary}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def _summarize(text: str) -> str:
    summary = " ".join(str(text).split())
    if len(summary) > SUMMARY_CHARS:
        summary = summary[: SUMMARY_CHARS - 1] + "…"
    return summary


def _digest_envelope(
    name: str,
    *,
    event: str,
    thread_key: str,
    path: str,
    summary: str,
    count: int,
    fingerprint: str,
) -> dict:
    return {
        "from": {"role": "system", "id": f"adapter:{name}"},
        "to": {"role": "router", "id": ""},
        "kind": "system",
        "thread_key": thread_key,
        "dedupe_key": f"adapter:{name}:{thread_key}:{fingerprint}",
        "body": {
            "event": event,
            "summary": summary,
            "count": count,
            "path": path,
        },
    }


def _emit_digest(
    root: Path,
    state: dict,
    *,
    name: str,
    event: str,
    thread_key: str,
    path: str,
    summary: str,
    count: int,
) -> int:
    """Write one digest envelope unless this thread already emitted it.

    Returns 1 when an envelope was written, 0 when it was deduplicated.
    """
    fingerprint = _fingerprint(name, event, summary)
    digests = _sub_dict(state, "digests")
    seen = digests.get(thread_key)
    if not isinstance(seen, list):
        seen = []
        digests[thread_key] = seen
    if fingerprint in seen:
        return 0
    envelope = _digest_envelope(
        name,
        event=event,
        thread_key=thread_key,
        path=path,
        summary=summary,
        count=count,
        fingerprint=fingerprint,
    )
    write_envelope(root, envelope)
    seen.append(fingerprint)
    if len(seen) > MAX_DIGEST_SEEN:
        del seen[:-MAX_DIGEST_SEEN]
    return 1


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _sub_dict(state: dict, key: str) -> dict:
    value = state.get(key)
    if not isinstance(value, dict):
        value = {}
        state[key] = value
    return value


def _epoch(now: Any) -> float:
    if now is None:
        return time.time()
    if isinstance(now, (int, float)):
        return float(now)
    return float(now.timestamp())


def _resolve(root: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else root / path


def _paths(root: Path, cfg: dict) -> list[Path]:
    raw = cfg.get("paths")
    if not isinstance(raw, list):
        return []
    paths: list[Path] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            path = _resolve(root, item)
            if path not in paths:
                paths.append(path)
    return paths


def _pids(cfg: dict) -> list[int]:
    raw = cfg.get("pids")
    if not isinstance(raw, list):
        return []
    pids: list[int] = []
    for item in raw:
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and item > 0:
            pid = item
        elif isinstance(item, str) and item.strip().isdigit():
            pid = int(item.strip())
        else:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def _has_targets(root: Path, name: str, cfg: dict) -> bool:
    if name == "process_exit":
        return bool(_pids(cfg))
    return bool(_paths(root, cfg))


# ---------------------------------------------------------------------------
# File adapters
# ---------------------------------------------------------------------------


def _match_any(line: str, _first_sight: bool) -> bool:
    return bool(line.strip())


def _match_crash(line: str, first_sight: bool) -> bool:
    if not line.strip():
        return False
    return bool(CRASH_RE.search(line)) or first_sight


def _match_build_failure(line: str, _first_sight: bool) -> bool:
    return bool(line.strip()) and bool(BUILD_FAILURE_RE.search(line))


def _file_adapter(
    root: Path,
    cfg: dict,
    state: dict,
    now: float,
    *,
    name: str,
    event: str,
    match: Callable[[str, bool], bool],
) -> dict[str, int]:
    counts = {"envelopes": 0, "matched": 0}
    files = _sub_dict(state, "files")
    for path in _paths(root, cfg):
        key = str(path)
        entry = files.get(key)
        entry = entry if isinstance(entry, dict) else {}
        lines, next_entry, _restarted = _read_new_lines(path, entry)
        # A file's first sight is also its "new file" trigger for crash_file.
        first_sight = not entry
        files[key] = next_entry
        matched = [line for line in lines if match(line, first_sight)]
        if not matched:
            continue
        counts["matched"] += len(matched)
        counts["envelopes"] += _emit_digest(
            root,
            state,
            name=name,
            event=event,
            thread_key=key,
            path=key,
            summary=_summarize(matched[0]),
            count=len(matched),
        )
    return counts


def _adapter_log_file(root: Path, cfg: dict, state: dict, now: float) -> dict[str, int]:
    return _file_adapter(
        root, cfg, state, now, name="log_file", event="log_line", match=_match_any)


def _adapter_crash_file(
    root: Path, cfg: dict, state: dict, now: float,
) -> dict[str, int]:
    return _file_adapter(
        root, cfg, state, now, name="crash_file", event="crash", match=_match_crash)


def _adapter_build_result(
    root: Path, cfg: dict, state: dict, now: float,
) -> dict[str, int]:
    return _file_adapter(
        root,
        cfg,
        state,
        now,
        name="build_result",
        event="build_failed",
        match=_match_build_failure,
    )


# ---------------------------------------------------------------------------
# Process exit
# ---------------------------------------------------------------------------


def _pid_status(pid: int) -> tuple[bool, int | None]:
    """``(alive, exit_code)`` for ``pid`` using only the stdlib.

    ``exit_code`` is ``None`` when the platform cannot report it (POSIX, or a
    pid the kernel has already recycled away).
    """
    if os.name == "nt":
        return _windows_pid_status(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        return True, None
    except OSError:
        return False, None
    return True, None


def _windows_pid_status(pid: int) -> tuple[bool, int | None]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        # Access denied means the process exists but is not ours to query.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED, None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False, None
        if code.value == STILL_ACTIVE:
            return True, None
        return False, int(code.value)
    finally:
        kernel32.CloseHandle(handle)


def _adapter_process_exit(
    root: Path, cfg: dict, state: dict, now: float,
) -> dict[str, int]:
    counts = {"envelopes": 0, "matched": 0}
    records = _sub_dict(state, "pids")
    for pid in _pids(cfg):
        key = str(pid)
        record = records.get(key)
        record = record if isinstance(record, dict) else {}
        if record.get("reported"):
            continue
        alive, code = _pid_status(pid)
        if alive:
            records[key] = {"alive": True}
            continue
        if code == 0:
            # A clean exit is not a failure event; stop polling it.
            records[key] = {"alive": False, "exit_code": 0, "reported": True}
            continue
        summary = f"pid {pid} exited"
        if code is not None:
            summary += f" (exit code {code})"
        counts["matched"] += 1
        counts["envelopes"] += _emit_digest(
            root,
            state,
            name="process_exit",
            event="process_exit",
            thread_key=key,
            path="",
            summary=summary,
            count=1,
        )
        records[key] = {
            "alive": False,
            "exit_code": code,
            "reported": True,
            "reported_at": round(now, 3),
        }
    return counts


_HANDLERS: dict[str, Callable[[Path, dict, dict, float], dict[str, int]]] = {
    "log_file": _adapter_log_file,
    "crash_file": _adapter_crash_file,
    "build_result": _adapter_build_result,
    "process_exit": _adapter_process_exit,
}


# ---------------------------------------------------------------------------
# One poll
# ---------------------------------------------------------------------------


def _disable(root: Path, name: str, state: dict, exc: Exception, now: float) -> None:
    """Disable one failed adapter, emitting exactly one ``adapter_failed``."""
    state["disabled"] = True
    state["error"] = f"{type(exc).__name__}: {exc}"
    state["disabled_at"] = round(now, 3)
    try:
        emit_event(
            logs_dir(root),
            component="adapter",
            event="adapter_failed",
            level="ERROR",
            message=state["error"],
            data={"adapter": name},
        )
    except Exception:
        pass
    try:
        save_state(root, name, state)
    except Exception:
        pass


def run_once(root: Path, now: Any = None) -> dict[str, dict]:
    """Poll every enabled adapter once; return per-adapter counts.

    Each adapter's failure is contained: it is disabled and reported once,
    and the remaining adapters still run.
    """
    root = Path(root)
    epoch = _epoch(now)
    config = load_config(root)
    enabled = config.get("enabled")
    enabled = enabled if isinstance(enabled, dict) else {}

    counts: dict[str, dict] = {}
    for name in ADAPTERS:
        counts[name] = {"envelopes": 0, "matched": 0}
        if enabled.get(name) is False:
            counts[name]["disabled"] = True
            continue

        cfg = config.get(name)
        cfg = cfg if isinstance(cfg, dict) else {}
        if not _has_targets(root, name, cfg):
            continue

        state = load_state(root, name)
        if state.get("disabled"):
            counts[name]["disabled"] = True
            continue

        try:
            before = json.dumps(state, ensure_ascii=False, sort_keys=True)
            result = _HANDLERS[name](root, cfg, state, epoch)
            after = json.dumps(state, ensure_ascii=False, sort_keys=True)
            if after != before:
                state["updated_at"] = round(epoch, 3)
                save_state(root, name, state)
        except Exception as exc:
            _disable(root, name, state, exc, epoch)
            counts[name]["failed"] = True
            counts[name]["error"] = f"{type(exc).__name__}: {exc}"
            continue
        counts[name] = result
    return counts

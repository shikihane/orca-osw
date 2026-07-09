from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_configured = False
_verbose = False
_file_handlers: dict[tuple[str, str, int], Path] = {}

LOG_RETENTION_DAYS = 7
MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB per file
MAX_LOG_BACKUPS = 5
EVENTS_FILENAME = "events.jsonl"


def setup_logging(verbose: bool = False) -> None:
    global _configured, _verbose
    if _configured:
        return
    _configured = True
    _verbose = verbose

    level = logging.DEBUG if verbose else logging.INFO

    try:
        from rich.logging import RichHandler

        handler = RichHandler(
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            tracebacks_show_locals=verbose,
            markup=False,
        )
    except ImportError:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    logging.basicConfig(level=level, handlers=[handler], force=True)


def enable_file_logging(log_dir: Path, prefix: str = "osw") -> Path:
    """Add a rotating file handler. Returns the log file path.

    Each process/prefix pair creates at most one file:
    <prefix>_YYYYMMDD_HHMMSS_<pid>.log
    Files rotate at MAX_LOG_BYTES with up to MAX_LOG_BACKUPS backups.
    Logs older than LOG_RETENTION_DAYS are cleaned up.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    key = (str(log_dir.resolve()), prefix, os.getpid())
    existing = _file_handlers.get(key)
    if existing is not None:
        return existing

    now = datetime.now()
    safe_prefix = "".join(c if c.isalnum() or c in "._-" else "_" for c in prefix)
    filename = f"{safe_prefix}_{now:%Y%m%d_%H%M%S}_{os.getpid()}.log"
    log_path = log_dir / filename

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=MAX_LOG_BACKUPS,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logging.getLogger().addHandler(file_handler)
    _file_handlers[key] = log_path

    _cleanup_old_logs(log_dir)

    return log_path


def events_file(log_dir: Path) -> Path:
    return log_dir / EVENTS_FILENAME


def emit_event(
    log_dir: Path,
    *,
    component: str,
    event: str,
    level: str = "INFO",
    agent_id: str = "",
    terminal: str = "",
    phase: str = "",
    message: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one structured event to the OSW JSONL event stream."""
    log_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level.upper(),
        "component": component,
        "event": event,
        "agent_id": agent_id,
        "terminal": terminal,
        "phase": phase,
        "message": " ".join((message or "").split()),
        "data": data or {},
    }
    with events_file(log_dir).open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload


def read_events(
    log_dir: Path,
    *,
    agent_id: str | None = None,
    tail: int | None = None,
) -> list[dict[str, Any]]:
    path = events_file(log_dir)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if agent_id and event.get("agent_id") != agent_id:
                continue
            events.append(event)
    if tail is not None and tail >= 0:
        return events[-tail:]
    return events


def format_event_line(event: dict[str, Any]) -> str:
    parts = [
        str(event.get("ts", "")),
        str(event.get("level", "")),
        str(event.get("component", "")),
        str(event.get("event", "")),
    ]
    agent_id = event.get("agent_id")
    terminal = event.get("terminal")
    phase = event.get("phase")
    if agent_id:
        parts.append(f"agent={agent_id}")
    if terminal:
        parts.append(f"terminal={terminal}")
    if phase:
        parts.append(f"phase={phase}")
    message = " ".join(str(event.get("message", "")).split())
    if message:
        parts.append(message)
    return " ".join(p for p in parts if p)


def _cleanup_old_logs(log_dir: Path) -> None:
    cutoff = time.time() - (LOG_RETENTION_DAYS * 86400)
    for f in log_dir.glob("*.log*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"osw.{name}")

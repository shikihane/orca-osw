from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False
_verbose = False

LOG_RETENTION_DAYS = 7
MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB per file
MAX_LOG_BACKUPS = 5


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


def enable_file_logging(log_dir: Path) -> Path:
    """Add a rotating file handler. Returns the log file path.

    Each invocation creates a new file: osw_YYYYMMDD_HHMMSS_<pid>.log
    Files rotate at MAX_LOG_BYTES with up to MAX_LOG_BACKUPS backups.
    Logs older than LOG_RETENTION_DAYS are cleaned up.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    from datetime import datetime

    now = datetime.now()
    filename = f"osw_{now:%Y%m%d_%H%M%S}_{os.getpid()}.log"
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

    _cleanup_old_logs(log_dir)

    return log_path


def _cleanup_old_logs(log_dir: Path) -> None:
    cutoff = time.time() - (LOG_RETENTION_DAYS * 86400)
    for f in log_dir.glob("osw_*.log*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"osw.{name}")

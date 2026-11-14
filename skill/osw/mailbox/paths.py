"""Filesystem layout of the mailbox, under ``<root>/.orca/osw/mailbox``.

Runtime state stays inside the gitignored ``.orca/`` tree, exactly like the
existing OSW state directory.
"""

from __future__ import annotations

from pathlib import Path


def mailbox_dir(root: Path) -> Path:
    return root / ".orca" / "osw" / "mailbox"


def spool_dir(root: Path) -> Path:
    return mailbox_dir(root) / "spool"


def rejected_dir(root: Path) -> Path:
    return spool_dir(root) / "rejected"


def ingested_dir(root: Path) -> Path:
    return spool_dir(root) / "ingested"


def db_path(root: Path) -> Path:
    return mailbox_dir(root) / "mailbox.db"


def lock_path(root: Path) -> Path:
    return mailbox_dir(root) / "daemon.lock"


def status_path(root: Path) -> Path:
    return mailbox_dir(root) / "status.json"


def stop_path(root: Path) -> Path:
    return mailbox_dir(root) / "daemon.stop"


def token_path(root: Path) -> Path:
    return mailbox_dir(root) / "router.token"


def adapters_dir(root: Path) -> Path:
    return mailbox_dir(root) / "adapters"


def ensure_mailbox_dirs(root: Path) -> None:
    """Create the mailbox directory tree (idempotent)."""
    for directory in (
        mailbox_dir(root),
        spool_dir(root),
        rejected_dir(root),
        ingested_dir(root),
        adapters_dir(root),
    ):
        directory.mkdir(parents=True, exist_ok=True)

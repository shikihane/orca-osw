"""Spool: producers write envelopes here, ``ingest`` is the only mover in.

Layout under ``<root>/.orca/osw/mailbox/spool``:

- ``<id>.json`` -- a pending envelope, written atomically by a producer.
- ``<id>.json.tmp`` -- the in-flight write, never treated as an envelope.
- ``ingested/<id>.json`` -- claimed by ingesting, then handed to
  ``store.enqueue``.
- ``rejected/<id>.json`` -- refused, with ``<id>.json.error.txt`` beside it
  carrying the typed error code.

Only ``os.replace`` moves a file, so a concurrent reader never observes a
half-written envelope. The move is the claim. On Windows a rename goes through
a file handle and is idempotent, so two ingesters that race may both claim the
same file and both call ``store.enqueue``; the message id in the store is the
tie-breaker and the extra one is counted as a duplicate. Nothing is lost and
nothing is written twice.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .envelope import validate_envelope
from .errors import MailboxError
from .paths import ensure_mailbox_dirs, ingested_dir, rejected_dir, spool_dir

if TYPE_CHECKING:  # pragma: no cover - typing only, the store slice owns it
    from .store import Store

MAX_SPOOL_FILES = 1000


def write_envelope(root: Path, envelope: dict) -> str:
    """Validate and atomically drop one envelope into the spool; return its id."""
    env = validate_envelope(envelope)
    ensure_mailbox_dirs(root)
    if pending_count(root) >= MAX_SPOOL_FILES:
        raise MailboxError(
            "spool_full",
            f"{MAX_SPOOL_FILES} pending spool files; wait for the mailbox to ingest",
        )
    message_id = env["id"]
    name = f"{message_id}.json"
    final = spool_dir(root) / name
    tmp = spool_dir(root) / f"{name}.tmp"
    tmp.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return message_id


def ingest(root: Path, store: "Store", *, limit: int = 100) -> dict:
    """Move up to ``limit`` pending envelopes into the store.

    One bad file is rejected, never fatal. Returns
    ``{"ingested": n, "duplicates": n, "rejected": n}``.
    """
    ensure_mailbox_dirs(root)
    counts = {"ingested": 0, "duplicates": 0, "rejected": 0}
    for path in _pending_files(root)[: max(0, limit)]:
        try:
            outcome = _ingest_one(root, store, path)
        except Exception:  # transient, e.g. a Windows share violation: retry next round
            continue
        if outcome in counts:
            counts[outcome] += 1
    return counts


def pending_count(root: Path) -> int:
    """Number of envelopes waiting in the spool (``.tmp`` files never count)."""
    return len(_pending_files(root))


def read_pending(root: Path) -> list[dict]:
    """Valid pending envelopes, best effort: invalid files are skipped."""
    envelopes: list[dict] = []
    for path in _pending_files(root):
        try:
            envelopes.append(validate_envelope(json.loads(path.read_bytes())))
        except Exception:  # unreadable, invalid or moved away: best effort
            continue
    return envelopes


def _pending_files(root: Path) -> list[Path]:
    directory = spool_dir(root)
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    return [p for p in entries if p.is_file() and p.name.endswith(".json")]


def _ingest_one(root: Path, store: "Store", path: Path) -> str:
    try:
        raw = _read(path)
    except FileNotFoundError:
        return "retry"  # another ingester owns this file now
    except OSError:
        return "retry"  # transient share violation, e.g. a reader on Windows
    try:
        env = validate_envelope(json.loads(raw))
    except MailboxError as exc:
        _reject(root, path, exc.code, exc.message)
        return "rejected"
    except Exception as exc:  # unreadable or invalid JSON: still just one bad file
        _reject(root, path, "invalid_envelope", str(exc))
        return "rejected"

    claimed = _claim(root, path)
    if claimed is None:
        return "retry"
    try:
        _row, inserted = store.enqueue(env)
    except Exception as exc:
        code = exc.code if isinstance(exc, MailboxError) else "invalid_envelope"
        _reject(root, claimed, code, str(exc))
        return "rejected"
    return "ingested" if inserted else "duplicates"


def _read(path: Path) -> bytes:
    """Read a pending file, riding out a Windows share violation."""
    for delay in (0.0, 0.01, 0.05, 0.1):
        if delay:
            time.sleep(delay)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise
        except PermissionError:
            continue
    return path.read_bytes()


def _claim(root: Path, path: Path) -> Path | None:
    """Atomically move a pending file into ``ingested/``; None if it is gone."""
    claimed = ingested_dir(root) / path.name
    return claimed if _move(path, claimed) else None


def _move(source: Path, target: Path) -> bool:
    """``os.replace`` with a short retry; False when someone else moved it.

    On Windows a concurrent reader can hold the file open for a moment, so a
    share violation is retried instead of failing the ingest.
    """
    for delay in (0.0, 0.01, 0.05, 0.1):
        if delay:
            time.sleep(delay)
        try:
            os.replace(source, target)
        except FileNotFoundError:
            return False
        except PermissionError:
            continue
        return True
    return False


def _reject(root: Path, claimed: Path, code: str, message: str) -> None:
    _move(claimed, rejected_dir(root) / claimed.name)
    _write_error(root, claimed.name, code, message)


def _write_error(root: Path, name: str, code: str, message: str) -> None:
    lines = [code]
    if message and message != code:
        lines.append(message)
    path = rejected_dir(root) / f"{name}.error.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

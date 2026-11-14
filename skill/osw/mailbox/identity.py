"""Router identity: a role marker plus a per-project role token.

The transport address (terminal handle) says where a process is, not who it
is: a human in the same terminal keeps the human identity. Only a process
that carries ``OSW_ROLE=router`` *and* the project's role token is the
Router, and the Router may not run human-only commands (design §9.1, §16).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Mapping

from . import paths
from .errors import MailboxError

ROLE_ENV = "OSW_ROLE"
TOKEN_ENV = "OSW_ROLE_TOKEN"
ROUTER_ROLE = "router"
HUMAN_ROLE = "human"

TOKEN_BYTES = 32
PRIVATE_MODE = 0o600


def generate_token(root: Path) -> str:
    """Return this project's router token, creating it on first use.

    The token is a lowercase hex secret in :func:`paths.token_path` with
    owner-only mode. An existing token is returned unchanged, so a restarted
    Router re-registers with the same secret; only an absent or empty file is
    written.
    """
    token = read_token(root)
    if token:
        return token
    path = paths.token_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = secrets.token_hex(TOKEN_BYTES)
    try:
        handle = os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_MODE),
            "w",
            encoding="utf-8",
        )
    except FileExistsError:
        # Another process won the race; whatever it wrote is the token.
        # An empty file (a crashed first run) is written again.
        existing = read_token(root)
        if existing:
            return existing
        path.write_text(candidate, encoding="utf-8")
        _restrict(path)
        return candidate
    with handle:
        handle.write(candidate)
    _restrict(path)
    return candidate


def read_token(root: Path) -> str:
    """This project's router token, or ``""`` when there is none yet."""
    try:
        return paths.token_path(root).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def current_role(root: Path, *, env: Mapping[str, str] | None = None) -> str:
    """``router`` only for the router marker with a matching role token."""
    source = os.environ if env is None else env
    marker = str(source.get(ROLE_ENV) or "").strip().lower()
    if marker != ROUTER_ROLE:
        return HUMAN_ROLE
    supplied = str(source.get(TOKEN_ENV) or "").strip()
    if not supplied:
        return HUMAN_ROLE
    token = read_token(root)
    if token and secrets.compare_digest(supplied, token):
        return ROUTER_ROLE
    return HUMAN_ROLE


def require_human_identity(root: Path, *, command: str = "") -> None:
    """Refuse a human-only command when the caller is the Router.

    The Router decides; the Dispatcher executes. ``command`` names the verb
    so the refusal reads like an instruction.
    """
    if current_role(root) != ROUTER_ROLE:
        return
    label = (command or "this command").strip()
    raise MailboxError(
        "router_forbidden",
        f"{label} is human-only: the Router writes a Decision and the "
        "Dispatcher executes it",
    )


def _restrict(path: Path) -> None:
    """Best-effort owner-only mode; Windows only toggles the read-only bit."""
    try:
        os.chmod(path, PRIVATE_MODE)
    except OSError:
        pass

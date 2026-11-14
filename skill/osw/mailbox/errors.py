"""Typed errors for the mailbox control plane.

The taxonomy is closed: call sites surface these codes and never invent their
own. ``OrcaError`` and ``HostError`` stay untouched.
"""

from __future__ import annotations

CODES = frozenset({
    "invalid_envelope",
    "body_too_large",
    "unknown_kind",
    "unknown_role",
    "invalid_priority",
    "invalid_field",
    "mailbox_locked",
    "mailbox_unavailable",
    "spool_full",
    "lease_lost",
    "unknown_message",
    "unknown_ticket",
    "duplicate_decision",
    "decision_invalid",
    "mandate_missing",
    "mandate_invalid",
    "mandate_denied",
    "mandate_expired",
    "close_precondition_failed",
    "ask_not_open",
    "router_forbidden",
    "adapter_failed",
    "store_corrupt",
})


class MailboxError(Exception):
    """One typed control-plane failure.

    ``code`` is one of :data:`CODES`; ``message`` is operator-facing detail.
    """

    def __init__(self, code: str, message: str = "") -> None:
        if code not in CODES:
            raise ValueError(f"unknown mailbox error code: {code}")
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}" if message else code)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}

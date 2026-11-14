"""Mailbox: the OSW control-plane store, spool, daemon and adapters.

Design: ``docs/superpowers/specs/2026-11-14-osw-control-plane-design.md``.
The package is the only writer of control state; producers write envelopes to
the spool, and the Mailbox owns messages, tickets, decisions, effects and the
outbox.
"""

from __future__ import annotations

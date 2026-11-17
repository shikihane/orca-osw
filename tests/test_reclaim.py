"""Reclamation policy: which finished agents may lose their terminal.

The policy is one predicate (`decision`) and one closer (`close_if_open`),
shared by the watcher at the end of a turn and by `osw sweep`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from osw import reclaim, state
from osw.hosts import local_host
from osw.orca_cli import OrcaError


LOCAL = local_host()


def _agent(**extra) -> dict:
    record = {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "pane_key": "tab:leaf",
        "state": "done",
        "origin": "new",
    }
    record.update(extra)
    return record


# --- decision ------------------------------------------------------------

@pytest.mark.parametrize("state_name,reason", [
    ("working", "not_settled"),
    ("starting", "not_settled"),
    ("error", "not_clean"),
    ("lost", "not_clean"),
    ("", "not_settled"),
])
def test_decision_keeps_unsettled_and_failed_turns(state_name, reason):
    allowed, why = reclaim.decision(_agent(state=state_name))
    assert (allowed, why) == (False, reason)


@pytest.mark.parametrize("state_name", ["error", "lost"])
def test_decision_force_widens_to_failed_turns(state_name):
    assert reclaim.decision(_agent(state=state_name), force=True) == (True, "")


@pytest.mark.parametrize("origin", ["use", "enroll", "", None])
def test_decision_never_touches_an_adopted_pane(origin):
    allowed, why = reclaim.decision(_agent(origin=origin))
    assert (allowed, why) == (False, "adopted_pane")


def test_decision_leaves_a_supervised_dispatch_terminal_to_orca():
    allowed, why = reclaim.decision(_agent(launch_mode="supervised"))
    assert (allowed, why) == (False, "supervised")


def test_decision_respects_a_human_reopened_turn():
    allowed, why = reclaim.decision(_agent(), human_turn=True)
    assert (allowed, why) == (False, "human_turn")


def test_decision_needs_a_pane_to_close():
    allowed, why = reclaim.decision(_agent(terminal="", pane_key=""))
    assert (allowed, why) == (False, "no_terminal")


def test_decision_allows_a_done_agent_osw_launched():
    assert reclaim.decision(_agent()) == (True, "")


def test_decision_accepts_the_legacy_pane_key_spelling():
    assert reclaim.decision(_agent(terminal="", pane_key="", paneKey="tab:leaf")) \
        == (True, "")


# --- open_terminal -------------------------------------------------------

def test_open_terminal_prefers_pane_identity_over_a_stale_handle():
    terminals = [
        {"handle": "term-a", "tabId": "tab", "leafId": "other"},
        {"handle": "term-b", "tabId": "tab", "leafId": "leaf"},
    ]
    assert reclaim.open_terminal(terminals, _agent())["handle"] == "term-b"


def test_open_terminal_falls_back_to_the_handle_without_a_pane_key():
    terminals = [{"handle": "term-a", "tabId": "tab", "leafId": "x"}]
    found = reclaim.open_terminal(terminals, _agent(pane_key=""))
    assert found["handle"] == "term-a"


def test_open_terminal_reports_a_pane_that_is_gone():
    terminals = [{"handle": "term-b", "tabId": "tab", "leafId": "leaf"}]
    gone = _agent(pane_key="", terminal="term-a")
    assert reclaim.open_terminal(terminals, gone) is None


# --- close_if_open -------------------------------------------------------

@pytest.mark.anyio
async def test_close_if_open_closes_the_matched_terminal(tmp_path):
    state.init_state_dir(tmp_path)
    close = AsyncMock(return_value={})
    with patch("osw.reclaim.terminal_close", close):
        closed = await reclaim.close_if_open(
            tmp_path, _agent(), LOCAL,
            [{"handle": "term-cur", "tabId": "tab", "leafId": "leaf"}],
        )

    assert closed is True
    close.assert_awaited_once_with("term-cur", host=LOCAL)


@pytest.mark.anyio
async def test_close_if_open_is_idempotent_when_the_pane_is_gone(tmp_path):
    state.init_state_dir(tmp_path)
    close = AsyncMock(return_value={})
    with patch("osw.reclaim.terminal_close", close):
        closed = await reclaim.close_if_open(tmp_path, _agent(), LOCAL, [])

    assert closed is False
    close.assert_not_awaited()


@pytest.mark.anyio
async def test_close_if_open_lists_when_no_snapshot_is_given(tmp_path):
    state.init_state_dir(tmp_path)
    listing = AsyncMock(return_value=[
        {"handle": "term-cur", "tabId": "tab", "leafId": "leaf"}])
    close = AsyncMock(return_value={})
    with patch("osw.reclaim.terminal_list", listing), \
         patch("osw.reclaim.terminal_close", close):
        closed = await reclaim.close_if_open(tmp_path, _agent(), LOCAL)

    assert closed is True
    close.assert_awaited_once_with("term-cur", host=LOCAL)


@pytest.mark.anyio
async def test_close_if_open_raises_when_the_listing_fails(tmp_path):
    state.init_state_dir(tmp_path)
    with patch("osw.reclaim.terminal_list",
               AsyncMock(side_effect=OrcaError("down", 1))):
        with pytest.raises(OrcaError):
            await reclaim.close_if_open(tmp_path, _agent(), LOCAL)

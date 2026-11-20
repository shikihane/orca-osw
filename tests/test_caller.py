"""Caller inference from Orca's hook cache.

No Orca runtime is reached: the cache entries and the local agent records
are the only inputs, and both are patched here.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from osw import caller


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _entry(
    pane_key: str,
    worktree: str,
    agent_type: str = "omp",
    session_id: str = "sess-1",
    handle: str = "",
) -> dict:
    entry = {
        "paneKey": pane_key,
        "worktreeId": f"uuid::{worktree}",
        "providerSession": {"key": "k", "id": session_id},
    }
    if agent_type:
        entry["agentType"] = agent_type
    if handle:
        entry["handle"] = handle
    return entry


def _record(pane_key: str, agent_id: str = "agent_001") -> dict:
    return {"agent_id": agent_id, "pane_key": pane_key, "state": "working"}


async def _infer(tmp_path, entries, records=None) -> dict:
    with patch(
        "osw.caller.hookcache.entries", return_value=entries,
    ), patch(
        "osw.caller.state.list_agents", return_value=records or {},
    ):
        return await caller.infer_caller_pane(tmp_path)


@pytest.mark.anyio
async def test_single_agent_pane_is_returned_as_the_caller(tmp_path):
    entries = [_entry("tab-1:pane-1", str(tmp_path))]

    result = await _infer(tmp_path, entries)

    assert result == {
        "handle": "",
        "pane_key": "tab-1:pane-1",
        "title": "omp sess-1",
    }


@pytest.mark.anyio
async def test_two_agent_panes_are_ambiguous_and_return_nothing(tmp_path):
    entries = [
        _entry("tab:one", str(tmp_path)),
        _entry("tab:two", str(tmp_path), agent_type="claude"),
    ]

    assert await _infer(tmp_path, entries) == {}


@pytest.mark.anyio
async def test_worktree_path_matches_across_separators(tmp_path):
    as_posix = str(tmp_path).replace("\\", "/")
    entries = [_entry("tab:one", as_posix)]

    result = await _infer(tmp_path, entries)

    assert result["pane_key"] == "tab:one"


@pytest.mark.anyio
async def test_an_osw_owned_pane_is_excluded(tmp_path):
    entries = [_entry("tab:worker", str(tmp_path))]
    records = {"agent_001": _record("tab:worker")}

    assert await _infer(tmp_path, entries, records) == {}


@pytest.mark.anyio
async def test_an_entry_from_another_worktree_is_ignored(tmp_path):
    other = tmp_path / "elsewhere"
    entries = [_entry("tab:other", str(other))]

    assert await _infer(tmp_path, entries) == {}


@pytest.mark.anyio
async def test_an_entry_without_a_session_is_not_a_candidate(tmp_path):
    entries = [_entry("tab:one", str(tmp_path), session_id="")]

    assert await _infer(tmp_path, entries) == {}


@pytest.mark.anyio
async def test_an_empty_cache_yields_no_caller(tmp_path):
    assert await _infer(tmp_path, []) == {}

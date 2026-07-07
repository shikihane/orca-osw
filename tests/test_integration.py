from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anyio
from typer.testing import CliRunner

from osw import state as state_mod
from osw.cli import app
from osw.server import run_server

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _hang_forever(*args: object, **kwargs: object) -> dict:
    """A drop-in replacement for terminal_wait that never resolves.

    Used so the per-agent watcher (started by handle_new/handle_use) parks
    itself waiting for "tui-idle" and never advances to send the forced
    handoff prompt or flip agent state away from "assigned". This keeps
    the tests deterministic: without it, the watcher (driven entirely by
    instantly-resolving mocks) could race ahead of the assertions in the
    test body and move the agent to "idle"/"handoff_failed" before we get
    a chance to observe "assigned".
    """
    await anyio.sleep_forever()
    return {}  # pragma: no cover - unreachable, sleep_forever never returns


async def _wait_for_result(
    root: Path, request_id: str, timeout: float = 5.0
) -> dict | None:
    """Async-friendly analogue of state.read_result for use inside a task group.

    state.read_result() blocks the whole thread via time.sleep(), which would
    starve the cooperatively-scheduled server tasks running on the same event
    loop. This polls with anyio.sleep() instead so inbox_loop/watcher tasks
    keep making progress while we wait.
    """
    path = state_mod.results_dir(root) / f"{request_id}.json"
    deadline = anyio.current_time() + timeout
    while anyio.current_time() < deadline:
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                await anyio.sleep(0.05)
                continue
            try:
                path.unlink()
            except OSError:
                pass
            return data
        await anyio.sleep(0.05)
    return None


_TEST_MODELS = {
    "strong": [{"name": "strong-test", "command": "codex"}],
    "medium": [{"name": "medium-test", "command": "pi"}],
    "weak": [],
}


def _configure_models(root: Path) -> None:
    """Model tiers start empty after init; tests populate them explicitly."""
    state = state_mod.read_state(root)
    state["models"] = json.loads(json.dumps(_TEST_MODELS))
    state_mod.write_state(root, state)


async def run_server_briefly(root: Path, duration: float = 2.0) -> None:
    """Run the server for a limited time, then cancel it.

    Small helper kept close to the shape suggested by the task brief; not
    used by every test since most tests need to interleave inbox writes and
    result reads while the server is alive rather than just letting it run
    for a fixed duration.
    """
    async with anyio.create_task_group() as tg:
        tg.start_soon(run_server, root)
        await anyio.sleep(duration)
        tg.cancel_scope.cancel()


# ---------------------------------------------------------------------------
# 1. Full "new" agent flow: create, verify state, delete, verify removal.
# ---------------------------------------------------------------------------

def test_full_new_agent_flow(tmp_path):
    state_mod.init_state_dir(tmp_path)
    _configure_models(tmp_path)

    async def scenario() -> None:
        mock_create = AsyncMock(
            return_value={"result": {"terminal": {"handle": "mock_term_001"}}, "ok": True}
        )
        mock_send = AsyncMock(return_value={"ok": True})
        mock_wait = AsyncMock(side_effect=_hang_forever)
        mock_list = AsyncMock(return_value=[])

        with patch("osw.server.terminal_create", mock_create), \
             patch("osw.server.terminal_send", mock_send), \
             patch("osw.server.terminal_wait", mock_wait), \
             patch("osw.server.terminal_list", mock_list):
            async with anyio.create_task_group() as tg:
                tg.start_soon(run_server, tmp_path)

                # --- create a new agent ---
                request_id = state_mod.write_request(
                    tmp_path, "new", {"prompt": "do something useful"}
                )
                result = await _wait_for_result(tmp_path, request_id)

                assert result is not None
                assert result["ok"] is True
                assert result["agent_id"] == "agent_001"
                assert result["terminal"] == "mock_term_001"

                state = state_mod.read_state(tmp_path)
                agent = state["agents"]["agent_001"]
                assert agent["state"] == "assigned"
                assert agent["terminal"] == "mock_term_001"
                assert agent["worktree_path"] == str(tmp_path)

                # --- remove the agent ---
                del_request_id = state_mod.write_request(
                    tmp_path, "del", {"agent_id": "agent_001"}
                )
                del_result = await _wait_for_result(tmp_path, del_request_id)

                assert del_result is not None
                assert del_result["ok"] is True
                assert del_result["agent_id"] == "agent_001"

                state = state_mod.read_state(tmp_path)
                assert "agent_001" not in state["agents"]

                tg.cancel_scope.cancel()

    anyio.run(scenario)


# ---------------------------------------------------------------------------
# 2. "use" rejects a terminal whose worktree doesn't match the project root.
# ---------------------------------------------------------------------------

def test_use_rejects_wrong_directory(tmp_path):
    state_mod.init_state_dir(tmp_path)
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()

    async def scenario() -> None:
        mock_info = AsyncMock(
            return_value={"terminal": "term-x", "worktreePath": str(other_dir)}
        )
        mock_wait = AsyncMock(side_effect=_hang_forever)

        with patch("osw.server.terminal_info", mock_info), \
             patch("osw.server.terminal_wait", mock_wait):
            async with anyio.create_task_group() as tg:
                tg.start_soon(run_server, tmp_path)

                request_id = state_mod.write_request(
                    tmp_path, "use", {"terminal": "term-x", "prompt": ""}
                )
                result = await _wait_for_result(tmp_path, request_id)

                assert result is not None
                assert result["ok"] is False
                assert "does not match" in result["error"]

                state = state_mod.read_state(tmp_path)
                assert state["agents"] == {}

                tg.cancel_scope.cancel()

    anyio.run(scenario)


# ---------------------------------------------------------------------------
# 3. "use" accepts a terminal whose worktree matches the project root.
# ---------------------------------------------------------------------------

def test_use_accepts_correct_directory(tmp_path):
    state_mod.init_state_dir(tmp_path)

    async def scenario() -> None:
        mock_info = AsyncMock(
            return_value={"terminal": "term-y", "worktreePath": str(tmp_path)}
        )
        mock_send = AsyncMock(return_value={"ok": True})
        mock_wait = AsyncMock(side_effect=_hang_forever)

        with patch("osw.server.terminal_info", mock_info), \
             patch("osw.server.terminal_send", mock_send), \
             patch("osw.server.terminal_wait", mock_wait):
            async with anyio.create_task_group() as tg:
                tg.start_soon(run_server, tmp_path)

                request_id = state_mod.write_request(
                    tmp_path,
                    "use",
                    {"terminal": "term-y", "prompt": "hello"},
                )
                result = await _wait_for_result(tmp_path, request_id)

                assert result is not None
                assert result["ok"] is True
                assert result["agent_id"] == "agent_001"
                assert result["terminal"] == "term-y"

                state = state_mod.read_state(tmp_path)
                assert "agent_001" in state["agents"]
                assert state["agents"]["agent_001"]["terminal"] == "term-y"
                assert state["agents"]["agent_001"]["worktree_path"] == str(tmp_path)

                tg.cancel_scope.cancel()

    anyio.run(scenario)


# ---------------------------------------------------------------------------
# 4. "all" broadcasts a message to every managed agent's terminal.
# ---------------------------------------------------------------------------

def test_all_broadcasts_to_managed_agents(tmp_path):
    state_mod.init_state_dir(tmp_path)
    _configure_models(tmp_path)

    async def scenario() -> None:
        mock_create = AsyncMock(
            side_effect=[
                {"result": {"terminal": {"handle": "term-a"}}, "ok": True},
                {"result": {"terminal": {"handle": "term-b"}}, "ok": True},
            ]
        )
        mock_send = AsyncMock(return_value={"ok": True})
        mock_wait = AsyncMock(side_effect=_hang_forever)

        with patch("osw.server.terminal_create", mock_create), \
             patch("osw.server.terminal_send", mock_send), \
             patch("osw.server.terminal_wait", mock_wait):
            async with anyio.create_task_group() as tg:
                tg.start_soon(run_server, tmp_path)

                req1 = state_mod.write_request(tmp_path, "new", {"prompt": "task 1"})
                result1 = await _wait_for_result(tmp_path, req1)
                assert result1 is not None and result1["ok"] is True

                req2 = state_mod.write_request(tmp_path, "new", {"prompt": "task 2"})
                result2 = await _wait_for_result(tmp_path, req2)
                assert result2 is not None and result2["ok"] is True

                # Discard the prompt-delivery calls made by handle_new so the
                # assertions below only see calls made by the "all" broadcast.
                mock_send.reset_mock()

                req_all = state_mod.write_request(
                    tmp_path, "all", {"message": "status please"}
                )
                result_all = await _wait_for_result(tmp_path, req_all)

                assert result_all is not None
                assert result_all["ok"] is True
                assert set(result_all["sent"]) == {"agent_001", "agent_002"}

                called_handles = {
                    call.args[0] for call in mock_send.call_args_list
                }
                assert called_handles == {"term-a", "term-b"}
                assert mock_send.call_count == 2
                for call in mock_send.call_args_list:
                    assert call.args[1] == "status please"

                tg.cancel_scope.cancel()

    anyio.run(scenario)


# ---------------------------------------------------------------------------
# 5. `list` CLI command shows agents present in state.
# ---------------------------------------------------------------------------

def test_list_shows_agents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    state = state_mod.read_state(tmp_path)
    state["agents"] = {
        "agent_001": {
            "agent_id": "agent_001",
            "terminal": "term-a",
            "worktree_path": str(tmp_path),
            "state": "assigned",
            "caller_terminal": "caller-t1",
            "last_prompt": "Fix the bug",
        },
    }
    state_mod.write_state(tmp_path, state)

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "agent_001" in result.output
    assert "assigned" in result.output
    assert "term-a" in result.output
    assert "Fix the bug" in result.output


# ---------------------------------------------------------------------------
# 6. `status` CLI command reports serve state.
# ---------------------------------------------------------------------------

def test_status_reports_serve_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "not running" in result.output.lower()

    state = state_mod.read_state(tmp_path)
    state["serve"] = {"pid": os.getpid(), "started_at": "2026-01-01T00:00:00+00:00"}
    state_mod.write_state(tmp_path, state)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "not running" not in result.output.lower()
    assert "serve: running" in result.output.lower()

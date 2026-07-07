from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from osw import orca_cli
from osw.orca_cli import OrcaError


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _mock_resolve_orca():
    with patch("osw.orca_cli._resolve_orca", return_value="orca"):
        yield


def _completed(returncode=0, stdout=b"{}", stderr=b""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.anyio
async def test_run_orca_builds_correct_args():
    mock_run = AsyncMock(return_value=_completed(stdout=b"{}"))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        await orca_cli.run_orca("terminal", "list")

    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "orca"
    assert "terminal" in cmd
    assert "list" in cmd
    assert "--json" in cmd


@pytest.mark.anyio
async def test_run_orca_parses_json():
    payload = {"foo": "bar", "n": 1}
    mock_run = AsyncMock(return_value=_completed(stdout=json.dumps(payload).encode()))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        result = await orca_cli.run_orca("worktree", "current")

    assert result == payload


@pytest.mark.anyio
async def test_run_orca_raises_on_nonzero():
    mock_run = AsyncMock(
        return_value=_completed(returncode=1, stdout=b"", stderr=b"boom")
    )
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        with pytest.raises(OrcaError) as exc_info:
            await orca_cli.run_orca("terminal", "close", "--terminal", "abc")

    assert exc_info.value.message == "boom"
    assert exc_info.value.returncode == 1


@pytest.mark.anyio
async def test_run_orca_appends_json_flag():
    mock_run = AsyncMock(return_value=_completed(stdout=b"{}"))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        await orca_cli.run_orca("worktree", "current")

    cmd = mock_run.call_args.args[0]
    assert cmd.count("--json") == 1


@pytest.mark.anyio
async def test_run_orca_no_duplicate_json_flag():
    mock_run = AsyncMock(return_value=_completed(stdout=b"{}"))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        await orca_cli.run_orca("worktree", "current", "--json")

    cmd = mock_run.call_args.args[0]
    assert cmd.count("--json") == 1


@pytest.mark.anyio
async def test_worktree_current_args():
    payload = {"path": "/tmp/wt"}
    mock_run = AsyncMock(return_value=_completed(stdout=json.dumps(payload).encode()))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        result = await orca_cli.worktree_current()

    cmd = mock_run.call_args.args[0]
    assert cmd == ["orca", "worktree", "current", "--json"]
    assert result == payload


@pytest.mark.anyio
async def test_terminal_list_args():
    payload = [{"handle": "t1"}, {"handle": "t2"}]
    mock_run = AsyncMock(return_value=_completed(stdout=json.dumps(payload).encode()))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        result = await orca_cli.terminal_list()

    cmd = mock_run.call_args.args[0]
    assert cmd == [
        "orca",
        "terminal",
        "list",
        "--worktree",
        "active",
        "--json",
    ]
    assert result == payload


@pytest.mark.anyio
async def test_terminal_list_unwraps_dict():
    payload = {"terminals": [{"handle": "t1"}]}
    mock_run = AsyncMock(return_value=_completed(stdout=json.dumps(payload).encode()))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        result = await orca_cli.terminal_list()

    assert result == [{"handle": "t1"}]


@pytest.mark.anyio
async def test_terminal_list_unwraps_nested_result():
    payload = {"id": "x", "ok": True, "result": {"terminals": [{"handle": "t1"}]}}
    mock_run = AsyncMock(return_value=_completed(stdout=json.dumps(payload).encode()))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        result = await orca_cli.terminal_list()

    assert result == [{"handle": "t1"}]


@pytest.mark.anyio
async def test_terminal_create_args():
    payload = {"handle": "t1"}
    mock_run = AsyncMock(return_value=_completed(stdout=json.dumps(payload).encode()))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        result = await orca_cli.terminal_create("codex")

    cmd = mock_run.call_args.args[0]
    assert cmd == [
        "orca",
        "terminal",
        "create",
        "--worktree",
        "active",
        "--command",
        "codex",
        "--json",
    ]
    assert result == payload


@pytest.mark.anyio
async def test_terminal_send_args():
    mock_run = AsyncMock(return_value=_completed(stdout=b"{}"))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        await orca_cli.terminal_send("t1", "hello world")

    cmd = mock_run.call_args.args[0]
    assert cmd == [
        "orca",
        "terminal",
        "send",
        "--terminal",
        "t1",
        "--text",
        "hello world",
        "--enter",
        "--json",
    ]


@pytest.mark.anyio
async def test_terminal_wait_args():
    mock_run = AsyncMock(return_value=_completed(stdout=b"{}"))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        await orca_cli.terminal_wait("t1")

    cmd = mock_run.call_args.args[0]
    assert cmd == [
        "orca",
        "terminal",
        "wait",
        "--terminal",
        "t1",
        "--for",
        "tui-idle",
        "--timeout-ms",
        "300000",
        "--json",
    ]


@pytest.mark.anyio
async def test_terminal_wait_custom_event_and_timeout():
    mock_run = AsyncMock(return_value=_completed(stdout=b"{}"))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        await orca_cli.terminal_wait("t1", event="exit", timeout_ms=5000)

    cmd = mock_run.call_args.args[0]
    assert cmd == [
        "orca",
        "terminal",
        "wait",
        "--terminal",
        "t1",
        "--for",
        "exit",
        "--timeout-ms",
        "5000",
        "--json",
    ]


@pytest.mark.anyio
async def test_terminal_close_args():
    mock_run = AsyncMock(return_value=_completed(stdout=b"{}"))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        await orca_cli.terminal_close("t1")

    cmd = mock_run.call_args.args[0]
    assert cmd == ["orca", "terminal", "close", "--terminal", "t1", "--json"]


@pytest.mark.anyio
async def test_terminal_read_args():
    payload = {"result": {"terminal": {"tail": ["line1"], "status": "running"}}}
    mock_run = AsyncMock(return_value=_completed(stdout=json.dumps(payload).encode()))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        result = await orca_cli.terminal_read("t1", limit=50)

    cmd = mock_run.call_args.args[0]
    assert cmd == [
        "orca",
        "terminal",
        "read",
        "--terminal",
        "t1",
        "--limit",
        "50",
        "--json",
    ]
    assert result == payload


@pytest.mark.anyio
async def test_terminal_read_with_cursor():
    mock_run = AsyncMock(return_value=_completed(stdout=b"{}"))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        await orca_cli.terminal_read("t1", limit=100, cursor="42")

    cmd = mock_run.call_args.args[0]
    assert cmd == [
        "orca",
        "terminal",
        "read",
        "--terminal",
        "t1",
        "--limit",
        "100",
        "--cursor",
        "42",
        "--json",
    ]


@pytest.mark.anyio
async def test_terminal_show_no_handle():
    payload = {"result": {"terminal": {"handle": "t-current"}}}
    mock_run = AsyncMock(return_value=_completed(stdout=json.dumps(payload).encode()))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        result = await orca_cli.terminal_show()

    cmd = mock_run.call_args.args[0]
    assert cmd == ["orca", "terminal", "show", "--json"]
    assert result == payload


@pytest.mark.anyio
async def test_terminal_show_with_handle():
    payload = {"result": {"terminal": {"handle": "t1"}}}
    mock_run = AsyncMock(return_value=_completed(stdout=json.dumps(payload).encode()))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        result = await orca_cli.terminal_show("t1")

    cmd = mock_run.call_args.args[0]
    assert cmd == ["orca", "terminal", "show", "--terminal", "t1", "--json"]
    assert result == payload


@pytest.mark.anyio
async def test_terminal_info_args():
    payload = {"handle": "t1", "worktreePath": "/repo/worktree"}
    mock_run = AsyncMock(return_value=_completed(stdout=json.dumps(payload).encode()))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        result = await orca_cli.terminal_info("t1")

    cmd = mock_run.call_args.args[0]
    assert cmd == ["orca", "terminal", "info", "--terminal", "t1", "--json"]
    assert result == payload

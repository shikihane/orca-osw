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


async def _run(coro_factory, stdout=b"{}"):
    """Run a wrapper with a mocked orca process; return (result, cmd)."""
    mock_run = AsyncMock(return_value=_completed(stdout=stdout))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        result = await coro_factory()
    return result, mock_run.call_args.args[0]


# ---------------------------------------------------------------------------
# Command construction: every wrapper must build the exact orca CLI call
# ---------------------------------------------------------------------------

WRAPPER_CASES = [
    (lambda: orca_cli.worktree_current(),
     ["worktree", "current"]),
    (lambda: orca_cli.terminal_list(),
     ["terminal", "list", "--worktree", "active"]),
    (lambda: orca_cli.terminal_create("codex"),
     ["terminal", "create", "--worktree", "active", "--command", "codex"]),
    (lambda: orca_cli.terminal_wait("t1"),
     ["terminal", "wait", "--terminal", "t1", "--for", "tui-idle", "--timeout-ms", "300000"]),
    (lambda: orca_cli.terminal_wait("t1", event="exit", timeout_ms=5000),
     ["terminal", "wait", "--terminal", "t1", "--for", "exit", "--timeout-ms", "5000"]),
    (lambda: orca_cli.terminal_close("t1"),
     ["terminal", "close", "--terminal", "t1"]),
    (lambda: orca_cli.terminal_read("t1", limit=50),
     ["terminal", "read", "--terminal", "t1", "--limit", "50"]),
    (lambda: orca_cli.terminal_read("t1", limit=100, cursor="42"),
     ["terminal", "read", "--terminal", "t1", "--limit", "100", "--cursor", "42"]),
    (lambda: orca_cli.terminal_show(),
     ["terminal", "show"]),
    (lambda: orca_cli.terminal_show("t1"),
     ["terminal", "show", "--terminal", "t1"]),
    (lambda: orca_cli.worktree_ps(),
     ["worktree", "ps"]),
]


@pytest.mark.anyio
@pytest.mark.parametrize("factory,expected", WRAPPER_CASES,
                         ids=[" ".join(c[1][:2]) + f"#{i}" for i, c in enumerate(WRAPPER_CASES)])
async def test_wrapper_builds_expected_command(factory, expected):
    _, cmd = await _run(factory)
    assert cmd == ["orca", *expected, "--json"]


@pytest.mark.anyio
async def test_terminal_send_writes_text_and_submit_in_one_call():
    mock_run = AsyncMock(return_value=_completed(stdout=b"{}"))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        await orca_cli.terminal_send("t1", "hello world")

    cmds = [call.args[0] for call in mock_run.call_args_list]
    assert cmds == [
        [
            "orca", "terminal", "send", "--terminal", "t1",
            "--text", "hello world", "--enter", "--json",
        ],
    ]


# ---------------------------------------------------------------------------
# run_orca basics
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_run_orca_parses_json_and_dedups_json_flag():
    payload = {"foo": "bar"}
    result, cmd = await _run(
        lambda: orca_cli.run_orca("worktree", "current", "--json"),
        stdout=json.dumps(payload).encode(),
    )
    assert result == payload
    assert cmd.count("--json") == 1


@pytest.mark.anyio
async def test_run_orca_passes_hidden_process_kwargs():
    startupinfo = object()
    mock_run = AsyncMock(return_value=_completed(stdout=b"{}"))
    with patch("osw.orca_cli.hidden_subprocess_kwargs",
               return_value={"startupinfo": startupinfo, "creationflags": 123}), \
         patch("osw.orca_cli.anyio.run_process", mock_run):
        await orca_cli.run_orca("worktree", "ps")

    assert mock_run.call_args.kwargs["startupinfo"] is startupinfo
    assert mock_run.call_args.kwargs["creationflags"] == 123


@pytest.mark.anyio
async def test_run_orca_raises_with_stderr_or_stdout():
    mock_run = AsyncMock(return_value=_completed(returncode=1, stderr=b"boom"))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        with pytest.raises(OrcaError) as exc_info:
            await orca_cli.run_orca("terminal", "close")
    assert exc_info.value.message == "boom"

    # stderr empty -> stdout used as the error message
    mock_run = AsyncMock(return_value=_completed(returncode=1, stdout=b"oops", stderr=b""))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        with pytest.raises(OrcaError) as exc_info:
            await orca_cli.run_orca("terminal", "close")
    assert exc_info.value.message == "oops"


# ---------------------------------------------------------------------------
# Response unwrapping: orca nests everything under {result: {...}}
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_terminal_list_unwraps_nested_result():
    payload = {"id": "x", "ok": True, "result": {"terminals": [{"handle": "t1"}]}}
    result, _ = await _run(lambda: orca_cli.terminal_list(),
                           stdout=json.dumps(payload).encode())
    assert result == [{"handle": "t1"}]


@pytest.mark.anyio
async def test_worktree_ps_unwraps_worktrees():
    payload = {
        "result": {
            "worktrees": [
                {
                    "id": "wt1",
                    "agents": [
                        {"paneKey": "tab:leaf", "state": "done"},
                    ],
                }
            ]
        }
    }
    result, _ = await _run(lambda: orca_cli.worktree_ps(),
                           stdout=json.dumps(payload).encode())
    assert result == payload["result"]["worktrees"]


@pytest.mark.anyio
async def test_detect_current_terminal():
    payload = {"result": {"terminals": [
        {"handle": "t1", "preview": "other"},
        {"handle": "t2", "preview": "log osw_trace_abc here"},
    ]}}
    result, _ = await _run(lambda: orca_cli.detect_current_terminal("osw_trace_abc"),
                           stdout=json.dumps(payload).encode())
    assert result == "t2"

    result, _ = await _run(lambda: orca_cli.detect_current_terminal("missing"),
                           stdout=json.dumps(payload).encode())
    assert result is None

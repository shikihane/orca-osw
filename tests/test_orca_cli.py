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
def _mock_orca_lookup():
    with patch("osw.orca_cli.shutil.which", return_value="orca"):
        yield


def _completed(returncode=0, stdout=b"{}", stderr=b""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


async def _run(
    coro_factory,
    stdout=b'{"result":{"terminal":{},"terminals":[],"worktrees":[]}}',
):
    """Run a wrapper with a mocked orca process; return (result, cmd)."""
    mock_run = AsyncMock(return_value=_completed(stdout=stdout))
    with patch("osw.orca_cli.anyio.run_process", mock_run):
        result = await coro_factory()
    return result, mock_run.call_args.args[0]


def test_resolve_orca_command_honors_exported_override(monkeypatch):
    monkeypatch.setenv("ORCA_CLI_COMMAND", "orca-ide --profile dev")
    monkeypatch.delenv("ORCA_DEV_REPO_ROOT", raising=False)

    with patch("osw.orca_cli.shutil.which", return_value="resolved-orca"):
        command = orca_cli.resolve_orca_command()

    assert command == ["resolved-orca", "--profile", "dev"]


def test_resolve_orca_command_uses_dev_cli_for_dev_checkout(monkeypatch):
    monkeypatch.delenv("ORCA_CLI_COMMAND", raising=False)
    monkeypatch.setenv("ORCA_DEV_REPO_ROOT", "D:/src/orca")

    with patch("osw.orca_cli.shutil.which", return_value="resolved-orca-dev") as lookup:
        command = orca_cli.resolve_orca_command()

    assert command == ["resolved-orca-dev"]
    lookup.assert_called_once_with("orca-dev")


def test_resolve_orca_command_avoids_screen_reader_outside_orca_on_linux(monkeypatch):
    monkeypatch.delenv("ORCA_CLI_COMMAND", raising=False)
    monkeypatch.delenv("ORCA_DEV_REPO_ROOT", raising=False)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)

    with patch("osw.orca_cli.sys.platform", "linux"), \
         patch("osw.orca_cli.shutil.which", return_value="resolved-orca-ide") as lookup:
        command = orca_cli.resolve_orca_command()

    assert command == ["resolved-orca-ide"]
    lookup.assert_called_once_with("orca-ide")


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


@pytest.mark.anyio
async def test_run_orca_preserves_structured_error_code():
    payload = {
        "ok": False,
        "error": {
            "code": "terminal_handle_stale",
            "message": "Terminal handle belongs to an older runtime",
        },
    }
    mock_run = AsyncMock(return_value=_completed(
        returncode=1,
        stdout=json.dumps(payload).encode(),
    ))

    with patch("osw.orca_cli.anyio.run_process", mock_run):
        with pytest.raises(OrcaError) as exc_info:
            await orca_cli.terminal_show("old-handle")

    assert exc_info.value.code == "terminal_handle_stale"
    assert exc_info.value.message == "Terminal handle belongs to an older runtime"


@pytest.mark.anyio
async def test_run_orca_rejects_error_envelope_even_with_zero_exit_code():
    payload = {
        "ok": False,
        "error": {
            "code": "runtime_unavailable",
            "message": "Orca runtime is not reachable",
        },
    }
    mock_run = AsyncMock(return_value=_completed(
        stdout=json.dumps(payload).encode(),
    ))

    with patch("osw.orca_cli.anyio.run_process", mock_run):
        with pytest.raises(OrcaError) as exc_info:
            await orca_cli.orca_status()

    assert exc_info.value.code == "runtime_unavailable"
    assert exc_info.value.message == "Orca runtime is not reachable"


@pytest.mark.anyio
async def test_compatibility_report_checks_every_read_contract(monkeypatch):
    monkeypatch.delenv("ORCA_CLI_COMMAND", raising=False)
    monkeypatch.delenv("ORCA_DEV_REPO_ROOT", raising=False)
    responses = [
        {
            "result": {
                "runtime": {
                    "state": "ready", "reachable": True,
                    "appVersion": "1.4.166",
                },
                "graph": {"state": "ready"},
            }
        },
        {"result": {"worktree": {"path": "D:/repo"}}},
        {"result": {"terminals": [{"handle": "term-a"}]}},
        {"result": {"terminal": {
            "handle": "term-a", "tabId": "tab-a", "leafId": "leaf-a",
            "lastOutputAt": 1,
        }}},
        {"result": {"worktrees": [{"agents": [{
            "paneKey": "tab-a:leaf-a", "state": "done",
            "stateStartedAt": 1, "updatedAt": 2,
        }]}]}},
    ]
    process = AsyncMock(side_effect=[
        _completed(stdout=json.dumps(payload).encode())
        for payload in responses
    ])

    with patch("osw.orca_cli.anyio.run_process", process):
        report = await orca_cli.compatibility_report()

    assert report["ok"] is True
    assert report["contract"] == {
        "terminal_list": "ok",
        "terminal_show": "ok",
        "worktree_ps": "ok",
        "agent_status": "ok",
    }
    assert len(process.await_args_list) == 5


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
async def test_terminal_list_rejects_unknown_response_shape():
    payload = {"id": "x", "ok": True, "result": {"items": []}}

    with pytest.raises(OrcaError) as exc_info:
        await _run(
            lambda: orca_cli.terminal_list(),
            stdout=json.dumps(payload).encode(),
        )

    assert exc_info.value.code == "orca_contract_mismatch"
    assert "result.terminals" in exc_info.value.message


@pytest.mark.anyio
async def test_terminal_list_rejects_non_object_entries():
    payload = {
        "id": "x",
        "ok": True,
        "result": {"terminals": [{"handle": "t1"}, "unexpected"]},
    }

    with pytest.raises(OrcaError) as exc_info:
        await _run(
            lambda: orca_cli.terminal_list(),
            stdout=json.dumps(payload).encode(),
        )

    assert exc_info.value.code == "orca_contract_mismatch"
    assert "terminal entries" in exc_info.value.message


@pytest.mark.anyio
async def test_terminal_show_rejects_unknown_response_shape():
    payload = {"id": "x", "ok": True, "result": {"items": []}}

    with pytest.raises(OrcaError) as exc_info:
        await _run(
            lambda: orca_cli.terminal_show("t1"),
            stdout=json.dumps(payload).encode(),
        )

    assert exc_info.value.code == "orca_contract_mismatch"
    assert "result.terminal" in exc_info.value.message


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
async def test_worktree_ps_rejects_unknown_response_shape():
    payload = {"id": "x", "ok": True, "result": {"items": []}}

    with pytest.raises(OrcaError) as exc_info:
        await _run(
            lambda: orca_cli.worktree_ps(),
            stdout=json.dumps(payload).encode(),
        )

    assert exc_info.value.code == "orca_contract_mismatch"
    assert "result.worktrees" in exc_info.value.message


@pytest.mark.anyio
async def test_worktree_ps_rejects_non_object_entries():
    payload = {
        "id": "x",
        "ok": True,
        "result": {"worktrees": [{"agents": []}, "unexpected"]},
    }

    with pytest.raises(OrcaError) as exc_info:
        await _run(
            lambda: orca_cli.worktree_ps(),
            stdout=json.dumps(payload).encode(),
        )

    assert exc_info.value.code == "orca_contract_mismatch"
    assert "worktree entries" in exc_info.value.message


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

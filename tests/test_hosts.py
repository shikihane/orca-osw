from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from osw import hosts
from osw.hosts import Host, HostError


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _completed(returncode=0, stdout=b"", stderr=b""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _mock_orca_lookup():
    with patch("osw.orca_cli.shutil.which", return_value="orca"):
        yield


def _remote(**overrides) -> Host:
    data = {
        "name": "fnnas",
        "ssh": "fnnas",
        "environment": "fnnas",
        "worktree": "path:/vol1/1000/orca/workspaces",
        "remote_root": "/vol1/1000/orca/workspaces",
        "home": "/vol1/1000/shiki",
    }
    data.update(overrides)
    return Host(**data)


def _batch_destinations(batch_file: str) -> str:
    return Path(batch_file).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Agent references
# ---------------------------------------------------------------------------

def test_split_ref_prefixed_bare_and_local(tmp_path):
    assert hosts.split_ref("fnnas/agent_001") == ("fnnas", "agent_001")
    assert hosts.split_ref("agent_001") == ("local", "agent_001")
    assert hosts.split_ref("local/agent_001") == ("local", "agent_001")


def test_resolve_agent_ref_remote_and_refused_unknown(tmp_path):
    hosts.save_host(tmp_path, _remote())
    host, agent_id = hosts.resolve_agent_ref(tmp_path, "fnnas/agent_001")
    assert host.name == "fnnas"
    assert agent_id == "agent_001"

    local, bare = hosts.resolve_agent_ref(tmp_path, "agent_001")
    assert local.is_local
    assert bare == "agent_001"

    with pytest.raises(HostError) as exc:
        hosts.resolve_agent_ref(tmp_path, "nope/agent_001")
    assert exc.value.code == hosts.UNKNOWN_HOST
    assert "fnnas" in exc.value.message


# ---------------------------------------------------------------------------
# Workspace rules: one place derives the selector and the comparison
# ---------------------------------------------------------------------------

def test_worktree_selector_is_local_path_or_the_registered_selector():
    assert _remote().worktree_selector("D:/repo") == \
        "path:/vol1/1000/orca/workspaces"
    assert hosts.local_host().worktree_selector("D:/repo") == "path:D:/repo"


def test_workspace_path_prefers_the_discovered_root_then_a_path_selector():
    assert _remote().workspace_path() == "/vol1/1000/orca/workspaces"
    assert _remote(remote_root="", worktree="path:/srv/repo").workspace_path() \
        == "/srv/repo"
    assert _remote(remote_root="", worktree="name:main").workspace_path() == ""


def test_same_workspace_compares_posix_strings_remotely_and_resolved_paths_locally(
    tmp_path,
):
    remote = _remote()
    assert remote.same_workspace("/vol1/1000/orca/workspaces/") is True
    assert remote.same_workspace("/vol1/1000/orca/other") is False
    assert remote.same_workspace("") is False
    assert _remote(remote_root="", worktree="path:/srv/repo").same_workspace(
        "/srv/repo/",
    ) is True

    local = Host(worktree=f"path:{tmp_path}")
    assert local.same_workspace(str(tmp_path)) is True
    assert local.same_workspace(str(tmp_path / "nested")) is False
    assert hosts.local_host().same_workspace(str(tmp_path)) is False


@pytest.mark.anyio
async def test_unusable_remote_paths_carry_the_callers_code():
    bad = "/srv/bad\nname"
    with pytest.raises(HostError) as exc:
        await _remote().read_file(bad)
    assert exc.value.code == hosts.REMOTE_READ_FAILED

    with pytest.raises(HostError) as exc:
        await _remote().write_file(bad, b"x")
    assert exc.value.code == hosts.REMOTE_WRITE_FAILED

    with pytest.raises(HostError) as exc:
        await _remote().exists(bad)
    assert exc.value.code == hosts.REMOTE_COMMAND_FAILED

    with pytest.raises(HostError) as exc:
        await _remote().ensure_dir(bad)
    assert exc.value.code == hosts.REMOTE_COMMAND_FAILED


# ---------------------------------------------------------------------------
# Remote argv construction
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_remote_exec_uses_batch_mode_connect_timeout_and_quotes_arguments():
    run = AsyncMock(return_value=_completed())
    with patch("osw.hosts.anyio.run_process", run):
        await _remote().exec(["terminal", "send", "--text", "two words"])

    argv = run.call_args.args[0]
    assert argv[:5] == [
        "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={hosts.CONNECT_TIMEOUT_SECS}",
    ]
    assert argv[5] == "fnnas"
    assert argv[6] == "terminal send --text 'two words'"


@pytest.mark.anyio
async def test_remote_exec_cd_prefixes_a_quoted_working_directory():
    run = AsyncMock(return_value=_completed())
    with patch("osw.hosts.anyio.run_process", run):
        await _remote().exec(["git", "status"], cwd="/tmp/two words")

    assert run.call_args.args[0][6] == "cd '/tmp/two words' && git status"


@pytest.mark.anyio
async def test_permission_denied_maps_to_its_code():
    run = AsyncMock(return_value=_completed(
        returncode=255, stderr=b"shiki@fnnas: Permission denied (publickey).",
    ))
    with patch("osw.hosts.anyio.run_process", run):
        with pytest.raises(HostError) as exc:
            await _remote().exec(["true"])
    assert exc.value.code == hosts.HOST_PERMISSION_DENIED


@pytest.mark.anyio
async def test_unreachable_maps_to_its_code():
    run = AsyncMock(return_value=_completed(
        returncode=255, stderr=b"ssh: Could not resolve hostname fnnas",
    ))
    with patch("osw.hosts.anyio.run_process", run):
        with pytest.raises(HostError) as exc:
            await _remote().exec(["true"])
    assert exc.value.code == hosts.HOST_UNREACHABLE


@pytest.mark.anyio
async def test_exists_reads_the_exit_status():
    true_run = AsyncMock(return_value=_completed(returncode=0))
    with patch("osw.hosts.anyio.run_process", true_run):
        assert await _remote().exists("/vol1/a.md") is True

    false_run = AsyncMock(return_value=_completed(returncode=1))
    with patch("osw.hosts.anyio.run_process", false_run):
        assert await _remote().exists("/vol1/a.md") is False


# ---------------------------------------------------------------------------
# Remote files: sftp get/put+rename
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_remote_write_puts_to_a_temporary_name_then_renames():
    batches: list[str] = []

    async def fake_run(argv, **kwargs):
        batches.append(_batch_destinations(argv[argv.index("-b") + 1]))
        return _completed()

    with patch("osw.hosts.anyio.run_process", fake_run):
        await _remote().write_file("/srv/osw/handoff.md", b"hello")

    batch = batches[0]
    lines = [line for line in batch.splitlines() if line]
    assert lines[0].startswith("put ")
    assert '"/srv/osw/handoff.md.osw-' in lines[0]
    assert lines[0].endswith('.tmp"')
    assert lines[1].startswith("rename ")
    assert lines[1].endswith('"/srv/osw/handoff.md"')


@pytest.mark.anyio
async def test_remote_write_failure_is_a_named_error():
    run = AsyncMock(return_value=_completed(returncode=1, stderr=b"no such dir"))
    with patch("osw.hosts.anyio.run_process", run):
        with pytest.raises(HostError) as exc:
            await _remote().write_file("/srv/missing/handoff.md", b"x")
    assert exc.value.code == hosts.REMOTE_WRITE_FAILED


@pytest.mark.anyio
async def test_remote_read_gets_through_sftp_and_returns_bytes(tmp_path):
    async def fake_run(argv, **kwargs):
        batch = _batch_destinations(argv[argv.index("-b") + 1])
        destination = batch.split('"')[3]
        Path(destination).write_bytes(b"# handoff\n")
        return _completed()

    with patch("osw.hosts.anyio.run_process", fake_run):
        assert await _remote().read_file("/srv/handoff.md") == b"# handoff\n"


@pytest.mark.anyio
async def test_remote_read_failure_is_a_named_error():
    run = AsyncMock(return_value=_completed(returncode=1, stderr=b"not found"))
    with patch("osw.hosts.anyio.run_process", run):
        with pytest.raises(HostError) as exc:
            await _remote().read_file("/srv/handoff.md")
    assert exc.value.code == hosts.REMOTE_READ_FAILED


# ---------------------------------------------------------------------------
# Local implementation: plain filesystem, no tooling required
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_local_file_operations_are_plain_filesystem(tmp_path):
    host = hosts.local_host()
    path = tmp_path / "nested" / "file.md"
    await host.ensure_dir(str(path.parent))
    await host.write_file(str(path), b"bytes")
    assert await host.exists(str(path)) is True
    assert await host.read_file(str(path)) == b"bytes"
    assert await host.exists(str(tmp_path / "absent")) is False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_roundtrip_and_removal(tmp_path):
    first = _remote()
    second = _remote(name="n100", ssh="n100", environment="n100")
    hosts.save_host(tmp_path, first)
    hosts.save_host(tmp_path, second)

    loaded = hosts.read_hosts(tmp_path)
    assert set(loaded) == {"fnnas", "n100"}
    assert loaded["fnnas"].worktree == "path:/vol1/1000/orca/workspaces"
    assert loaded["fnnas"].home == "/vol1/1000/shiki"

    removed = hosts.remove_host(tmp_path, "fnnas")
    assert removed is not None
    assert set(hosts.read_hosts(tmp_path)) == {"n100"}


def test_malformed_registry_is_a_stable_error_not_an_empty_fleet(tmp_path):
    path = hosts.registry_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(HostError) as exc:
        hosts.read_hosts(tmp_path)
    assert exc.value.code == hosts.HOST_REGISTRY_INVALID

    path.write_text(json.dumps({"version": 99, "hosts": {}}), encoding="utf-8")
    with pytest.raises(HostError) as exc:
        hosts.read_hosts(tmp_path)
    assert exc.value.code == hosts.HOST_REGISTRY_INVALID


# ---------------------------------------------------------------------------
# Orca command construction
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_remote_orca_call_targets_the_paired_runtime():
    from osw import orca_cli

    run = AsyncMock(return_value=_completed(stdout=b"{}"))
    with patch("osw.orca_cli.anyio.run_process", run):
        await orca_cli.run_orca("terminal", "list", host=_remote())

    argv = run.call_args.args[0]
    assert argv == [
        "orca", "terminal", "list", "--environment", "fnnas", "--json",
    ]


# ---------------------------------------------------------------------------
# Reachability: the probe argv, the two state codes, and loud defects
# ---------------------------------------------------------------------------

def test_reachability_of_a_live_remote_host_runs_the_probe_and_reports_reachable():
    run = AsyncMock(return_value=_completed(returncode=0))
    with patch("osw.hosts.anyio.run_process", run):
        assert hosts.reachability(_remote()) == "reachable"

    argv = run.call_args.args[0]
    assert argv == [
        "ssh", "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={hosts.CONNECT_TIMEOUT_SECS}",
        "fnnas", "true",
    ]


def test_reachability_reports_permission_denied_for_an_unauthorised_key():
    run = AsyncMock(return_value=_completed(
        returncode=255, stderr=b"fnnas: Permission denied (publickey).",
    ))
    with patch("osw.hosts.anyio.run_process", run):
        assert hosts.reachability(_remote()) == "permission_denied"


def test_reachability_reports_unreachable_for_a_transport_failure():
    run = AsyncMock(return_value=_completed(
        returncode=255, stderr=b"ssh: connect to host fnnas port 22: "
                              b"Connection timed out",
    ))
    with patch("osw.hosts.anyio.run_process", run):
        assert hosts.reachability(_remote()) == "unreachable"


def test_reachability_lets_a_defect_propagate_instead_of_calling_the_host_dead():
    run = AsyncMock(side_effect=TypeError("run() got an unexpected keyword"))
    with patch("osw.hosts.anyio.run_process", run):
        with pytest.raises(TypeError):
            hosts.reachability(_remote())

    misuse = AsyncMock(side_effect=HostError(
        hosts.REMOTE_READ_FAILED, "not a reachability state",
    ))
    with patch("osw.hosts.anyio.run_process", misuse):
        with pytest.raises(HostError) as exc:
            hosts.reachability(_remote())
    assert exc.value.code == hosts.REMOTE_READ_FAILED


# ---------------------------------------------------------------------------
# Doctor ladder
# ---------------------------------------------------------------------------

def _doctor_payloads() -> list:
    return [
        _completed(),                                              # ssh true
        _completed(returncode=127, stderr=b"'cmd.exe' is not recognized"),
        _completed(stdout=json.dumps(
            {"result": {"projects": [{"id": "repo:x"}]}}
        ).encode()),
        _completed(stdout=json.dumps({"result": {"worktrees": [
            {"path": "/vol1/1000/orca/workspaces", "displayName": "workspaces"},
        ]}}).encode()),
        _completed(stdout=(
            b"HOME=/vol1/1000/shiki\n"
            b"OS=Linux\n"
            b"/usr/bin/orca\n"
            b"/usr/bin/claude\n"
        )),
    ]


@pytest.mark.anyio
async def test_doctor_runs_every_rung_and_persists_discovered_facts(tmp_path):
    host = _remote(home="", platform="", worktree="path:/vol1/1000/orca/workspaces")
    hosts.save_host(tmp_path, host)
    run = AsyncMock(side_effect=_doctor_payloads())
    with patch("osw.hosts.anyio.run_process", run):
        report = await hosts.doctor(tmp_path, host, "claude")

    assert report["ok"] is True
    assert [rung["rung"] for rung in report["rungs"]] == list(hosts.DOCTOR_RUNGS)

    stored = hosts.read_hosts(tmp_path)["fnnas"]
    assert stored.providers["claude"] == "/usr/bin/claude"
    assert stored.orca_path == "/usr/bin/orca"
    assert stored.remote_root == "/vol1/1000/orca/workspaces"
    assert stored.home == "/vol1/1000/shiki"
    assert stored.platform == "Linux"
    assert stored.platform == "Linux"


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["orca_contract_mismatch", ""])
async def test_doctor_reports_an_orca_error_as_the_failing_rung(tmp_path, code):
    from osw.orca_cli import OrcaError

    host = _remote()
    connection = AsyncMock(side_effect=[
        _completed(), _completed(returncode=127),
    ])
    orca = AsyncMock(side_effect=OrcaError(
        "Orca contract mismatch for `project list`", 1, code=code,
    ))
    with patch("osw.hosts.anyio.run_process", connection), \
            patch("osw.orca_cli.run_orca", orca):
        report = await hosts.doctor(tmp_path, host, "claude", persist=False)

    assert [rung["rung"] for rung in report["rungs"]] == [
        "connection", "workspace",
    ]
    assert report["ok"] is False
    assert report["code"] == (code or hosts.REMOTE_COMMAND_FAILED)
    assert report["rungs"][-1]["code"] == (code or hosts.REMOTE_COMMAND_FAILED)
    assert report["rungs"][-1]["message"] == (
        "Orca contract mismatch for `project list`"
    )
    assert report["rungs"][-1]["next"]


@pytest.mark.anyio
async def test_doctor_stops_at_the_first_failing_rung(tmp_path):
    host = _remote()
    run = AsyncMock(side_effect=[
        _completed(),
        _completed(returncode=127),
        _completed(stdout=b'{"result":{"projects":[]}}'),
    ])
    with patch("osw.hosts.anyio.run_process", run):
        report = await hosts.doctor(tmp_path, host, "claude", persist=False)

    assert report["ok"] is False
    assert [rung["rung"] for rung in report["rungs"]] == [
        "connection", "workspace",
    ]
    assert report["rungs"][-1]["code"] == hosts.REMOTE_NO_PROJECT
    assert report["rungs"][-1]["next"]


@pytest.mark.anyio
async def test_doctor_reports_a_missing_provider_without_probing_further(tmp_path):
    host = _remote()
    run = AsyncMock(side_effect=[
        _completed(),
        _completed(returncode=127),
        _completed(stdout=json.dumps(
            {"result": {"projects": [{"id": "repo:x"}]}}
        ).encode()),
        _completed(stdout=json.dumps({"result": {"worktrees": [
            {"path": "/vol1/1000/orca/workspaces"},
        ]}}).encode()),
        _completed(stdout=b"HOME=/home/shiki\nOS=Linux\n/usr/bin/orca\n"),
    ])
    with patch("osw.hosts.anyio.run_process", run):
        report = await hosts.doctor(tmp_path, host, "claude", persist=False)

    assert report["ok"] is False
    assert report["rungs"][-1]["code"] == hosts.PROVIDER_MISSING
    assert len(report["rungs"]) == 4
    assert host.providers == {}


@pytest.mark.anyio
async def test_doctor_refuses_a_missing_worktree_selects_the_known_ones(tmp_path):
    host = _remote(worktree="")
    run = AsyncMock(side_effect=[
        _completed(),
        _completed(returncode=127),
        _completed(stdout=json.dumps(
            {"result": {"projects": [{"id": "repo:x"}]}}
        ).encode()),
    ])
    with patch("osw.hosts.anyio.run_process", run):
        report = await hosts.doctor(tmp_path, host, "claude", persist=False)

    assert report["rungs"][-1]["code"] == hosts.REMOTE_NO_WORKTREE
    assert len(report["rungs"]) == 3


# ---------------------------------------------------------------------------
# Windows remotes: best effort, cmd.exe is the shell
# ---------------------------------------------------------------------------

def _windows(**overrides) -> Host:
    data = {
        "name": "n100",
        "ssh": "hy@100.66.1.5",
        "environment": "n100",
        "worktree": "path:C:/Users/hy/orca-workspaces",
        "remote_root": "C:/Users/hy/orca-workspaces",
        "home": "C:/Users/hy",
        "platform": "Windows",
    }
    data.update(overrides)
    return Host(**data)


def _script_path(script: str, prefix: str) -> str:
    return script[len(prefix):].split(" (exit 0)")[0].replace('"', "")


class _WindowsShell:
    """Stands in for anyio.run_process on a cmd.exe host.

    Answers by command text (not call order) so a probe can add or drop
    an existence check without rewriting every expectation.
    """

    def __init__(self, where=None, existing=(), home=r"C:\Users\hy",
                 worktrees=None):
        self.where = {name: list(paths) for name, paths in (where or {}).items()}
        self.existing = {path.replace("\\", "/") for path in existing}
        self.home = home
        self.worktrees = list(worktrees or [])
        self.commands: list[str] = []
        self.batches: list[str] = []

    async def __call__(self, argv, **kwargs):
        if argv and argv[0] == "sftp":
            self.batches.append(_batch_destinations(argv[argv.index("-b") + 1]))
            return _completed()
        if argv[:2] == ["orca", "project"]:
            return _completed(stdout=json.dumps(
                {"result": {"projects": [{"id": "repo:x"}]}},
            ).encode())
        if argv[:2] == ["orca", "worktree"]:
            return _completed(stdout=json.dumps(
                {"result": {"worktrees": self.worktrees}},
            ).encode())
        script = argv[-1]
        self.commands.append(script)
        if script == "true":
            return _completed(
                returncode=1,
                stderr=b"'true' is not recognized as an internal or external command",
            )
        if script == "cmd.exe /c exit 0":
            return _completed()
        if script == "cmd.exe /c ver":
            return _completed(
                stdout=b"\r\nMicrosoft Windows [Version 10.0.19045.3803]\r\n",
            )
        if script == "cmd.exe /c echo %USERPROFILE%":
            return _completed(stdout=(self.home + "\r\n").encode())
        if script.startswith("cmd.exe /c where "):
            name = script[len("cmd.exe /c where "):]
            paths = self.where.get(name) or []
            if not paths:
                return _completed(
                    returncode=1,
                    stderr=b"INFO: Could not find files for the given pattern(s).\r\n",
                )
            return _completed(stdout=("\r\n".join(paths) + "\r\n").encode())
        if script.startswith("cmd.exe /c if exist "):
            path = _script_path(script, "cmd.exe /c if exist ")
            found = path.replace("\\", "/") in self.existing
            return _completed(returncode=0 if found else 1)
        return _completed(returncode=1, stderr=b"unexpected command")


@pytest.mark.anyio
async def test_windows_exec_quotes_with_cmd_conventions():
    run = AsyncMock(return_value=_completed())
    with patch("osw.hosts.anyio.run_process", run):
        await _windows().exec(["terminal", "send", "--text", "two words"])

    argv = run.call_args.args[0]
    assert argv[6] == 'terminal send --text "two words"'
    assert "'" not in argv[6]
    assert "sh -c" not in " ".join(argv)


@pytest.mark.anyio
async def test_windows_exec_cd_prefixes_a_cmd_working_directory():
    run = AsyncMock(return_value=_completed())
    with patch("osw.hosts.anyio.run_process", run):
        await _windows().exec(["git", "status"], cwd=r"C:\Users\hy\two words")

    assert run.call_args.args[0][6] == (
        'cd /d "C:\\Users\\hy\\two words" && git status'
    )


@pytest.mark.anyio
async def test_windows_exists_uses_if_exist_and_reads_the_exit_status():
    for returncode, expected in ((0, True), (1, False)):
        run = AsyncMock(return_value=_completed(returncode=returncode))
        with patch("osw.hosts.anyio.run_process", run):
            assert await _windows().exists(r"C:\Users\hy\a.md") is expected

    script = run.call_args.args[0][-1]
    assert script == (
        "cmd.exe /c if exist C:\\Users\\hy\\a.md (exit 0) else (exit 1)"
    )
    assert "test -e" not in script


@pytest.mark.anyio
async def test_windows_exists_transport_failure_is_not_missing():
    run = AsyncMock(return_value=_completed(
        returncode=255, stderr=b"ssh: Could not resolve hostname n100",
    ))
    with patch("osw.hosts.anyio.run_process", run):
        with pytest.raises(HostError) as exc:
            await _windows().exists("C:/Users/hy/a.md")
    assert exc.value.code == hosts.HOST_UNREACHABLE


@pytest.mark.anyio
async def test_windows_ensure_dir_uses_if_not_exist_mkdir():
    run = AsyncMock(return_value=_completed())
    with patch("osw.hosts.anyio.run_process", run):
        await _windows().ensure_dir("C:/Users/hy/.orca/osw")

    assert run.call_args.args[0][-1] == (
        "cmd.exe /c if not exist C:\\Users\\hy\\.orca\\osw "
        "mkdir C:\\Users\\hy\\.orca\\osw"
    )


@pytest.mark.anyio
async def test_windows_sftp_batches_use_forward_slashes_on_both_sides():
    batches: list[str] = []

    async def fake_run(argv, **kwargs):
        batch = _batch_destinations(argv[argv.index("-b") + 1])
        batches.append(batch)
        line = next(entry for entry in batch.splitlines() if entry)
        if line.startswith("get "):
            Path(line.split('"')[3]).write_bytes(b"payload")
        return _completed()

    with patch("osw.hosts.anyio.run_process", fake_run):
        await _windows().read_file(r"C:\Users\hy\handoff.md")
        await _windows().write_file(r"C:\Users\hy\handoff.md", b"hello")

    get_line = batches[0].strip()
    assert "\\" not in get_line
    assert get_line.startswith('get "C:/Users/hy/handoff.md" ')
    assert get_line.split('"')[3].endswith("/payload")

    put_line, rename_line = [
        line for line in batches[1].splitlines() if line
    ]
    assert "\\" not in batches[1]
    assert put_line.startswith('put "')
    assert put_line.split('"')[1].endswith("/payload")
    assert '"C:/Users/hy/handoff.md.osw-' in put_line
    assert put_line.endswith('.tmp"')
    assert rename_line.endswith('"C:/Users/hy/handoff.md"')


def test_windows_join_uses_ntpath_and_workspace_comparison_folds_case():
    host = _windows()
    assert host.join("C:/Users/hy", ".orca", "osw", "handoff.md") == (
        "C:/Users/hy\\.orca\\osw\\handoff.md"
    )
    assert host.same_workspace("c:\\users\\HY\\orca-workspaces\\") is True
    assert host.same_workspace(
        "C:/Users/hy/orca-workspaces/nested",
    ) is False

    assert _remote().join("/vol1", "osw", "handoff.md") == (
        "/vol1/osw/handoff.md"
    )


def test_dirname_follows_the_host_convention(tmp_path):
    windows = r"C:\Users\hy\.orca\osw\handoffs\misc_001_20260101_000000.md"
    assert _windows().dirname(windows) == r"C:\Users\hy\.orca\osw\handoffs"

    posix = "/home/u/.orca/osw/handoffs/misc_001_20260101_000000.md"
    assert _remote(home="/home/u").dirname(posix) == "/home/u/.orca/osw/handoffs"

    parent = tmp_path / "handoffs"
    assert hosts.local_host().dirname(str(parent / "misc_001.md")) == str(parent)


@pytest.mark.anyio
async def test_doctor_detects_windows_and_resolves_its_provider(tmp_path):
    host = _windows(home="", platform="", remote_root="")
    hosts.save_host(tmp_path, host)
    shell = _WindowsShell(
        where={
            "claude": [
                r"C:\Users\hy\AppData\Local\hermes\node\claude",
                r"C:\Users\hy\AppData\Local\hermes\node\claude.cmd",
            ],
        },
        existing=[
            r"C:\Users\hy\AppData\Local\hermes\node\claude",
            r"C:\Users\hy\AppData\Local\hermes\node\claude.cmd",
        ],
        worktrees=[{
            "path": r"C:\Users\hy\orca-workspaces",
            "displayName": "workspaces",
        }],
    )
    with patch("osw.hosts.anyio.run_process", shell):
        report = await hosts.doctor(tmp_path, host, "claude")

    assert report["ok"] is True
    assert [rung["rung"] for rung in report["rungs"]] == list(
        hosts.DOCTOR_RUNGS,
    )
    stored = hosts.read_hosts(tmp_path)["n100"]
    assert stored.platform == "Windows"
    assert stored.home == r"C:\Users\hy"
    assert stored.providers["claude"] == (
        r"C:\Users\hy\AppData\Local\hermes\node\claude.cmd"
    )
    assert stored.orca_path == ""
    assert stored.remote_root == r"C:\Users\hy\orca-workspaces"

    assert "cmd.exe /c where claude" in shell.commands
    assert "cmd.exe /c where orca" in shell.commands
    assert "cmd.exe /c echo %USERPROFILE%" in shell.commands
    assert "cmd.exe /c ver" in shell.commands
    assert not any("sh -c" in command for command in shell.commands)
    assert not any("test -e" in command for command in shell.commands)


@pytest.mark.anyio
async def test_doctor_reports_a_missing_windows_provider_with_the_same_next(
    tmp_path,
):
    host = _windows()
    shell = _WindowsShell(
        where={},
        worktrees=[{
            "path": "C:/Users/hy/orca-workspaces",
            "displayName": "workspaces",
        }],
    )
    with patch("osw.hosts.anyio.run_process", shell):
        report = await hosts.doctor(tmp_path, host, "claude", persist=False)

    assert report["ok"] is False
    assert report["rungs"][-1]["code"] == hosts.PROVIDER_MISSING
    assert report["rungs"][-1]["next"] == (
        "install the provider CLI for the SSH account"
    )
    assert host.providers == {}


@pytest.mark.anyio
async def test_doctor_reports_unreachable_when_neither_shell_answers(tmp_path):
    host = _windows(platform="")
    run = AsyncMock(side_effect=[
        _completed(returncode=1),
        _completed(returncode=2, stderr=b"cmd.exe refused"),
    ])
    with patch("osw.hosts.anyio.run_process", run):
        report = await hosts.doctor(tmp_path, host, "claude", persist=False)

    assert report["ok"] is False
    assert report["code"] == hosts.HOST_UNREACHABLE
    assert [rung["rung"] for rung in report["rungs"]] == ["connection"]


class _MingwShell(_WindowsShell):
    """A Git-for-Windows host: the POSIX probe succeeds and reports the
    emulation layer, while cmd.exe also answers."""

    async def __call__(self, argv, **kwargs):
        if argv[-1].startswith("sh -c "):
            self.commands.append(argv[-1])
            return _completed(stdout=(
                b"HOME=/c/Users/hy\n"
                b"OS=MINGW64_NT-10.0-19045\n"
            ))
        if argv[-1] == "true":
            return _completed()
        return await super().__call__(argv, **kwargs)


@pytest.mark.anyio
async def test_provider_probe_switches_a_git_shell_host_to_windows():
    host = _windows(platform="", home="")
    shell = _MingwShell(
        where={
            "claude": [
                r"C:\Users\hy\AppData\Local\hermes\node\claude",
                r"C:\Users\hy\AppData\Local\hermes\node\claude.cmd",
            ],
        },
        existing=[
            r"C:\Users\hy\AppData\Local\hermes\node\claude",
            r"C:\Users\hy\AppData\Local\hermes\node\claude.cmd",
        ],
    )

    with patch("osw.hosts.anyio.run_process", shell):
        rung = await hosts._probe_provider(host, "claude")

    assert rung["ok"] is True
    assert rung["path"] == (
        r"C:\Users\hy\AppData\Local\hermes\node\claude.cmd"
    )
    assert host.platform == "Windows"
    assert host.home == r"C:\Users\hy"
    assert "cmd.exe /c where claude" in shell.commands


@pytest.mark.anyio
async def test_connection_rung_marks_windows_when_true_succeeds_and_cmd_answers():
    host = _windows(platform="", home="")
    run = AsyncMock(side_effect=[_completed(), _completed()])

    with patch("osw.hosts.anyio.run_process", run):
        rung = await hosts._probe_connection(host)

    assert rung == {"rung": "connection", "ok": True, "platform": "Windows"}
    assert host.platform == "Windows"
    assert run.call_args.args[0][-1] == "cmd.exe /c exit 0"


@pytest.mark.anyio
async def test_connection_rung_keeps_posix_when_cmd_is_absent():
    host = _windows(platform="", home="")
    run = AsyncMock(side_effect=[
        _completed(),
        _completed(returncode=127, stderr=b"'cmd.exe' is not recognized"),
    ])

    with patch("osw.hosts.anyio.run_process", run):
        rung = await hosts._probe_connection(host)

    assert rung == {"rung": "connection", "ok": True}
    assert "platform" not in rung
    assert host.platform == ""


@pytest.mark.anyio
async def test_windows_worktree_match_accepts_forward_slash_case_differences():
    host = _windows(
        platform="Windows",
        worktree="path:C:/Users/hy/ORCA-workspaces",
        remote_root="",
    )
    data = {"result": {"worktrees": [{
        "path": r"C:\Users\hy\orca-workspaces",
        "displayName": "workspaces",
    }]}}

    with patch("osw.orca_cli.run_orca", AsyncMock(return_value=data)):
        rung = await hosts._probe_worktree(host)

    assert rung["ok"] is True
    assert rung["path"] == r"C:\Users\hy\orca-workspaces"
    assert host.remote_root == r"C:\Users\hy\orca-workspaces"


@pytest.mark.anyio
async def test_provider_after_git_shell_detection_uses_where_not_sh():
    host = _windows(platform="", home="")
    detection = AsyncMock(side_effect=[_completed(), _completed()])
    with patch("osw.hosts.anyio.run_process", detection):
        await hosts._probe_connection(host)
    assert host.platform == "Windows"

    shell = _WindowsShell(
        where={
            "claude": [
                r"C:\Users\hy\AppData\Local\hermes\node\claude",
                r"C:\Users\hy\AppData\Local\hermes\node\claude.cmd",
            ],
        },
        existing=[
            r"C:\Users\hy\AppData\Local\hermes\node\claude",
            r"C:\Users\hy\AppData\Local\hermes\node\claude.cmd",
        ],
    )
    with patch("osw.hosts.anyio.run_process", shell):
        rung = await hosts._probe_provider(host, "claude")

    assert rung["path"] == (
        r"C:\Users\hy\AppData\Local\hermes\node\claude.cmd"
    )
    assert "cmd.exe /c where claude" in shell.commands
    assert not any("sh -c" in command for command in shell.commands)
    assert not any("HOME=" in command for command in shell.commands)

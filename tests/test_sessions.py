from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from osw import hosts, sessions
from osw.hosts import Host


FIXTURE = Path(__file__).parent / "fixtures" / "last_status" / "last-status.json"
OMP_PANE = (
    "ac6f5ce8-3804-43d7-ace9-6145309a175b:4e6b7212-276d-47a9-9d00-7511381d034d"
)
KIMI_PANE = (
    "9f1d2c3b-0000-4000-8000-000000000001:5e6f7a8b-0000-4000-8000-000000000002"
)
NO_SESSION_PANE = (
    "11111111-2222-4333-8444-555555555555:66666666-7777-4888-9999-000000000000"
)


def _write_status(path: Path, payload) -> Path:
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    return path


def test_session_for_pane_returns_provider_session_id():
    assert (
        sessions.session_for_pane(OMP_PANE, path=FIXTURE)
        == "01a129dd-0b79-7613-82a5-6054d7e8034e"
    )


def test_session_for_pane_without_transcript_path():
    """kimi and grok report a session id but no transcriptPath."""
    assert (
        sessions.session_for_pane(KIMI_PANE, path=FIXTURE)
        == "3b0c7f52-6a1e-4d8e-9c2a-7f1e2d3c4b5a"
    )


@pytest.mark.parametrize("pane", [NO_SESSION_PANE, "tab-x:leaf-y", ""])
def test_session_for_pane_without_entry_or_session_is_empty(pane):
    assert sessions.session_for_pane(pane, path=FIXTURE) == ""


def test_session_for_pane_missing_file_is_empty(tmp_path):
    assert sessions.session_for_pane(OMP_PANE, path=tmp_path / "nope.json") == ""


@pytest.mark.parametrize("version", [1, 3, "2", None])
def test_session_for_pane_rejects_unknown_version(tmp_path, version):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if version is None:
        data.pop("version")
    else:
        data["version"] = version
    path = _write_status(tmp_path / "last-status.json", data)

    assert sessions.session_for_pane(OMP_PANE, path=path) == ""


@pytest.mark.parametrize("payload", [
    "{not json",
    "",
    "[]",
    {"version": 2, "entries": []},
    {"version": 2, "entries": {OMP_PANE: "oops"}},
    {"version": 2, "entries": {OMP_PANE: {"providerSession": "oops"}}},
    {"version": 2, "entries": {OMP_PANE: {"providerSession": {"id": 42}}}},
])
def test_session_for_pane_malformed_content_is_empty(tmp_path, payload):
    path = _write_status(tmp_path / "last-status.json", payload)

    assert sessions.session_for_pane(OMP_PANE, path=path) == ""


def test_default_path_is_under_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))

    expected = tmp_path / "orca" / "agent-hooks" / "last-status.json"
    assert sessions.last_status_path() == expected

    expected.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE, expected)
    assert (
        sessions.session_for_pane(OMP_PANE)
        == "01a129dd-0b79-7613-82a5-6054d7e8034e"
    )


# ---------------------------------------------------------------------------
# Host-aware cache: the same file on whichever machine runs the agent
# ---------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    return "asyncio"


def _completed(returncode=0, stdout=b"", stderr=b""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _remote() -> Host:
    return Host(
        name="fnnas",
        ssh="fnnas",
        environment="fnnas",
        home="/vol1/1000/shiki",
    )


def _status(pane_key: str, session_id: str) -> bytes:
    return json.dumps({
        "version": sessions.LAST_STATUS_VERSION,
        "entries": {pane_key: {"providerSession": {"id": session_id}}},
    }).encode("utf-8")


class _RemoteFiles:
    """Stands in for anyio.run_process: answers `test -e` and sftp get
    against a fixed virtual tree, recording which paths were probed."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.probes: list[str] = []

    async def __call__(self, argv, **kwargs):
        if argv[0] == "sftp":
            batch = Path(argv[argv.index("-b") + 1]).read_text(encoding="utf-8")
            line = next(line for line in batch.splitlines() if line)
            _, rest = line.split(" ", 1)
            source, destination = (
                part.strip('"') for part in rest.rsplit(" ", 1)
            )
            payload = self.files.get(source)
            if payload is None:
                return _completed(returncode=1, stderr=b"no such file")
            Path(destination).write_bytes(payload)
            return _completed()
        script = argv[-1]
        if script.startswith("test -e "):
            self.probes.append(script[len("test -e "):])
            found = script[len("test -e "):] in self.files
            return _completed(returncode=0 if found else 1)
        return _completed()


def _candidates(host: Host) -> list[str]:
    return [host.join(host.home, rel) for rel in sessions.SESSION_CACHE_CANDIDATES]


def test_parse_last_status_is_the_shared_reader():
    text = FIXTURE.read_text(encoding="utf-8")
    assert sessions.parse_last_status(text, OMP_PANE) == (
        sessions.session_for_pane(OMP_PANE, path=FIXTURE)
    )
    assert sessions.parse_last_status('{"version": 1}', OMP_PANE) == ""


@pytest.mark.anyio
async def test_local_session_cache_is_the_status_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = await sessions.session_cache_path(hosts.local_host())
    assert path == str(sessions.last_status_path())


@pytest.mark.anyio
async def test_remote_cache_probe_caches_and_reuses_the_first_hit(tmp_path):
    host = _remote()
    first, second, _ = _candidates(host)
    transport = _RemoteFiles({second: _status(OMP_PANE, "sess-remote")})

    with patch("osw.hosts.anyio.run_process", transport), patch(
        "osw.hosts.save_host",
    ) as save:
        assert await sessions.session_cache_path(host, tmp_path) == second
        assert transport.probes == [first, second]
        assert save.call_args.args == (tmp_path, host)

        probed = list(transport.probes)
        assert await sessions.session_cache_path(host, tmp_path) == second
        assert transport.probes == probed

    assert host.session_cache == second


@pytest.mark.anyio
async def test_remote_without_a_cache_names_every_path_it_looked_for():
    host = _remote()
    transport = _RemoteFiles()

    with patch("osw.hosts.anyio.run_process", transport):
        with pytest.raises(hosts.HostError) as exc:
            await sessions.session_cache_path(host, None)

    assert exc.value.code == hosts.REMOTE_SESSION_UNKNOWN
    assert "fnnas" in exc.value.message
    for path in _candidates(host):
        assert path in exc.value.message
    assert host.session_cache == ""


@pytest.mark.anyio
async def test_session_on_host_returns_the_recorded_id():
    host = _remote()
    _, cache, _ = _candidates(host)
    transport = _RemoteFiles({cache: _status(OMP_PANE, "sess-remote")})

    with patch("osw.hosts.anyio.run_process", transport):
        assert await sessions.session_on_host(host, OMP_PANE) == "sess-remote"
        assert await sessions.session_on_host(host, NO_SESSION_PANE) == ""

    # the second call reused the cached path instead of probing again
    assert transport.probes == _candidates(host)[:2]


@pytest.mark.anyio
async def test_session_on_host_read_failure_names_the_cache(tmp_path):
    host = _remote()
    cache = _candidates(host)[0]
    host.session_cache = cache

    with patch("osw.hosts.anyio.run_process", _RemoteFiles()):
        with pytest.raises(hosts.HostError) as exc:
            await sessions.session_on_host(host, OMP_PANE, tmp_path)

    assert exc.value.code == hosts.REMOTE_SESSION_UNKNOWN
    assert cache in exc.value.message


@pytest.mark.anyio
async def test_local_session_on_host_reads_the_local_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    target = tmp_path / "orca" / "agent-hooks" / "last-status.json"
    target.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE, target)

    local = hosts.local_host()
    assert await sessions.session_on_host(local, OMP_PANE) == (
        sessions.session_for_pane(OMP_PANE)
    )
    assert await sessions.session_on_host(local, NO_SESSION_PANE) == ""


@pytest.mark.anyio
async def test_session_for_agent_delegates_to_the_local_cache(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    target = tmp_path / "orca" / "agent-hooks" / "last-status.json"
    target.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE, target)

    assert await sessions.session_for_agent(hosts.local_host(), OMP_PANE) == (
        sessions.session_for_pane(OMP_PANE)
    )
    assert await sessions.session_for_agent(
        hosts.local_host(), NO_SESSION_PANE,
    ) == ""


@pytest.mark.anyio
async def test_session_for_agent_reads_the_remote_cache():
    host = _remote()
    _, cache, _ = _candidates(host)
    transport = _RemoteFiles({cache: _status(OMP_PANE, "sess-remote")})

    with patch("osw.hosts.anyio.run_process", transport):
        assert await sessions.session_for_agent(host, OMP_PANE) == "sess-remote"
        assert await sessions.session_for_agent(host, NO_SESSION_PANE) == ""


@pytest.mark.anyio
async def test_session_for_agent_propagates_a_missing_remote_cache(
    tmp_path,
):
    host = _remote()

    with patch("osw.hosts.anyio.run_process", _RemoteFiles()):
        with pytest.raises(hosts.HostError) as exc:
            await sessions.session_for_agent(host, OMP_PANE, tmp_path)

    assert exc.value.code == hosts.REMOTE_SESSION_UNKNOWN
    assert "fnnas" in exc.value.message
    for path in _candidates(host):
        assert path in exc.value.message


# ---------------------------------------------------------------------------
# Windows remotes: the roaming profile holds the cache
# ---------------------------------------------------------------------------

def _windows_host() -> Host:
    return Host(
        name="n100",
        ssh="hy@100.66.1.5",
        environment="n100",
        home=r"C:\Users\hy",
        platform="Windows",
    )


class _WindowsFiles:
    """The cmd.exe counterpart of _RemoteFiles: answers `if exist` and
    sftp get for a Windows host."""

    def __init__(self, files=None):
        self.files = {
            path.replace("\\", "/"): payload
            for path, payload in (files or {}).items()
        }
        self.probes: list[str] = []

    async def __call__(self, argv, **kwargs):
        if argv[0] == "sftp":
            batch = Path(argv[argv.index("-b") + 1]).read_text(encoding="utf-8")
            line = next(line for line in batch.splitlines() if line)
            _, rest = line.split(" ", 1)
            source, destination = (
                part.strip('"') for part in rest.rsplit(" ", 1)
            )
            payload = self.files.get(source.replace("\\", "/"))
            if payload is None:
                return _completed(returncode=1, stderr=b"no such file")
            Path(destination).write_bytes(payload)
            return _completed()
        script = argv[-1]
        if script.startswith("cmd.exe /c if exist "):
            path = script[len("cmd.exe /c if exist "):].split(" (exit 0)")[0]
            path = path.replace('"', "")
            self.probes.append(path)
            found = path.replace("\\", "/") in self.files
            return _completed(returncode=0 if found else 1)
        return _completed()


def test_windows_cache_candidates_replace_the_posix_layouts():
    assert sessions.session_cache_candidates(_windows_host()) == (
        sessions.WINDOWS_SESSION_CACHE_CANDIDATES
    )
    assert sessions.session_cache_candidates(_remote()) == (
        sessions.SESSION_CACHE_CANDIDATES
    )


@pytest.mark.anyio
async def test_windows_remote_probes_only_the_roaming_profile_cache():
    host = _windows_host()
    expected = host.join(
        host.home,
        "AppData/Roaming/orca/agent-hooks/last-status.json",
    )
    transport = _WindowsFiles({expected: _status(OMP_PANE, "sess-win")})

    with patch("osw.hosts.anyio.run_process", transport):
        assert await sessions.session_cache_path(host) == expected

    assert transport.probes == [expected.replace("/", "\\")]
    assert not any(".config" in path for path in transport.probes)
    assert host.session_cache == expected


@pytest.mark.anyio
async def test_windows_remote_session_is_read_from_the_roaming_cache():
    host = _windows_host()
    cache = host.join(
        host.home,
        "AppData/Roaming/orca/agent-hooks/last-status.json",
    )
    transport = _WindowsFiles({cache: _status(OMP_PANE, "sess-win")})

    with patch("osw.hosts.anyio.run_process", transport):
        assert await sessions.session_on_host(host, OMP_PANE) == "sess-win"
        assert await sessions.session_on_host(host, NO_SESSION_PANE) == ""

    assert transport.probes == [cache.replace("/", "\\")]


@pytest.mark.anyio
async def test_windows_without_a_cache_names_only_the_roaming_path():
    host = _windows_host()
    transport = _WindowsFiles()

    with patch("osw.hosts.anyio.run_process", transport):
        with pytest.raises(hosts.HostError) as exc:
            await sessions.session_cache_path(host, None)

    assert exc.value.code == hosts.REMOTE_SESSION_UNKNOWN
    assert "AppData/Roaming/orca/agent-hooks/last-status.json" in (
        exc.value.message
    )
    assert ".config" not in exc.value.message
    assert len(transport.probes) == 1

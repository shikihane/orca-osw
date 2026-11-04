"""The host seam: one value owns every external interaction OSW performs.

A `Host` carries the machine's identity and the two planes OSW needs:
Orca's dispatch plane (the local CLI targeting another runtime through
`--environment`/`--on`) and the data plane (SSH: `ssh` for commands,
`sftp` for file bytes). Local hosts use subprocesses and the plain
filesystem, so a single-machine install behaves exactly as before and
the seam is invisible.

Nothing outside this module may build a remote argument list or touch a
remote file directly. Errors map onto a closed taxonomy so scripts and
agents can branch on `code` instead of parsing prose.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Sequence

import anyio

from osw.process import hidden_subprocess_kwargs
from osw.state import state_dir, write_json_atomic

LOCAL_NAME = "local"

CONNECT_TIMEOUT_SECS = 10
REMOTE_TIMEOUT_SECS = 120
SFTP_TIMEOUT_SECS = 120

# Closed error taxonomy. Call sites surface these codes; they never
# invent their own.
HOST_UNREACHABLE = "host_unreachable"
HOST_PERMISSION_DENIED = "host_permission_denied"
REMOTE_NO_PROJECT = "remote_no_project"
REMOTE_NO_WORKTREE = "remote_no_worktree"
PROVIDER_MISSING = "provider_missing"
REMOTE_SESSION_UNKNOWN = "remote_session_unknown"
UNKNOWN_HOST = "unknown_host"
HOST_REGISTRY_INVALID = "host_registry_invalid"
REMOTE_READ_FAILED = "remote_read_failed"
REMOTE_WRITE_FAILED = "remote_write_failed"
REMOTE_COMMAND_FAILED = "remote_command_failed"

REGISTRY_VERSION = 1
_UNSAFE_REMOTE_PATH = re.compile(r'["\r\n]')
# cmd.exe still expands %VAR% and treats these as operators after the
# quoting is stripped, so a Windows remote path carrying them cannot be
# passed safely on one command line.
_UNSAFE_WINDOWS_PATH = re.compile(r"[&|<>^%]")
_SAFE_HOST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$")
_SAFE_PROVIDER = re.compile(r"^[A-Za-z0-9._-]+$")
_WINDOWS_PLATFORM = "Windows"
# A Windows host with Git for Windows answers the POSIX probe (its
# `true.exe` and `sh` are on PATH), and `uname -s` reports the emulation
# layer instead of the native platform.
_WINDOWS_SHELL_TOKENS = ("mingw", "msys", "cygwin")
# `where` can list an extensionless shim beside the launcher cmd.exe can
# actually start (Git-for-Windows ships a 308-byte `claude` and a
# `claude.cmd`), so a PATHEXT suffix wins over a bare name.
_WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")


class HostError(Exception):
    """A stable, branchable failure from the host seam."""

    def __init__(
        self,
        code: str,
        message: str,
        host: str = "",
        detail: str = "",
    ) -> None:
        self.code = code
        self.message = message
        self.host = host
        self.detail = detail
        super().__init__(message)


@dataclass
class Result:
    """The bytes and status one host command produced."""

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace").strip()

    @property
    def error_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace").strip()


async def run_process(argv: Sequence[str], **kwargs: Any) -> Result:
    """The single async process runner every external call funnels through.

    Tests substitute this one symbol; there is no separate transport
    double. Orca CLI calls run through it too, so patching it observes
    every interaction.
    """
    merged = {**hidden_subprocess_kwargs(), **kwargs}
    result = await anyio.run_process(list(argv), check=False, **merged)
    return Result(result.returncode, result.stdout, result.stderr)


def _map_transport_failure(result: Result, host: str, action: str) -> HostError:
    stderr = result.error_text
    low = stderr.lower()
    if "permission denied" in low or "publickey" in low \
            or "too many authentication failures" in low:
        return HostError(
            HOST_PERMISSION_DENIED,
            f"{host}: SSH key is not authorised ({action})",
            host=host,
            detail=stderr,
        )
    return HostError(
        HOST_UNREACHABLE,
        f"{host}: cannot reach the host over SSH ({action})",
        host=host,
        detail=stderr,
    )


async def _capture(
    argv: Sequence[str], *, timeout: float, host: str, action: str,
    cwd: str = "",
) -> Result:
    try:
        with anyio.fail_after(timeout):
            if cwd:
                return await run_process(argv, cwd=cwd)
            return await run_process(argv)
    except TimeoutError:
        raise HostError(
            HOST_UNREACHABLE,
            f"{host}: timed out after {timeout:.0f}s ({action})",
            host=host,
        )
    except FileNotFoundError:
        raise HostError(
            HOST_UNREACHABLE,
            f"{host}: '{argv[0]}' is not installed on this machine",
            host=host,
        )
    except OSError as exc:
        raise HostError(
            HOST_UNREACHABLE,
            f"{host}: failed to start '{argv[0]}': {exc}",
            host=host,
        )


def _is_windows_platform(name: str) -> bool:
    value = (name or "").strip().lower()
    if value.startswith("win"):
        return True
    return any(token in value for token in _WINDOWS_SHELL_TOKENS)


@dataclass
class Host:
    """One addressable machine.

    `ssh` empty means this machine. `environment` is Orca's selector for
    a paired server (id or name; `--on`/`--environment`), and
    `worktree`/`remote_root` name the workspace Orca manages there.
    `orca_path`, `providers`, `platform`, `home` and `session_cache` are
    facts doctor discovered and later calls reuse.
    """

    name: str = LOCAL_NAME
    ssh: str = ""
    environment: str = ""
    worktree: str = ""
    remote_root: str = ""
    orca_path: str = ""
    providers: dict[str, str] = field(default_factory=dict)
    platform: str = ""
    home: str = ""
    session_cache: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = LOCAL_NAME
        if self.environment == "" and not self.is_local:
            self.environment = self.name

    @property
    def is_local(self) -> bool:
        return not self.ssh.strip()

    @property
    def is_windows(self) -> bool:
        """A remote whose default shell is cmd.exe.

        A local Windows machine already has its own filesystem and
        process semantics; this flag governs only remote quoting and
        path conventions.
        """
        return _is_windows_platform(self.platform)

    # -- workspace rules --------------------------------------------------

    def worktree_selector(self, local_root: str) -> str:
        """The Orca worktree selector for this host.

        A remote host carries the selector the operator registered; this
        machine addresses its own workspace by path, so no call site has
        to branch on `is_local` for it.
        """
        return f"path:{local_root}" if self.is_local else self.worktree

    def workspace_path(self) -> str:
        """The workspace path this host's selector names, when known.

        `doctor` records the resolved path; before that a `path:` selector
        already names it. A name/branch/id selector names no path.
        """
        if self.remote_root:
            return self.remote_root
        prefix = "path:"
        if self.worktree.startswith(prefix):
            return self.worktree[len(prefix):]
        return ""

    def same_workspace(self, path: str) -> bool:
        """Whether `path` is this host's workspace.

        A path on a remote host is compared as the string it is
        (resolving it here would resolve it against this machine): POSIX
        by trailing-slash-stripped equality, Windows case-insensitively
        with both separators folded. A local path is compared resolved,
        as the filesystem sees it.
        """
        expected = self.workspace_path()
        if not path or not expected:
            return False
        if self.is_windows:
            return _normalise_windows(path) == _normalise_windows(expected)
        if self.is_local:
            return Path(path).resolve() == Path(expected).resolve()
        return path.rstrip("/") == expected.rstrip("/")

    # -- dispatch plane ---------------------------------------------------

    def orca_argv(self, args: Sequence[str]) -> list[str]:
        """Full argv of the Orca CLI call that targets this host's Orca.

        Local calls stay byte-identical to before. A remote call runs
        the local CLI against the paired runtime, because the pairing
        connection is the dispatch plane (the Run and its inbox stay
        here).
        """
        from osw.orca_cli import resolve_orca_command

        cmd = [*resolve_orca_command(), *args]
        if not self.is_local:
            selector = self.environment or self.name
            cmd.extend(["--environment", selector])
        return cmd

    # -- data plane -------------------------------------------------------

    def join(self, *parts: str) -> str:
        """Join path parts in the host's own convention."""
        clean = [str(part) for part in parts if str(part)]
        if not clean:
            return ""
        if self.is_local:
            return os.path.join(*clean)
        if self.is_windows:
            return ntpath.join(*clean)
        return posixpath.join(*clean)

    def dirname(self, path: str) -> str:
        """The parent of `path` in the host's own convention.

        A Windows remote's path is backslashed, so splitting it with
        POSIX rules would yield `""` for the whole parent and make the
        handoff directory unusable.
        """
        if self.is_local:
            return os.path.dirname(path)
        if self.is_windows:
            return ntpath.dirname(path)
        return posixpath.dirname(path)

    def wire_path(self, path: str, code: str = REMOTE_COMMAND_FAILED) -> str:
        """The form of `path` the far side's sftp accepts.

        Windows OpenSSH's sftp takes forward slashes, and a joined
        Windows path carries backslashes, so the wire form is normalised
        there. POSIX paths already are the wire form.
        """
        if not self.is_windows:
            return path
        _guard_remote_path(path, code)
        _guard_windows_path(path, code)
        return path.replace("\\", "/")

    def command_path(self, path: str, code: str = REMOTE_COMMAND_FAILED) -> str:
        """The form of `path` cmd.exe's own commands accept.

        `mkdir` treats a forward slash as a switch, so command paths are
        native (backslash) while sftp uses `wire_path`. Both are guarded
        before they reach a command line.
        """
        if not self.is_windows:
            return path
        _guard_remote_path(path, code)
        _guard_windows_path(path, code)
        return path.replace("/", "\\")

    def _ssh_argv(self, argv: Sequence[str], cwd: str = "") -> list[str]:
        if not _SAFE_HOST_NAME.match(self.ssh.strip()):
            raise HostError(
                HOST_UNREACHABLE,
                f"{self.name}: '{self.ssh}' is not a usable SSH destination",
                host=self.name,
            )
        cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={CONNECT_TIMEOUT_SECS}",
            self.ssh.strip(),
        ]
        script = (
            subprocess.list2cmdline(list(argv))
            if self.is_windows
            else shlex.join(list(argv))
        )
        if cwd:
            if self.is_windows:
                script = (
                    "cd /d "
                    f"{subprocess.list2cmdline([self.command_path(cwd)])}"
                    f" && {script}"
                )
            else:
                script = f"cd {shlex.quote(cwd)} && {script}"
        cmd.append(script)
        return cmd

    async def exec(
        self, argv: Sequence[str], *, cwd: str = "", timeout: float | None = None,
    ) -> Result:
        """Run one command on the host, returning its status and bytes."""
        if self.is_local:
            return await _capture(
                list(argv),
                timeout=timeout or REMOTE_TIMEOUT_SECS,
                host=self.name,
                action=f"exec {argv[0] if argv else ''}".strip(),
                cwd=cwd,
            )
        result = await _capture(
            self._ssh_argv(argv, cwd),
            timeout=timeout or REMOTE_TIMEOUT_SECS,
            host=self.name,
            action=f"exec {argv[0] if argv else ''}".strip(),
        )
        if result.returncode == 255:
            raise _map_transport_failure(result, self.name, "exec")
        return result

    async def read_file(self, path: str) -> bytes:
        """Read one file's bytes from the host."""
        if self.is_local:
            return Path(path).read_bytes()
        with tempfile.TemporaryDirectory(prefix="osw-host-") as tmp:
            local = Path(tmp) / "payload"
            local_text = local.as_posix() if self.is_windows else str(local)
            remote = self.wire_path(path, REMOTE_READ_FAILED)
            batch = (
                f'get {_sftp_quote(remote, REMOTE_READ_FAILED)} '
                f'{_sftp_quote(local_text, REMOTE_READ_FAILED)}\n'
            )
            result = await self._sftp(batch)
            if result.returncode != 0:
                raise HostError(
                    REMOTE_READ_FAILED,
                    f"{self.name}: cannot read {path}",
                    host=self.name,
                    detail=result.error_text,
                )
            try:
                return local.read_bytes()
            except OSError as exc:
                raise HostError(
                    REMOTE_READ_FAILED,
                    f"{self.name}: read of {path} produced no bytes ({exc})",
                    host=self.name,
                )

    async def write_file(self, path: str, data: bytes) -> None:
        """Write one file's bytes atomically on the host."""
        if self.is_local:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return
        _guard_remote_path(path, REMOTE_WRITE_FAILED)
        with tempfile.TemporaryDirectory(prefix="osw-host-") as tmp:
            local = Path(tmp) / "payload"
            local.write_bytes(data)
            local_text = local.as_posix() if self.is_windows else str(local)
            staged = f"{path}.osw-{uuid.uuid4().hex}.tmp"
            staged_wire = self.wire_path(staged, REMOTE_WRITE_FAILED)
            path_wire = self.wire_path(path, REMOTE_WRITE_FAILED)
            batch = (
                f'put {_sftp_quote(local_text, REMOTE_WRITE_FAILED)} '
                f'{_sftp_quote(staged_wire, REMOTE_WRITE_FAILED)}\n'
                f'rename {_sftp_quote(staged_wire, REMOTE_WRITE_FAILED)} '
                f'{_sftp_quote(path_wire, REMOTE_WRITE_FAILED)}\n'
            )
            result = await self._sftp(batch)
            if result.returncode != 0:
                raise HostError(
                    REMOTE_WRITE_FAILED,
                    f"{self.name}: cannot write {path}",
                    host=self.name,
                    detail=result.error_text,
                )

    async def exists(self, path: str) -> bool:
        """Whether a file exists on the host; transport failures raise."""
        if self.is_local:
            return Path(path).exists()
        if self.is_windows:
            command = self.command_path(path)
            result = await self.exec([
                "cmd.exe", "/c", "if", "exist", command,
                "(exit", "0)", "else", "(exit", "1)",
            ])
        else:
            _guard_remote_path(path, REMOTE_COMMAND_FAILED)
            result = await self.exec(["test", "-e", path])
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise HostError(
            REMOTE_COMMAND_FAILED,
            f"{self.name}: existence check failed for {path}",
            host=self.name,
            detail=result.error_text,
        )

    async def ensure_dir(self, path: str) -> None:
        """Create a directory (and parents) on the host."""
        if self.is_local:
            Path(path).mkdir(parents=True, exist_ok=True)
            return
        if self.is_windows:
            # cmd's mkdir creates intermediate directories, like -p.
            command = self.command_path(path)
            result = await self.exec([
                "cmd.exe", "/c", "if", "not", "exist", command,
                "mkdir", command,
            ])
        else:
            _guard_remote_path(path, REMOTE_COMMAND_FAILED)
            result = await self.exec(["mkdir", "-p", path])
        if result.returncode != 0:
            raise HostError(
                REMOTE_COMMAND_FAILED,
                f"{self.name}: cannot create {path}",
                host=self.name,
                detail=result.error_text,
            )

    async def _sftp(self, batch: str) -> Result:
        if not _SAFE_HOST_NAME.match(self.ssh.strip()):
            raise HostError(
                HOST_UNREACHABLE,
                f"{self.name}: '{self.ssh}' is not a usable SSH destination",
                host=self.name,
            )
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".sftp", delete=False,
        ) as handle:
            handle.write(batch)
            batch_path = handle.name
        try:
            result = await _capture(
                [
                    "sftp",
                    "-o", "BatchMode=yes",
                    "-o", f"ConnectTimeout={CONNECT_TIMEOUT_SECS}",
                    "-b", batch_path,
                    self.ssh.strip(),
                ],
                timeout=SFTP_TIMEOUT_SECS,
                host=self.name,
                action="sftp",
            )
        finally:
            try:
                os.unlink(batch_path)
            except OSError:
                pass
        if result.returncode == 255:
            raise _map_transport_failure(result, self.name, "sftp")
        return result

    # -- persistence ------------------------------------------------------

    def record(self) -> dict:
        """The stored form: the dataclass fields, nothing restated."""
        return asdict(self)

    @classmethod
    def from_record(cls, data: dict) -> "Host":
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            raise HostError(
                HOST_REGISTRY_INVALID,
                "host registry entry is not an object with a name",
            )
        values: dict[str, Any] = {
            item.name: str(data.get(item.name) or "")
            for item in fields(cls)
            if item.name not in ("name", "providers")
        }
        providers = data.get("providers")
        values["providers"] = (
            dict(providers) if isinstance(providers, dict) else {}
        )
        values["name"] = data["name"]
        return cls(**values)


def _sftp_quote(path: str, code: str = REMOTE_COMMAND_FAILED) -> str:
    _guard_remote_path(path, code)
    return f'"{path}"'


def _guard_remote_path(path: str, code: str) -> None:
    """Refuse a path that cannot be quoted for the far side.

    `code` is the caller's operation: a rejected read is a read failure,
    not a write failure, and branchable codes must not lie.
    """
    if not path or _UNSAFE_REMOTE_PATH.search(path):
        raise HostError(
            code,
            f"remote path is not usable: {path!r}",
        )


def _guard_windows_path(path: str, code: str) -> None:
    """Refuse a path cmd.exe would reinterpret as syntax or expansion."""
    if _UNSAFE_WINDOWS_PATH.search(path):
        raise HostError(
            code,
            f"remote path is not usable by cmd.exe: {path!r}",
        )


def _normalise_windows(path: str) -> str:
    """One comparison form for a Windows path: slashes, no trailing
    separator, folded case (NTFS is case-insensitive)."""
    return path.replace("\\", "/").rstrip("/").casefold()


_LOCAL = Host()


def local_host() -> Host:
    """This machine. Shared because it carries no state."""
    return _LOCAL


# ---------------------------------------------------------------------------
# Host registry: named machines, stored beside state.json rather than in it
# ---------------------------------------------------------------------------

def registry_file(root: Path) -> Path:
    return state_dir(root) / "hosts.json"


def read_hosts(root: Path) -> dict[str, Host]:
    """Every registered host by name. A malformed file is an error, never
    a silently empty fleet."""
    path = registry_file(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HostError(
            HOST_REGISTRY_INVALID,
            f"host registry {path} is unreadable: {exc}",
        )
    if not isinstance(data, dict) or data.get("version") != REGISTRY_VERSION:
        raise HostError(
            HOST_REGISTRY_INVALID,
            f"host registry {path} has an unknown format",
        )
    entries = data.get("hosts")
    if not isinstance(entries, dict):
        raise HostError(
            HOST_REGISTRY_INVALID,
            f"host registry {path} has no hosts table",
        )
    hosts: dict[str, Host] = {}
    for name, record in entries.items():
        if not isinstance(record, dict):
            raise HostError(
                HOST_REGISTRY_INVALID,
                f"host registry {path} entry {name!r} is not an object",
            )
        host = Host.from_record({**record, "name": record.get("name") or name})
        if host.name != name:
            raise HostError(
                HOST_REGISTRY_INVALID,
                f"host registry {path} entry {name!r} disagrees with its name",
            )
        hosts[name] = host
    return hosts


def write_hosts(root: Path, hosts: dict[str, Host]) -> None:
    write_json_atomic(
        registry_file(root),
        {
            "version": REGISTRY_VERSION,
            "hosts": {name: host.record() for name, host in hosts.items()},
        },
    )


def save_host(root: Path, host: Host) -> None:
    """Insert or replace one record, leaving the rest untouched."""
    hosts = read_hosts(root)
    hosts[host.name] = host
    write_hosts(root, hosts)


def remove_host(root: Path, name: str) -> Host | None:
    hosts = read_hosts(root)
    removed = hosts.pop(name, None)
    if removed is not None:
        write_hosts(root, hosts)
    return removed


def host_by_name(root: Path, name: str) -> Host | None:
    return read_hosts(root).get(name)


# ---------------------------------------------------------------------------
# Agent references: <host>/<agent_id>, with local/ optional
# ---------------------------------------------------------------------------

def split_ref(text: str) -> tuple[str, str]:
    """(host prefix, agent id). A bare id belongs to this machine."""
    value = (text or "").strip()
    if "/" not in value:
        return LOCAL_NAME, value
    host, agent_id = value.split("/", 1)
    return host or LOCAL_NAME, agent_id


def resolve_host(root: Path, name: str) -> Host:
    """The named host, refusing anything unregistered (a typo must not
    dispatch work to the wrong machine)."""
    if name in ("", LOCAL_NAME):
        return local_host()
    try:
        hosts = read_hosts(root)
    except HostError as exc:
        raise HostError(exc.code, exc.message, host=name)
    host = hosts.get(name)
    if host is None:
        known = ", ".join(sorted(hosts)) or "(none registered)"
        raise HostError(
            UNKNOWN_HOST,
            f"unknown host '{name}'; registered hosts: {known}",
            host=name,
        )
    return host


def resolve_agent_ref(root: Path, reference: str) -> tuple[Host, str]:
    """(host, agent id) for `<host>/<agent_id>` or a bare id."""
    name, agent_id = split_ref(reference)
    return resolve_host(root, name), agent_id


def host_for_agent(root: Path, agent: dict) -> Host:
    """The host an agent record belongs to; local when it names none."""
    name = str(agent.get("host") or LOCAL_NAME)
    if name == LOCAL_NAME:
        return local_host()
    return resolve_host(root, name)


def new_handoff_path(host: Host, agent_id: str) -> str:
    """Pre-allocate the handoff markdown path on the agent's own host.

    Remote handoffs live under the account's home, not inside the
    worktree, so OSW never pollutes a repository it does not own.
    """
    base = (host.home or host.remote_root).rstrip("/")
    if not base:
        raise HostError(
            REMOTE_NO_WORKTREE,
            f"{host.name}: no home or workspace path is known for handoffs",
            host=host.name,
        )
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return host.join(base, ".orca", "osw", "handoffs", f"{agent_id}_{stamp}.md")


# ---------------------------------------------------------------------------
# Doctor: connection -> workspace -> worktree -> provider
# ---------------------------------------------------------------------------

DOCTOR_RUNGS = ("connection", "workspace", "worktree", "provider")

_NEXT_ACTIONS = {
    HOST_UNREACHABLE: "check sshd on the host and the SSH destination",
    HOST_PERMISSION_DENIED: "authorise this machine's key (ssh-copy-id)",
    REMOTE_NO_PROJECT: "register a workspace in Orca on that host",
    REMOTE_NO_WORKTREE: "pass --worktree with a selector that host has",
    PROVIDER_MISSING: "install the provider CLI for the SSH account",
}


def _rung_ok(rung: str, **data: Any) -> dict:
    return {"rung": rung, "ok": True, **data}


def _rung_failed(rung: str, error: HostError) -> dict:
    return {
        "rung": rung,
        "ok": False,
        "code": error.code,
        "message": error.message,
        "next": _NEXT_ACTIONS.get(
            error.code,
            f"check that Orca answers on {error.host or 'that host'}",
        ),
    }


def _as_host_error(exc: Exception) -> HostError:
    """One failure shape for the ladder.

    The workspace and worktree rungs go through `run_orca`, which raises
    `OrcaError` (pairing failure, contract mismatch, non-zero CLI exit),
    not `HostError`. It keeps its code; an uncoded failure is a remote
    command failure.
    """
    if isinstance(exc, HostError):
        return exc
    return HostError(
        str(getattr(exc, "code", "") or REMOTE_COMMAND_FAILED),
        str(getattr(exc, "message", "") or exc),
    )


async def _probe_connection(host: Host) -> dict:
    if host.is_local:
        return _rung_ok("connection")
    result = await host.exec(["true"])
    if result.returncode == 0:
        # Git for Windows puts true.exe and sh on PATH, so a zero here
        # does not prove POSIX. cmd.exe answering is positive evidence
        # of Windows and costs one call only in doctor.
        native = await host.exec(["cmd.exe", "/c", "exit", "0"])
        if native.returncode == 0:
            host.platform = _WINDOWS_PLATFORM
            return _rung_ok("connection", platform=host.platform)
        return _rung_ok("connection")
    # A cmd.exe login shell has no `true`; ask it something it does
    # understand. Only cmd's own exit status proves the shell works, so
    # a failing POSIX probe that is not a transport failure (255) does
    # not make the host unreachable by itself.
    fallback = await host.exec(["cmd.exe", "/c", "exit", "0"])
    if fallback.returncode == 0:
        host.platform = _WINDOWS_PLATFORM
        return _rung_ok("connection", platform=host.platform)
    raise HostError(
        HOST_UNREACHABLE,
        f"{host.name}: SSH connection succeeded but neither a POSIX "
        "shell nor cmd.exe answered",
        host=host.name,
        detail=fallback.error_text or result.error_text,
    )


async def _probe_workspace(host: Host) -> dict:
    from osw.orca_cli import run_orca

    data = await run_orca("project", "list", host=host)
    result = data.get("result") if isinstance(data, dict) else None
    projects = result.get("projects") if isinstance(result, dict) else None
    if not isinstance(projects, list) or not projects:
        raise HostError(
            REMOTE_NO_PROJECT,
            f"{host.name}: Orca has no workspace registered there",
            host=host.name,
        )
    return _rung_ok("workspace", projects=len(projects))


def _worktree_matches(
    worktree: dict, selector: str, host: Host | None = None,
) -> bool:
    value = (selector or "").strip()
    if not value:
        return False
    if ":" in value:
        kind, _, target = value.partition(":")
        if kind == "path":
            path = str(worktree.get("path") or "")
            if host is not None and host.is_windows:
                return _normalise_windows(path) == _normalise_windows(target)
            return path == target
        if kind == "name":
            return str(worktree.get("displayName") or "") == target
        if kind == "branch":
            return str(worktree.get("branch") or "") == target
        if kind == "id":
            return str(worktree.get("id") or "") == target
        if kind == "identity":
            return str(worktree.get("identity") or "") == target
    return value in (
        str(worktree.get("displayName") or ""),
        str(worktree.get("path") or ""),
    )


async def _probe_worktree(host: Host) -> dict:
    from osw.orca_cli import run_orca

    if not host.worktree:
        raise HostError(
            REMOTE_NO_WORKTREE,
            f"{host.name}: no workspace selector is recorded for dispatch",
            host=host.name,
        )
    data = await run_orca("worktree", "list", host=host)
    result = data.get("result") if isinstance(data, dict) else None
    worktrees = result.get("worktrees") if isinstance(result, dict) else None
    if not isinstance(worktrees, list):
        raise HostError(
            REMOTE_NO_WORKTREE,
            f"{host.name}: Orca listed no worktrees",
            host=host.name,
        )
    for worktree in worktrees:
        if isinstance(worktree, dict) \
                and _worktree_matches(worktree, host.worktree, host):
            path = str(worktree.get("path") or "")
            if not path:
                break
            host.remote_root = path
            return _rung_ok("worktree", path=path)
    known = ", ".join(
        str(worktree.get("path") or worktree.get("displayName") or "?")
        for worktree in worktrees if isinstance(worktree, dict)
    ) or "(none)"
    raise HostError(
        REMOTE_NO_WORKTREE,
        f"{host.name}: no worktree matches '{host.worktree}'; "
        f"available: {known}",
        host=host.name,
    )


_PROBE_SCRIPT = (
    'printf "HOME=%s\\n" "$HOME"; '
    'printf "OS=%s\\n" "$(uname -s 2>/dev/null || echo unknown)"; '
    "command -v orca || true; "
    "command -v {provider} || true"
)


def _parse_probe(text: str) -> dict:
    home = platform = ""
    binaries: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("HOME="):
            home = line[len("HOME="):]
        elif line.startswith("OS="):
            platform = line[len("OS="):]
        else:
            binaries.append(line)
    return {"home": home, "platform": platform, "binaries": binaries}


async def _where(host: Host, name: str) -> list[str]:
    """Every path `where` reports, or none. A missing name is a normal
    answer (non-zero exit), not a failed probe."""
    result = await host.exec(["cmd.exe", "/c", "where", name])
    if result.returncode != 0:
        return []
    return [
        line.strip() for line in result.text.splitlines()
        if line.strip() and not line.strip().upper().startswith("INFO:")
    ]


async def _first_existing(host: Host, candidates: Sequence[str]) -> str:
    """`where` lists one path per PATHEXT match; only an on-disk file can
    be launched. Executable suffixes are tried first (in output order),
    then the rest, so a bare shim never beats `claude.cmd`."""
    expanded = [item for item in candidates
                if ntpath.splitext(item)[1].lower()
                in _WINDOWS_EXECUTABLE_SUFFIXES]
    expanded += [item for item in candidates if item not in expanded]
    for candidate in expanded:
        if await host.exists(candidate):
            return candidate
    return ""


async def _probe_provider_windows(host: Host, provider: str) -> dict:
    found = await _where(host, provider)
    orca_found = await _where(host, "orca")
    home_result = await host.exec(["cmd.exe", "/c", "echo", "%USERPROFILE%"])
    # `ver` answers only on cmd.exe; it is the second confirmation of the
    # platform after the connection rung and its output is not parsed.
    await host.exec(["cmd.exe", "/c", "ver"])

    host.platform = _WINDOWS_PLATFORM
    if home_result.returncode == 0 and home_result.text:
        host.home = home_result.text.splitlines()[0].strip() or host.home
    orca_path = await _first_existing(host, orca_found) if orca_found else ""
    if orca_path:
        host.orca_path = orca_path
    provider_path = await _first_existing(host, found) if found else ""
    if not provider_path:
        raise HostError(
            PROVIDER_MISSING,
            f"{host.name}: provider '{provider}' is not installed for the "
            "SSH account",
            host=host.name,
        )
    host.providers[provider] = provider_path
    return _rung_ok("provider", path=provider_path, orca_path=orca_path)


async def _probe_provider(host: Host, provider: str) -> dict:
    if not _SAFE_PROVIDER.match(provider or ""):
        raise HostError(
            PROVIDER_MISSING,
            f"'{provider}' is not a provider name",
            host=host.name,
        )
    if host.is_local:
        orca = shutil.which("orca") or ""
        found = shutil.which(provider) or ""
        probe = {
            "home": str(Path.home()),
            "platform": "Windows" if os.name == "nt" else "Linux",
            "binaries": [item for item in (orca, found) if item],
        }
    elif host.is_windows:
        return await _probe_provider_windows(host, provider)
    else:
        result = await host.exec(
            ["sh", "-c", _PROBE_SCRIPT.format(provider=provider)],
        )
        if result.returncode != 0:
            raise HostError(
                REMOTE_COMMAND_FAILED,
                f"{host.name}: provider probe failed",
                host=host.name,
                detail=result.error_text,
            )
        probe = _parse_probe(result.text)
    if not host.is_local and _is_windows_platform(probe["platform"]):
        # Git/MSYS answered `sh`, but dispatch must use native Windows
        # paths and cmd.exe, so re-probe the way a cmd.exe host expects.
        host.platform = _WINDOWS_PLATFORM
        return await _probe_provider_windows(host, provider)
    binaries = probe["binaries"]
    # The probe prints orca first and the provider second; a missing orca
    # shifts the provider into the first slot, so match by name.
    orca_path = next(
        (path for path in binaries
         if Path(path).name.split(".")[0].lower() == "orca"),
        "",
    )
    provider_path = next(
        (path for path in binaries if Path(path).name.split(".")[0] == provider),
        "",
    )
    if not provider_path:
        raise HostError(
            PROVIDER_MISSING,
            f"{host.name}: provider '{provider}' is not installed for the "
            "SSH account",
            host=host.name,
        )
    host.home = probe["home"] or host.home
    host.platform = probe["platform"] or host.platform
    if orca_path:
        host.orca_path = orca_path
    host.providers[provider] = provider_path
    return _rung_ok("provider", path=provider_path, orca_path=orca_path)


async def doctor(
    root: Path,
    host: Host,
    provider: str = "claude",
    persist: bool = True,
) -> dict:
    """Run the ladder in order and stop at the first failing rung.

    Returns {"host", "ok", "rungs"} where the last rung carries the
    error code, message and one next step. Rungs after a failure are
    never probed, so the operator is not told to fix a later problem.

    Both failure families are the ladder's: seam errors (`HostError`) and
    the Orca CLI's own (`OrcaError`) from the workspace/worktree rungs.
    """
    from osw.orca_cli import OrcaError

    report: dict = {"host": host.name, "ok": False, "rungs": []}
    probes = (
        ("connection", lambda: _probe_connection(host)),
        ("workspace", lambda: _probe_workspace(host)),
        ("worktree", lambda: _probe_worktree(host)),
        ("provider", lambda: _probe_provider(host, provider)),
    )
    for rung, probe in probes:
        try:
            outcome = await probe()
        except (HostError, OrcaError) as exc:
            failure = _as_host_error(exc)
            report["rungs"].append(_rung_failed(rung, failure))
            report["code"] = failure.code
            report["message"] = failure.message
            if persist:
                _persist_if_registered(root, host)
            return report
        report["rungs"].append(outcome)
        if rung in ("worktree", "provider") and persist:
            _persist_if_registered(root, host)
    report["ok"] = True
    return report


def _persist_if_registered(root: Path, host: Host) -> None:
    if host.name in ("", LOCAL_NAME):
        return
    try:
        hosts = read_hosts(root)
    except HostError:
        return
    if host.name in hosts:
        hosts[host.name] = host
        write_hosts(root, hosts)


def reachability(host: Host) -> str:
    """Synchronous one-attempt reachability for listings.

    Returns "reachable", "unreachable" or "permission_denied"; a dead
    host is a state of its row, not an exception for the caller. Only
    the two seam codes that describe a host's state are captured — any
    other failure is a defect here and propagates.
    """
    if host.is_local:
        return "reachable"
    return _reachability_code_result(host)


def _reachability_code_result(host: Host) -> str:
    async def probe() -> Result:
        # Host.exec bounds the probe itself; anyio.run has no timeout.
        return await host.exec(["true"], timeout=CONNECT_TIMEOUT_SECS + 5)

    try:
        anyio.run(probe)
    except HostError as exc:
        if exc.code == HOST_PERMISSION_DENIED:
            return "permission_denied"
        if exc.code == HOST_UNREACHABLE:
            return "unreachable"
        raise
    return "reachable"

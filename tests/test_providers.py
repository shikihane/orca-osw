from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from osw.hosts import Host
from osw.providers import (
    CODEX_EFFORTS,
    KNOWN_AGENT_CLIS,
    ProviderError,
    build_provider_command,
    codex_variants,
    ensure_workspace_trust,
    kimi_variants,
    normalize_provider,
    parse_claude_aliases,
    parse_grok_models,
    parse_omp_models,
    parse_pi_models,
    probe_variants,
    resume_args,
    resume_command,
    shell_command_line,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# Real output shapes captured from the actual CLIs

PI_LIST_MODELS = """\
provider  model                  context  max-out  thinking  images
deepseek  deepseek-v4-flash      1M       384K     yes       no
deepseek  deepseek-v4-pro        1M       384K     yes       no
openai    gpt-5.2                400K     128K     yes       yes
"""

CLAUDE_HELP = """\
  --model <model>                       Model for the current session. Provide
                                        an alias for the latest model (e.g.
                                        'fable', 'opus', or 'sonnet') or a
                                        model's full name (e.g.
                                        'claude-fable-5').
"""

GROK_MODELS = """\
You are not authenticated.

Default model: grok-4.5

Available models:
  * grok-4.5 (default)
  - claude-*
  - grok-build-0.1
  - grok-4.20-multi-agent-0309
  - grok-4.3
"""

OMP_MODELS_JSON = json.dumps({
    "models": [
        {
            "provider": "openai-codex",
            "id": "gpt-5.3-codex",
            "selector": "openai-codex/gpt-5.3-codex",
            "thinking": ["low", "medium", "high", "xhigh"],
        },
        {
            "provider": "anthropic",
            "id": "claude-opus-4-6",
            "selector": "anthropic/claude-opus-4-6",
            "thinking": ["low", "medium", "high", "max"],
        },
    ],
})


def test_parse_pi_models():
    variants = parse_pi_models(PI_LIST_MODELS)
    assert {"name": "pi-deepseek-v4-pro",
            "command": "pi --model deepseek/deepseek-v4-pro"} in variants
    assert {"name": "pi-gpt-5.2",
            "command": "pi --model openai/gpt-5.2"} in variants
    # header row must not become a variant
    assert all(v["name"] != "pi-model" for v in variants)


def test_parse_claude_aliases():
    variants = parse_claude_aliases(CLAUDE_HELP)
    assert {"name": "claude-opus", "command": "claude --model opus"} in variants
    assert {"name": "claude-sonnet", "command": "claude --model sonnet"} in variants
    # the full-name example must be skipped
    assert all("claude-fable-5" not in v["name"] for v in variants)


def test_parsers_tolerate_garbage():
    assert parse_pi_models("") == []
    assert parse_pi_models("unexpected error text") == []
    assert parse_claude_aliases("") == []
    assert parse_claude_aliases("no model flag here") == []


def test_codex_variants_from_config():
    variants = codex_variants({"model": "gpt-5.5"})
    assert len(variants) == len(CODEX_EFFORTS)
    assert {"name": "codex-gpt-5.5-high",
            "command": "codex -c model_reasoning_effort=high -m gpt-5.5"} in variants


def test_codex_variants_without_config():
    variants = codex_variants({})
    # still selectable: efforts against the CLI's own default model
    assert {"name": "codex-model-medium",
            "command": "codex -c model_reasoning_effort=medium"} in variants


def test_build_provider_command_for_claude():
    command = build_provider_command(
        "claude",
        model="sonnet",
        thinking="high",
    )

    assert command == "claude --dangerously-skip-permissions --model sonnet --effort high"


def test_build_provider_command_for_codex():
    command = build_provider_command(
        "codex",
        model="gpt-5",
        thinking="medium",
    )

    assert command == (
        "codex --dangerously-bypass-approvals-and-sandbox "
        "-c model_reasoning_effort=medium -m gpt-5"
    )


def test_omp_is_scanned_as_an_agent_cli():
    assert "omp" in KNOWN_AGENT_CLIS


def test_parse_omp_models():
    variants = parse_omp_models(OMP_MODELS_JSON)

    assert variants == [
        {
            "name": "omp-openai-codex-gpt-5.3-codex",
            "command": "omp --model openai-codex/gpt-5.3-codex",
        },
        {
            "name": "omp-anthropic-claude-opus-4-6",
            "command": "omp --model anthropic/claude-opus-4-6",
        },
    ]


def test_parse_omp_models_tolerates_invalid_entries_and_duplicates():
    output = json.dumps({
        "models": [
            {"selector": "openai/gpt-5"},
            {"selector": " openai/gpt-5 "},
            {"selector": ""},
            {"id": "missing-provider"},
            "garbage",
        ],
    })

    assert parse_omp_models(output) == [
        {
            "name": "omp-openai-gpt-5",
            "command": "omp --model openai/gpt-5",
        },
    ]
    assert parse_omp_models("") == []
    assert parse_omp_models("not json") == []
    assert parse_omp_models(json.dumps({"models": "garbage"})) == []


def test_probe_variants_for_omp_uses_json_model_catalog():
    with patch("osw.providers._run_capture", return_value=OMP_MODELS_JSON) as run:
        variants = probe_variants("omp", path=r"C:\bin\omp.exe")

    run.assert_called_once_with([r"C:\bin\omp.exe", "models", "--json"])
    assert variants[0]["command"] == (
        "omp --model openai-codex/gpt-5.3-codex"
    )


def test_build_provider_command_for_omp():
    command = build_provider_command(
        "omp",
        model="openai-codex/gpt-5.3-codex",
        thinking="xhigh",
    )

    assert command == (
        "omp --auto-approve --model openai-codex/gpt-5.3-codex "
        "--thinking xhigh"
    )


def test_kimi_variants_from_config():
    # Shape of ~/.kimi-code/config.toml's [models.*] tables
    config = {"models": {
        "kimi-code/k3": {"model": "k3"},
        "kimi-code/kimi-for-coding": {"model": "kimi-for-coding"},
    }}
    variants = kimi_variants(config)
    assert {"name": "kimi-k3",
            "command": "kimi --model kimi-code/k3"} in variants
    assert {"name": "kimi-kimi-for-coding",
            "command": "kimi --model kimi-code/kimi-for-coding"} in variants


def test_kimi_variants_without_config():
    assert kimi_variants({}) == []
    assert kimi_variants({"models": "garbage"}) == []


def test_kimi_variants_skip_non_table_and_empty_entries():
    config = {"models": {
        "kimi-code/k3": {"model": "k3"},
        "stray-scalar": "oops",
        "": {"model": "unnamed"},
    }}
    variants = kimi_variants(config)
    assert variants == [
        {"name": "kimi-k3", "command": "kimi --model kimi-code/k3"},
    ]


def test_kimi_variants_disambiguate_colliding_tails():
    config = {"models": {
        "kimi-code/k3": {"model": "k3"},
        "other/k3": {"model": "k3"},
    }}
    names = [v["name"] for v in kimi_variants(config)]
    assert names == ["kimi-kimi-code-k3", "kimi-other-k3"]


def test_kimi_variants_quote_alias_with_whitespace():
    config = {"models": {"odd alias/k3": {"model": "k3"}}}
    variants = kimi_variants(config)
    assert variants == [
        {"name": "kimi-k3", "command": 'kimi --model "odd alias/k3"'},
    ]


def test_build_provider_command_for_kimi():
    command = build_provider_command("kimi", model="kimi-code/k3")

    assert command == "kimi --yolo --model kimi-code/k3"


def test_build_provider_command_kimi_rejects_thinking():
    with pytest.raises(ProviderError, match="does not accept --thinking"):
        build_provider_command("kimi", thinking="max")


def test_grok_is_scanned_as_an_agent_cli():
    assert "grok" in KNOWN_AGENT_CLIS


def test_parse_grok_models():
    variants = parse_grok_models(GROK_MODELS)
    assert variants == [
        {"name": "grok-grok-4.5", "command": "grok --model grok-4.5"},
        {"name": "grok-grok-build-0.1", "command": "grok --model grok-build-0.1"},
        {"name": "grok-grok-4.20-multi-agent-0309",
         "command": "grok --model grok-4.20-multi-agent-0309"},
        {"name": "grok-grok-4.3", "command": "grok --model grok-4.3"},
    ]
    # gateway wildcard patterns are not selectable models
    assert all("*" not in v["command"] for v in variants)


def test_parse_grok_models_tolerates_garbage():
    assert parse_grok_models("") == []
    assert parse_grok_models("unexpected error text") == []
    # rows before the header never count
    assert parse_grok_models("* grok-4.5 (default)") == []


def test_probe_variants_for_grok_uses_models_command():
    with patch("osw.providers._run_capture", return_value=GROK_MODELS) as run:
        variants = probe_variants("grok", path=r"C:\bin\grok.exe")

    run.assert_called_once_with([r"C:\bin\grok.exe", "models"])
    assert {"name": "grok-grok-4.5",
            "command": "grok --model grok-4.5"} in variants


def test_build_provider_command_for_grok():
    command = build_provider_command(
        "grok",
        model="grok-4.3",
        thinking="high",
    )

    assert command == (
        "grok --always-approve --trust --model grok-4.3 "
        "--reasoning-effort high"
    )


def test_build_provider_command_for_pi():
    command = build_provider_command(
        "pi",
        model="deepseek/deepseek-v4-pro",
        thinking="low",
    )

    assert command == "pi --approve --model deepseek/deepseek-v4-pro --thinking low"


def test_build_provider_command_omits_optional_flags():
    assert build_provider_command("claude") == "claude --dangerously-skip-permissions"
    assert build_provider_command("codex") == (
        "codex --dangerously-bypass-approvals-and-sandbox"
    )
    assert build_provider_command("pi") == "pi --approve"
    assert build_provider_command("kimi") == "kimi --yolo"
    assert build_provider_command("omp") == "omp --auto-approve"
    assert build_provider_command("grok") == "grok --always-approve --trust"


def test_build_provider_command_absolute_binary_keeps_local_output():
    """A remote launch starts with the path `host doctor` discovered.

    Without one — the local case — the command is byte-identical to the
    pre-existing output, which the watcher and every stored record rely
    on.
    """
    local = build_provider_command("claude", "sonnet", "high")
    assert local == (
        "claude --dangerously-skip-permissions --model sonnet --effort high"
    )
    assert build_provider_command("claude", "sonnet", "high", binary="") == local
    assert build_provider_command("claude", "sonnet", "high", binary=None) == local
    assert build_provider_command(
        "claude", "sonnet", "high", binary="/opt/agents/claude",
    ) == (
        "/opt/agents/claude --dangerously-skip-permissions "
        "--model sonnet --effort high"
    )
    # The dsh launcher takes the same binary slot, before its target.
    assert build_provider_command(
        "dsh", binary="/usr/local/bin/dsh-tui",
    ) == "/usr/local/bin/dsh-tui ."


def test_dsh_is_scanned_as_an_agent_cli():
    assert "dsh-tui" in KNOWN_AGENT_CLIS


def test_normalize_provider_maps_orca_agent_names():
    # Orca's ps row for a dsh pane names the launcher.
    assert normalize_provider("dsh-tui") == "dsh"
    assert normalize_provider(" DSH-TUI ") == "dsh"
    assert normalize_provider("claude") == "claude"


def test_build_provider_command_accepts_the_orca_dsh_name():
    assert build_provider_command("dsh-tui") == "dsh-tui ."


def test_build_provider_command_for_dsh():
    # The launcher (not the `dsh` harness) is what Orca recognises; `.`
    # is its workspace target, as in Orca's own `dsh-tui .` launchCmd.
    assert build_provider_command("dsh") == "dsh-tui ."


@pytest.mark.parametrize("kwargs", [{"model": "deepseek-flash"},
                                    {"thinking": "high"}])
def test_build_provider_command_dsh_rejects_model_and_thinking(kwargs):
    with pytest.raises(ProviderError, match="no model or effort launch flag"):
        build_provider_command("dsh", **kwargs)


def test_probe_variants_for_dsh_lists_the_default_launch(monkeypatch):
    monkeypatch.setattr("osw.providers.shutil.which",
                        lambda name: "C:/bin/dsh-tui.cmd" if name == "dsh-tui" else None)
    assert probe_variants("dsh") == [{"name": "dsh-default", "command": "dsh-tui ."}]


def test_probe_variants_for_dsh_without_launcher(monkeypatch):
    monkeypatch.setattr("osw.providers.shutil.which", lambda name: None)
    assert probe_variants("dsh") == []


def test_resume_args_for_dsh():
    assert resume_args("dsh", "sid") == ["dsh-tui", ".", "--resume", "sid"]


def test_build_provider_command_rejects_unknown_provider():
    with pytest.raises(ProviderError, match="unsupported provider 'gemini'"):
        build_provider_command("gemini")


def test_build_provider_command_strips_optional_values():
    assert build_provider_command(
        "claude", model=" sonnet ", thinking="  ",
    ) == "claude --dangerously-skip-permissions --model sonnet"


# --- ensure_workspace_trust ---

def _completed(returncode=0, stdout=b"", stderr=b""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _remote_host() -> Host:
    return Host(
        name="fnnas",
        ssh="fnnas",
        environment="fnnas",
        home="/vol1/1000/shiki",
    )


def _windows_remote_host() -> Host:
    """The N100 smoke machine: Windows 10 reached over SSH."""
    return Host(
        name="n100",
        ssh="n100",
        environment="n100",
        home="C:\\Users\\hy",
        platform="Windows",
    )


class _FakeTransport:
    """Stands in for anyio.run_process over ssh/sftp.

    Records every argv and sftp batch, and keeps a virtual remote tree so
    a second call sees exactly what the first one staged and renamed.
    """

    def __init__(self, files=None, *, fail_writes=False, transport_error=b""):
        self.files = dict(files or {})
        self.fail_writes = fail_writes
        self.transport_error = transport_error
        self.scripts: list[str] = []
        self.batches: list[list[str]] = []
        self.puts: list[tuple[str, bytes]] = []
        self._staged: dict[str, bytes] = {}

    async def __call__(self, argv, **kwargs):
        if self.transport_error:
            return _completed(returncode=255, stderr=self.transport_error)
        if argv[0] == "sftp":
            return self._sftp(argv)
        script = argv[-1]
        self.scripts.append(script)
        if script.startswith("test -e "):
            found = script[len("test -e "):] in self.files
            return _completed(returncode=0 if found else 1)
        return _completed()

    def _sftp(self, argv):
        batch_path = Path(argv[argv.index("-b") + 1])
        lines = [
            line
            for line in batch_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.batches.append(lines)
        for line in lines:
            verb, rest = line.split(" ", 1)
            source, destination = (
                part.strip('"') for part in rest.rsplit(" ", 1)
            )
            if verb == "put":
                if self.fail_writes:
                    return _completed(returncode=1, stderr=b"permission denied")
                payload = Path(source).read_bytes()
                self._staged[destination] = payload
                self.puts.append((destination, payload))
            elif verb == "rename":
                if self.fail_writes:
                    return _completed(returncode=1, stderr=b"permission denied")
                self.files[destination] = self._staged.pop(source)
            elif verb == "get":
                payload = self.files.get(source)
                if payload is None:
                    return _completed(returncode=1, stderr=b"no such file")
                Path(destination).write_bytes(payload)
        return _completed()


@pytest.mark.anyio
async def test_trust_claude_writes_and_preserves(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    conf = tmp_path / ".claude.json"
    conf.write_text(json.dumps({"numStartups": 1, "projects": {}}), encoding="utf-8")
    root = tmp_path / "proj"
    assert await ensure_workspace_trust("claude", root) == "trusted"
    data = json.loads(conf.read_text(encoding="utf-8"))
    assert data["numStartups"] == 1
    assert data["projects"][root.as_posix()]["hasTrustDialogAccepted"] is True
    # idempotent
    assert await ensure_workspace_trust("claude", root) == "already trusted"


@pytest.mark.anyio
async def test_trust_codex_appends_toml(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    codex = tmp_path / ".codex"
    codex.mkdir()
    conf = codex / "config.toml"
    conf.write_text('model = "gpt-5.2"\n', encoding="utf-8")
    root = tmp_path / "proj"
    assert await ensure_workspace_trust("codex", root) == "trusted"
    with conf.open("rb") as f:
        data = tomllib.load(f)
    assert data["model"] == "gpt-5.2"
    assert data["projects"][str(root)]["trust_level"] == "trusted"
    # idempotent
    assert await ensure_workspace_trust("codex", root) == "already trusted"


@pytest.mark.anyio
async def test_trust_kimi_writes_hash_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "proj"
    assert await ensure_workspace_trust("kimi", root) == "trusted"
    digest = hashlib.sha256(root.as_posix().encode()).hexdigest()[:12]
    trust_file = tmp_path / ".kimi-code" / "workspace-trust" / f"wd_proj_{digest}"
    payload = json.loads(trust_file.read_text(encoding="utf-8"))
    assert payload["root"] == root.as_posix()
    assert payload["trustedAt"] > 0
    # idempotent
    assert await ensure_workspace_trust("kimi", root) == "already trusted"


@pytest.mark.anyio
async def test_trust_unknown_provider_is_noop(tmp_path):
    assert await ensure_workspace_trust("pi", tmp_path) == ""


@pytest.mark.anyio
async def test_remote_claude_trust_puts_to_a_temporary_name_then_renames():
    host = _remote_host()
    target = "/vol1/1000/shiki/.claude.json"
    root = "/vol1/1000/orca/workspaces/proj"
    transport = _FakeTransport(files={target: b'{"numStartups": 3}'})

    with patch("osw.hosts.anyio.run_process", transport):
        assert await ensure_workspace_trust("claude", root, host) == "trusted"
        assert await ensure_workspace_trust("claude", root, host) == "already trusted"

    write_batch = next(b for b in transport.batches if b[0].startswith("put"))
    put_line, rename_line = write_batch
    staged, payload = transport.puts[0]
    assert staged.startswith(f"{target}.osw-") and staged.endswith(".tmp")
    assert put_line.startswith('put "') and put_line.endswith(f'"{staged}"')
    assert rename_line == f'rename "{staged}" "{target}"'
    data = json.loads(payload)
    assert data["numStartups"] == 3
    assert data["projects"][root]["hasTrustDialogAccepted"] is True
    assert transport.files[target] == payload
    assert len(transport.puts) == 1


@pytest.mark.anyio
async def test_remote_codex_trust_appends_a_parseable_section():
    host = _remote_host()
    target = "/vol1/1000/shiki/.codex/config.toml"
    root = "/vol1/1000/orca/workspaces/proj"
    transport = _FakeTransport(files={target: b'model = "gpt-5.2"\n'})

    with patch("osw.hosts.anyio.run_process", transport):
        assert await ensure_workspace_trust("codex", root, host) == "trusted"
        assert await ensure_workspace_trust("codex", root, host) == "already trusted"

    _, payload = transport.puts[0]
    data = tomllib.loads(payload.decode("utf-8"))
    assert data["model"] == "gpt-5.2"
    assert data["projects"][root]["trust_level"] == "trusted"
    write_batch = next(b for b in transport.batches if b[0].startswith("put"))
    assert write_batch[1] == f'rename "{transport.puts[0][0]}" "{target}"'
    assert "mkdir -p /vol1/1000/shiki/.codex" in transport.scripts
    assert len(transport.puts) == 1


@pytest.mark.anyio
async def test_remote_kimi_trust_creates_the_hashed_file():
    host = _remote_host()
    root = "/vol1/1000/orca/workspaces/proj"
    digest = hashlib.sha256(root.encode()).hexdigest()[:12]
    target = f"/vol1/1000/shiki/.kimi-code/workspace-trust/wd_proj_{digest}"
    transport = _FakeTransport()

    with patch("osw.hosts.anyio.run_process", transport):
        assert await ensure_workspace_trust("kimi", root, host) == "trusted"
        assert await ensure_workspace_trust("kimi", root, host) == "already trusted"

    staged, payload = transport.puts[0]
    assert transport.batches[0][1] == f'rename "{staged}" "{target}"'
    assert json.loads(payload)["root"] == root
    assert "mkdir -p /vol1/1000/shiki/.kimi-code/workspace-trust" in transport.scripts
    assert len(transport.puts) == 1


@pytest.mark.anyio
async def test_windows_remote_claude_trust_keys_the_path_with_forward_slashes():
    """claude normalises a Windows path to forward slashes before using
    it as a ~/.claude.json project key: on the N100 its own entry is
    `C:/Users/hy/...`, so the backslashed spelling never matches and the
    trust dialog still appeared after OSW's write."""
    host = _windows_remote_host()
    root = "C:\\Users\\hy\\orca-workspaces"
    target = "C:/Users/hy/.claude.json"  # sftp's wire spelling
    transport = _FakeTransport(files={target: b'{"projects": {}}'})

    with patch("osw.hosts.anyio.run_process", transport):
        assert await ensure_workspace_trust("claude", root, host) == "trusted"
        # idempotent: the second call finds the key it wrote
        assert await ensure_workspace_trust("claude", root, host) == (
            "already trusted"
        )

    assert target in transport.files
    projects = json.loads(transport.files[target])["projects"]
    assert projects["C:/Users/hy/orca-workspaces"][
        "hasTrustDialogAccepted"
    ] is True
    assert root not in projects
    assert len(transport.puts) == 1


@pytest.mark.anyio
async def test_windows_remote_codex_trust_keeps_the_native_windows_key():
    """codex's config.toml table is the host-native path on that
    machine, backslashes and all; only claude flips the separators."""
    host = _windows_remote_host()
    root = "C:\\Users\\hy\\orca-workspaces"
    target = "C:/Users/hy/.codex/config.toml"
    transport = _FakeTransport(files={target: b'model = "gpt-5.2"\n'})

    with patch("osw.hosts.anyio.run_process", transport):
        assert await ensure_workspace_trust("codex", root, host) == "trusted"
        assert await ensure_workspace_trust("codex", root, host) == (
            "already trusted"
        )

    data = tomllib.loads(transport.files[target].decode("utf-8"))
    assert data["model"] == "gpt-5.2"
    assert data["projects"][root]["trust_level"] == "trusted"
    assert "C:/Users/hy/orca-workspaces" not in data["projects"]
    assert len(transport.puts) == 1


@pytest.mark.anyio
async def test_local_host_keeps_its_keys_when_the_platform_reads_windows(
    tmp_path, monkeypatch,
):
    """This machine's platform can read "Windows" too. The local branch
    must still key claude with `Path.as_posix()` and codex with
    `str(root)`, exactly as it did before the Windows-remote fix."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "proj"
    local = Host(platform="Windows")
    assert local.is_local and local.is_windows

    assert await ensure_workspace_trust("claude", root, local) == "trusted"
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert data["projects"][root.as_posix()]["hasTrustDialogAccepted"] is True

    assert await ensure_workspace_trust("codex", root, local) == "trusted"
    with (tmp_path / ".codex" / "config.toml").open("rb") as f:
        config = tomllib.load(f)
    assert config["projects"][str(root)]["trust_level"] == "trusted"


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["claude", "codex", "kimi"])
async def test_remote_trust_keys_the_posix_root_string(provider):
    """The remote TUI looks up the path it was launched with, so every
    provider store must carry the POSIX string, never a Windows spelling
    (`str(Path('/srv/repo'))` is `\\srv\\repo` here)."""
    host = _remote_host()
    transport = _FakeTransport()

    with patch("osw.hosts.anyio.run_process", transport):
        note = await ensure_workspace_trust(provider, "/srv/repo", host)

    assert note == "trusted"
    _, payload = transport.puts[0]
    text = payload.decode("utf-8")
    assert "\\\\" not in text and "\\srv" not in text
    if provider == "claude":
        assert json.loads(text)["projects"]["/srv/repo"][
            "hasTrustDialogAccepted"
        ] is True
    elif provider == "codex":
        assert tomllib.loads(text)["projects"]["/srv/repo"][
            "trust_level"
        ] == "trusted"
    else:
        digest = hashlib.sha256(b"/srv/repo").hexdigest()[:12]
        assert any(
            path.endswith(f"/workspace-trust/wd_repo_{digest}")
            for path in transport.files
        )
        kimi_payload = json.loads(text)
        assert kimi_payload["root"] == "/srv/repo"
        assert kimi_payload["trustedAt"] > 0


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["claude", "codex", "kimi"])
async def test_remote_trust_normalizes_a_path_root_to_posix(provider):
    """A call site that still hands a `Path` for a remote root gets the
    same POSIX key, not `\\srv\\repo`."""
    host = _remote_host()
    transport = _FakeTransport()

    with patch("osw.hosts.anyio.run_process", transport):
        assert await ensure_workspace_trust(provider, Path("/srv/repo"), host) == (
            "trusted"
        )

    text = transport.puts[0][1].decode("utf-8")
    assert "\\\\" not in text and "\\srv" not in text
    digest = hashlib.sha256(b"/srv/repo").hexdigest()[:12]
    assert "/srv/repo" in text or any(
        path.endswith(f"/workspace-trust/wd_repo_{digest}")
        for path in transport.files
    )


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["claude", "codex", "kimi"])
async def test_remote_write_failure_is_reported_not_raised(provider):
    host = _remote_host()
    transport = _FakeTransport(fail_writes=True)

    with patch("osw.hosts.anyio.run_process", transport):
        note = await ensure_workspace_trust(provider, Path("/vol1/w/proj"), host)

    assert note == "trust pre-record failed"


@pytest.mark.anyio
async def test_remote_transport_failure_is_reported_not_raised():
    host = _remote_host()
    transport = _FakeTransport(
        transport_error=b"shiki@fnnas: Permission denied (publickey).",
    )

    with patch("osw.hosts.anyio.run_process", transport):
        note = await ensure_workspace_trust("claude", Path("/vol1/w/proj"), host)

    assert note == "trust pre-record failed"


# ---------------------------------------------------------------------------
# resume_command (plan 1.7): resume flag + the launch autonomy/model flags
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider,session,expected", [
    ("claude", "76348f77-8386-4307-931d-ad15d77926b3",
     "claude --dangerously-skip-permissions --resume 76348f77-8386-4307-931d-ad15d77926b3"),
    ("codex", "01a129ad-d704-78c3-9e2f-c24dbdcc519d",
     "codex resume 01a129ad-d704-78c3-9e2f-c24dbdcc519d --dangerously-bypass-approvals-and-sandbox"),
    ("pi", "01a129a9-be63-7cc2-b873-7693deb785d5",
     "pi --approve --session 01a129a9-be63-7cc2-b873-7693deb785d5"),
    ("kimi", "session_467482d2-7221-42b3-ac98-67ab7582550e",
     "kimi --yolo --session session_467482d2-7221-42b3-ac98-67ab7582550e"),
    ("grok", "01a129ad-f296-70f3-8604-69cebcaa63e7",
     "grok --always-approve --trust --resume 01a129ad-f296-70f3-8604-69cebcaa63e7"),
    ("omp", "01a129dd-0b79-7613-82a5-6054d7e8034e",
     "omp --auto-approve --resume 01a129dd-0b79-7613-82a5-6054d7e8034e"),
    # The TUI launcher takes the resume flag after its workspace target.
    ("dsh", "4024baec-099e-46b9-852b-31159b6c3513",
     "dsh-tui . --resume 4024baec-099e-46b9-852b-31159b6c3513"),
])
def test_resume_command_per_provider(provider, session, expected):
    assert resume_command(provider, session) == expected


def test_resume_command_keeps_model_and_thinking_flags():
    assert resume_command("claude", "sid", "opus", "high") == (
        "claude --dangerously-skip-permissions --model opus --effort high --resume sid"
    )
    assert resume_command("codex", "sid", "gpt-5", "high") == (
        "codex resume sid --dangerously-bypass-approvals-and-sandbox "
        "-c model_reasoning_effort=high -m gpt-5"
    )
    assert resume_command("pi", "sid", "openai/gpt-5", "low") == (
        "pi --approve --model openai/gpt-5 --thinking low --session sid"
    )
    assert resume_command("kimi", "session_x", "kimi-code/k3") == (
        "kimi --yolo --model kimi-code/k3 --session session_x"
    )
    assert resume_command("grok", "sid", "grok-4", "high") == (
        "grok --always-approve --trust --model grok-4 --reasoning-effort high --resume sid"
    )


def test_resume_command_matches_launch_command_flags():
    launch = build_provider_command("claude", "sonnet", "medium")
    assert resume_command("claude", "sid", "sonnet", "medium") == f"{launch} --resume sid"


def test_resume_command_uses_absolute_binary_and_keeps_local_output():
    """A recorded absolute provider path heads resume argv (the head of
    the same `binary=` seam `build_provider_command` uses); empty or
    absent leaves the local command byte-identical."""
    remote = "/opt/agents/claude/bin/claude"
    assert resume_command("claude", "sid", "sonnet", binary=remote) == (
        f"{remote} --dangerously-skip-permissions --model sonnet --resume sid"
    )
    # codex's resume subcommand follows the executable.
    assert resume_args("codex", "sid", binary="/usr/local/bin/codex")[:3] == [
        "/usr/local/bin/codex", "resume", "sid",
    ]
    # dsh's launcher keeps its workspace target after the executable.
    assert resume_args("dsh", "sid", binary="/opt/dsh/bin/dsh-tui") == [
        "/opt/dsh/bin/dsh-tui", ".", "--resume", "sid",
    ]
    for local in (None, "", "   "):
        assert resume_command("claude", "sid", "sonnet", binary=local) == (
            "claude --dangerously-skip-permissions --model sonnet --resume sid"
        )
        assert resume_args("codex", "sid", binary=local) == [
            "codex", "resume", "sid", "--dangerously-bypass-approvals-and-sandbox",
        ]


def test_resume_command_adds_kimi_session_prefix():
    assert resume_command("kimi", "467482d2-7221") == "kimi --yolo --session session_467482d2-7221"


def test_resume_command_rejects_empty_session():
    with pytest.raises(ProviderError):
        resume_command("claude", "  ")


def test_resume_command_rejects_unsupported_provider():
    with pytest.raises(ProviderError):
        resume_command("aider", "sid")


def test_resume_command_rejects_kimi_thinking():
    with pytest.raises(ProviderError):
        resume_command("kimi", "session_x", thinking="high")


# ---------------------------------------------------------------------------
# shell_command_line (plan 2.1): resume command typed into a live shell
# ---------------------------------------------------------------------------

def test_resume_args_are_the_resume_command_tokens():
    assert resume_args("claude", "sid", "opus") == [
        "claude", "--dangerously-skip-permissions", "--model", "opus", "--resume", "sid",
    ]
    assert resume_args("codex", "sid")[:3] == ["codex", "resume", "sid"]


@pytest.mark.parametrize("shell,expected", [
    ("powershell", "claude --model 'claude-opus-5-5[1m]' --resume 'a b'"),
    ("cmd", 'claude --model "claude-opus-5-5[1m]" --resume "a b"'),
    ("posix", "claude --model 'claude-opus-5-5[1m]' --resume 'a b'"),
])
def test_shell_command_line_quotes_per_shell(shell, expected):
    args = ["claude", "--model", "claude-opus-5-5[1m]", "--resume", "a b"]
    assert shell_command_line(args, shell) == expected


def test_shell_command_line_plain_tokens_stay_bare():
    args = ["codex", "resume", "01a1-ff", "-c", "model_reasoning_effort=high"]
    for shell in ("powershell", "cmd", "posix"):
        assert shell_command_line(args, shell) == " ".join(args)


def test_shell_command_line_powershell_escapes_quotes_and_calls_quoted_binary():
    assert shell_command_line(["my tool", "it's"], "powershell") == "& 'my tool' 'it''s'"


@pytest.mark.parametrize("bad", ['say "hi"', "100%", "two\nlines"])
def test_shell_command_line_cmd_rejects_unescapable_args(bad):
    with pytest.raises(ProviderError):
        shell_command_line(["claude", bad], "cmd")


def test_shell_command_line_rejects_newlines_and_unknown_shells():
    with pytest.raises(ProviderError):
        shell_command_line(["claude", "a\nb"], "posix")
    with pytest.raises(ProviderError):
        shell_command_line(["claude"], "fish")

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from osw.providers import (
    CODEX_EFFORTS,
    KNOWN_AGENT_CLIS,
    ProviderError,
    build_provider_command,
    codex_variants,
    ensure_workspace_trust,
    kimi_variants,
    parse_claude_aliases,
    parse_grok_models,
    parse_omp_models,
    parse_pi_models,
    probe_variants,
    resume_args,
    resume_command,
    shell_command_line,
)

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


def test_build_provider_command_rejects_unknown_provider():
    with pytest.raises(ProviderError, match="unsupported provider 'gemini'"):
        build_provider_command("gemini")


def test_build_provider_command_strips_optional_values():
    assert build_provider_command(
        "claude", model=" sonnet ", thinking="  ",
    ) == "claude --dangerously-skip-permissions --model sonnet"


# --- ensure_workspace_trust ---

def test_trust_claude_writes_and_preserves(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    conf = tmp_path / ".claude.json"
    conf.write_text(json.dumps({"numStartups": 1, "projects": {}}), encoding="utf-8")
    root = tmp_path / "proj"
    assert ensure_workspace_trust("claude", root) == "trusted"
    data = json.loads(conf.read_text(encoding="utf-8"))
    assert data["numStartups"] == 1
    assert data["projects"][root.as_posix()]["hasTrustDialogAccepted"] is True
    # idempotent
    assert ensure_workspace_trust("claude", root) == "already trusted"


def test_trust_codex_appends_toml(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    codex = tmp_path / ".codex"
    codex.mkdir()
    conf = codex / "config.toml"
    conf.write_text('model = "gpt-5.2"\n', encoding="utf-8")
    root = tmp_path / "proj"
    assert ensure_workspace_trust("codex", root) == "trusted"
    with conf.open("rb") as f:
        data = tomllib.load(f)
    assert data["model"] == "gpt-5.2"
    assert data["projects"][str(root)]["trust_level"] == "trusted"
    # idempotent
    assert ensure_workspace_trust("codex", root) == "already trusted"


def test_trust_kimi_writes_hash_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "proj"
    assert ensure_workspace_trust("kimi", root) == "trusted"
    digest = hashlib.sha256(root.as_posix().encode()).hexdigest()[:12]
    trust_file = tmp_path / ".kimi-code" / "workspace-trust" / f"wd_proj_{digest}"
    payload = json.loads(trust_file.read_text(encoding="utf-8"))
    assert payload["root"] == root.as_posix()
    assert payload["trustedAt"] > 0
    # idempotent
    assert ensure_workspace_trust("kimi", root) == "already trusted"


def test_trust_unknown_provider_is_noop(tmp_path):
    assert ensure_workspace_trust("pi", tmp_path) == ""


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

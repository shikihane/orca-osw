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
    parse_omp_models,
    parse_pi_models,
    probe_variants,
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

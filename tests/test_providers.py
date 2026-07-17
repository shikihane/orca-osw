from __future__ import annotations

import pytest

from osw.providers import (
    CODEX_EFFORTS,
    ProviderError,
    build_provider_command,
    codex_variants,
    kimi_variants,
    parse_claude_aliases,
    parse_pi_models,
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


def test_build_provider_command_rejects_unknown_provider():
    with pytest.raises(ProviderError, match="unsupported provider 'gemini'"):
        build_provider_command("gemini")


def test_build_provider_command_strips_optional_values():
    assert build_provider_command(
        "claude", model=" sonnet ", thinking="  ",
    ) == "claude --dangerously-skip-permissions --model sonnet"

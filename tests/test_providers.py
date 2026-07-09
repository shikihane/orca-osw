from __future__ import annotations

import pytest

from osw.providers import (
    CODEX_EFFORTS,
    ProviderError,
    build_launch_command,
    build_provider_command,
    codex_variants,
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


def test_build_launch_command_for_claude():
    command = build_launch_command(
        "claude",
        "do the task",
        model="sonnet",
        thinking="high",
    )

    assert command == 'claude --model sonnet --effort high "do the task"'


def test_build_provider_command_for_claude():
    command = build_provider_command(
        "claude",
        model="sonnet",
        thinking="high",
    )

    assert command == "claude --model sonnet --effort high"


def test_build_launch_command_for_codex():
    command = build_launch_command(
        "codex",
        "do the task",
        model="gpt-5",
        thinking="medium",
    )

    assert command == 'codex -c model_reasoning_effort=medium -m gpt-5 "do the task"'


def test_build_launch_command_for_pi():
    command = build_launch_command(
        "pi",
        "do the task",
        model="deepseek/deepseek-v4-pro",
        thinking="low",
    )

    assert command == 'pi --model deepseek/deepseek-v4-pro --thinking low "do the task"'


def test_build_launch_command_omits_optional_flags():
    assert build_launch_command("claude", "do the task") == 'claude "do the task"'
    assert build_launch_command("codex", "do the task") == 'codex "do the task"'
    assert build_launch_command("pi", "do the task") == 'pi "do the task"'


def test_build_launch_command_rejects_unknown_provider():
    with pytest.raises(ProviderError, match="unsupported provider 'gemini'"):
        build_launch_command("gemini", "do the task")

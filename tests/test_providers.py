from __future__ import annotations

from osw.providers import parse_claude_aliases, parse_pi_models

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

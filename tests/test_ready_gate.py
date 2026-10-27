"""Deterministic readiness gate and frame hygiene (design §5.0 / §5.7)."""

from __future__ import annotations

import json
from pathlib import Path

from osw.ready_gate import (
    gate_verdict,
    jev_payload,
    normalized_hash,
    sanitize_frame,
)

FIXTURES = Path(__file__).parent / "fixtures" / "frames" / "claude"
DSH_FIXTURES = Path(__file__).parent / "fixtures" / "frames" / "dsh"


def _dsh_fixture(name: str) -> dict:
    data = json.loads(
        (DSH_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return {"result": {"terminal": {
        "source": data["source"], "tail": data["tail"],
    }}}


def _fixture(name: str) -> dict:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return {"result": {"terminal": {
        "source": data["source"], "tail": data["tail"],
    }}}


def test_sanitize_repairs_gbk_mojibake():
    assert sanitize_frame(["鉂\udcaf", "鈹\udc80" * 3]) == ["❯", "───"]


def test_sanitize_replaces_unrepairable_surrogate():
    assert sanitize_frame(["ab\udcafc"]) == ["ab\ufffdc"]


def test_sanitize_keeps_clean_lines_identical():
    data = json.loads(
        (FIXTURES / "idle_composer.json").read_text(encoding="utf-8"))
    assert sanitize_frame(data["tail"]) == data["tail"]


def test_sanitize_strips_controls():
    assert sanitize_frame(["\x1b[0m❯\x07"]) == ["[0m❯"]


def test_payload_drops_furniture_and_caps():
    long_line = "x" * 100
    payload = jev_payload(
        ["─" * 40, "   ", "", long_line] + [long_line] * 300, max_bytes=8192)
    assert "─" not in payload
    assert len(payload.encode("utf-8")) <= 8192
    assert payload.endswith(long_line)


def test_gate_on_real_fixtures():
    assert gate_verdict("claude", _fixture("startup_unavailable")) == \
        ("abstain", "no_usable_frame")
    assert gate_verdict("claude", _fixture("idle_composer")) == \
        ("ready", "composer_visible")
    assert gate_verdict("claude", _fixture("midturn_spinner_with_composer")) == \
        ("not_ready", "turn_running")


def test_gate_ignores_status_bar_marker_on_midturn_fixture():
    frame = _fixture("midturn_spinner_with_composer")
    tail = frame["result"]["terminal"]["tail"]
    assert any("claude-" in str(line) for line in tail)
    assert gate_verdict("claude", frame)[0] == "not_ready"


def test_gate_abstains_for_unpredicated_provider_and_bad_source():
    frame = {"result": {"terminal": {"source": "screen", "tail": ["❯"]}}}
    assert gate_verdict("grok", frame) == \
        ("abstain", "provider_without_predicate")
    stream = {"result": {"terminal": {"source": "stream", "tail": ["❯"]}}}
    assert gate_verdict("claude", stream) == ("abstain", "no_usable_frame")
    empty = {"result": {"terminal": {"source": "screen", "tail": []}}}
    assert gate_verdict("claude", empty) == ("abstain", "no_usable_frame")


def test_gate_on_real_dsh_fixtures():
    # Captured from DSH-TUI 0.17.0 (dsh 0.2.1-rc.2) in an Orca pane.
    assert gate_verdict("dsh", _dsh_fixture("launchpad_composer")) == \
        ("ready", "composer_visible")
    assert gate_verdict("dsh", _dsh_fixture("idle_composer")) == \
        ("ready", "composer_visible")


def test_gate_dsh_needs_the_composer_box_not_just_the_glyph():
    # The whale title, status bar and glyph-free footer are up before the
    # composer mounts; a `❯` outside a box is not DSH's composer.
    frame = {"result": {"terminal": {"source": "screen", "tail": [
        "        ✦ 🐋 DSH",
        "❯ not a composer",
        "  deepseek-flash · high · 完全访问",
    ]}}}
    assert gate_verdict("dsh", frame) == ("not_ready", "composer_missing")


def test_gate_dsh_boot_notice_outranks_the_composer():
    frame = {"result": {"terminal": {"source": "screen", "tail": [
        "                     ╭──────────────────────────────╮",
        "                     │ ❯ 说点什么，或输入 / 看命令… │",
        "                     ╰──────────────────────────────╯",
        "                                                  ● 正在启动 DSH…",
    ]}}}
    assert gate_verdict("dsh", frame) == ("not_ready", "harness_starting")


def test_gate_flags_upstream_api_retry_for_any_provider():
    frame = {"result": {"terminal": {"source": "screen", "tail": [
        "❯ ",
        "  Waiting for API response · will retry · check your network",
    ]}}}
    assert gate_verdict("claude", frame) == ("api_retry", "upstream_api_retrying")
    assert gate_verdict("pi", frame) == ("api_retry", "upstream_api_retrying")


def test_normalized_hash_ignores_spinner_and_counter_churn():
    a = ["✢ Proofing… (2m 55s · ↓ 4.1k tokens)", "❯"]
    b = ["✽ Proofing… (2m 57s · ↓ 4.2k tokens)", "❯"]
    assert normalized_hash(a) == normalized_hash(b)
    assert normalized_hash(a) != normalized_hash(["done", "❯"])

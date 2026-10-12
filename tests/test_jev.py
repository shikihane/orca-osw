"""Jev module: config/status helpers, raw store, fail-open classification."""

from __future__ import annotations

import json
import os

from osw import state
from osw.jev import (
    emit_jev_degraded,
    enforce_active,
    ensure_proxy_env,
    masked_key,
    notify_class,
    resolve_enforce_providers,
    resolve_mode,
    startup_should_hold,
    write_raw,
)
from osw.log import read_events


def test_mode_is_the_global_switch(tmp_path, monkeypatch):
    state.init_state_dir(tmp_path)
    assert resolve_mode(tmp_path) == "off"  # unset switch -> off
    assert resolve_enforce_providers(tmp_path) == ()

    monkeypatch.setenv("OSW_JEV_MODE", "enforce")
    assert resolve_mode(tmp_path) == "enforce"
    assert resolve_enforce_providers(tmp_path) == ("claude", "omp")
    assert enforce_active(tmp_path, "claude") is True
    assert enforce_active(tmp_path, "omp") is True
    assert enforce_active(tmp_path, "unknown") is False

    monkeypatch.setenv("OSW_JEV_MODE", "shadow")
    assert resolve_mode(tmp_path) == "shadow"
    assert enforce_active(tmp_path, "claude") is False
    assert resolve_enforce_providers(tmp_path) == ()

    monkeypatch.setenv("OSW_JEV_MODE", "bogus")
    assert resolve_mode(tmp_path) == "off"  # invalid -> off


def test_raw_store_survives_lone_surrogate(tmp_path):
    state.init_state_dir(tmp_path)
    payload = {"state": {"screen": "x\udcaf"}, "mode": "shadow"}
    path = write_raw(tmp_path, "agent_001", "ready", payload)
    assert path.is_file()
    on_disk = path.read_text(encoding="utf-8")
    assert "\\udcaf" in on_disk          # ensure_ascii escape, valid JSON
    assert json.loads(on_disk)["state"]["screen"] == "x\udcaf"


def test_jev_events_carry_no_frame_text(tmp_path):
    state.init_state_dir(tmp_path)
    emit_jev_degraded(tmp_path, "agent_001", "shadow", "no_key", "ready")
    events = read_events(state.logs_dir(tmp_path))
    text = json.dumps(events, ensure_ascii=False)
    assert "jev_degraded" in text
    assert "no_key" in text
    assert "screen" not in text
    assert "❯" not in text


def test_threshold_helpers():
    assert startup_should_hold({"probabilities": {"blocked_startup": 0.9}})
    assert not startup_should_hold({"probabilities": {"blocked_startup": 0.3}})
    assert not startup_should_hold({})
    assert notify_class({"probabilities": {"blocked_dialog": 0.9}}) == \
        ("blocked_dialog", 0.9)
    assert notify_class({"probabilities": {"shell_prompt": 0.85}}) == \
        ("shell_prompt", 0.85)
    assert notify_class({"probabilities": {"blocked_dialog": 0.2}}) is None


def test_masked_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert masked_key() == "(absent)"
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-test-1234567890abcdef")
    masked = masked_key()
    assert masked.startswith("sk-tes")
    assert "\u2026" in masked


# ---------------------------------------------------------------------------
# Judgment calls with a mocked client (blocker_kind, plan \u00a72.2; ready_state)
# ---------------------------------------------------------------------------

from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest

from osw import jev


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _frame(lines):
    return {"result": {"terminal": {"source": "screen", "tail": list(lines)}}}


def _response(question_id, choice, probabilities):
    return SimpleNamespace(
        choices={question_id: SimpleNamespace(
            choice=choice, probabilities=probabilities, confidence=0.91)},
        usage=SimpleNamespace(input_tokens=321),
        model="jev-1.13.0",
    )


class _FakeClient:
    """Async-context client whose `system_one` is an AsyncMock."""

    def __init__(self, system_one):
        self.system_one = system_one

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _mock_client(monkeypatch, system_one):
    client = _FakeClient(system_one)
    monkeypatch.setattr(jev, "_build_client", lambda: client)
    return client


def _jev_events(tmp_path):
    return read_events(state.logs_dir(tmp_path))


def test_blocker_question_options_follow_ready_state_ordering():
    kinds = list(jev.BLOCKER_CRITERIA)
    assert set(kinds) == {
        "update_prompt", "trust_folder", "auth", "permission",
        "announcement", "rate_limit", "crashed_to_shell", "other",
    }
    assert tuple(kinds) == jev.BLOCKER_KINDS
    # like ready_state: the option that leads to an action is never first
    # (the model leans toward the first option); `other` sits right before
    # it, as it does before `ready_composer`
    assert kinds[-1] == "update_prompt"
    assert kinds[-2] == "other"
    assert list(jev.READY_CRITERIA)[-2:] == ["other", "ready_composer"]


def test_blocker_question_is_a_choice_over_the_criteria():
    question = jev._build_blocker_question()
    from typesafe_sdk import Choice

    assert isinstance(question, Choice)
    assert dict(question.criteria) == dict(jev.BLOCKER_CRITERIA)
    assert question.instructions == jev.BLOCKER_INSTRUCTIONS


@pytest.mark.anyio
async def test_ask_blocker_returns_answer_and_stores_raw_record(tmp_path, monkeypatch):
    state.init_state_dir(tmp_path)
    system_one = AsyncMock(return_value=_response(
        jev.BLOCKER_QUESTION_ID, "update_prompt",
        {"update_prompt": 0.93, "other": 0.04}))
    _mock_client(monkeypatch, system_one)

    answer = await jev.ask_blocker(
        tmp_path, "agent_001", "codex",
        _frame(["\u2728 Update available! 0.153.4 -> 0.162.1", "1. Update now", "2. Skip"]),
        "enforce")

    assert answer == {
        "choice": "update_prompt",
        "probabilities": {"update_prompt": 0.93, "other": 0.04},
        "confidence": 0.91,
    }
    kwargs = system_one.await_args.kwargs
    assert list(kwargs["questions"]) == [jev.BLOCKER_QUESTION_ID]
    assert kwargs["state"]["provider"] == "codex"
    assert "Update available" in kwargs["state"]["screen"]
    raws = list(jev.jev_dir(tmp_path).glob("agent_001_blocker_*.json"))
    assert len(raws) == 1
    record = json.loads(raws[0].read_text(encoding="utf-8"))
    assert record["response"]["choice"] == "update_prompt"
    assert record["mode"] == "enforce"
    calls = [e for e in _jev_events(tmp_path) if e["event"] == "jev_call"]
    assert calls[-1]["data"]["point"] == "blocker"
    assert calls[-1]["data"]["verdict"] == "update_prompt"
    assert "Update available" not in json.dumps(calls, ensure_ascii=False)


@pytest.mark.anyio
async def test_ask_blocker_fails_open_without_key(tmp_path, monkeypatch):
    state.init_state_dir(tmp_path)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    answer = await jev.ask_blocker(
        tmp_path, "agent_001", "codex", _frame(["x"]), "shadow")

    assert answer is None
    degraded = [e for e in _jev_events(tmp_path) if e["event"] == "jev_degraded"]
    assert degraded[-1]["data"] == {
        "reason": "no_key", "point": "blocker", "mode": "shadow"}


@pytest.mark.anyio
@pytest.mark.parametrize("error,reason", [
    (RuntimeError("HTTP 429 Too Many Requests"), "rate_limited"),
    (RuntimeError("boom"), "api_error"),
    (TimeoutError(), "timeout"),
])
async def test_ask_blocker_fails_open_on_call_errors(tmp_path, monkeypatch, error, reason):
    state.init_state_dir(tmp_path)
    _mock_client(monkeypatch, AsyncMock(side_effect=error))

    answer = await jev.ask_blocker(
        tmp_path, "agent_001", "codex", _frame(["x"]), "enforce")

    assert answer is None
    degraded = [e for e in _jev_events(tmp_path) if e["event"] == "jev_degraded"]
    assert degraded[-1]["data"]["reason"] == reason
    assert degraded[-1]["data"]["point"] == "blocker"


@pytest.mark.anyio
async def test_ask_blocker_without_frame_degrades(tmp_path, monkeypatch):
    state.init_state_dir(tmp_path)
    system_one = AsyncMock()
    _mock_client(monkeypatch, system_one)

    answer = await jev.ask_blocker(
        tmp_path, "agent_001", "codex", {"result": {"terminal": {}}}, "enforce")

    assert answer is None
    system_one.assert_not_awaited()


@pytest.mark.anyio
async def test_ask_ready_keeps_its_contract_with_mocked_client(tmp_path, monkeypatch):
    state.init_state_dir(tmp_path)
    system_one = AsyncMock(return_value=_response(
        jev.READY_QUESTION_ID, "ready_composer",
        {"ready_composer": 0.8, "blocked_dialog": 0.1}))
    _mock_client(monkeypatch, system_one)

    answer = await jev.ask_ready(
        tmp_path, "agent_001", "claude", _frame(["\u276f"]), "shadow", "abstain")

    assert answer["choice"] == "ready_composer"
    assert list(system_one.await_args.kwargs["questions"]) == [jev.READY_QUESTION_ID]
    raws = list(jev.jev_dir(tmp_path).glob("agent_001_ready_*.json"))
    assert json.loads(raws[0].read_text(encoding="utf-8"))["gate_verdict"] == "abstain"
    call = [e for e in _jev_events(tmp_path) if e["event"] == "jev_call"][-1]
    assert call["data"]["point"] == "ready"
    assert call["data"]["p_dialog"] == 0.1

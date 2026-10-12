"""Blocker discovery, whitelist, decision and update lock (plan §2.2)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from osw import blockers
from osw.blockers import (
    FORBIDDEN_KINDS,
    KINDS,
    WHITELIST,
    Detection,
    acquire_update_lock,
    classify,
    decide,
    detect,
    release_update_lock,
    update_lock_path,
)

FRAMES = Path(__file__).parent / "fixtures" / "frames"


def _fixture_text(provider: str, kind: str) -> str:
    data = json.loads(
        (FRAMES / provider / f"{kind}.json").read_text(encoding="utf-8"))
    return "\n".join(data["tail"])


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent_wait", [
    {"reason": "agent-update-prompt"},
    "agent-update-prompt",
])
def test_detect_prefers_orca_agent_wait_reason(agent_wait):
    detection = detect(
        {"agentWait": agent_wait},
        {"probabilities": {"blocked_startup": 0.95}},
    )

    assert detection == Detection(
        source="agent_wait", kind="update_prompt",
        signal="agent-update-prompt", p=None)


def test_detect_unknown_agent_wait_reason_leaves_kind_open():
    detection = detect({"agentWait": {"reason": "agent-something-new"}}, None)

    assert detection == Detection(
        source="agent_wait", kind="", signal="agent-something-new", p=None)


@pytest.mark.parametrize("probabilities,signal,p", [
    ({"blocked_dialog": 0.9, "blocked_startup": 0.95}, "blocked_dialog", 0.9),
    ({"blocked_startup": 0.75}, "blocked_startup", 0.75),
])
def test_detect_falls_back_to_ready_state(probabilities, signal, p):
    detection = detect({"agentWait": None}, {"probabilities": probabilities})

    assert detection == Detection(
        source="ready_state", kind="", signal=signal, p=p)


@pytest.mark.parametrize("observation,answer", [
    ({}, {"probabilities": {"blocked_dialog": 0.5, "blocked_startup": 0.5}}),
    ({}, {"probabilities": {"ready_composer": 0.99}}),
    ({"agentWait": ""}, None),
    (None, None),
    ({}, {"probabilities": {"blocked_dialog": "garbage"}}),
])
def test_detect_reports_nothing_without_a_signal(observation, answer):
    assert detect(observation, answer) is None


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

def test_classify_keeps_a_kind_orca_already_named():
    detection = Detection("agent_wait", "update_prompt", "agent-update-prompt", None)

    assert classify(detection, {"choice": "auth",
                                "probabilities": {"auth": 0.99}}) == "update_prompt"


@pytest.mark.parametrize("answer,expected", [
    ({"choice": "trust_folder", "probabilities": {"trust_folder": 0.9}}, "trust_folder"),
    ({"choice": "update_prompt", "probabilities": {"update_prompt": 0.6}}, "other"),
    ({"choice": "nonsense", "probabilities": {"nonsense": 0.99}}, "other"),
    ({"choice": "auth"}, "other"),
    (None, "other"),
])
def test_classify_uses_a_confident_blocker_kind_answer(answer, expected):
    detection = Detection("ready_state", "", "blocked_dialog", 0.9)

    assert classify(detection, answer) == expected


# ---------------------------------------------------------------------------
# whitelist, forbidden kinds, decide
# ---------------------------------------------------------------------------

def test_kinds_match_the_jev_question():
    from osw.jev import BLOCKER_KINDS

    assert set(KINDS) == set(BLOCKER_KINDS)


def test_whitelist_holds_only_the_sampled_codex_update_prompt():
    assert WHITELIST == {("codex", "update_prompt"): ("enter",)}


def test_every_whitelist_entry_has_a_frame_sample():
    for provider, kind in WHITELIST:
        sample = FRAMES / provider / f"{kind}.json"
        assert sample.is_file(), sample
        data = json.loads(sample.read_text(encoding="utf-8"))
        assert data["provider"] == provider
        assert data["label"] == kind
        assert not blockers.forbidden(kind, "\n".join(data["tail"]))


def test_codex_update_prompt_sample_is_the_recorded_dialog():
    data = json.loads(
        (FRAMES / "codex" / "update_prompt.json").read_text(encoding="utf-8"))
    text = "\n".join(data["tail"])

    assert "Update available!" in text
    assert "1. Update now" in text
    assert "2. Skip" in text
    assert data["capture_path"] == "transcribed"
    assert data["default_option"] == "1. Update now"
    assert data["transcribed_from"].endswith(
        "2026-10-11-v2-v3-v5-session-mapping.md")
    readme = (FRAMES / "README.md").read_text(encoding="utf-8")
    assert "codex/update_prompt" in readme
    assert "文字转录、非原始抓帧" in readme


def test_forbidden_kinds_are_never_whitelisted():
    assert FORBIDDEN_KINDS == frozenset({"auth", "permission"})
    assert not any(kind in FORBIDDEN_KINDS for _, kind in WHITELIST)


@pytest.mark.parametrize("mode,expected", [
    ("off", "notify"), ("shadow", "shadow"), ("enforce", "act"),
])
def test_decide_whitelisted_entry_follows_the_mode(mode, expected):
    text = _fixture_text("codex", "update_prompt")

    assert decide("codex", "update_prompt", mode, text) == expected
    assert decide("codex", "update_prompt", mode) == expected


@pytest.mark.parametrize("provider,kind", [
    ("claude", "update_prompt"),     # no sample for claude's dialog
    ("codex", "trust_folder"),
    ("codex", "other"),
    ("codex", "bogus"),
])
def test_decide_outside_the_whitelist_only_notifies(provider, kind):
    for mode in ("off", "shadow", "enforce"):
        assert decide(provider, kind, mode) == "notify"


@pytest.mark.parametrize("kind", ["auth", "permission"])
def test_decide_forbidden_kind_only_notifies_even_if_whitelisted(monkeypatch, kind):
    monkeypatch.setitem(blockers.WHITELIST, ("codex", kind), ("enter",))

    assert decide("codex", kind, "enforce") == "notify"
    assert decide("codex", kind, "shadow") == "notify"


@pytest.mark.parametrize("text", [
    "Delete all local sessions? 1. Yes 2. No",
    "Remove the workspace and its history",
    "Upgrade to Pro: $20/month — enter payment details",
    "Your free credits ran out. Purchase more credits to continue",
    "确认删除该会话？",
    "继续使用需要付费订阅",
])
def test_decide_never_acts_on_delete_or_payment_screens(text):
    assert blockers.forbidden("update_prompt", text)
    assert decide("codex", "update_prompt", "enforce", text) == "notify"


def test_decide_rejects_an_unknown_mode():
    assert decide("codex", "update_prompt", "bogus") == "notify"


# ---------------------------------------------------------------------------
# machine-wide update lock
# ---------------------------------------------------------------------------

def test_update_lock_allows_one_holder_per_provider(tmp_path):
    assert acquire_update_lock("codex", "agent_001", directory=tmp_path) is True
    assert acquire_update_lock("codex", "agent_002", directory=tmp_path) is False
    # another provider is independent
    assert acquire_update_lock("claude", "agent_002", directory=tmp_path) is True

    assert release_update_lock("codex", "agent_001", directory=tmp_path) is True
    assert not update_lock_path("codex", tmp_path).exists()
    assert acquire_update_lock("codex", "agent_002", directory=tmp_path) is True


def test_update_lock_records_owner_and_expiry(tmp_path):
    before = time.time()
    acquire_update_lock("codex", "agent_001", directory=tmp_path, ttl_secs=60)

    data = json.loads(update_lock_path("codex", tmp_path).read_text(encoding="utf-8"))
    assert data["owner"] == "agent_001"
    assert data["pid"] == os.getpid()
    assert before + 59 <= data["expires_at"] <= time.time() + 61


def test_release_by_a_non_holder_keeps_the_lock(tmp_path):
    acquire_update_lock("codex", "agent_001", directory=tmp_path)

    assert release_update_lock("codex", "agent_002", directory=tmp_path) is False
    assert update_lock_path("codex", tmp_path).exists()
    assert release_update_lock("pi", "agent_001", directory=tmp_path) is False


def test_expired_lock_is_taken_over(tmp_path):
    """A holder that crashed never releases: its lock must not dead-lock."""
    path = update_lock_path("codex", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "owner": "crashed", "pid": 999999,
        "acquired_at": time.time() - 7200, "expires_at": time.time() - 3600,
    }), encoding="utf-8")

    assert acquire_update_lock("codex", "agent_001", directory=tmp_path) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["owner"] == "agent_001"
    assert list(tmp_path.glob("*stale*")) == []


def test_unreadable_lock_is_taken_over_only_after_the_ttl(tmp_path):
    path = update_lock_path("codex", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert acquire_update_lock(
        "codex", "agent_001", directory=tmp_path, ttl_secs=600) is False
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert acquire_update_lock(
        "codex", "agent_001", directory=tmp_path, ttl_secs=600) is True


def test_update_lock_is_exclusive_under_contention(tmp_path):
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def contend(i):
        barrier.wait()
        results.append(acquire_update_lock("codex", f"agent_{i:03d}",
                                           directory=tmp_path))

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1


def test_update_lock_default_directory_is_user_level(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    path = update_lock_path("codex")

    assert path == tmp_path / "osw" / "locks" / "update-codex.lock"
    assert acquire_update_lock("codex", "agent_001") is True
    assert path.exists()
    assert release_update_lock("codex", "agent_001") is True

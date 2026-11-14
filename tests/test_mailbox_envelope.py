"""Unit tests for the mailbox envelope and the router identity."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone

import pytest

from osw.mailbox import envelope as env_mod
from osw.mailbox import identity, paths
from osw.mailbox.errors import MailboxError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def minimal(**overrides) -> dict:
    raw = {
        "from": {"role": "human", "id": "terminal:term_abc"},
        "to": {"role": "router"},
        "kind": "request",
        "body": {"text": "把 watcher 的超时改成可配置"},
    }
    raw.update(overrides)
    return raw


def envelope(**overrides) -> dict:
    return env_mod.validate_envelope(minimal(**overrides))


def code_of(exc: MailboxError) -> str:
    return exc.code


def js_bytes(value) -> int:
    """The serialized size callers actually see (``ensure_ascii=False``)."""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


# ---------------------------------------------------------------------------
# Canonical shape
# ---------------------------------------------------------------------------

def test_validate_fills_every_canonical_field():
    env = envelope()

    assert list(env) == list(env_mod.CANONICAL_KEYS)
    assert env["v"] == 1
    assert re.match(r"^msg_\d{8}T\d{6}\d{3}Z_[0-9a-f]{6}$", env["id"])
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$", env["ts"])
    assert env["from"] == {
        "role": "human", "id": "terminal:term_abc", "trust": "trusted"}
    assert env["to"] == {"role": "router", "id": ""}
    assert env["kind"] == "request"
    assert env["correlation"] == {
        "ticket": "", "agent": "", "dispatch": "", "ask": "", "decision": ""}
    assert env["thread_key"] == ""
    assert env["reply_to"] == ""
    assert env["dedupe_key"] == ""
    assert env["priority"] == 0
    assert env["ttl_s"] == 0
    assert env["body"] == {"text": "把 watcher 的超时改成可配置"}
    assert env["state"] == "queued"
    assert env["attempts"] == 0
    assert env["lease"] is None
    assert env["trace"] == ""


def test_validate_is_idempotent():
    once = envelope(thread_key="chat:main", priority=2, ttl_s=60)
    twice = env_mod.validate_envelope(once)

    assert twice == once
    assert list(twice) == list(env_mod.CANONICAL_KEYS)


def test_validate_keeps_fast_path_annotations():
    raw = envelope(intent="answer", attention="human")

    again = env_mod.validate_envelope(raw)

    assert again["intent"] == "answer"
    assert again["attention"] == "human"


def test_validate_keeps_explicit_values():
    env = envelope(
        id="msg_20260101T000000000Z_abcdef",
        ts="2026-01-01T00:00:00.000Z",
        priority=2,
        ttl_s=30,
        state="queued",
        attempts=0,
        trace="router",
        dedupe_key="orca-msg-1",
        reply_to="msg_1",
        correlation={"ticket": "code_001", "agent": "code_001"},
    )

    assert env["id"] == "msg_20260101T000000000Z_abcdef"
    assert env["ts"] == "2026-01-01T00:00:00.000Z"
    assert env["priority"] == 2
    assert env["ttl_s"] == 30
    assert env["correlation"]["ticket"] == "code_001"
    assert env["correlation"]["agent"] == "code_001"
    assert env["correlation"]["dispatch"] == ""
    assert env["dedupe_key"] == "orca-msg-1"
    assert env["reply_to"] == "msg_1"


def test_correlation_drops_unknown_keys():
    env = envelope(correlation={"ticket": "t1", "session": "s1"})

    assert env["correlation"] == {
        "ticket": "t1", "agent": "", "dispatch": "", "ask": "", "decision": ""}


def test_timestamp_is_normalized():
    env = envelope(ts="2026-11-14T13:15:00+02:00")

    assert env["ts"] == "2026-11-14T11:15:00.000Z"


# ---------------------------------------------------------------------------
# new_id
# ---------------------------------------------------------------------------

def test_new_id_format_is_deterministic_in_its_stamp():
    moment = datetime(2026, 11, 14, 13, 15, 0, 123000, tzinfo=timezone.utc)

    ident = env_mod.new_id(now=moment)

    assert ident.startswith("msg_20261114T131500123Z_")
    assert re.match(r"^msg_20261114T131500123Z_[0-9a-f]{6}$", ident)


def test_new_id_uses_the_prefix_and_stays_unique():
    ids = {env_mod.new_id("dec") for _ in range(100)}

    assert len(ids) == 100
    assert all(re.match(r"^dec_\d{8}T\d{6}\d{3}Z_[0-9a-f]{6}$", i) for i in ids)


def test_new_id_reads_a_naive_now_as_utc():
    moment = datetime(2026, 11, 14, 13, 15, 0, 123000)

    assert env_mod.new_id(now=moment).startswith("msg_20261114T131500123Z_")


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ({"from": {"role": "human"}, "kind": "request"}, 0),
    ({"from": {"role": "human"}, "kind": "notice"}, 0),
    ({"from": {"role": "worker"}, "kind": "ask"}, 1),
    ({"from": {"role": "watcher"}, "kind": "blocked"}, 1),
    ({"from": {"role": "worker"}, "kind": "report"}, 2),
    ({"from": {"role": "worker"}, "kind": "request"}, 3),
    ({"from": {"role": "system"}, "kind": "notice"}, 3),
    ({"from": {"role": "timer"}, "kind": "timer"}, 3),
    ({}, 3),
])
def test_default_priority_floor(raw, expected):
    assert env_mod.default_priority(raw) == expected


def test_priority_defaults_are_applied_per_message():
    assert envelope(priority=None)["priority"] == 0
    assert envelope(**{"from": {"role": "worker", "id": "code_001"},
                       "kind": "ask"})["priority"] == 1
    assert envelope(**{"from": {"role": "worker"}, "kind": "report"})["priority"] == 2


@pytest.mark.parametrize("value", [4, -1, 99])
def test_priority_outside_range_is_rejected(value):
    with pytest.raises(MailboxError) as exc:
        envelope(priority=value)

    assert code_of(exc.value) == "invalid_priority"


@pytest.mark.parametrize("value", ["2", 2.0, True, [2]])
def test_priority_wrong_type_is_rejected(value):
    with pytest.raises(MailboxError) as exc:
        envelope(priority=value)

    assert code_of(exc.value) == "invalid_priority"


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role, expected", [
    ("human", "trusted"),
    ("watcher", "trusted"),
    ("system", "trusted"),
    ("router", "trusted"),
    ("timer", "trusted"),
    ("worker", "untrusted"),
])
def test_trust_for_role(role, expected):
    assert env_mod.trust_for(role) == expected


def test_trust_for_unknown_role_is_typed():
    with pytest.raises(MailboxError) as exc:
        env_mod.trust_for("boss")

    assert code_of(exc.value) == "unknown_role"


def test_worker_cannot_claim_trust():
    env = envelope(**{"from": {"role": "worker", "id": "code_001",
                               "trust": "trusted"}})

    assert env["from"]["trust"] == "untrusted"


def test_worker_can_stay_untrusted():
    env = envelope(**{"from": {"role": "worker", "trust": "untrusted"}})

    assert env["from"]["trust"] == "untrusted"


def test_human_may_ask_for_stricter_trust():
    env = envelope(**{"from": {"role": "human", "id": "terminal:term_abc",
                               "trust": "untrusted"}})

    assert env["from"]["trust"] == "untrusted"


def test_unknown_trust_word_falls_back_to_the_role_floor():
    trusted = envelope(**{"from": {"role": "system", "trust": "Trusted!"}})
    worker = envelope(**{"from": {"role": "worker", "trust": "Trusted"}})

    assert trusted["from"]["trust"] == "trusted"
    assert worker["from"]["trust"] == "untrusted"


def test_non_string_trust_is_rejected():
    with pytest.raises(MailboxError) as exc:
        envelope(**{"from": {"role": "human", "trust": True}})

    assert code_of(exc.value) == "invalid_envelope"


# ---------------------------------------------------------------------------
# Roles, kinds, state
# ---------------------------------------------------------------------------

def test_unknown_from_role_is_typed():
    with pytest.raises(MailboxError) as exc:
        envelope(**{"from": {"role": "boss"}})

    assert code_of(exc.value) == "unknown_role"


def test_unknown_to_role_is_typed():
    with pytest.raises(MailboxError) as exc:
        envelope(**{"to": {"role": "oracle"}})

    assert code_of(exc.value) == "unknown_role"


def test_endpoints_must_be_objects():
    for raw in ({"from": "human"}, {"to": ["router"]},
                {"from": {"id": "x"}}):
        with pytest.raises(MailboxError) as exc:
            envelope(**raw)
        assert code_of(exc.value) == "invalid_envelope"


def test_unknown_kind_is_typed():
    with pytest.raises(MailboxError) as exc:
        envelope(kind="gossip")

    assert code_of(exc.value) == "unknown_kind"


def test_missing_kind_is_an_invalid_envelope():
    raw = minimal()
    del raw["kind"]

    with pytest.raises(MailboxError) as exc:
        env_mod.validate_envelope(raw)

    assert code_of(exc.value) == "invalid_envelope"


def test_non_object_envelope_is_rejected():
    with pytest.raises(MailboxError) as exc:
        env_mod.validate_envelope(["not", "an", "envelope"])

    assert code_of(exc.value) == "invalid_envelope"


def test_unknown_version_is_rejected():
    with pytest.raises(MailboxError) as exc:
        envelope(v=2)

    assert code_of(exc.value) == "invalid_envelope"


@pytest.mark.parametrize("raw", [
    {"id": ""},
    {"id": 7},
    {"ts": "yesterday"},
    {"ts": 1234},
    {"thread_key": {"a": 1}},
    {"attempts": -1},
    {"attempts": "2"},
    {"lease": "owner"},
    {"state": ""},
])
def test_wrong_field_types_are_invalid_envelopes(raw):
    with pytest.raises(MailboxError) as exc:
        envelope(**raw)

    assert code_of(exc.value) == "invalid_envelope"


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------

def test_ttl_defaults_to_zero_and_is_kept():
    assert envelope()["ttl_s"] == 0
    assert envelope(ttl_s=120)["ttl_s"] == 120


@pytest.mark.parametrize("value", [-1, -3600, "60", 1.5, True])
def test_bad_ttl_is_an_invalid_field(value):
    with pytest.raises(MailboxError) as exc:
        envelope(ttl_s=value)

    assert code_of(exc.value) == "invalid_field"


# ---------------------------------------------------------------------------
# Body and its 16 KiB cap
# ---------------------------------------------------------------------------

def test_body_defaults_to_an_empty_object():
    raw = minimal()
    del raw["body"]

    assert env_mod.validate_envelope(raw)["body"] == {}


def test_body_cap_is_measured_on_json_utf8():
    prefix = len(json.dumps({"t": ""}, ensure_ascii=False).encode("utf-8"))
    at_cap = {"t": "x" * (env_mod.MAX_BODY_BYTES - prefix)}

    assert js_bytes(at_cap) == env_mod.MAX_BODY_BYTES
    assert envelope(body=at_cap)["body"] == at_cap

    with pytest.raises(MailboxError) as exc:
        envelope(body={"t": at_cap["t"] + "x"})

    assert code_of(exc.value) == "body_too_large"


def test_body_cap_counts_utf8_bytes_not_characters():
    body = {"text": "中" * 6000}          # 6000 chars, 18000 bytes
    assert len(body["text"]) < env_mod.MAX_BODY_BYTES
    assert js_bytes(body) > env_mod.MAX_BODY_BYTES

    with pytest.raises(MailboxError) as exc:
        envelope(body=body)

    assert code_of(exc.value) == "body_too_large"


def test_unicode_body_is_preserved():
    body = {"text": "把 watcher 的超时改成可配置", "summary": "配置化超时"}

    assert envelope(body=body)["body"] == body


def test_non_serializable_body_is_an_invalid_envelope():
    with pytest.raises(MailboxError) as exc:
        envelope(body={"path": object()})

    assert code_of(exc.value) == "invalid_envelope"


@pytest.mark.parametrize("outcome", ["progress", "waiting", "ready",
                                     "blocked", "failed", "dropped"])
def test_report_outcomes_are_accepted(outcome):
    env = envelope(**{"from": {"role": "worker", "id": "code_001"},
                      "kind": "report",
                      "body": {"outcome": outcome, "text": "ok"}})

    assert env["body"]["outcome"] == outcome


def test_report_without_outcome_is_fine():
    env = envelope(kind="report", body={"text": "still working"})

    assert env["body"] == {"text": "still working"}


@pytest.mark.parametrize("body", [{"outcome": "done"}, {"outcome": 3}])
def test_bad_report_outcome_is_rejected(body):
    with pytest.raises(MailboxError) as exc:
        envelope(kind="report", body=body)

    assert code_of(exc.value) == "invalid_envelope"


def test_empty_report_outcome_reads_as_absent():
    env = envelope(kind="report", body={"outcome": "", "text": "working"})

    assert env["body"]["outcome"] == ""


def test_outcome_is_only_a_report_field():
    env = envelope(kind="notice", body={"outcome": "done"})

    assert env["body"]["outcome"] == "done"


# ---------------------------------------------------------------------------
# Decision card
# ---------------------------------------------------------------------------

def test_card_shape():
    env = envelope(priority=2, correlation={"ticket": "code_001"})

    card = env_mod.card(env, ticket={"id": "code_001", "stage": "working"})

    assert list(card) == list(env_mod.CARD_KEYS)
    assert card["message_id"] == env["id"]
    assert card["kind"] == "request"
    assert card["from"] == "human:terminal:term_abc"
    assert card["trust"] == "trusted"
    assert card["intent"] == "request"
    assert card["attention"] == "must_answer"
    assert card["priority"] == 2
    assert card["summary"] == "把 watcher 的超时改成可配置"
    assert card["tickets"] == [{"id": "code_001", "stage": "working"}]
    assert card["detail_hint"] == f"osw mailbox show {env['id']}"


def test_card_without_ticket_has_no_tickets():
    assert env_mod.card(envelope())["tickets"] == []


def test_card_summary_prefers_body_summary():
    env = envelope(body={"summary": "配置化超时", "text": "把 watcher 的超时改成可配置"})

    assert env_mod.card(env)["summary"] == "配置化超时"


def test_card_summary_falls_back_to_body_text():
    env = envelope(body={"text": "第一行\n第二行"})

    assert env_mod.card(env)["summary"] == "第一行"


def test_card_summary_falls_back_to_a_string_body():
    env = envelope(body="一行正文\n还有一行")

    assert env_mod.card(env)["summary"] == "一行正文"


def test_card_summary_argument_overrides_the_body():
    env = envelope()

    assert env_mod.card(env, summary="人工指定")["summary"] == "人工指定"


def test_card_summary_is_clamped_to_200_chars():
    env = envelope(body={"text": "字" * 500})

    summary = env_mod.card(env)["summary"]

    assert len(summary) == env_mod.MAX_SUMMARY_CHARS


def test_card_with_long_chinese_summary_stays_under_1kib():
    env = envelope(body={"summary": "中文摘要" * 100})
    natural = env_mod.card(env)
    assert natural["summary"] == ("中文摘要" * 100)[:200]
    assert js_bytes(natural) <= 1024

    # A long transport address leaves no room for a 200-char summary.
    wide = envelope(
        **{"from": {"role": "worker", "id": "terminal:" + "t" * 400}},
        body={"summary": "中文摘要" * 100},
    )
    card = env_mod.card(wide)

    assert js_bytes(card) <= env_mod.MAX_CARD_BYTES
    assert list(card) == list(env_mod.CARD_KEYS)
    assert card["summary"].endswith("…")
    assert len(card["summary"]) <= env_mod.MAX_SUMMARY_CHARS
    assert ("中文摘要" * 100).startswith(card["summary"].rstrip("…"))
    assert card["message_id"] == wide["id"]
    assert card["detail_hint"] == f"osw mailbox show {wide['id']}"
    assert card["tickets"] == []


def test_card_with_unicode_body_and_ticket_stays_under_1kib():
    env = envelope(
        **{"from": {"role": "watcher", "id": "terminal:" + "观" * 150}},
        kind="report",
        body={"outcome": "ready", "summary": "完成：把 watcher 的超时改成可配置" * 20},
    )

    card = env_mod.card(env, ticket={"id": "code_001", "stage": "review"})

    assert js_bytes(card) <= env_mod.MAX_CARD_BYTES
    assert card["summary"].endswith("…")
    assert card["tickets"] == [{"id": "code_001", "stage": "review"}]
    assert card["attention"] == "must_answer"
    assert card["trust"] == "trusted"


@pytest.mark.parametrize("kind, role, expected", [
    ("ask", "worker", "must_answer"),
    ("blocked", "watcher", "must_answer"),
    ("request", "human", "must_answer"),
    ("followup", "human", "must_answer"),
    ("report", "worker", "fyi"),
    ("notice", "system", "fyi"),
    ("status", "human", "fyi"),
])
def test_card_attention_floor(kind, role, expected):
    env = envelope(**{"from": {"role": role, "id": "x"}, "kind": kind,
                      "body": {}})

    assert env_mod.card(env)["attention"] == expected


@pytest.mark.parametrize("outcome, expected", [
    ("progress", "fyi"), ("waiting", "fyi"),
    ("ready", "must_answer"), ("failed", "must_answer"),
    ("blocked", "must_answer"), ("dropped", "must_answer"),
])
def test_card_attention_follows_the_report_outcome(outcome, expected):
    env = envelope(**{"from": {"role": "worker"}, "kind": "report",
                      "body": {"outcome": outcome}})

    assert env_mod.card(env)["attention"] == expected


def test_card_uses_fast_path_annotations():
    env = envelope(kind="notice", intent="answer", attention="human")

    card = env_mod.card(env)

    assert card["intent"] == "answer"
    assert card["attention"] == "human"


def test_card_rejects_unknown_annotation_values():
    env = envelope(kind="notice", attention="urgent", intent="")

    card = env_mod.card(env)

    assert card["attention"] == "fyi"
    assert card["intent"] == "notice"


def test_card_tolerates_a_partial_envelope():
    card = env_mod.card({"body": "裸正文"})

    assert card == {
        "message_id": "",
        "kind": "",
        "from": "",
        "trust": "untrusted",
        "intent": "",
        "attention": "fyi",
        "priority": 3,
        "summary": "裸正文",
        "tickets": [],
        "detail_hint": "",
    }


def test_card_priority_falls_back_to_the_floor():
    card = env_mod.card({"kind": "ask", "from": {"role": "worker"}})

    assert card["priority"] == 1


# ---------------------------------------------------------------------------
# Exact command grammar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "osw new claude --prefix research 看看测试为什么挂了",
    "osw use code_001 继续",
    "osw stop code_001",
    "osw status",
    "osw status --json",
    "osw list",
    "osw logs code_001",
    "osw\tnew",
])
def test_is_exact_command_accepts_bare_osw_verbs(text):
    assert env_mod.is_exact_command(text) is True


@pytest.mark.parametrize("text", [
    "",
    "ls",
    " osw new x",
    "osw",
    "osw serve",
    "osw newx",
    "osw statuss",
    "osw new x; rm -rf /",
    "osw new x && rm -rf /",
    "osw status | grep ok",
    "osw status > out.txt",
    "osw status < in.txt",
    "osw status &",
    "osw new `whoami`",
    "osw new $HOME",
    "osw new a\nosw stop b",
    "osw status\r",
    123,
    None,
])
def test_is_exact_command_rejects_anything_else(text):
    assert env_mod.is_exact_command(text) is False


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_generate_token_creates_a_private_hex_file(tmp_path):
    token = identity.generate_token(tmp_path)

    path = paths.token_path(tmp_path)
    assert path.is_file()
    assert re.match(r"^[0-9a-f]{64}$", token)
    assert path.read_text(encoding="utf-8") == token
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_generate_token_is_stable(tmp_path):
    first = identity.generate_token(tmp_path)
    second = identity.generate_token(tmp_path)

    assert first == second
    assert identity.read_token(tmp_path) == first


def test_generate_token_replaces_an_empty_file(tmp_path):
    path = paths.token_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")

    token = identity.generate_token(tmp_path)

    assert re.match(r"^[0-9a-f]{64}$", token)
    assert identity.read_token(tmp_path) == token


def test_read_token_is_empty_without_a_file(tmp_path):
    assert identity.read_token(tmp_path) == ""


@pytest.mark.parametrize("env, expected", [
    ({"OSW_ROLE": "router"}, "human"),
    ({"OSW_ROLE": "router", "OSW_ROLE_TOKEN": "nope"}, "human"),
    ({"OSW_ROLE": "human", "OSW_ROLE_TOKEN": "TOKEN"}, "human"),
    ({"OSW_ROLE_TOKEN": "TOKEN"}, "human"),
    ({"OSW_ROLE": "worker", "OSW_ROLE_TOKEN": "TOKEN"}, "human"),
    ({}, "human"),
])
def test_current_role_needs_marker_and_token(tmp_path, env, expected):
    token = identity.generate_token(tmp_path)
    supplied = {k: (token if v == "TOKEN" else v) for k, v in env.items()}

    assert identity.current_role(tmp_path, env=supplied) == expected


def test_current_role_is_router_with_marker_and_token(tmp_path):
    token = identity.generate_token(tmp_path)

    role = identity.current_role(
        tmp_path, env={"OSW_ROLE": "router", "OSW_ROLE_TOKEN": token})

    assert role == "router"


def test_current_role_ignores_a_token_without_a_token_file(tmp_path):
    role = identity.current_role(
        tmp_path,
        env={"OSW_ROLE": "router", "OSW_ROLE_TOKEN": "a" * 64},
    )

    assert role == "human"


def test_current_role_reads_the_environment_by_default(tmp_path, monkeypatch):
    token = identity.generate_token(tmp_path)
    monkeypatch.setenv(identity.ROLE_ENV, "router")
    monkeypatch.setenv(identity.TOKEN_ENV, token)

    assert identity.current_role(tmp_path) == "router"

    monkeypatch.setenv(identity.TOKEN_ENV, "wrong")
    assert identity.current_role(tmp_path) == "human"


def test_require_human_identity_allows_a_human(tmp_path, monkeypatch):
    identity.generate_token(tmp_path)
    monkeypatch.delenv(identity.ROLE_ENV, raising=False)
    monkeypatch.delenv(identity.TOKEN_ENV, raising=False)

    assert identity.require_human_identity(tmp_path, command="osw new") is None


def test_require_human_identity_refuses_the_router(tmp_path, monkeypatch):
    token = identity.generate_token(tmp_path)
    monkeypatch.setenv(identity.ROLE_ENV, "router")
    monkeypatch.setenv(identity.TOKEN_ENV, token)

    with pytest.raises(MailboxError) as exc:
        identity.require_human_identity(tmp_path, command="osw new")

    assert exc.value.code == "router_forbidden"
    assert "osw new" in exc.value.message


def test_require_human_identity_names_an_unnamed_command(tmp_path, monkeypatch):
    token = identity.generate_token(tmp_path)
    monkeypatch.setenv(identity.ROLE_ENV, "router")
    monkeypatch.setenv(identity.TOKEN_ENV, token)

    with pytest.raises(MailboxError) as exc:
        identity.require_human_identity(tmp_path)

    assert exc.value.code == "router_forbidden"
    assert "this command" in exc.value.message

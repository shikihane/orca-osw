"""Tests for the Mandate: closed schema, HEAD precedence, narrowing, expiry."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from osw.mandate import (
    MANDATE_RELATIVE_PATH,
    MandateError,
    answer_allowed,
    check_action,
    check_open_from,
    expires_at_epoch,
    load_mandate,
    narrow,
    validate,
)

DEFAULT = {
    "actions": [
        "accept", "followup", "answer", "stop", "retry", "reject",
        "escalate", "noop", "close",
    ],
    "answer": "free_text",
    "max_retries": 2,
    "max_workers": 4,
    "providers": ["claude", "codex", "pi", "omp", "kimi", "grok", "dsh"],
    "open_from": [],
    "close": "router",
    "expires_at": "",
}

# A loose base: empty ``open_from`` means "human only", so it can never be
# narrowed from; these tests start from a document that names two sources.
LOOSE = {**DEFAULT, "open_from": ["build", "crash"]}

NARROWED = {
    "actions": ["accept", "noop"],
    "answer": "options_only",
    "max_retries": 1,
    "max_workers": 2,
    "providers": ["claude"],
    "open_from": ["build"],
    "expires_at": "2026-12-31T00:00:00Z",
}

CJK = re.compile(r"[\u4e00-\u9fff]")


# --- helpers ---------------------------------------------------------------

def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
    )


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "osw@example.invalid")
    _git(root, "config", "user.name", "OSW Test")
    _git(root, "config", "core.autocrlf", "false")
    return root


def _mandate_path(root: Path) -> Path:
    path = root / MANDATE_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_mandate(root: Path, doc: dict) -> Path:
    path = _mandate_path(root)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _commit(root: Path, message: str = "mandate") -> None:
    _git(root, "add", "--", MANDATE_RELATIVE_PATH)
    _git(root, "commit", "-q", "-m", message)


def _code(exc: pytest.ExceptionInfo[MandateError]) -> str:
    return exc.value.code


# --- committed schema ------------------------------------------------------

def test_committed_mandate_is_valid_and_matches_the_contract() -> None:
    path = Path(__file__).resolve().parents[1] / MANDATE_RELATIVE_PATH
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert validate(doc) == DEFAULT


def test_validate_returns_a_copy_and_does_not_mutate() -> None:
    source = json.loads(json.dumps(DEFAULT))
    result = validate(source)
    assert result == source
    result["actions"].append("admin")
    assert "admin" not in source["actions"]


# --- closed schema ---------------------------------------------------------

@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"extra": 1}, id="unknown-key"),
        pytest.param({"actions": "accept"}, id="actions-not-list"),
        pytest.param({"actions": ["accept", "delete"]}, id="unknown-action"),
        pytest.param({"actions": ["accept", "accept"]}, id="duplicate-action"),
        pytest.param({"answer": "freeform"}, id="unknown-answer-mode"),
        pytest.param({"max_retries": "2"}, id="max-retries-string"),
        pytest.param({"max_retries": True}, id="max-retries-bool"),
        pytest.param({"max_retries": -1}, id="max-retries-negative"),
        pytest.param({"max_workers": 2.5}, id="max-workers-float"),
        pytest.param({"providers": ["claude", ""]}, id="empty-provider"),
        pytest.param({"open_from": "build"}, id="open-from-not-list"),
        pytest.param({"close": "worker"}, id="unknown-close-authority"),
        pytest.param({"expires_at": 20261231}, id="expires-at-not-string"),
        pytest.param({"expires_at": "next tuesday"}, id="unparseable-expiry"),
    ],
)
def test_validate_rejects_unknown_keys_and_wrong_types(mutation: dict) -> None:
    with pytest.raises(MandateError) as excinfo:
        validate({**DEFAULT, **mutation})
    assert _code(excinfo) == "mandate_invalid"


def test_validate_rejects_missing_fields() -> None:
    doc = dict(DEFAULT)
    del doc["close"]
    with pytest.raises(MandateError) as excinfo:
        validate(doc)
    assert _code(excinfo) == "mandate_invalid"


# --- narrowing -------------------------------------------------------------

def test_narrow_without_request_returns_the_base() -> None:
    assert narrow(DEFAULT) == DEFAULT
    assert narrow(DEFAULT, {}) == DEFAULT


def test_narrow_accepts_every_shrinking_field() -> None:
    result = narrow(
        LOOSE,
        {
            "actions": ["noop"],
            "answer": "options_only",
            "max_retries": 0,
            "max_workers": 1,
            "providers": ["codex"],
            "open_from": ["build"],
            "expires_at": "2027-01-01T00:00:00Z",
        },
    )
    assert result["actions"] == ["noop"]
    assert result["answer"] == "options_only"
    assert result["max_retries"] == 0
    assert result["max_workers"] == 1
    assert result["providers"] == ["codex"]
    assert result["open_from"] == ["build"]
    assert result["close"] == "router"
    assert result["expires_at"] == "2027-01-01T00:00:00Z"
    assert validate(result) == result


@pytest.mark.parametrize(
    "requested, field",
    [
        pytest.param({"actions": ["accept", "noop", "stop"]}, "actions", id="actions"),
        pytest.param({"providers": ["claude", "codex"]}, "providers", id="providers"),
        pytest.param(
            {"open_from": ["build", "crash"]}, "open_from", id="open-from"
        ),
        pytest.param({"max_retries": 2}, "max_retries", id="max-retries"),
        pytest.param({"max_workers": 4}, "max_workers", id="max-workers"),
        pytest.param({"answer": "free_text"}, "answer", id="answer-rank"),
        pytest.param({"close": "human"}, "close", id="close-authority"),
        pytest.param(
            {"expires_at": "2027-01-01T00:00:00Z"}, "expires_at", id="later-expiry"
        ),
        pytest.param({"expires_at": "2027-01-01"}, "expires_at", id="later-date"),
        pytest.param({"expires_at": ""}, "expires_at", id="drop-expiry"),
    ],
)
def test_narrow_rejects_every_widening(requested: dict, field: str) -> None:
    base = narrow(LOOSE, NARROWED)
    with pytest.raises(MandateError) as excinfo:
        narrow(base, requested)
    assert _code(excinfo) == "mandate_denied"
    assert field in excinfo.value.message
    assert CJK.search(excinfo.value.message)


def test_narrow_rejects_unknown_request_key() -> None:
    with pytest.raises(MandateError) as excinfo:
        narrow(DEFAULT, {"power": "unlimited"})
    assert _code(excinfo) == "mandate_invalid"


def test_narrow_rejects_wrong_request_type() -> None:
    with pytest.raises(MandateError) as excinfo:
        narrow(DEFAULT, {"actions": "noop"})
    assert _code(excinfo) == "mandate_invalid"


def test_narrow_keeps_the_base_close_and_expiry_when_not_requested() -> None:
    base = narrow(LOOSE, NARROWED)
    result = narrow(base, {"max_workers": 1})
    assert result["close"] == "router"
    assert result["expires_at"] == base["expires_at"]
    assert result["max_workers"] == 1


# --- load_mandate ----------------------------------------------------------

def test_load_prefers_committed_head_over_the_working_tree(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _write_mandate(root, DEFAULT)
    _commit(root)
    wider = {**DEFAULT, "providers": DEFAULT["providers"] + ["gemini"]}
    _write_mandate(root, wider)
    assert load_mandate(root) == DEFAULT


def test_load_reads_an_explicit_ref(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _write_mandate(root, DEFAULT)
    _commit(root)
    _write_mandate(root, {**DEFAULT, "max_workers": 1})
    _commit(root, "narrow")
    assert load_mandate(root, ref="HEAD~1") == DEFAULT
    assert load_mandate(root)["max_workers"] == 1


def test_load_falls_back_to_the_working_tree_when_head_lacks_the_file(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "repo")
    (root / "README.md").write_text("no mandate yet\n", encoding="utf-8")
    _git(root, "add", "--", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    _write_mandate(root, DEFAULT)
    assert load_mandate(root) == DEFAULT


def test_load_falls_back_when_the_directory_is_not_a_repository(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    _write_mandate(root, DEFAULT)
    assert load_mandate(root) == DEFAULT


def test_load_uses_the_bundled_mandate_when_the_repo_has_none(
    tmp_path: Path,
) -> None:
    """A project repository without a mandate file gets the bundled one.

    The control plane serves any project root, so `mandate_missing` is no
    longer the answer for "this repo does not carry OSW's file"; only a
    missing *bundled* mandate would be.
    """
    root = _repo(tmp_path / "repo")
    (root / "README.md").write_text("empty\n", encoding="utf-8")
    _git(root, "add", "--", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    assert load_mandate(root) == DEFAULT


def test_load_reports_invalid_committed_json(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    path = _mandate_path(root)
    path.write_text("{not json", encoding="utf-8")
    _commit(root)
    with pytest.raises(MandateError) as excinfo:
        load_mandate(root)
    assert _code(excinfo) == "mandate_invalid"


def test_load_reports_invalid_working_tree_schema(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    _write_mandate(root, {**DEFAULT, "extra": True})
    with pytest.raises(MandateError) as excinfo:
        load_mandate(root)
    assert _code(excinfo) == "mandate_invalid"


# --- expiry ----------------------------------------------------------------

def test_expires_at_epoch_reads_zulu_and_empty() -> None:
    assert expires_at_epoch(DEFAULT) is None
    assert expires_at_epoch({"expires_at": "1970-01-01T00:00:01Z"}) == 1.0
    assert expires_at_epoch({"expires_at": "1970-01-01T08:00:00+08:00"}) == 0.0


def test_check_action_refuses_an_expired_mandate() -> None:
    mandate = narrow(DEFAULT, {"expires_at": "2026-01-01T00:00:00Z"})
    with pytest.raises(MandateError) as excinfo:
        check_action(mandate, "noop", now=expires_at_epoch(mandate))
    assert _code(excinfo) == "mandate_expired"
    assert CJK.search(excinfo.value.message)


def test_check_action_allows_a_mandate_that_never_expires() -> None:
    assert check_action(DEFAULT, "noop", now=2**40) is None


def test_check_action_allows_an_unexpired_mandate() -> None:
    mandate = narrow(DEFAULT, {"expires_at": "2030-01-01T00:00:00Z"})
    assert check_action(mandate, "noop", now=0) is None


# --- actions and authority -------------------------------------------------

@pytest.mark.parametrize("action", DEFAULT["actions"])
def test_check_action_allows_every_listed_action(action: str) -> None:
    answer = {"answer_text": "1) 是"} if action == "answer" else {}
    close = {"close_authority": "router"} if action == "close" else {}
    assert check_action(DEFAULT, action, now=0, **answer, **close) is None


def test_check_action_refuses_an_action_outside_the_mandate() -> None:
    with pytest.raises(MandateError) as excinfo:
        check_action(narrow(DEFAULT, {"actions": ["noop"]}), "stop", now=0)
    assert _code(excinfo) == "mandate_denied"
    assert "stop" in excinfo.value.message
    assert CJK.search(excinfo.value.message)


def test_narrow_never_changes_close_authority() -> None:
    with pytest.raises(MandateError) as excinfo:
        narrow(DEFAULT, {"close": "human"})
    assert _code(excinfo) == "mandate_denied"
    assert narrow(DEFAULT, {"close": "router"})["close"] == "router"


def test_close_requires_human_authority_when_the_mandate_says_human() -> None:
    mandate = validate({**DEFAULT, "close": "human"})
    with pytest.raises(MandateError) as excinfo:
        check_action(mandate, "close", now=0, close_authority="router")
    assert _code(excinfo) == "mandate_denied"
    assert check_action(mandate, "close", now=0, close_authority="human") is None


def test_check_action_refuses_an_answer_when_the_mode_is_none() -> None:
    mandate = narrow(DEFAULT, {"answer": "none"})
    with pytest.raises(MandateError) as excinfo:
        check_action(mandate, "answer", now=0, answer_text="1) 是")
    assert _code(excinfo) == "mandate_denied"
    assert "none" in excinfo.value.message


def test_check_action_refuses_a_malformed_options_only_answer() -> None:
    mandate = narrow(DEFAULT, {"answer": "options_only"})
    with pytest.raises(MandateError) as excinfo:
        check_action(mandate, "answer", now=0, answer_text="好")
    assert _code(excinfo) == "mandate_denied"
    assert check_action(mandate, "answer", now=0, answer_text="1) 好") is None


# --- check_open_from -------------------------------------------------------

def test_open_from_empty_means_human_only() -> None:
    assert check_open_from(DEFAULT, "") is True
    assert check_open_from(DEFAULT, "build") is False


def test_open_from_names_the_allowed_sources() -> None:
    mandate = narrow(LOOSE, {"open_from": ["build"]})
    assert check_open_from(mandate, "") is True
    assert check_open_from(mandate, "build") is True
    assert check_open_from(mandate, "log_file") is False


# --- answer modes ----------------------------------------------------------

def test_answer_mode_none_refuses_every_answer() -> None:
    mandate = narrow(DEFAULT, {"answer": "none"})
    assert answer_allowed(mandate, "1) 是") is False
    assert answer_allowed(mandate, "- 否") is False


@pytest.mark.parametrize(
    "text, allowed",
    [
        pytest.param("1) 是", True, id="digit-paren"),
        pytest.param("12) 继续", True, id="multi-digit-paren"),
        pytest.param("- 否", True, id="dash"),
        pytest.param("好", False, id="plain-text"),
        pytest.param("1) 是\n2) 否", False, id="multiline"),
        pytest.param("", False, id="empty"),
        pytest.param("x" * 513, False, id="over-512"),
    ],
)
def test_answer_mode_options_only(text: str, allowed: bool) -> None:
    mandate = narrow(DEFAULT, {"answer": "options_only"})
    assert answer_allowed(mandate, text) is allowed


@pytest.mark.parametrize(
    "text, allowed",
    [
        pytest.param("这是一段中文自由回答。", True, id="chinese"),
        pytest.param("x" * 4096, True, id="at-4096"),
        pytest.param("x" * 4097, False, id="over-4096"),
        pytest.param("第一行\n第二行", False, id="multiline"),
        pytest.param("   ", False, id="blank"),
    ],
)
def test_answer_mode_free_text(text: str, allowed: bool) -> None:
    assert answer_allowed(DEFAULT, text) is allowed


def test_chinese_reason_survives_as_operator_text() -> None:
    """Reasons and answers are Chinese text, not ASCII-only."""
    with pytest.raises(MandateError) as excinfo:
        check_action(
            narrow(DEFAULT, {"actions": ["noop"]}), "accept", now=0
        )
    assert CJK.search(excinfo.value.message)
    mandate = narrow(DEFAULT, {"answer": "free_text"})
    assert answer_allowed(mandate, "先跑构建，失败再回滚。")


def test_load_mandate_falls_back_to_the_bundled_file(tmp_path: Path) -> None:
    """A project that is not the OSW repository still gets a mandate."""
    assert not (tmp_path / MANDATE_RELATIVE_PATH).exists()
    mandate = load_mandate(tmp_path)
    assert mandate["max_workers"] == DEFAULT["max_workers"]
    assert mandate["close"] == "router"


def test_load_mandate_prefers_the_project_file(tmp_path: Path) -> None:
    target = tmp_path / MANDATE_RELATIVE_PATH
    target.parent.mkdir(parents=True)
    narrowed = dict(DEFAULT, max_workers=1)
    target.write_text(json.dumps(narrowed), encoding="utf-8")
    assert load_mandate(tmp_path)["max_workers"] == 1

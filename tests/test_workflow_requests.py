"""The ``osw workflow`` request verbs and the real ``run`` wiring.

A human decision never touches run state. ``accept``, ``reject``, ``retry`` and
``stop`` write one request side file under the run's ``requests/`` directory
and return; the scheduler consumes it. These tests build a run archive by hand
(``run.json`` plus one step record) and assert on the side files it leaves
behind, on the refusal codes, and on the JSON shapes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from osw.cli import app
from osw.mailbox import identity
from osw.workflow import archive, board, defs

runner = CliRunner()

RUN_ID = "20261117T160245Z_audit-routes_9f3a11"
STEP_KEY = "0f3a1b2c3d4e5f60718293a4b5c6d7e8"
WORKFLOW = "audit-routes"

RUN_DOC = {
    "run_id": RUN_ID,
    "definition": {"name": WORKFLOW, "path": ".orca/osw/workflows/audit-routes.json"},
    "state": "running",
    "policy": "strict",
    "verify": "suspect",
    "max_attempts": 2,
    "max_agents": 64,
    "started": "2026-11-17T16:02:45Z",
    "phases": [{"id": "scan", "state": "done", "steps": [STEP_KEY]}],
}

STEP_DOC = {
    "step_key": STEP_KEY,
    "phase": "scan",
    "index": 0,
    "prompt_sha": "0" * 32,
    "schema_id": "",
    "state": "done",
    "attempts": [],
    "agent": "code_041",
    "handoff": "",
    "verdict": "",
    "error": "",
    "warning": "",
}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """A real ``~/.orca/osw/workflows`` never changes an outcome here."""
    monkeypatch.setattr(defs, "home_workflows_dir", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _seed_run(root: Path, *, run: dict | None = None,
              step: dict | None = None) -> None:
    """A hand-written archive: one run header and one finished step record."""
    header = board.runs_dir(root) / RUN_ID / board.RUN_FILE
    header.parent.mkdir(parents=True, exist_ok=True)
    header.write_text(json.dumps(run or RUN_DOC), encoding="utf-8")
    step_path = archive.step_path(root, RUN_ID, STEP_KEY)
    step_path.parent.mkdir(parents=True, exist_ok=True)
    step_path.write_text(json.dumps(step or STEP_DOC), encoding="utf-8")


def _requests(root: Path) -> list[Path]:
    directory = board.runs_dir(root) / RUN_ID / "requests"
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file())


def _install_definition(root: Path, payload: dict) -> None:
    path = root / ".orca" / "osw" / "workflows" / f"{WORKFLOW}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _single_phase_definition(count: int, *, max_agents: int = 64,
                             approval: str | None = None) -> dict:
    payload: dict = {
        "name": WORKFLOW,
        "max_agents": max_agents,
        "phases": [
            {"id": f"p{index}", "mode": "single", "task": f"step {index}"}
            for index in range(count)
        ],
    }
    if approval is not None:
        payload["approval"] = approval
    return payload


# ---------------------------------------------------------------------------
# Request side files
# ---------------------------------------------------------------------------

def test_accept_writes_exactly_one_request(_isolated_home):
    _seed_run(_isolated_home)

    result = runner.invoke(
        app, ["workflow", "accept", RUN_ID, STEP_KEY, "--reason", "looks right"],
    )

    assert result.exit_code == 0, result.output
    requests = _requests(_isolated_home)
    assert len(requests) == 1
    assert "accept" in requests[0].name
    assert STEP_KEY in requests[0].name
    assert RUN_ID in result.output
    assert STEP_KEY in result.output
    assert "accept" in result.output


@pytest.mark.parametrize("verb", ["reject", "retry"])
def test_reject_and_retry_write_one_request_each(_isolated_home, verb):
    _seed_run(_isolated_home)

    result = runner.invoke(
        app, ["workflow", verb, RUN_ID, STEP_KEY, "--reason", "because"],
    )

    assert result.exit_code == 0, result.output
    requests = _requests(_isolated_home)
    assert len(requests) == 1
    assert verb in requests[0].name
    assert verb in result.output


def test_stop_writes_one_request_without_a_step(_isolated_home):
    _seed_run(_isolated_home)

    result = runner.invoke(
        app, ["workflow", "stop", RUN_ID, "--reason", "budget"],
    )

    assert result.exit_code == 0, result.output
    requests = _requests(_isolated_home)
    assert len(requests) == 1
    assert "stop" in requests[0].name
    assert "stop" in result.output
    assert RUN_ID in result.output


def test_the_scheduler_consumes_each_request_once(_isolated_home):
    _seed_run(_isolated_home)
    runner.invoke(
        app, ["workflow", "accept", RUN_ID, STEP_KEY, "--reason", "looks right"],
    )

    consumed = archive.consume_requests(_isolated_home, RUN_ID)

    assert len(consumed) == 1
    assert "looks right" in json.dumps(consumed, default=str)
    assert archive.consume_requests(_isolated_home, RUN_ID) == []
    assert _requests(_isolated_home) == []


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_router_identity_is_refused(_isolated_home, monkeypatch):
    root = _isolated_home
    _seed_run(root)
    monkeypatch.setenv(identity.ROLE_ENV, identity.ROUTER_ROLE)
    monkeypatch.setenv(identity.TOKEN_ENV, identity.generate_token(root))

    result = runner.invoke(
        app, ["workflow", "accept", RUN_ID, STEP_KEY, "--reason", "mine"],
    )

    assert result.exit_code == 1
    assert "router_forbidden" in result.output
    assert _requests(root) == []


@pytest.mark.parametrize("verb", ["accept", "reject", "retry"])
def test_missing_reason_is_a_usage_error(_isolated_home, verb):
    _seed_run(_isolated_home)

    result = runner.invoke(app, ["workflow", verb, RUN_ID, STEP_KEY])

    assert result.exit_code == 2
    assert "--reason" in result.output
    assert _requests(_isolated_home) == []


@pytest.mark.parametrize(
    "argv",
    [
        ["accept", "run_404", STEP_KEY],
        ["reject", "run_404", STEP_KEY],
        ["retry", "run_404", STEP_KEY],
        ["stop", "run_404"],
    ],
)
def test_unknown_run_is_refused(_isolated_home, argv):
    _seed_run(_isolated_home)

    result = runner.invoke(app, ["workflow", *argv, "--reason", "why"])

    assert result.exit_code == 1
    assert "run_not_found" in result.output
    assert _requests(_isolated_home) == []


@pytest.mark.parametrize("verb", ["accept", "reject", "retry"])
def test_unknown_step_is_refused(_isolated_home, verb):
    _seed_run(_isolated_home)

    result = runner.invoke(
        app, ["workflow", verb, RUN_ID, "f" * 32, "--reason", "why"],
    )

    assert result.exit_code == 1
    assert "step_not_found" in result.output
    assert RUN_ID in result.output
    assert "f" * 32 in result.output
    assert _requests(_isolated_home) == []


# ---------------------------------------------------------------------------
# JSON shapes
# ---------------------------------------------------------------------------

def test_human_verb_json_names_run_step_and_verb(_isolated_home):
    _seed_run(_isolated_home)

    result = runner.invoke(
        app,
        ["workflow", "retry", RUN_ID, STEP_KEY, "--reason", "flaky", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "run": RUN_ID,
        "step": STEP_KEY,
        "verb": "retry",
        "reason": "flaky",
        "request": payload["request"],
    }
    assert Path(payload["request"]).is_file()


def test_stop_json_has_an_empty_step(_isolated_home):
    _seed_run(_isolated_home)

    result = runner.invoke(app, ["workflow", "stop", RUN_ID, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["verb"] == "stop"
    assert payload["step"] == ""
    assert payload["run"] == RUN_ID
    assert Path(payload["request"]).is_file()


# ---------------------------------------------------------------------------
# The real run verb
# ---------------------------------------------------------------------------

def _fake_executor(monkeypatch, *, result: dict | None = None,
                   error: Exception | None = None) -> dict:
    """Replace the one dispatch seam: record the call, return a finished run."""
    calls: dict = {}

    def run_run(root, definition, *, policy, verify, max_attempts, resume, approved):
        calls.update({
            "root": root,
            "definition": definition,
            "policy": policy,
            "verify": verify,
            "max_attempts": max_attempts,
            "resume": resume,
            "approved": approved,
        })
        if error is not None:
            raise error
        return result or {
            "run_id": RUN_ID,
            "definition": WORKFLOW,
            "state": "done",
            "started": "2026-11-17T16:02:45Z",
            "steps": [{"step_id": STEP_KEY, "phase": "p0", "state": "done"}],
        }

    monkeypatch.setattr("osw.workflow.executor.run_run", run_run)
    return calls


def test_run_passes_the_options_and_prints_status(_isolated_home, monkeypatch):
    _install_definition(_isolated_home, _single_phase_definition(1))
    calls = _fake_executor(monkeypatch)

    result = runner.invoke(
        app,
        ["workflow", "run", WORKFLOW, "--resume", "--policy", "partial",
         "--verify", "all", "--max-attempts", "5", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert calls["policy"] == "partial"
    assert calls["verify"] == "all"
    assert calls["max_attempts"] == 5
    assert calls["resume"] is True
    assert calls["approved"] is True
    assert calls["root"] == _isolated_home
    assert f"run {RUN_ID}" in result.output


def test_run_json_prints_the_final_run(_isolated_home, monkeypatch):
    _install_definition(_isolated_home, _single_phase_definition(1))
    _fake_executor(monkeypatch)

    result = runner.invoke(app, ["workflow", "run", WORKFLOW, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_id"] == RUN_ID
    assert payload["state"] == "done"


def test_run_refuses_too_many_agents_before_dispatch(_isolated_home, monkeypatch):
    _install_definition(_isolated_home, _single_phase_definition(2, max_agents=1))
    calls = _fake_executor(monkeypatch)

    result = runner.invoke(app, ["workflow", "run", WORKFLOW, "--yes"])

    assert result.exit_code == 1
    assert "too_many_agents" in result.output
    assert "p1" in result.output
    assert calls == {}


def test_run_surfaces_the_approval_refusal_once(_isolated_home, monkeypatch):
    _install_definition(
        _isolated_home, _single_phase_definition(1, approval="human"),
    )
    _fake_executor(
        monkeypatch,
        error=defs.WorkflowError(
            "approval_unavailable", f"workflow {WORKFLOW!r} needs a person to accept it",
        ),
    )

    result = runner.invoke(app, ["workflow", "run", WORKFLOW])

    assert result.exit_code == 1
    assert "approval_unavailable" in result.output
    assert result.output.strip().count("\n") == 0


def test_run_rejects_an_unknown_policy(_isolated_home, monkeypatch):
    _install_definition(_isolated_home, _single_phase_definition(1))
    calls = _fake_executor(monkeypatch)

    result = runner.invoke(app, ["workflow", "run", WORKFLOW, "--policy", "lax"])

    assert result.exit_code == 2
    assert calls == {}


def test_help_lists_every_verb():
    result = runner.invoke(app, ["workflow", "--help"])

    assert result.exit_code == 0, result.output
    for verb in ("ls", "show", "status", "run", "stop", "accept", "reject", "retry"):
        assert verb in result.output

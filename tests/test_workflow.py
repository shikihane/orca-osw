"""The read-only ``osw workflow`` surface: definitions, runs and dry runs.

The fixture definition is project-style (``.orca/osw/workflows/``), so each
test that goes through the CLI copies it into the temp project root. The home
directory is redirected too, so a real ``~/.orca/osw/workflows`` never changes
the outcome.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from osw import workflow as workflow_pkg
from osw.cli import app
from osw.workflow import board
from osw.workflow import defs

runner = CliRunner()
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workflow" / "audit-routes.json"
DEFINITION_NAME = "audit-routes"

RUN_FIXTURE = {
    "run_id": "run_001",
    "definition": "audit-routes",
    "definition_path": ".orca/osw/workflows/audit-routes.json",
    "state": "running",
    "started": "2026-11-17T12:00:00Z",
    "finished": None,
    "phases": [{"id": "scan", "steps": 1}, {"id": "audit", "steps": 3}],
    "steps": [
        {"step_id": "scan", "phase": "scan", "state": "done"},
        {"step_id": "audit-1", "phase": "audit", "state": "done"},
        {"step_id": "audit-2", "phase": "audit", "state": "running"},
        {"step_id": "audit-3", "phase": "audit", "state": "failed", "error": "boom"},
    ],
}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Definitions never escape the temp project root."""
    home = tmp_path / "home" / ".orca" / "osw" / "workflows"
    monkeypatch.setattr(defs, "home_workflows_dir", lambda: home)
    return home


def _install_definition(root: Path, payload: dict | None = None,
                        name: str = DEFINITION_NAME) -> Path:
    path = root / ".orca" / "osw" / "workflows" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload) if payload is not None else FIXTURE.read_text(encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    return path


def _write_run(root: Path, payload: dict = RUN_FIXTURE) -> Path:
    path = board.runs_dir(root) / payload["run_id"] / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------

def test_expand_keeps_for_each_counts_open(tmp_path):
    _install_definition(tmp_path)

    definition = defs.load_definition(tmp_path, DEFINITION_NAME)
    planned = defs.expand(definition)

    assert [phase.id for phase in planned] == ["scan", "audit", "verify", "report"]
    assert [phase.mode for phase in planned] == ["single", "for_each", "for_each", "single"]
    # Static phases are one agent; the fan-out width is the run's to know.
    assert [phase.tasks for phase in planned] == [1, None, None, 1]
    assert planned[3].from_ids == ("audit", "verify")
    assert defs.static_agent_total(planned) == 2
    assert defs.has_runtime_fanout(planned)
    assert definition.max_agents == 64


def test_unknown_field_names_the_key_and_phase(tmp_path):
    _install_definition(tmp_path, {
        "name": "audit-routes",
        "phases": [
            {"id": "scan", "mode": "single", "task": "scan", "retries": 3},
        ],
    })

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "unknown_field"
    assert "retries" in caught.value.message
    assert "scan" in caught.value.message


def test_unknown_top_level_field_is_refused(tmp_path):
    _install_definition(tmp_path, {
        "name": "audit-routes",
        "schedule": "hourly",
        "phases": [{"id": "scan", "mode": "single", "task": "scan"}],
    })

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "unknown_field"
    assert "schedule" in caught.value.message


def test_forward_phase_reference_is_refused(tmp_path):
    _install_definition(tmp_path, {
        "name": "audit-routes",
        "phases": [
            {"id": "scan", "mode": "single", "task": "scan", "from": "audit"},
            {"id": "audit", "mode": "for_each", "from": "scan", "task": "audit"},
        ],
    })

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "bad_phase_ref"
    assert "audit" in caught.value.message


def test_unknown_phase_reference_is_refused(tmp_path):
    _install_definition(tmp_path, {
        "name": "audit-routes",
        "phases": [{"id": "scan", "mode": "single", "task": "scan", "from": "nope"}],
    })

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "bad_phase_ref"


def test_unstable_placeholder_is_refused(tmp_path):
    _install_definition(tmp_path, {
        "name": "audit-routes",
        "phases": [{"id": "scan", "mode": "single", "task": "Run at {{now}}."}],
    })

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "unstable_placeholder"
    assert "{{now}}" in caught.value.message


def test_unstable_placeholder_is_case_insensitive(tmp_path):
    _install_definition(tmp_path, {
        "name": "audit-routes",
        "phases": [{"id": "scan", "mode": "single", "task": "ID {{ UUID }}"}],
    })

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "unstable_placeholder"
    assert "uuid" in caught.value.message.lower()


def test_missing_definition_lists_both_paths(tmp_path):
    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "no_definition"
    assert str(tmp_path / ".orca" / "osw" / "workflows") in caught.value.message
    assert ".orca" in caught.value.message


def test_for_each_without_from_is_invalid(tmp_path):
    _install_definition(tmp_path, {
        "name": "audit-routes",
        "phases": [{"id": "audit", "mode": "for_each", "task": "audit"}],
    })

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "definition_invalid"


def test_duplicate_phase_id_is_invalid(tmp_path):
    _install_definition(tmp_path, {
        "name": "audit-routes",
        "phases": [
            {"id": "scan", "mode": "single", "task": "a"},
            {"id": "scan", "mode": "single", "task": "b"},
        ],
    })

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "definition_invalid"
    assert "duplicate" in caught.value.message


def test_project_definition_beats_home_definition(tmp_path, _isolated_home):
    _install_definition(tmp_path)
    _isolated_home.mkdir(parents=True, exist_ok=True)
    (_isolated_home / f"{DEFINITION_NAME}.json").write_text(
        json.dumps({
            "name": "home-copy",
            "phases": [{"id": "only", "mode": "single", "task": "home"}],
        }),
        encoding="utf-8",
    )

    definition = defs.load_definition(tmp_path, DEFINITION_NAME)

    assert definition.name == "audit-routes"
    assert str(tmp_path) in str(definition.path)


# ---------------------------------------------------------------------------
# Run archives
# ---------------------------------------------------------------------------

def test_phase_rows_counts_by_state(tmp_path):
    _write_run(tmp_path)
    run = board.read_run(tmp_path, "run_001")

    rows = {row["id"]: row for row in board.phase_rows(run)}

    assert rows["scan"] == {"id": "scan", "total": 1, "done": 1, "running": 0,
                            "failed": 0, "waiting": 0}
    # `audit` records three steps up front but has only emitted three records.
    assert rows["audit"] == {"id": "audit", "total": 3, "done": 1, "running": 1,
                             "failed": 1, "waiting": 0}


def test_phase_total_falls_back_to_step_records(tmp_path):
    payload = {
        "run_id": "run_002",
        "definition": "audit-routes",
        "state": "running",
        "started": "2026-11-17T12:00:00Z",
        "steps": [
            {"step_id": "a", "phase": "scan", "state": "done"},
            {"step_id": "b", "phase": "scan", "state": "queued"},
        ],
    }
    _write_run(tmp_path, payload)

    rows = board.phase_rows(board.read_run(tmp_path, "run_002"))

    assert rows == [{"id": "scan", "total": 2, "done": 1, "running": 0,
                     "failed": 0, "waiting": 1}]


def test_read_run_missing_is_a_refusal(tmp_path):
    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        board.read_run(tmp_path, "run_999")

    assert caught.value.code == "run_not_found"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_dry_run_prints_plan_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_definition(tmp_path)

    result = runner.invoke(app, ["workflow", "run", DEFINITION_NAME, "--dry-run"])

    assert result.exit_code == 0, result.output
    output = result.output
    assert "DRY RUN (nothing dispatched)" in output
    assert f"DEF     {DEFINITION_NAME}   {tmp_path / '.orca' / 'osw' / 'workflows' / 'audit-routes.json'}" in output
    assert "PHASE   MODE       FROM     TASKS" in output
    assert "scan    single     -        1" in output
    assert "audit   for_each   scan     ? (expanded at run time: one agent per item)" in output
    assert "report  single     audit,verify 1" in output
    assert "AGENTS  2 + N   max_agents 64" in output
    assert not board.runs_dir(tmp_path).exists()


def test_dry_run_json_lists_phases(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_definition(tmp_path)

    result = runner.invoke(app, ["workflow", "run", DEFINITION_NAME, "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["agents"] == "2 + N"
    assert [phase["tasks"] for phase in payload["phases"]] == [1, None, None, 1]
    assert not board.runs_dir(tmp_path).exists()


def test_run_without_a_definition_refuses_before_dispatch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["workflow", "run", DEFINITION_NAME])

    assert result.exit_code == 1
    assert "no_definition" in result.output
    assert not board.runs_dir(tmp_path).exists()


def test_ls_with_no_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["workflow", "ls"])

    assert result.exit_code == 0, result.output
    assert "No runs." in result.output


def test_ls_lists_run_archives(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_run(tmp_path)

    result = runner.invoke(app, ["workflow", "ls"])

    assert result.exit_code == 0, result.output
    assert "RUN_ID  DEFINITION  STATE  STARTED" in result.output
    assert "run_001" in result.output
    assert "audit-routes" in result.output


def test_status_reports_phase_counts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_run(tmp_path)

    result = runner.invoke(app, ["workflow", "status", "run_001"])

    assert result.exit_code == 0, result.output
    output = result.output
    assert "run run_001  def audit-routes  running  started 2026-11-17T12:00:00Z" in output
    assert "DONE/TOTAL  RUNNING  FAILED  WAITING" in output
    assert "scan        1/1         0        0       0" in output
    assert "audit       1/3         1        1       0" in output
    # A failed and a running step present the STEPS section.
    assert "STEPS" in output
    assert "audit-3" in output


def test_status_omits_steps_when_nothing_is_active(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_run(tmp_path, {
        "run_id": "run_003",
        "definition": "audit-routes",
        "state": "done",
        "started": "2026-11-17T12:00:00Z",
        "steps": [{"step_id": "scan", "phase": "scan", "state": "done"}],
    })

    result = runner.invoke(app, ["workflow", "status", "run_003"])

    assert result.exit_code == 0, result.output
    assert "STEPS" not in result.output


def test_status_defaults_to_the_newest_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_run(tmp_path)
    _write_run(tmp_path, {
        "run_id": "run_002",
        "definition": "audit-routes",
        "state": "done",
        "started": "2026-11-18T12:00:00Z",
        "steps": [],
    })

    result = runner.invoke(app, ["workflow", "status"])

    assert result.exit_code == 0, result.output
    assert "run run_002" in result.output


def test_status_missing_run_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["workflow", "status", "run_404"])

    assert result.exit_code == 1
    assert "run_not_found" in result.output


def test_show_lists_every_step(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_run(tmp_path)

    result = runner.invoke(app, ["workflow", "show", "run_001"])

    assert result.exit_code == 0, result.output
    output = result.output
    assert "run run_001  def audit-routes" in output
    assert "PHASES" in output
    for step_id in ("scan", "audit-1", "audit-2", "audit-3"):
        assert step_id in output


def test_help_lists_every_verb():
    result = runner.invoke(app, ["workflow", "--help"])

    assert result.exit_code == 0, result.output
    for verb in ("ls", "show", "status", "run"):
        assert verb in result.output


# ---------------------------------------------------------------------------
# Rounds, gates and recovery: the schema additions (plan §1)
# ---------------------------------------------------------------------------

def _phase_set(**top_level) -> dict:
    """One ``single`` phase, plus whatever top-level keys a test adds."""
    payload = {
        "name": DEFINITION_NAME,
        "phases": [{"id": "scan", "mode": "single", "task": "Scan the tree."}],
    }
    payload.update(top_level)
    return payload


@pytest.mark.parametrize(
    "goal",
    ["Ship {{output}}.", "Finish by {{now}}.", "Turn {{item}} green."],
)
def test_goal_refuses_every_placeholder(tmp_path, goal):
    _install_definition(tmp_path, _phase_set(goal=goal))

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "unknown_placeholder"
    assert "goal" in caught.value.message


@pytest.mark.parametrize("goal", ["", "   ", 5])
def test_goal_must_be_a_non_empty_string(tmp_path, goal):
    _install_definition(tmp_path, _phase_set(goal=goal))

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "definition_invalid"
    assert "goal" in caught.value.message


@pytest.mark.parametrize("value", [0, -1, 21, True, "3", 2.5, None])
def test_max_rounds_is_bounded_and_never_a_bool(tmp_path, value):
    _install_definition(tmp_path, _phase_set(max_rounds=value))

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "definition_invalid"
    assert "max_rounds" in caught.value.message


def test_max_rounds_defaults_to_one_and_accepts_the_cap(tmp_path):
    _install_definition(tmp_path, _phase_set())
    assert defs.load_definition(tmp_path, DEFINITION_NAME).max_rounds == 1

    _install_definition(tmp_path, _phase_set(max_rounds=defs.MAX_ROUNDS_CAP))
    assert defs.load_definition(tmp_path, DEFINITION_NAME).max_rounds == 20


def test_repeat_phase_criteria_may_use_item_placeholders(tmp_path):
    _install_definition(tmp_path, {
        "name": DEFINITION_NAME,
        "goal": "Every route has an auth check.",
        "max_rounds": 2,
        "phases": [
            {"id": "scan", "mode": "single", "task": "List the routes."},
            {
                "id": "advance",
                "mode": "for_each",
                "from": "scan",
                "task": "Fix {{item.path}}.",
                "criteria": "Every {{item}} named route has an auth check.",
                "repeat": True,
            },
        ],
    })

    definition = defs.load_definition(tmp_path, DEFINITION_NAME)

    assert definition.phases[1].criteria == "Every {{item}} named route has an auth check."


def test_item_placeholder_in_criteria_is_refused_on_a_single_phase(tmp_path):
    entry = {"id": "scan", "mode": "single", "task": "Scan.",
             "criteria": "Every {{item}} is fixed."}
    _install_definition(tmp_path, {"name": DEFINITION_NAME, "phases": [entry]})

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "definition_invalid"
    assert "criteria" in caught.value.message
    assert "{{item}}" in caught.value.message


@pytest.mark.parametrize("criteria", ["{{output}} is valid JSON.", "{{findings}} are fixed."])
def test_criteria_accepts_no_other_placeholder(tmp_path, criteria):
    entry = {"id": "scan", "mode": "single", "task": "Scan.", "criteria": criteria}
    _install_definition(tmp_path, {"name": DEFINITION_NAME, "phases": [entry]})

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "unknown_placeholder"


def test_unstable_placeholder_in_criteria_is_refused(tmp_path):
    entry = {"id": "scan", "mode": "single", "task": "Scan.",
             "criteria": "The report is newer than {{today}}."}
    _install_definition(tmp_path, {"name": DEFINITION_NAME, "phases": [entry]})

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "unstable_placeholder"


def test_empty_criteria_is_refused(tmp_path):
    entry = {"id": "scan", "mode": "single", "task": "Scan.", "criteria": "  "}
    _install_definition(tmp_path, {"name": DEFINITION_NAME, "phases": [entry]})

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "definition_invalid"
    assert "criteria" in caught.value.message


@pytest.mark.parametrize(
    "evidence",
    [
        "../secrets.md",
        "docs/../../secrets.md",
        "C:/secrets.md",
        "C:\\secrets.md",
        "/etc/passwd",
        "docs/{{item}}.md",
        "",
        "docs/auth.md",
    ],
)
def test_evidence_must_be_relative_literal_paths(tmp_path, evidence):
    entry = {"id": "scan", "mode": "single", "task": "Scan.", "evidence": evidence}
    _install_definition(tmp_path, {"name": DEFINITION_NAME, "phases": [entry]})

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "definition_invalid"
    assert "evidence" in caught.value.message


def test_evidence_entries_round_trip(tmp_path):
    entry = {"id": "scan", "mode": "single", "task": "Scan.",
             "evidence": ["docs/auth.md", "src/routes/*.py"]}
    _install_definition(tmp_path, {"name": DEFINITION_NAME, "phases": [entry]})

    definition = defs.load_definition(tmp_path, DEFINITION_NAME)

    assert definition.phases[0].evidence == ("docs/auth.md", "src/routes/*.py")


def test_absent_or_empty_evidence_means_no_evidence(tmp_path):
    _install_definition(tmp_path, {
        "name": DEFINITION_NAME,
        "phases": [
            {"id": "scan", "mode": "single", "task": "Scan."},
            {"id": "audit", "mode": "single", "task": "Audit.", "evidence": []},
        ],
    })

    definition = defs.load_definition(tmp_path, DEFINITION_NAME)

    assert definition.phases[0].evidence == ()
    assert definition.phases[1].evidence == ()


def test_repeat_must_be_a_boolean(tmp_path):
    entry = {"id": "scan", "mode": "single", "task": "Scan.", "repeat": "yes"}
    _install_definition(tmp_path, {"name": DEFINITION_NAME, "phases": [entry]})

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "definition_invalid"
    assert "repeat" in caught.value.message


@pytest.mark.parametrize(
    "top_level, phase, missing",
    [
        ({"max_rounds": 2},
         {"id": "audit", "mode": "single", "task": "Audit.",
          "criteria": "The tree is audited.", "repeat": True},
         "'mode': 'for_each'"),
        ({"max_rounds": 2},
         {"id": "advance", "mode": "for_each", "from": "scan", "task": "Fix {{item}}.",
          "repeat": True},
         "'criteria'"),
        ({"max_rounds": 1},
         {"id": "advance", "mode": "for_each", "from": "scan", "task": "Fix {{item}}.",
          "criteria": "Every {{item}} is fixed.", "repeat": True},
         "'max_rounds'"),
    ],
)
def test_repeat_preconditions_each_name_the_field(tmp_path, top_level, phase, missing):
    _install_definition(tmp_path, {
        "name": DEFINITION_NAME,
        "phases": [{"id": "scan", "mode": "single", "task": "List the routes."}, phase],
        **top_level,
    })

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "definition_invalid"
    assert missing in caught.value.message


def test_repeat_without_from_names_the_from_field(tmp_path):
    _install_definition(tmp_path, {
        "name": DEFINITION_NAME,
        "max_rounds": 2,
        "phases": [{
            "id": "advance", "mode": "for_each", "task": "Fix {{item}}.",
            "criteria": "Every {{item}} is fixed.", "repeat": True,
        }],
    })

    with pytest.raises(workflow_pkg.WorkflowError) as caught:
        defs.load_definition(tmp_path, DEFINITION_NAME)

    assert caught.value.code == "definition_invalid"
    assert "'from'" in caught.value.message


def test_repeat_false_with_max_rounds_above_one_is_allowed(tmp_path):
    _install_definition(tmp_path, _phase_set(max_rounds=3))

    definition = defs.load_definition(tmp_path, DEFINITION_NAME)

    assert definition.max_rounds == 3
    assert definition.phases[0].repeat is False


def test_valid_repeat_definition_expands_rounds_and_multiplies_the_total(tmp_path):
    _install_definition(tmp_path, {
        "name": DEFINITION_NAME,
        "goal": "Every route has an auth check.",
        "max_rounds": 3,
        "phases": [
            {"id": "scan", "mode": "single", "task": "List the routes."},
            {
                "id": "advance",
                "mode": "for_each",
                "from": "scan",
                "task": "Fix {{item.path}}.",
                "criteria": "Every {{item}} named route has an auth check.",
                "evidence": ["docs/auth.md"],
                "repeat": True,
            },
            {"id": "report", "mode": "single", "from": "advance",
             "task": "Write the report.", "criteria": "The report restates the goal."},
        ],
    })

    definition = defs.load_definition(tmp_path, DEFINITION_NAME)
    planned = defs.expand(definition)

    assert definition.goal == "Every route has an auth check."
    assert definition.phases[1].repeat is True
    assert [phase.criteria for phase in definition.phases] == [
        "", "Every {{item}} named route has an auth check.",
        "The report restates the goal.",
    ]
    assert [phase.criteria for phase in planned] == [False, True, True]
    assert [phase.rounds for phase in planned] == [1, 3, 1]
    assert defs.static_agent_total(planned) == 2
    counted = [defs.PlannedPhase(id="a", mode="for_each", from_ids=(), tasks=3,
                                 criteria=True, rounds=3)]
    assert defs.static_agent_total(counted) == 9


def test_definition_without_the_new_keys_behaves_exactly_as_before(tmp_path):
    _install_definition(tmp_path)

    definition = defs.load_definition(tmp_path, DEFINITION_NAME)
    planned = defs.expand(definition)

    assert definition.goal == ""
    assert definition.max_rounds == 1
    assert [phase.criteria for phase in definition.phases] == [""] * 4
    assert [phase.evidence for phase in definition.phases] == [()] * 4
    assert [phase.repeat for phase in definition.phases] == [False] * 4
    assert [phase.criteria for phase in planned] == [False] * 4
    assert [phase.rounds for phase in planned] == [1] * 4
    assert [phase.tasks for phase in planned] == [1, None, None, 1]
    assert defs.static_agent_total(planned) == 2


def test_new_error_codes_are_registered():
    for code in ("run_running", "gate_invalid"):
        assert code in defs.CODES
        assert workflow_pkg.WorkflowError(code, "x").code == code

"""``workflow plan``: one sentence in, a validated definition out.

The planning agent is substituted, so these tests describe the contract rather
than a provider: the file the agent writes is what ``defs`` judges.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osw.workflow import author, defs

VALID = {
    "name": "flow",
    "max_agents": 8,
    "phases": [
        {"id": "scan", "mode": "single", "task": "List the routes, JSON to {{output}}"},
        {"id": "work", "mode": "for_each", "from": ["scan"], "task": "Check {{item}}"},
    ],
}


def _writer(payload, *, name="flow"):
    """A dispatch that writes ``payload`` where the planner expects it."""
    def dispatch(plan, definition, root):
        path = author.definition_path(Path(root), name)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(body, encoding="utf-8")
        assert str(path) in plan["prompt"], "the agent must be told where to write"
        return {"agent": "code_001", "terminal": "term_001"}
    return dispatch


def _done(root, agent_id):
    return "done"


def test_plan_validates_the_file_the_agent_writes(tmp_path):
    request = "check every route for missing auth"
    path, definition = author.author(
        tmp_path, request, name="flow", dispatch=_writer(VALID), wait=_done,
    )

    assert path == author.definition_path(tmp_path, "flow")
    assert definition.name == "flow"
    assert [phase.id for phase in definition.phases] == ["scan", "work"]
    assert definition.phases[1].from_ids == ("scan",)


def test_plan_names_the_line_an_agent_got_wrong(tmp_path):
    broken = {"name": "flow", "phases": [{"id": "scan", "mode": "single",
                                          "task": "x", "bogus": 1}]}
    with pytest.raises(defs.WorkflowError) as caught:
        author.author(tmp_path, "audit", name="flow",
                      dispatch=_writer(broken), wait=_done)

    assert caught.value.code == "unknown_field"
    assert "code_001" in caught.value.message, "the person needs to know who wrote it"
    assert author.definition_path(tmp_path, "flow").is_file(), "the file stays for one-line fixes"


def test_plan_refuses_to_overwrite_an_existing_definition(tmp_path):
    existing = author.definition_path(tmp_path, "flow")
    existing.parent.mkdir(parents=True)
    existing.write_text("{}", encoding="utf-8")

    with pytest.raises(defs.WorkflowError) as caught:
        author.author(tmp_path, "audit", name="flow",
                      dispatch=_writer(VALID), wait=_done)

    assert caught.value.code == "definition_exists"
    assert existing.read_text(encoding="utf-8") == "{}"


def test_plan_reports_an_agent_that_wrote_nothing(tmp_path):
    def dispatch(plan, definition, root):
        return {"agent": "code_001", "terminal": "term_001"}

    def failed(root, agent_id):
        return "error"

    with pytest.raises(defs.WorkflowError) as caught:
        author.author(tmp_path, "audit", name="flow", dispatch=dispatch, wait=failed)

    assert caught.value.code == "plan_failed"
    assert "error" in caught.value.message


def test_plan_refuses_a_placeholder_that_does_not_exist(tmp_path):
    """`{{project_root}}` is not substituted, so the plan must not pass as valid."""
    drafted = {
        "name": "flow",
        "phases": [
            {"id": "scan", "mode": "single",
             "task": "Audit every route under {{project_root}} and write {{output}}"},
        ],
    }
    with pytest.raises(defs.WorkflowError) as caught:
        author.author(tmp_path, "audit", name="flow",
                      dispatch=_writer(drafted), wait=_done)

    assert caught.value.code == "unknown_placeholder"
    assert "{{project_root}}" in caught.value.message


def test_plan_names_the_file_from_the_request_when_no_name_is_given(tmp_path):
    path, _ = author.author(
        tmp_path, "Audit every HTTP route", name="", dispatch=_writer(VALID, name="audit-every-http"),
        wait=_done,
    )
    assert path.name == "audit-every-http.json"


def test_plan_prompt_shows_the_goal_rounds_and_gate_keys(tmp_path):
    prompt = author.prompt_for(tmp_path, "flow", "audit every route")

    for key in ('"goal"', '"max_rounds"', '"criteria"', '"evidence"', '"repeat"'):
        assert key in prompt, f"the planner must be shown {key}"


def test_plan_prompt_states_the_goal_criteria_repeat_and_final_rules(tmp_path):
    prompt = author.prompt_for(tmp_path, "flow", "audit every route")
    flat = " ".join(prompt.split())

    assert "Write goal: one sentence naming the end state the person asked for." in flat
    assert "declares criteria" in flat and "evidence" in flat
    assert 'the phase that does the bulk work is "repeat": true and max_rounds is 3' in flat
    assert "The final phase's criteria must restate the goal" in flat
    # The pre-existing rules survive the addition.
    assert "Use 2 to 6 phases." in flat
    assert "{{project_root}}" in flat and "{{now}}" in flat


def test_plan_prompt_for_an_open_ended_request_names_repeat_and_max_rounds(tmp_path):
    prompt = author.prompt_for(
        tmp_path, "advance", "keep going until the bench cards are all executed",
    )
    flat = " ".join(prompt.split())

    assert "keep going until the bench cards are all executed" in flat
    assert "open-ended" in flat
    assert '"repeat": true' in flat
    assert "max_rounds is 3" in flat


def test_plan_prompt_never_offers_a_placeholder_inside_goal(tmp_path):
    prompt = author.prompt_for(tmp_path, "flow", "audit every route")

    for line in prompt.splitlines():
        if "goal" in line and "{{" in line:
            assert "no {{" in line, f"goal placeholders are refused, not taught: {line!r}"

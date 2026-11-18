"""The archive half of rounds, gates and recovery (plan §2).

Two promises are tested here: an archive written before rounds existed
recomputes exactly the keys it holds, and a new archive records the round,
key version, goal and scheduler heartbeat the scheduler needs to resume.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from osw.workflow import archive, board, defs


def _definition(tmp_path: Path, **top_level) -> defs.Definition:
    raw = {
        "name": "advance-routes",
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
    }
    raw.update(top_level)
    return defs.parse_definition(raw, tmp_path / "advance-routes.json")


def _legacy_definition(tmp_path: Path) -> defs.Definition:
    return defs.parse_definition({
        "name": "legacy",
        "phases": [{"id": "scan", "mode": "single", "task": "Scan the tree."}],
    }, tmp_path / "legacy.json")


# ---------------------------------------------------------------------------
# Step keys
# ---------------------------------------------------------------------------

def test_step_key_v1_reproduces_todays_material():
    material = "\0".join(("scan", "0", "Scan the tree.", ""))
    today = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    assert archive.step_key("scan", 0, "Scan the tree.", "",
                            keys_version=1) == today
    assert archive.step_key("scan", 0, "Scan the tree.", "",
                            round_no=0, keys_version=1) == today


def test_step_key_v2_prefixes_the_version_and_round():
    prompt = "Fix {{item}}."
    material = "|".join((str(archive.KEYS_VERSION), "1", "advance", "0", prompt, ""))
    expected = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    assert archive.step_key("advance", 0, prompt, "", round_no=1) == expected
    assert archive.step_key("advance", 0, prompt, "", round_no=1) != \
        archive.step_key("advance", 0, prompt, "", round_no=2)


def test_step_key_defaults_to_the_current_version_and_differs_from_v1():
    prompt = "Scan the tree."
    assert archive.step_key("scan", 0, prompt, "") == archive.step_key(
        "scan", 0, prompt, "", round_no=0, keys_version=archive.KEYS_VERSION)
    assert archive.step_key("scan", 0, prompt, "") != archive.step_key(
        "scan", 0, prompt, "", keys_version=1)


# ---------------------------------------------------------------------------
# Paths and round filtering
# ---------------------------------------------------------------------------

def test_gate_output_path_names_the_phase_and_round(tmp_path):
    path = archive.gate_output_path(tmp_path, "run_001", "advance", 2)

    assert path.name == "advance-gate-r2.json"
    assert path.parent.name == archive.OUTPUTS_DIR
    assert path.parent.parent.name == "run_001"


def test_phase_round_steps_filters_by_round_and_orders_by_index(tmp_path):
    run = archive.create_run(tmp_path, _definition(tmp_path))

    for index, round_no in ((1, 1), (0, 1), (0, 2)):
        key = archive.step_key("advance", index, f"p{round_no}", "", round_no=round_no)
        archive.write_step(tmp_path, run, {
            "step_key": key, "phase": "advance", "index": index,
            "round": round_no, "state": "done",
        })

    first = archive.phase_round_steps(tmp_path, run, "advance", 1)
    second = archive.phase_round_steps(tmp_path, run, "advance", 2)

    assert [record["index"] for record in first] == [0, 1]
    assert [record["round"] for record in first] == [1, 1]
    assert [record["index"] for record in second] == [0]
    assert archive.phase_round_steps(tmp_path, run, "advance", 3) == []


def test_phase_round_steps_reads_a_record_without_round_as_round_zero(tmp_path):
    run = archive.create_run(tmp_path, _legacy_definition(tmp_path))
    archive.write_step(tmp_path, run, {
        "step_key": archive.step_key("scan", 0, "Scan the tree.", "",
                                     keys_version=1),
        "phase": "scan", "index": 0, "state": "done",
    })

    records = archive.phase_round_steps(tmp_path, run, "scan", 0)

    assert [record["index"] for record in records] == [0]
    assert archive.phase_round_steps(tmp_path, run, "scan", 1) == []


# ---------------------------------------------------------------------------
# Run header
# ---------------------------------------------------------------------------

def test_create_run_writes_the_round_header(tmp_path):
    definition = _definition(tmp_path)

    run = archive.create_run(tmp_path, definition)
    saved = board.read_run(tmp_path, run["run_id"])

    assert saved["keys_version"] == archive.KEYS_VERSION == 2
    assert saved["goal"] == "Every route has an auth check."
    assert saved["goal_proposals"] == []
    assert saved["scheduler"] == {"pid": 0, "host": "", "started_at": "",
                                  "beat_at": ""}
    assert [entry["round"] for entry in saved["phases"]] == [1, 1]
    assert all(entry["gate"] == {"round": 0, "step": "", "pass": None,
                                 "reason": ""} for entry in saved["phases"])
    assert [entry["id"] for entry in saved["phases"]] == ["scan", "advance"]
    assert all(entry["state"] == "pending" for entry in saved["phases"])


def test_create_run_without_a_goal_writes_an_empty_goal(tmp_path):
    run = archive.create_run(tmp_path, _legacy_definition(tmp_path))

    assert run["goal"] == ""
    assert run["keys_version"] == 2


def test_read_run_reports_keys_version_one_for_a_legacy_header(tmp_path):
    path = board.runs_dir(tmp_path) / "run_old" / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "run_id": "run_old", "definition": "legacy", "state": "done",
        "phases": [{"id": "scan", "steps": []}], "steps": [],
    }), encoding="utf-8")

    saved = board.read_run(tmp_path, "run_old")

    assert saved["keys_version"] == 1
    # The version 1 key function reproduces the record the archive holds.
    assert archive.step_key("scan", 0, "Scan the tree.", "", keys_version=1) == \
        hashlib.sha256("\0".join(
            ("scan", "0", "Scan the tree.", "")).encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Scheduler liveness
# ---------------------------------------------------------------------------

def test_scheduler_alive_is_false_without_a_scheduler_block():
    assert archive.scheduler_alive(Path("."), {}) is False
    assert archive.scheduler_alive(Path("."), {"scheduler": None}) is False


@pytest.mark.parametrize("pid", [0, -1])
def test_scheduler_alive_is_false_for_a_missing_pid(pid):
    run = {"scheduler": {"pid": pid, "beat_at": archive.now_iso()}}

    assert archive.scheduler_alive(Path("."), run) is False


def test_scheduler_alive_is_false_for_a_stale_beat():
    stale = (datetime.now(timezone.utc) - timedelta(
        seconds=archive.SCHEDULER_STALE_SECONDS + 5)).isoformat()
    run = {"scheduler": {"pid": os.getpid(), "beat_at": stale}}

    assert archive.scheduler_alive(Path("."), run) is False


def test_scheduler_alive_is_true_for_this_process_with_a_fresh_beat():
    run = {"scheduler": {"pid": os.getpid(), "beat_at": archive.now_iso()}}

    assert archive.scheduler_alive(Path("."), run) is True


def test_scheduler_alive_refuses_an_unparsable_beat():
    run = {"scheduler": {"pid": os.getpid(), "beat_at": "not a timestamp"}}

    assert archive.scheduler_alive(Path("."), run) is False

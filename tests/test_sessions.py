from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from osw import sessions


FIXTURE = Path(__file__).parent / "fixtures" / "last_status" / "last-status.json"
OMP_PANE = (
    "ac6f5ce8-3804-43d7-ace9-6145309a175b:4e6b7212-276d-47a9-9d00-7511381d034d"
)
KIMI_PANE = (
    "9f1d2c3b-0000-4000-8000-000000000001:5e6f7a8b-0000-4000-8000-000000000002"
)
NO_SESSION_PANE = (
    "11111111-2222-4333-8444-555555555555:66666666-7777-4888-9999-000000000000"
)


def _write_status(path: Path, payload) -> Path:
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    return path


def test_session_for_pane_returns_provider_session_id():
    assert (
        sessions.session_for_pane(OMP_PANE, path=FIXTURE)
        == "01a129dd-0b79-7613-82a5-6054d7e8034e"
    )


def test_session_for_pane_without_transcript_path():
    """kimi and grok report a session id but no transcriptPath."""
    assert (
        sessions.session_for_pane(KIMI_PANE, path=FIXTURE)
        == "3b0c7f52-6a1e-4d8e-9c2a-7f1e2d3c4b5a"
    )


@pytest.mark.parametrize("pane", [NO_SESSION_PANE, "tab-x:leaf-y", ""])
def test_session_for_pane_without_entry_or_session_is_empty(pane):
    assert sessions.session_for_pane(pane, path=FIXTURE) == ""


def test_session_for_pane_missing_file_is_empty(tmp_path):
    assert sessions.session_for_pane(OMP_PANE, path=tmp_path / "nope.json") == ""


@pytest.mark.parametrize("version", [1, 3, "2", None])
def test_session_for_pane_rejects_unknown_version(tmp_path, version):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if version is None:
        data.pop("version")
    else:
        data["version"] = version
    path = _write_status(tmp_path / "last-status.json", data)

    assert sessions.session_for_pane(OMP_PANE, path=path) == ""


@pytest.mark.parametrize("payload", [
    "{not json",
    "",
    "[]",
    {"version": 2, "entries": []},
    {"version": 2, "entries": {OMP_PANE: "oops"}},
    {"version": 2, "entries": {OMP_PANE: {"providerSession": "oops"}}},
    {"version": 2, "entries": {OMP_PANE: {"providerSession": {"id": 42}}}},
])
def test_session_for_pane_malformed_content_is_empty(tmp_path, payload):
    path = _write_status(tmp_path / "last-status.json", payload)

    assert sessions.session_for_pane(OMP_PANE, path=path) == ""


def test_default_path_is_under_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))

    expected = tmp_path / "orca" / "agent-hooks" / "last-status.json"
    assert sessions.last_status_path() == expected

    expected.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE, expected)
    assert (
        sessions.session_for_pane(OMP_PANE)
        == "01a129dd-0b79-7613-82a5-6054d7e8034e"
    )

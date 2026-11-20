from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from osw import hookcache, sessions


OMP_PANE = (
    "48e25656-ee8c-4acc-8557-4829302a35d9:c4de4fee-2a49-4d5f-8825-7385153e2702"
)
KIMI_PANE = (
    "9f1d2c3b-0000-4000-8000-000000000001:5e6f7a8b-0000-4000-8000-000000000002"
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Every test starts and ends with no parsed generation."""
    hookcache.reset_cache()
    yield
    hookcache.reset_cache()


def _entry(pane_key, session="sess-1", agent="OMP", worktree="wt-uuid::E:\\bit\\x"):
    return {
        "paneKey": pane_key,
        "tabId": pane_key.split(":", 1)[0],
        "worktreeId": worktree,
        "providerSession": {"key": "session_id", "id": session},
        "payload": {"state": "done", "agentType": agent},
        "receivedAt": 1795188092191,
    }


def _database(path: Path, entries=(), schema_version=202) -> Path:
    """A database with Orca's real schema: meta(key, value), rows(key, seq, value)."""
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "CREATE TABLE rows(key TEXT PRIMARY KEY, seq INTEGER NOT NULL, value TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO meta VALUES ('schema_version', ?)", (str(schema_version),)
    )
    for seq, entry in enumerate(entries, start=1):
        connection.execute(
            "INSERT INTO rows VALUES (?, ?, ?)",
            ("entry" + entry["paneKey"], seq, json.dumps(entry)),
        )
    connection.commit()
    connection.close()
    return path


def _json_cache(path: Path, entries, version=2) -> Path:
    path.write_text(
        json.dumps({"version": version, "entries": entries}), encoding="utf-8"
    )
    return path


def _record_connections(monkeypatch):
    """Count the cache's read-only opens, keeping the real connect.

    Fixture setup writes through the same module, so only a `mode=ro`
    URI is counted: that is the one `hookcache` issues for a read.
    """
    real = sqlite3.connect

    class Recorder:
        calls = 0

        def __call__(self, *args, **kwargs):
            target = str(args[0] if args else kwargs.get("database", ""))
            if "mode=ro" in target:
                Recorder.calls += 1
            return real(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", Recorder())
    return Recorder


# ---------------------------------------------------------------------------
# The SQLite cache Orca 1.4.265 writes
# ---------------------------------------------------------------------------

def test_entries_resolve_from_the_sqlite_cache(tmp_path):
    path = _database(tmp_path / "last-status.db", [
        _entry(OMP_PANE, session="01a1f823-bfab", agent="OMP",
               worktree="490f6236-1844-4848-a7e8-b0ccde1e8f3b::E:\\bit\\reseach"),
        _entry(KIMI_PANE, session="3b0c7f52", agent="kimi",
               worktree="7ccb10b3-0829-4db9-b2f5-a3b8237ba526::E:/bit/photo_craft"),
    ])

    cached = hookcache.entries(path)
    assert set(cached) == {OMP_PANE, KIMI_PANE}
    assert cached[OMP_PANE]["payload"]["agentType"] == "OMP"

    assert hookcache.session_id(OMP_PANE, path) == "01a1f823-bfab"
    assert hookcache.session_id(KIMI_PANE, path) == "3b0c7f52"
    assert hookcache.agent_type(OMP_PANE, path) == "omp"
    assert hookcache.agent_type(KIMI_PANE, path) == "kimi"
    assert hookcache.worktree_path(OMP_PANE, path) == "E:\\bit\\reseach"
    assert hookcache.worktree_path(KIMI_PANE, path) == "E:/bit/photo_craft"


def test_unknown_pane_reads_empty(tmp_path):
    path = _database(tmp_path / "last-status.db", [_entry(OMP_PANE)])

    assert hookcache.session_id("tab-x:leaf-y", path) == ""
    assert hookcache.agent_type("tab-x:leaf-y", path) == ""
    assert hookcache.worktree_path("tab-x:leaf-y", path) == ""
    assert hookcache.session_id("", path) == ""


@pytest.mark.parametrize("schema_version", [202, 203, 900])
def test_current_and_newer_schema_versions_are_read(tmp_path, schema_version):
    path = _database(
        tmp_path / "last-status.db", [_entry(OMP_PANE)], schema_version=schema_version
    )

    assert hookcache.session_id(OMP_PANE, path) == "sess-1"


@pytest.mark.parametrize("schema_version", [201, 1, 0, "202a", ""])
def test_unknown_schema_version_reads_empty(tmp_path, schema_version):
    path = _database(
        tmp_path / "last-status.db", [_entry(OMP_PANE)], schema_version=schema_version
    )

    assert hookcache.entries(path) == {}
    assert hookcache.session_id(OMP_PANE, path) == ""


def test_schema_without_a_version_row_reads_empty(tmp_path):
    path = tmp_path / "last-status.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "CREATE TABLE rows(key TEXT PRIMARY KEY, seq INTEGER NOT NULL, value TEXT NOT NULL)"
    )
    connection.commit()
    connection.close()

    assert hookcache.entries(path) == {}


def test_rows_that_are_not_entries_are_skipped(tmp_path):
    path = _database(tmp_path / "last-status.db", [_entry(OMP_PANE)])
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO rows VALUES ('entrybad', 9, '{not json')")
    connection.execute("INSERT INTO rows VALUES ('entrylist', 10, '[]')")
    connection.execute("INSERT INTO rows VALUES ('entrynokey', 11, '{}')")
    connection.execute("INSERT INTO rows VALUES ('other', 12, '{}')")
    connection.commit()
    connection.close()

    assert set(hookcache.entries(path)) == {OMP_PANE}


def test_corrupt_database_reads_empty(tmp_path):
    path = tmp_path / "last-status.db"
    path.write_bytes(b"this is not a database" * 100)

    assert hookcache.entries(path) == {}
    assert hookcache.session_id(OMP_PANE, path) == ""


def test_corrupt_database_falls_back_to_the_legacy_json(tmp_path):
    path = tmp_path / "last-status.db"
    path.write_bytes(b"this is not a database" * 100)
    _json_cache(tmp_path / "last-status.json", {OMP_PANE: _entry(OMP_PANE)})

    assert hookcache.session_id(OMP_PANE, path) == "sess-1"
    assert hookcache.agent_type(OMP_PANE, path) == "omp"


def test_missing_caches_read_empty(tmp_path):
    assert hookcache.entries(tmp_path / "last-status.db") == {}
    assert hookcache.session_id(OMP_PANE, tmp_path / "last-status.db") == ""

    directory = tmp_path / "directory"
    directory.mkdir()
    assert hookcache.entries(directory) == {}


# ---------------------------------------------------------------------------
# The legacy JSON cache, still what older Orca releases write
# ---------------------------------------------------------------------------

def test_legacy_json_resolves_when_there_is_no_database(tmp_path):
    _json_cache(tmp_path / "last-status.json", {
        OMP_PANE: _entry(OMP_PANE, session="legacy-sess",
                         worktree="wt-uuid::E:\\bit\\legacy"),
    })

    assert hookcache.session_id(OMP_PANE, tmp_path / "last-status.db") == "legacy-sess"
    assert hookcache.worktree_path(
        OMP_PANE, tmp_path / "last-status.db"
    ) == "E:\\bit\\legacy"


@pytest.mark.parametrize("version", [1, 3, "2", None])
def test_legacy_json_with_another_version_reads_empty(tmp_path, version):
    path = tmp_path / "last-status.json"
    data = {"entries": {OMP_PANE: _entry(OMP_PANE)}}
    if version is not None:
        data["version"] = version
    path.write_text(json.dumps(data), encoding="utf-8")

    assert hookcache.entries(path) == {}


def test_a_named_json_path_skips_the_database(tmp_path):
    path = _database(tmp_path / "last-status.db", [_entry(OMP_PANE, session="from-db")])
    _json_cache(tmp_path / "last-status.json", {
        OMP_PANE: _entry(OMP_PANE, session="from-json"),
    })

    assert hookcache.session_id(OMP_PANE, path) == "from-db"
    assert hookcache.session_id(OMP_PANE, tmp_path / "last-status.json") == "from-json"


def test_the_default_path_follows_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    hooks = tmp_path / "orca" / "agent-hooks"
    hooks.mkdir(parents=True)
    _database(hooks / "last-status.db", [_entry(OMP_PANE, session="default-db")])

    assert hookcache.default_path() == hooks / "last-status.db"
    assert hookcache.session_id(OMP_PANE) == "default-db"


# ---------------------------------------------------------------------------
# Caching rule: one open per generation
# ---------------------------------------------------------------------------

def test_second_lookup_does_not_reopen_the_database(tmp_path, monkeypatch):
    path = _database(tmp_path / "last-status.db", [_entry(OMP_PANE)])
    recorder = _record_connections(monkeypatch)

    assert hookcache.session_id(OMP_PANE, path) == "sess-1"
    assert hookcache.session_id(OMP_PANE, path) == "sess-1"
    assert hookcache.agent_type(OMP_PANE, path) == "omp"
    assert hookcache.worktree_path(OMP_PANE, path) == "E:\\bit\\x"
    assert set(hookcache.entries(path)) == {OMP_PANE}

    assert recorder.calls == 1


def test_a_corrupt_database_is_not_reopened_within_a_generation(tmp_path, monkeypatch):
    path = tmp_path / "last-status.db"
    path.write_bytes(b"this is not a database" * 100)
    _json_cache(tmp_path / "last-status.json", {OMP_PANE: _entry(OMP_PANE)})
    recorder = _record_connections(monkeypatch)

    assert hookcache.session_id(OMP_PANE, path) == "sess-1"
    assert hookcache.session_id(OMP_PANE, path) == "sess-1"

    assert recorder.calls == 1


def test_a_missing_database_is_never_opened(tmp_path, monkeypatch):
    _json_cache(tmp_path / "last-status.json", {OMP_PANE: _entry(OMP_PANE)})
    recorder = _record_connections(monkeypatch)

    assert hookcache.session_id(OMP_PANE, tmp_path / "last-status.db") == "sess-1"
    assert recorder.calls == 0


def test_a_changed_file_starts_a_new_generation(tmp_path, monkeypatch):
    path = _database(tmp_path / "last-status.db", [_entry(OMP_PANE, session="one")])
    recorder = _record_connections(monkeypatch)
    assert hookcache.session_id(OMP_PANE, path) == "one"

    path.unlink()
    _database(path, [_entry(OMP_PANE, session="two")])
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))

    assert hookcache.session_id(OMP_PANE, path) == "two"
    assert recorder.calls == 2


def test_reset_cache_forces_a_reread(tmp_path, monkeypatch):
    path = _database(tmp_path / "last-status.db", [_entry(OMP_PANE)])
    recorder = _record_connections(monkeypatch)

    assert hookcache.session_id(OMP_PANE, path) == "sess-1"
    assert recorder.calls == 1

    hookcache.reset_cache()
    assert hookcache.session_id(OMP_PANE, path) == "sess-1"
    assert recorder.calls == 2


def test_entries_returns_a_copy_of_the_mapping(tmp_path):
    path = _database(tmp_path / "last-status.db", [_entry(OMP_PANE)])

    hookcache.entries(path).pop(OMP_PANE)
    assert hookcache.session_id(OMP_PANE, path) == "sess-1"


# ---------------------------------------------------------------------------
# sessions.py routing: SQLite first, legacy JSON second
# ---------------------------------------------------------------------------

def test_session_for_pane_reads_the_database_first(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    hooks = tmp_path / "orca" / "agent-hooks"
    hooks.mkdir(parents=True)
    _database(hooks / "last-status.db", [_entry(OMP_PANE, session="db-session")])
    _json_cache(hooks / "last-status.json", {
        OMP_PANE: _entry(OMP_PANE, session="json-session"),
    })

    assert sessions.session_for_pane(OMP_PANE) == "db-session"


def test_session_for_pane_falls_back_to_the_legacy_json(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    hooks = tmp_path / "orca" / "agent-hooks"
    hooks.mkdir(parents=True)
    _json_cache(hooks / "last-status.json", {
        OMP_PANE: _entry(OMP_PANE, session="json-session"),
    })

    assert sessions.session_for_pane(OMP_PANE) == "json-session"


def test_newer_orca_is_not_a_new_constant_in_sessions():
    assert sessions.LAST_STATUS_VERSION == hookcache.LEGACY_JSON_VERSION
    assert sessions.HOOK_CACHE_SCHEMA_VERSION == hookcache.SQLITE_SCHEMA_VERSION

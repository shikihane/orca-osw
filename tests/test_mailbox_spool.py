"""Spool slice: atomic producer writes, single-writer ingest, failure isolation."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from osw.mailbox import envelope, spool
from osw.mailbox.errors import CODES, MailboxError
from osw.mailbox.paths import (
    ensure_mailbox_dirs,
    ingested_dir,
    rejected_dir,
    spool_dir,
)


class FakeStore:
    """Store contract subset: ``enqueue(envelope) -> (row | None, inserted)``."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    def enqueue(self, env: dict) -> tuple[dict | None, bool]:
        with self._lock:
            if env["id"] in self.rows:
                return None, False
            self.rows[env["id"]] = env
            return env, True


class BrokenStore(FakeStore):
    def __init__(self, code: str = "store_corrupt") -> None:
        super().__init__()
        self.code = code

    def enqueue(self, env: dict) -> tuple[dict | None, bool]:
        raise MailboxError(self.code, "boom")


def make_env(**overrides) -> dict:
    env = {
        "v": 1,
        "id": envelope.new_id(),
        "ts": "2026-11-14T13:15:00.123Z",
        "from": {"role": "human", "id": "terminal:term_abc", "trust": "trusted"},
        "to": {"role": "router", "id": ""},
        "kind": "request",
        "correlation": {
            "ticket": "",
            "agent": "",
            "dispatch": "",
            "ask": "",
            "decision": "",
        },
        "thread_key": "",
        "reply_to": "",
        "priority": 0,
        "ttl_s": 0,
        "dedupe_key": "",
        "body": {"text": "hello"},
        "state": "queued",
        "attempts": 0,
        "lease": None,
        "trace": "",
    }
    env.update(overrides)
    return env


def spool_files(root: Path) -> list[Path]:
    directory = spool_dir(root)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file())


def tmp_files(root: Path) -> list[Path]:
    directory = spool_dir(root)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.rglob("*.tmp"))


def error_text(root: Path, name: str) -> str:
    return (rejected_dir(root) / f"{name}.error.txt").read_text(encoding="utf-8")


def error_code(root: Path, name: str) -> str:
    return error_text(root, name).splitlines()[0]


# --- write_envelope -------------------------------------------------------


def test_write_envelope_lands_one_valid_file(tmp_path):
    env = make_env()

    message_id = spool.write_envelope(tmp_path, env)

    assert message_id == env["id"]
    final = spool_dir(tmp_path) / f"{message_id}.json"
    assert final.is_file()
    stored = json.loads(final.read_text(encoding="utf-8"))
    assert stored["id"] == message_id
    assert stored["v"] == 1
    assert stored["state"] == "queued"
    assert spool_files(tmp_path) == [final]
    assert tmp_files(tmp_path) == []


def test_write_envelope_fills_defaults(tmp_path):
    message_id = spool.write_envelope(
        tmp_path,
        {
            "from": {"role": "human", "id": "terminal:term_abc"},
            "to": {"role": "router"},
            "kind": "request",
            "body": {"text": "hi"},
        },
    )

    stored = json.loads((spool_dir(tmp_path) / f"{message_id}.json").read_text("utf-8"))
    assert stored["id"] == message_id
    assert stored["attempts"] == 0
    assert stored["lease"] is None
    assert stored["priority"] == 0
    assert set(stored["correlation"]) == {"ticket", "agent", "dispatch", "ask", "decision"}


def test_write_envelope_rejects_invalid_before_touching_the_spool(tmp_path):
    with pytest.raises(MailboxError) as raised:
        spool.write_envelope(tmp_path, make_env(kind="not-a-kind"))

    assert raised.value.code in {"invalid_envelope", "unknown_kind"}
    assert not spool_dir(tmp_path).exists()  # refused before anything is written
    assert spool_files(tmp_path) == []
    assert tmp_files(tmp_path) == []


# --- ingest ---------------------------------------------------------------


def test_ingest_moves_valid_files_into_ingested(tmp_path):
    store = FakeStore()
    ids = [spool.write_envelope(tmp_path, make_env(body={"text": f"n{i}"})) for i in range(3)]

    counts = spool.ingest(tmp_path, store)

    assert counts == {"ingested": 3, "duplicates": 0, "rejected": 0}
    assert sorted(store.rows) == sorted(ids)
    assert spool.pending_count(tmp_path) == 0
    assert sorted(p.name for p in ingested_dir(tmp_path).iterdir()) == sorted(
        f"{i}.json" for i in ids
    )
    assert list(rejected_dir(tmp_path).iterdir()) == []
    assert tmp_files(tmp_path) == []


def test_ingest_respects_limit(tmp_path):
    store = FakeStore()
    for i in range(5):
        spool.write_envelope(tmp_path, make_env(body={"text": f"n{i}"}))

    counts = spool.ingest(tmp_path, store, limit=2)

    assert counts["ingested"] == 2
    assert spool.pending_count(tmp_path) == 3


def test_ingest_counts_duplicate_message_id_as_duplicate(tmp_path):
    store = FakeStore()
    env = make_env()
    spool.write_envelope(tmp_path, env)
    first = spool.ingest(tmp_path, store)
    spool.write_envelope(tmp_path, env)  # same id re-sent by a retrying producer

    second = spool.ingest(tmp_path, store)

    assert first == {"ingested": 1, "duplicates": 0, "rejected": 0}
    assert second == {"ingested": 0, "duplicates": 1, "rejected": 0}
    assert list(store.rows) == [env["id"]]
    assert spool.pending_count(tmp_path) == 0


def test_ingest_treats_message_already_in_store_as_duplicate(tmp_path):
    store = FakeStore()
    env = make_env()
    store.enqueue(env)  # the mailbox already holds this id (e.g. after a restart)
    spool.write_envelope(tmp_path, env)

    counts = spool.ingest(tmp_path, store)

    assert counts == {"ingested": 0, "duplicates": 1, "rejected": 0}
    assert len(store.rows) == 1
    assert spool.pending_count(tmp_path) == 0


def test_ingest_rejects_invalid_json_and_keeps_going(tmp_path):
    ensure_mailbox_dirs(tmp_path)
    store = FakeStore()
    bad = spool_dir(tmp_path) / "msg_broken.json"
    bad.write_text("{not json", encoding="utf-8")
    good_id = spool.write_envelope(tmp_path, make_env())

    counts = spool.ingest(tmp_path, store)

    assert counts == {"ingested": 1, "duplicates": 0, "rejected": 1}
    assert list(store.rows) == [good_id]
    assert (rejected_dir(tmp_path) / "msg_broken.json").is_file()
    assert error_code(tmp_path, "msg_broken.json") == "invalid_envelope"
    assert spool.pending_count(tmp_path) == 0


def test_ingest_rejects_invalid_envelope_with_error_code(tmp_path):
    ensure_mailbox_dirs(tmp_path)
    store = FakeStore()
    bad = make_env(to={"role": "nobody"})
    (spool_dir(tmp_path) / f"{bad['id']}.json").write_text(
        json.dumps(bad), encoding="utf-8"
    )

    counts = spool.ingest(tmp_path, store)

    assert counts == {"ingested": 0, "duplicates": 0, "rejected": 1}
    assert store.rows == {}
    assert (rejected_dir(tmp_path) / f"{bad['id']}.json").is_file()
    code = error_code(tmp_path, f"{bad['id']}.json")
    assert code in CODES
    assert code in {"invalid_envelope", "unknown_role"}


def test_ingest_rejects_when_the_store_raises(tmp_path):
    store = BrokenStore("store_corrupt")
    env = make_env()
    spool.write_envelope(tmp_path, env)

    counts = spool.ingest(tmp_path, store)

    assert counts == {"ingested": 0, "duplicates": 0, "rejected": 1}
    assert (rejected_dir(tmp_path) / f"{env['id']}.json").is_file()
    assert error_code(tmp_path, f"{env['id']}.json") == "store_corrupt"
    assert spool.pending_count(tmp_path) == 0


def test_ingest_on_missing_spool_is_harmless(tmp_path):
    assert spool.ingest(tmp_path, FakeStore()) == {
        "ingested": 0,
        "duplicates": 0,
        "rejected": 0,
    }
    assert spool.pending_count(tmp_path) == 0
    assert spool.read_pending(tmp_path) == []


# --- cap ------------------------------------------------------------------


def test_cap_constant_is_one_thousand():
    assert spool.MAX_SPOOL_FILES == 1000


def test_write_envelope_refuses_over_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(spool, "MAX_SPOOL_FILES", 3)
    store = FakeStore()
    for i in range(3):
        spool.write_envelope(tmp_path, make_env(body={"text": f"n{i}"}))

    with pytest.raises(MailboxError) as raised:
        spool.write_envelope(tmp_path, make_env(body={"text": "over"}))

    assert raised.value.code == "spool_full"
    assert spool.pending_count(tmp_path) == 3
    assert tmp_files(tmp_path) == []

    spool.ingest(tmp_path, store, limit=1)
    spool.write_envelope(tmp_path, make_env(body={"text": "room again"}))
    assert spool.pending_count(tmp_path) == 3


# --- bodies and pending views --------------------------------------------


def test_unicode_body_round_trips(tmp_path):
    store = FakeStore()
    body = {"text": "中文内容 · emoji 🚀 · combining é"}
    env = make_env(body=body)

    spool.write_envelope(tmp_path, env)
    on_disk = (spool_dir(tmp_path) / f"{env['id']}.json").read_text(encoding="utf-8")
    assert "中文内容" in on_disk  # ensure_ascii=False, not \\u escapes
    spool.ingest(tmp_path, store)

    assert store.rows[env["id"]]["body"] == body


def test_pending_count_and_read_pending_ignore_tmp_and_subdirs(tmp_path):
    first = spool.write_envelope(tmp_path, make_env(body={"text": "one"}))
    second = spool.write_envelope(tmp_path, make_env(body={"text": "two"}))
    (spool_dir(tmp_path) / "msg_half.json.tmp").write_text("{half", encoding="utf-8")
    (spool_dir(tmp_path) / "msg_garbage.json").write_text("{nope", encoding="utf-8")

    assert spool.pending_count(tmp_path) == 3
    pending = spool.read_pending(tmp_path)
    assert sorted(env["id"] for env in pending) == sorted([first, second])


def test_read_pending_returns_validated_envelopes(tmp_path):
    spool.write_envelope(
        tmp_path,
        {
            "from": {"role": "worker", "id": "agent_001"},
            "to": {"role": "router"},
            "kind": "report",
            "body": {"outcome": "ready", "summary": "done"},
        },
    )

    pending = spool.read_pending(tmp_path)

    assert len(pending) == 1
    assert pending[0]["kind"] == "report"
    assert pending[0]["from"]["trust"] == "untrusted"
    assert pending[0]["id"].startswith("msg_")


# --- concurrency ----------------------------------------------------------


def test_concurrent_writer_reader_and_two_ingesters(tmp_path):
    store = FakeStore()
    total = 40
    writer_done = threading.Event()
    stop = threading.Event()
    failures: list[BaseException] = []
    seen = {"ingested": 0, "duplicates": 0, "rejected": 0}
    seen_lock = threading.Lock()
    ids: list[str] = []
    ids_lock = threading.Lock()

    def writer() -> None:
        try:
            for i in range(total):
                message_id = spool.write_envelope(
                    tmp_path, make_env(body={"text": f"payload {i}"})
                )
                with ids_lock:
                    ids.append(message_id)
        except BaseException as exc:  # noqa: BLE001 - reported by the main thread
            failures.append(exc)
        finally:
            writer_done.set()

    def ingester() -> None:
        try:
            while not stop.is_set():
                counts = spool.ingest(tmp_path, store, limit=5)
                with seen_lock:
                    for key, value in counts.items():
                        seen[key] += value
                time.sleep(0.005)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                for env in spool.read_pending(tmp_path):
                    assert env["id"].startswith("msg_")
                    assert env["state"] == "queued"
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=ingester),
        threading.Thread(target=ingester),
        threading.Thread(target=reader),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 60
    while len(store.rows) < total and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert failures == []
    assert writer_done.is_set()
    assert sorted(store.rows) == sorted(ids)
    assert len(store.rows) == total
    assert spool.pending_count(tmp_path) == 0
    # Every file reaches the store exactly once. On Windows a rename goes
    # through a handle and is idempotent, so two ingesters that race may both
    # claim one file; the store's id keeps the message single and the loser is
    # counted as a duplicate.
    assert seen["ingested"] == total
    assert seen["ingested"] + seen["duplicates"] >= total
    assert seen["rejected"] == 0
    assert list(rejected_dir(tmp_path).iterdir()) == []
    assert tmp_files(tmp_path) == []

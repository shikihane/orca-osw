from __future__ import annotations

import json
import subprocess
import sys

from osw.log import read_events
from osw.mailbox import adapters, paths, spool
from osw.state import logs_dir


def _write_config(root, **payload) -> None:
    path = adapters.config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _pending(root) -> list[dict]:
    return sorted(
        spool.read_pending(root),
        key=lambda env: (env["body"].get("summary", ""), env["body"].get("count", 0)),
    )


def _run(root):
    return adapters.run_once(root)


def test_run_once_without_config_reports_every_adapter(tmp_path):
    counts = _run(tmp_path)

    assert set(counts) == set(adapters.ADAPTERS)
    assert all(count["envelopes"] == 0 for count in counts.values())
    assert not paths.mailbox_dir(tmp_path).exists()


def test_log_file_aggregates_a_burst_into_one_digest(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("alpha\nbeta\n", encoding="utf-8")
    _write_config(tmp_path, log_file={"paths": [str(log)]})

    counts = _run(tmp_path)

    assert counts["log_file"] == {"envelopes": 1, "matched": 2}
    envelopes = _pending(tmp_path)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env["kind"] == "system"
    assert env["from"]["role"] == "system"
    assert env["from"]["id"] == "adapter:log_file"
    assert env["thread_key"] == str(log)
    assert env["priority"] == 3
    assert env["body"] == {
        "event": "log_line",
        "summary": "alpha",
        "count": 2,
        "path": str(log),
    }
    state = adapters.load_state(tmp_path, "log_file")
    assert state["files"][str(log)]["offset"] == log.stat().st_size

    # Nothing new: no envelope, no extra spool file.
    assert _run(tmp_path)["log_file"] == {"envelopes": 0, "matched": 0}
    assert len(_pending(tmp_path)) == 1


def test_log_file_rotation_restarts_the_read_position(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("one\n", encoding="utf-8")
    _write_config(tmp_path, log_file={"paths": [str(log)]})
    assert _run(tmp_path)["log_file"]["envelopes"] == 1

    rotated = tmp_path / "app.log.1"
    log.rename(rotated)
    # Longer than the old offset, so only the file identity reveals rotation.
    log.write_text("two longer generation\n", encoding="utf-8")

    counts = _run(tmp_path)

    assert counts["log_file"]["envelopes"] == 1
    assert [env["body"]["summary"] for env in _pending(tmp_path)] == [
        "one",
        "two longer generation",
    ]
    # The rotated file is not configured, so it is never read.
    assert _run(tmp_path)["log_file"]["envelopes"] == 0


def test_log_file_truncation_restarts_the_read_position(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("a long first generation line\n", encoding="utf-8")
    _write_config(tmp_path, log_file={"paths": [str(log)]})
    assert _run(tmp_path)["log_file"]["envelopes"] == 1

    log.write_text("short\n", encoding="utf-8")

    counts = _run(tmp_path)

    assert counts["log_file"] == {"envelopes": 1, "matched": 1}
    assert [env["body"]["summary"] for env in _pending(tmp_path)] == [
        "a long first generation line",
        "short",
    ]


def test_crash_file_matches_a_signature(tmp_path):
    crash = tmp_path / "crash.log"
    crash.write_text("", encoding="utf-8")
    _write_config(tmp_path, crash_file={"paths": [str(crash)]})
    assert _run(tmp_path)["crash_file"]["envelopes"] == 0

    with crash.open("a", encoding="utf-8") as handle:
        handle.write("starting up\n")
    assert _run(tmp_path)["crash_file"]["envelopes"] == 0

    with crash.open("a", encoding="utf-8") as handle:
        handle.write("*** panic: kernel heap corrupt\nSIGSEGV in module 7\n")

    counts = _run(tmp_path)

    assert counts["crash_file"] == {"envelopes": 1, "matched": 2}
    env = _pending(tmp_path)[0]
    assert env["body"]["event"] == "crash"
    assert env["body"]["count"] == 2
    assert "panic: kernel heap corrupt" in env["body"]["summary"]
    assert env["thread_key"] == str(crash)


def test_crash_file_new_file_is_a_trigger(tmp_path):
    crash = tmp_path / "crash.log"
    crash.write_text("something broke badly\n", encoding="utf-8")
    _write_config(tmp_path, crash_file={"paths": [str(crash)]})

    counts = _run(tmp_path)

    assert counts["crash_file"]["envelopes"] == 1
    assert _pending(tmp_path)[0]["body"]["event"] == "crash"


def test_build_result_reports_a_failure_line(tmp_path):
    build = tmp_path / "build.log"
    build.write_text("compiling module\nBUILD FAILED in 4s\n", encoding="utf-8")
    _write_config(tmp_path, build_result={"paths": [str(build)]})

    counts = _run(tmp_path)

    assert counts["build_result"] == {"envelopes": 1, "matched": 1}
    env = _pending(tmp_path)[0]
    assert env["body"]["event"] == "build_failed"
    assert env["body"]["summary"] == "BUILD FAILED in 4s"
    assert env["thread_key"] == str(build)


def test_adapters_keep_independent_read_positions(tmp_path):
    shared = tmp_path / "shared.log"
    shared.write_text("starting\npanic: yes\n", encoding="utf-8")
    _write_config(
        tmp_path,
        log_file={"paths": [str(shared)]},
        crash_file={"paths": [str(shared)]},
    )

    counts = _run(tmp_path)

    assert counts["log_file"]["envelopes"] == 1
    assert counts["crash_file"]["envelopes"] == 1

    with shared.open("a", encoding="utf-8") as handle:
        handle.write("plain progress\n")
    counts = _run(tmp_path)

    assert counts["log_file"]["envelopes"] == 1
    assert counts["crash_file"]["envelopes"] == 0
    size = shared.stat().st_size
    assert adapters.load_state(tmp_path, "log_file")["files"][str(shared)]["offset"] == size
    assert adapters.load_state(tmp_path, "crash_file")["files"][str(shared)]["offset"] == size


def _spawn_exit(code: int = 3) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import sys; sys.exit({code})"])
    proc.wait(timeout=30)
    return proc


def test_process_exit_reports_an_exit_once(tmp_path):
    proc = _spawn_exit(3)
    _write_config(tmp_path, process_exit={"pids": [proc.pid]})

    counts = _run(tmp_path)

    assert counts["process_exit"] == {"envelopes": 1, "matched": 1}
    env = _pending(tmp_path)[0]
    assert env["body"]["event"] == "process_exit"
    assert env["body"]["path"] == ""
    assert env["body"]["summary"].startswith(f"pid {proc.pid} exited")
    assert env["thread_key"] == str(proc.pid)

    # Once reported, the pid is not reported again.
    assert _run(tmp_path)["process_exit"]["envelopes"] == 0
    assert len(_pending(tmp_path)) == 1


def test_process_exit_ignores_a_live_pid(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    _write_config(tmp_path, process_exit={"pids": [proc.pid]})
    try:
        assert _run(tmp_path)["process_exit"]["envelopes"] == 0
        assert _pending(tmp_path) == []
    finally:
        proc.terminate()
        proc.wait(timeout=30)

    assert _run(tmp_path)["process_exit"]["envelopes"] == 1


def test_adapter_failure_is_isolated_and_reported_once(tmp_path, monkeypatch):
    log = tmp_path / "app.log"
    log.write_text("alpha\n", encoding="utf-8")
    crash = tmp_path / "crash.log"
    crash.write_text("panic: boom\n", encoding="utf-8")
    _write_config(
        tmp_path,
        log_file={"paths": [str(log)]},
        crash_file={"paths": [str(crash)]},
    )

    def boom(_root, _cfg, _state, _now):
        raise RuntimeError("adapter exploded")

    monkeypatch.setitem(adapters._HANDLERS, "log_file", boom)

    counts = _run(tmp_path)

    assert counts["log_file"]["failed"] is True
    assert "adapter exploded" in counts["log_file"]["error"]
    assert counts["crash_file"] == {"envelopes": 1, "matched": 1}
    assert [env["body"]["event"] for env in _pending(tmp_path)] == ["crash"]

    failed_events = [
        event for event in read_events(logs_dir(tmp_path))
        if event["event"] == "adapter_failed"
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["data"] == {"adapter": "log_file"}

    state = adapters.load_state(tmp_path, "log_file")
    assert state["disabled"] is True
    assert "adapter exploded" in state["error"]
    assert not adapters.load_state(tmp_path, "crash_file").get("disabled")

    # The disabled adapter stays off; the healthy one keeps its own state.
    counts = _run(tmp_path)
    assert counts["log_file"]["disabled"] is True
    assert counts["crash_file"]["envelopes"] == 0
    assert len([
        event for event in read_events(logs_dir(tmp_path))
        if event["event"] == "adapter_failed"
    ]) == 1


def test_config_can_disable_one_adapter(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("alpha\n", encoding="utf-8")
    build = tmp_path / "build.log"
    build.write_text("BUILD FAILED\n", encoding="utf-8")
    _write_config(
        tmp_path,
        enabled={"log_file": False},
        log_file={"paths": [str(log)]},
        build_result={"paths": [str(build)]},
    )

    counts = _run(tmp_path)

    assert counts["log_file"]["disabled"] is True
    assert counts["log_file"]["envelopes"] == 0
    assert counts["build_result"]["envelopes"] == 1
    assert [env["body"]["event"] for env in _pending(tmp_path)] == ["build_failed"]


def test_digest_dedupe(tmp_path):
    crash = tmp_path / "crash.log"
    crash.write_text("", encoding="utf-8")
    _write_config(tmp_path, crash_file={"paths": [str(crash)]})
    assert _run(tmp_path)["crash_file"]["envelopes"] == 0

    with crash.open("a", encoding="utf-8") as handle:
        handle.write("panic: same signature\n")
        handle.write("padding line that lengthens the file beyond the rewrite\n")
    assert _run(tmp_path)["crash_file"]["envelopes"] == 1

    # Truncate and rewrite the same signature: the position restarts, the
    # digest is recognised and not emitted a second time.
    crash.write_text("panic: same signature\n", encoding="utf-8")
    counts = _run(tmp_path)

    assert counts["crash_file"] == {"envelopes": 0, "matched": 1}
    assert len(_pending(tmp_path)) == 1

    with crash.open("a", encoding="utf-8") as handle:
        handle.write("segfault: different\n")
    assert _run(tmp_path)["crash_file"]["envelopes"] == 1
    assert [env["body"]["summary"] for env in _pending(tmp_path)] == [
        "panic: same signature",
        "segfault: different",
    ]

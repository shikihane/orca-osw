# Windows Agent-State Lock Recovery Design

## Context

On Windows, replacing an agent-state JSON file can fail with
`PermissionError` while another process briefly has the target open for
reading. OSW currently performs one `os.replace()` call using a fixed
`<target>.tmp` path. A sharing conflict therefore escapes through
`Watcher._update()` and is misclassified as `watcher_crash`, even though the
provider terminal may still be working.

OSW itself does not retain agent-state read handles. `read_agent()` and
`list_agents()` use context managers and close each small JSON file
immediately after parsing. The expected failure is therefore a short
read/write collision, not a permanent lock. A failure that outlasts the retry
window indicates an unusual external holder, repeated starvation, or a real
permission problem.

## Goals

- Allow state replacement to survive short Windows sharing conflicts.
- Prevent concurrent writes from sharing a temporary path.
- Keep an active watcher supervising its provider after a state write fails.
- Retry an unpersisted in-memory state during the watcher's normal polling.
- Report exhausted persistence attempts as state-storage failures, not task or
  provider failures.
- Clean up temporary files after successful and failed writes.

## Non-goals

- No separate persistence process or background task.
- No unbounded retry loop.
- No attempt to identify or terminate an external process holding the file.
- No change to the agent-state JSON schema.

## State-Layer Design

`_atomic_write_json()` will create a uniquely named temporary file in the
target directory. Keeping it in the same directory preserves same-filesystem
replacement semantics, while a unique name prevents writers from truncating
or replacing one another's temporary files.

After serializing the complete payload, the function will call `os.replace()`.
Windows sharing errors identified by `winerror` 5 or 32 will receive a bounded
backoff with jitter. Retries use a monotonic 1.0-second deadline, begin with a
10-millisecond delay, double up to a 100-millisecond cap, and never sleep past
the remaining deadline. This is long enough to outlive normal OSW reads
without hiding persistent permission or environmental faults.

If replacement still fails, the state layer will raise a dedicated
`StateWriteError` containing the destination and original OS error. Only
replace failures carrying `winerror` 5 or 32 are retried; errors without one
of those Windows codes fail immediately. A `finally` cleanup will remove the
unique temporary file when it still exists.

## Watcher Design

The watcher treats `self.agent` as its desired in-memory state and tracks
whether that state is dirty. `_update()` merges fields into memory, marks the
state dirty, and attempts to flush it. A `StateWriteError` does not escape the
watcher state machine; the watcher logs a structured `state_storage_error`
event and continues supervising the provider.

While dirty, normal watcher polling attempts another flush at the existing
poll interval. This supplies periodic recovery without introducing another
task, thread, or queue. Only the first failure in an outage is reported. A
successful later flush clears the dirty flag and emits one
`state_storage_recovered` event.

Provider completion, timeout, and handoff decisions continue to use the
in-memory state. If the task finishes while persistence remains unavailable,
OSW still writes the independent completion report where possible and records
the storage-specific failure. It must not rewrite the outcome as
`watcher_crash` or provider failure.

## Error Boundaries

- Recognized transient replace conflict: retry inside the state layer.
- Retry window exhausted: raise `StateWriteError` to the watcher.
- Watcher receives `StateWriteError`: retain dirty state, report once, and
  continue provider supervision.
- Persistence recovers: atomically write the newest full in-memory state and
  report recovery once.
- Unrelated state or programming error: preserve the existing fatal-error
  behavior rather than suppressing it.

## Test Strategy

State tests will cover:

1. A Windows reader released after a short delay allows the write to succeed.
2. A reader held beyond the retry window produces `StateWriteError` within a
   bounded time and leaves no temporary file.
3. Concurrent writers use distinct temporary paths and leave valid JSON.
4. Repeated `list_agents()` calls competing with writes do not expose partial
   JSON or temporary files as agent records.
5. Non-sharing errors are not retried.

Watcher tests will use mocked Orca calls, as required by the repository test
guidelines, and will cover:

1. A state flush failure does not terminate active supervision.
2. Dirty state is retried during normal polling and eventually persisted.
3. Failure and recovery events are emitted once per outage.
4. A storage failure is not recorded as `watcher_crash` or task failure.

The focused state and watcher suites will run first, followed by the complete
`pytest -q` suite.

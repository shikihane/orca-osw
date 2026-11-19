"""Write a workflow definition from one sentence, then hand it to a person.

A definition is a closed-schema JSON file, which is the right *storage* shape
and the wrong *entry point*: nobody should have to hand-write phase ids and
barriers to run a review. This module turns a request into a definition the
only way that stays inside the project's rules — one ordinary agent, a prompt
carrying the schema, and the existing validator as the judge. The agent writes
the file; ``defs`` decides whether it is usable; the person sees the expanded
plan before anything runs.

Nothing here is a second scheduler: no run exists yet, no step is dispatched,
and the file the agent writes is ordinary input to ``workflow run``.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from osw import state
from osw.log import get_logger
from osw.workflow import defs, executor

log = get_logger("workflow.author")

#: How long one planning agent may take before the person is told it stalled.
PLAN_TIMEOUT_SECONDS = 900.0

#: Poll interval while waiting for the planning agent's turn to end.
PLAN_POLL_SECONDS = 3.0

Dispatch = Callable[[Mapping[str, Any], Any, Path], dict]
Wait = Callable[[Path, str], str]

_TERMINAL = ("done", "error", "lost")

_PROMPT = """Write exactly one file and nothing else: __PATH__

That file is an OSW workflow definition (JSON, closed schema). The person asked for:

<<<
__REQUEST__
>>>

Schema, with no keys beyond these:

  {"name": "__NAME__",
   "max_agents": <integer, at most 64>,
   "goal": "<one sentence: the end state the person asked for>",
   "max_rounds": <integer 1..20, default 1>,
   "phases": [
     {"id": "<short slug>",
      "mode": "single" | "for_each",
      "from": ["<id of an earlier phase>"],   // only when this phase consumes that phase's output
      "task": "<the complete instruction one agent receives>",
      "criteria": "<how a blind judge decides this phase reached its goal>",
      "evidence": ["<repo-relative path or glob of a file the judge reads>"],
      "repeat": false}
   ]}

Rules:
- Use 2 to 6 phases. The first phase discovers the work; a later for_each phase
  does the bulk work; a final single phase summarises.
- Write goal: one sentence naming the end state the person asked for. It is
  run-level text and holds no placeholder of any kind: no {{output}}, no
  {{item}}, no {{item.<field>}}.
- Every phase whose product is a deliverable declares criteria (judgeable
  offline from the files on disk, written before the work runs) and evidence
  (the repo-relative file globs the judge reads). A for_each phase's criteria
  may use {{item}} and {{item.<field>}}; a single phase's criteria may not.
  criteria is never given {{output}}: the judge reads the step's outputs itself.
  Phrase criteria as what one command proves — name the command and the output
  it must show — not as a count of source lines or a wording check that a
  reformat can break without changing behaviour.
- If the request is open-ended ("continue until X", "推进到完成"), the phase
  that does the bulk work is "repeat": true and max_rounds is 3 unless the
  request says otherwise. repeat belongs only to a for_each phase and needs
  non-empty from, non-empty criteria and max_rounds greater than 1.
- Size max_agents for the whole run, not for one phase: every for_each unit,
  one gate agent per phase that declares criteria, and every round a repeat
  phase may take all draw on the same ceiling. A ceiling below that is refused
  with too_many_agents only after the earlier phases have already been paid for,
  so count them before choosing the number.
- The final phase's criteria must restate the goal, not "a summary exists".
- A phase whose results a later phase consumes must write a JSON array to the
  path __OUTPUT__. Its task must say to write exactly that file.
- A for_each phase's task uses {{item}} (or {{item.<field>}} for an object
  field) and must not refer to the other items.
- Every evidence entry is literal: a relative path or glob, with no
  placeholder, no ".." and no drive letter.
- These are the only placeholders that exist: {{output}}, {{item}},
  {{item.<field>}}. There is no {{project_root}}, {{repo}}, {{date}} or any
  other name — the agent already runs inside the repository, so it should
  refer to files by relative path.
- Never use {{now}}, {{uuid}}, {{random}} or any other clock or random value.
- Each task must be self-contained: the agent sees nothing but that text.
- Every phase id is lowercase letters, digits, dashes or underscores.

Do not create, modify or delete any other file. Do not run git. When the file
exists and holds valid JSON, reply with exactly one line: done
"""


def _slug(text: str) -> str:
    """A stable file stem from a request: ascii words, else ``plan``."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    stem = "-".join(words[:3]).strip("-")
    return stem or "plan"


def stem_for(request: str, name: str = "") -> str:
    """The file stem this request will be saved under."""
    return (name or "").strip() or _slug(request)


def definition_path(root: Path, name: str) -> Path:
    """Where a project definition of ``name`` lives."""
    return Path(root) / ".orca" / "osw" / "workflows" / f"{name}.json"


def prompt_for(root: Path, name: str, request: str) -> str:
    """The single prompt one planning agent receives."""
    return (
        _PROMPT.replace("__PATH__", str(definition_path(root, name)))
        .replace("__NAME__", name)
        .replace("__REQUEST__", request.strip())
        .replace("__OUTPUT__", "{{output}}")
    )


def wait_for_agent(
    root: Path, agent_id: str,
    *,
    timeout: float = PLAN_TIMEOUT_SECONDS,
    interval: float = PLAN_POLL_SECONDS,
) -> str:
    """Block until the planning agent's turn ends; return its final state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            record = state.read_agent(root, agent_id)
        except FileNotFoundError:
            record = {}
        current = str(record.get("state") or "")
        if current in _TERMINAL:
            return current
        time.sleep(interval)
    return "timeout"


def author(
    root: Path,
    request: str,
    *,
    name: str = "",
    dispatch: Dispatch = executor.default_dispatch,
    wait: Wait | None = None,
) -> tuple[Path, defs.Definition]:
    """Have one agent write a definition for ``request`` and validate it.

    The file is the deliverable either way: an invalid one stays on disk so the
    person can fix one line instead of starting over.
    """
    root = Path(root)
    wanted = stem_for(request, name)
    path = definition_path(root, wanted)
    if path.exists():
        raise defs.WorkflowError(
            "definition_exists",
            f"{path} already exists; pass --name <other> to keep both",
        )
    if not request.strip():
        raise defs.WorkflowError("definition_invalid", "empty request")
    # The drafting agent is one ordinary `new`, which refuses to dispatch in a
    # project that has never been initialized.
    if not state.state_file(root).exists():
        state.init_state_dir(root)

    plan = {"prompt": prompt_for(root, wanted, request), "index": 0, "phase": "plan"}
    outcome = dispatch(plan, None, root)
    agent_id = str(outcome.get("agent") or "")
    if not agent_id:
        raise defs.WorkflowError(
            "plan_failed", "the planning agent was not created",
        )

    final = (wait or wait_for_agent)(root, agent_id)
    if final != "done":
        detail = f"the planning agent finished {final}"
        if not path.exists():
            raise defs.WorkflowError("plan_failed", detail)
        # It wrote the file but did not finish cleanly: the validator decides.

    try:
        definition = defs.load_definition(root, wanted)
    except defs.WorkflowError as exc:
        raise defs.WorkflowError(
            exc.code,
            f"{exc.message} (written by {agent_id}; fix that line or rerun with --name)",
        ) from exc
    return path, definition

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
   "phases": [
     {"id": "<short slug>",
      "mode": "single" | "for_each",
      "from": ["<id of an earlier phase>"],   // only when this phase consumes that phase's output
      "task": "<the complete instruction one agent receives>"}
   ]}

Rules:
- Use 2 to 6 phases. The first phase discovers the work; a later for_each phase
  does the bulk work; a final single phase summarises.
- A phase whose results a later phase consumes must write a JSON array to the
  path __OUTPUT__. Its task must say to write exactly that file.
- A for_each phase's task uses {{item}} (or {{item.<field>}} for an object
  field) and must not refer to the other items.
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
    wanted = (name or _slug(request)).strip()
    path = definition_path(root, wanted)
    if path.exists():
        raise defs.WorkflowError(
            "definition_exists",
            f"{path} already exists; pass --name <other> to keep both",
        )
    if not request.strip():
        raise defs.WorkflowError("definition_invalid", "empty request")

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

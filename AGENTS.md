# Repository Guidelines

## Project Structure & Module Organization

This repository contains OSW, a Python CLI for managing Orca-backed agent sessions.
Source code lives under `skill/osw/`, with `skill/osw.py` as the entry point.
The deterministic readiness gate and the optional Jev (TypeSafe) integration
live in `skill/osw/ready_gate.py` and `skill/osw/jev.py`; the offline
evaluation harness is `tools/jev_eval.py`. `skill/osw/sessions.py` reads
provider session ids from Orca's agent-hook cache
(`%APPDATA%/orca/agent-hooks/last-status.json`, a non-public format checked
by `version`). `skill/osw/blockers.py` holds the blocking-screen policy
(detection, classification, the per-mode decision, the `(provider, kind)`
key whitelist — only codex's update prompt — never-automated kinds, and
the machine-wide update lock); every whitelist entry needs a screen sample
under `tests/fixtures/frames/`. In `shadow` mode a whitelisted blocker is
recorded (`blocker_shadow_action`) and notified, never acted on. A task or
followed turn that ps tracked without ever showing a tool call finishes
`done` with `tools_observed: false` and `warning=no_tool_activity`; it is
never resent.
The Codex skill guide is `skill/SKILL.md`. Tests live in `tests/`, with
`conftest.py` adding the local package path. User-facing docs are in
`README.md`, `README_zh.md`, `QUICK.md`, and `QUICK_zh.md`. Runtime state such
as `.orca/osw/agents/`, logs, reports, and handoffs is generated locally and
should not be treated as source.

## Build, Test, and Development Commands

- `python -m pip install anyio typer rich pytest` installs local development
  dependencies.
- `pytest -q` runs the full test suite.
- `pytest tests/test_cli.py -v` runs focused CLI tests.
- `python skill/osw.py --help` checks the top-level CLI.
- `python skill/osw.py new --help` and `python skill/osw.py use --help` verify
  provider and adoption options.
- `python skill/osw.py enroll|guard|revive|stop --help` cover takeover and
  recovery: `enroll` records unmanaged agent panes (`enrolled`), `guard`
  sets the `guardian` and follows a running turn (`follow_turn`), `revive`
  resumes a provider session — in the original pane when it is a confirmed
  empty shell, else in a new terminal (`placement: in_place|new_terminal`;
  `resume_turn` for a `lost` agent) — and `stop` interrupts the current turn
  (`completion_source: interrupted`). Supervised workers' questions reach
  the guardian as `# [osw] ask ...` (ids in `notified_asks`); on Windows
  answer with `resources\bin\orca.exe orchestration reply`, since
  `orca.cmd` refuses it.

## Coding Style & Naming Conventions

Use Python 3 style with 4-space indentation, type hints for public helpers, and
small functions with clear names. Keep CLI behavior in `skill/osw/cli.py`,
provider command construction in `skill/osw/providers.py`, persistent state
helpers in `skill/osw/state.py`, and watcher logic in `skill/osw/watcher.py`.
Agent ids use `<prefix>_NNN`, for example `agent_001`, `research_001`, or
`debug_001`. While a watcher runs it is the only writer of its agent
record; the CLI signals it through side files such as the stop request
`.orca/osw/agents/<id>.stop`. OSW itself may inject only
`watcher.HANDOFF_TEMPLATE`, `watcher.RESUME_TEMPLATE`, the keys of
`blockers.WHITELIST` entries, and the resume command `revive` types into a
confirmed empty shell.

## Testing Guidelines

Tests use `pytest` and `typer.testing.CliRunner`. Add or update tests before
changing behavior. Prefer focused tests near the affected module, such as
`tests/test_providers.py` for provider command formatting and `tests/test_cli.py`
for user-visible CLI behavior. Mock Orca calls with `AsyncMock`; do not require
a live Orca instance for unit tests.

## Commit & Pull Request Guidelines

Follow the existing commit style: `feat:`, `fix:`, `refactor:`, `docs:`,
`test:`, and `chore:`. Keep commits scoped and describe the behavioral change.
Pull requests should include a short summary, test results such as `pytest -q`,
and any CLI examples affected by the change. Link related issues when available.

## Security & Configuration Tips

Provider launch commands may include autonomy flags such as Claude permission
bypass or Codex sandbox bypass. Before launch, `osw new` also pre-records
workspace trust for claude/codex/kimi (via `ensure_workspace_trust` in
`skill/osw/providers.py`) so the TUI skips its interactive "trust this
directory?" prompt; grok instead launches with `--always-approve --trust`,
where `--trust` grants folder trust for the workspace. DSH gets neither: its
launch (`dsh-tui .`) has no trust or approval flag, so its permission preset
(`/permission`) and first-run setup stay the operator's choice, and OSW
records no autonomy for it. Use them only in
trusted, externally controlled
workspaces. Do not commit generated `.orca/` runtime state, local logs, reports,
or handoff files. Never launch a nested agent process inside a managed pane
without clearing the inherited `ORCA_*` environment variables.

## Skill Installation Requirements

Install or update OSW with `python install.py`, which physically copies the
complete `skill/` directory into each tool's own skill directory. Each
destination must be an independent, ordinary directory containing ordinary
copied files.

- Codex destination: `C:\Users\shiki\.codex\skills\osw\`
- Agents destination: `C:\Users\shiki\.agents\skills\osw\`
- Claude destination: `C:\Users\shiki\.claude\skills\osw\`
- Never use junctions, symbolic links, hard links, directory links, reparse
  points, or any other link-like mechanism for either the directory or its
  files.
- Never configure an installed skill to execute from or resolve back to
  this repository's `skill/` directory.
- Validate after every update that all destinations are real directories,
  contain independent physical copies, and still work if the repository path
  is unavailable (`install.py` performs these checks automatically).

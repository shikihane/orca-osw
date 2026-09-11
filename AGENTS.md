# Repository Guidelines

## Project Structure & Module Organization

This repository contains OSW, a Python CLI for managing Orca-backed agent sessions.
Source code lives under `skill/osw/`, with `skill/osw.py` as the entry point.
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

## Coding Style & Naming Conventions

Use Python 3 style with 4-space indentation, type hints for public helpers, and
small functions with clear names. Keep CLI behavior in `skill/osw/cli.py`,
provider command construction in `skill/osw/providers.py`, persistent state
helpers in `skill/osw/state.py`, and watcher logic in `skill/osw/watcher.py`.
Agent ids use `<prefix>_NNN`, for example `agent_001`, `research_001`, or
`debug_001`.

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
where `--trust` grants folder trust for the workspace. Use them only in
trusted, externally controlled
workspaces. Do not commit generated `.orca/` runtime state, local logs, reports,
or handoff files.

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

# Frame fixtures

Real captured frames (production bytes→UTF-8 path) used by `tests/test_ready_gate.py`.

| label | expected verdict |
|---|---|
| startup_unavailable | abstain |
| idle_composer | ready |
| midturn_spinner_with_composer | not_ready (turn_running) |

Still to capture: `trust_dialog`, `shell_prompt` (claude exited), `startup_shell_echo` (does not exist on Claude Code 2.1.281 — the first drawn frame already contains the composer, design §12.5-3).

## dsh (`frames/dsh/`, DSH-TUI 0.17.0 on dsh 0.2.1-rc.2, Orca pane)

| sample | source | expected verdict |
|---|---|---|
| `launchpad_composer` | `orca terminal read --screen` at boot | ready (composer_visible) |
| `idle_composer` | `orca terminal read --screen` after a turn | ready (composer_visible) |

## Blocker samples (`tests/test_blockers.py`, plan §2.2)

Every `osw.blockers.WHITELIST` entry `(provider, kind)` needs a sample at
`<provider>/<kind>.json`.

| sample | source | default option | whitelisted keys |
|---|---|---|---|
| `codex/update_prompt` | dialog strings recorded in `docs/research/2026-10-11-v2-v3-v5-session-mapping.md` (codex 0.153.4 → 0.162.1, 2026-10-11) | `1. Update now` | Enter (runs the update) |

`codex/update_prompt` is a text transcription, not a raw captured frame
(文字转录、非原始抓帧): only the strings the research recorded
("✨ Update available! 0.153.4 -> 0.162.1", "1. Update now", "2. Skip")
are reproduced, not the real screen's layout, glyphs or other lines.
Replace it with a real `--screen` capture (`capture_path: run_orca-bytes`)
when one is taken.

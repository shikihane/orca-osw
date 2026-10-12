#!/usr/bin/env python3
"""Offline evaluation harness for the ready judgment (design §8).

Two layers, both runnable without touching a live worker:

1. ``deterministic`` (default, offline): scores the Phase 0 gate
   predicate (``osw.ready_gate.gate_verdict``) against the labeled
   real-frame fixtures in ``tests/fixtures/frames/``. Exits non-zero on
   any mismatch — this is the gate's regression harness.
2. ``--jev``: additionally scores the Jev ``ready_state`` Choice against
   the same fixtures through ``osw.jev.ask_ready`` (requires
   ``TYPESAFE_API_KEY`` and the optional ``typesafe-sdk``; degrades with
   a clear message and exit code 2 when the environment cannot call).
   ``--alt-order`` re-orders the Choice criteria (``ready_composer``
   first) to quantify option-order bias (§5.5 / §8.2).

Usage::

    python tools/jev_eval.py [--fixtures DIR] [--jev] [--alt-order]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skill"))

from osw import state  # noqa: E402
from osw.jev import ask_ready  # noqa: E402
from osw.ready_gate import gate_verdict  # noqa: E402

DEFAULT_FIXTURES = REPO / "tests" / "fixtures" / "frames" / "claude"

# Expected deterministic verdict and expected Jev choice per fixture label.
EXPECTATIONS = {
    "startup_unavailable": ("abstain", "blocked_startup"),
    "idle_composer": ("ready", "ready_composer"),
    "midturn_spinner_with_composer": ("not_ready", "working"),
}


def load_fixtures(directory: Path) -> list[dict]:
    fixtures = []
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_path"] = path
        fixtures.append(data)
    return fixtures


def frame_body(fixture: dict) -> dict:
    return {"result": {"terminal": {
        "source": fixture.get("source"), "tail": fixture.get("tail") or [],
    }}}


def run_deterministic(fixtures: list[dict]) -> int:
    failures = 0
    print("== deterministic gate ==")
    for fixture in fixtures:
        label = fixture.get("label", fixture["_path"].stem)
        expected, _ = EXPECTATIONS.get(label, (None, None))
        if expected is None:
            print(f"  {label:<32} skipped (no expectation)")
            continue
        verdict, reason = gate_verdict("claude", frame_body(fixture))
        ok = verdict == expected
        failures += 0 if ok else 1
        mark = "ok " if ok else "FAIL"
        print(f"  {mark} {label:<32} expected={expected:<10} "
              f"got={verdict} ({reason})")
    return failures


async def run_jev(fixtures: list[dict], alt_order: bool) -> int:
    import osw.jev as jev

    if alt_order:
        criteria = dict(jev.READY_CRITERIA)
        criteria["ready_composer"] = criteria.pop("ready_composer")
        jev.READY_CRITERIA.clear()
        jev.READY_CRITERIA.update({"ready_composer": criteria.pop("ready_composer"),
                                   **criteria})
    root = Path(tempfile.mkdtemp(prefix="jev_eval_"))
    state.init_state_dir(root)
    degraded = 0
    failures = 0
    print(f"== Jev ready_state (order={'alt' if alt_order else 'default'}) ==")
    for fixture in fixtures:
        label = fixture.get("label", fixture["_path"].stem)
        _, expected_choice = EXPECTATIONS.get(label, (None, None))
        if expected_choice is None:
            print(f"  {label:<32} skipped (no expectation)")
            continue
        answer = await ask_ready(root, "eval", "claude", frame_body(fixture),
                                 "eval", "fixture")
        if answer is None:
            degraded += 1
            print(f"  degraded {label:<32} (see jev_degraded; check key/SDK)")
            continue
        choice = answer.get("choice", "")
        ok = choice == expected_choice
        failures += 0 if ok else 1
        mark = "ok " if ok else "FAIL"
        print(f"  {mark} {label:<32} expected={expected_choice:<16} "
              f"got={choice} (p={answer.get('probabilities', {}).get(choice)})")
    print(f"  raw records: {jev.jev_dir(root)}")
    if degraded == len(fixtures):
        print("env cannot call Jev (no key / no SDK) — exit 2")
        return 2
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--jev", action="store_true",
                        help="also score the Jev ready_state question")
    parser.add_argument("--alt-order", action="store_true",
                        help="re-order criteria with ready_composer first")
    args = parser.parse_args()

    fixtures = load_fixtures(args.fixtures)
    if not fixtures:
        print(f"no fixtures under {args.fixtures}")
        return 2
    failures = run_deterministic(fixtures)
    if args.jev:
        failures += asyncio.run(run_jev(fixtures, args.alt_order))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

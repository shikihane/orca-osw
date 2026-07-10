"""Install the OSW skill as independent physical copies.

Copies the complete skill/ directory into each destination's
skills/osw, replacing whatever is there. Link-shaped destinations
(symlinks, junctions) are removed as links — never followed — and
replaced with real directories: installed skills must keep working
even when this repository is unavailable, so nothing may resolve
back here.

Usage:
    python install.py                # install to the default skill roots
    python install.py DIR [DIR ...]  # install into the given skills roots
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "skill"
SKILL_NAME = "osw"
DEFAULT_SKILL_ROOTS = [
    Path.home() / ".codex" / "skills",
    Path.home() / ".agents" / "skills",
    Path.home() / ".claude" / "skills",
]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _: False)
    return path.is_symlink() or isjunction(path)


def remove_existing(dest: Path) -> None:
    if is_link(dest):
        try:
            os.rmdir(dest)  # directory link: removes the reparse point only
        except NotADirectoryError:
            os.unlink(dest)
    elif dest.is_dir():
        shutil.rmtree(dest)
    elif dest.exists():
        dest.unlink()


def digest_tree(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(root).as_posix()
        files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def validate(dest: Path) -> list[str]:
    problems = []
    if is_link(dest):
        problems.append("destination is still a link")
    for path in dest.rglob("*"):
        if is_link(path):
            problems.append(f"link inside the install: {path}")
    resolved = dest.resolve()
    if resolved == SOURCE or SOURCE in resolved.parents:
        problems.append("destination resolves back into this repository")
    if digest_tree(SOURCE) != digest_tree(dest):
        problems.append("installed files do not match the source")
    smoke = subprocess.run(
        [sys.executable, "-B", str(dest / "osw.py"), "--help"],
        capture_output=True,
        cwd=str(dest),
        timeout=120,
    )
    if smoke.returncode != 0:
        detail = (smoke.stderr or smoke.stdout).decode(errors="replace")
        problems.append(f"osw.py --help failed: {detail.strip()[:200]}")
    return problems


def install(root: Path) -> list[str]:
    dest = root / SKILL_NAME
    replaced_link = is_link(dest)
    remove_existing(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE, dest, ignore=IGNORE, symlinks=False)
    problems = validate(dest)
    if not problems:
        note = " (replaced a link)" if replaced_link else ""
        print(f"ok   {dest}{note}")
    return [f"FAIL {dest}: {problem}" for problem in problems]


def main(argv: list[str]) -> int:
    if not SOURCE.is_dir():
        print(f"FAIL source skill directory not found: {SOURCE}")
        return 1
    roots = [Path(arg).expanduser() for arg in argv] or DEFAULT_SKILL_ROOTS
    failures = []
    for root in roots:
        try:
            failures += install(root)
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"FAIL {root / SKILL_NAME}: {exc}")
    for line in failures:
        print(line)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

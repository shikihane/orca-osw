from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_install_produces_independent_physical_copy(tmp_path):
    root = tmp_path / "skills"

    result = subprocess.run(
        [sys.executable, str(REPO / "install.py"), str(root)],
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    dest = root / "osw"
    assert not dest.is_symlink()
    assert (dest / "SKILL.md").is_file()
    assert (dest / "osw.py").is_file()
    assert (dest / "osw" / "watcher.py").is_file()
    assert not (dest / "__pycache__").exists()
    assert not (dest / "osw" / "__pycache__").exists()


def test_install_replaces_existing_directory(tmp_path):
    root = tmp_path / "skills"
    stale = root / "osw"
    stale.mkdir(parents=True)
    (stale / "leftover.txt").write_text("old install", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(REPO / "install.py"), str(root)],
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (stale / "leftover.txt").exists()
    assert (stale / "SKILL.md").is_file()


def test_install_writes_the_slash_command_where_each_harness_reads_it(tmp_path):
    for harness, folder in ((".claude", "commands"), (".codex", "prompts")):
        root = tmp_path / harness / "skills"

        result = subprocess.run(
            [sys.executable, str(REPO / "install.py"), str(root)],
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        command = tmp_path / harness / folder / "osw-workflow.md"
        assert command.is_file()
        text = command.read_text(encoding="utf-8")
        assert "references/workflow.md" in text
        # The recipe itself ships beside the skill, so both entry points
        # read the same page instead of two copies that drift.
        recipe = root / "osw" / "references" / "workflow.md"
        assert recipe.is_file()
        recipe_text = recipe.read_text(encoding="utf-8")
        assert "workflow go" in recipe_text
        assert "workflow status" in recipe_text

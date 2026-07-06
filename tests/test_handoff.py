from __future__ import annotations

from osw import handoff


def test_extract_bare_filename():
    assert handoff.extract_handoff_filename("HANDOFF_fix_tests.md") == "HANDOFF_fix_tests.md"


def test_extract_from_path():
    assert (
        handoff.extract_handoff_filename("/home/user/HANDOFF_fix_tests.md")
        == "HANDOFF_fix_tests.md"
    )


def test_extract_inline():
    assert (
        handoff.extract_handoff_filename("文件路径: HANDOFF_task_001.md 已生成")
        == "HANDOFF_task_001.md"
    )


def test_extract_with_hyphens_dots():
    assert handoff.extract_handoff_filename("HANDOFF_my-task.v2.md") == "HANDOFF_my-task.v2.md"


def test_extract_no_match():
    assert handoff.extract_handoff_filename("没有 handoff 文件") is None


def test_extract_first_of_multiple():
    text = "先是 HANDOFF_first.md 然后是 HANDOFF_second.md"
    assert handoff.extract_handoff_filename(text) == "HANDOFF_first.md"


def test_completion_report_format():
    report = handoff.format_completion_report("agent_001", "term-1", "HANDOFF_fix_tests.md")
    assert report == (
        "子任务完成。\n"
        "agent: agent_001\n"
        "terminal: term-1\n"
        "handoff: HANDOFF_fix_tests.md"
    )


def test_handoff_failed_message():
    assert (
        handoff.format_handoff_failed_message()
        == "子任务已停止，但未能确认 HANDOFF_*.md 文件名。请检查 agent 输出。"
    )


def test_forced_handoff_prompt_content():
    prompt = handoff.FORCED_HANDOFF_PROMPT
    assert "/handoff" in prompt
    assert "HANDOFF_" in prompt
    assert ".md" in prompt

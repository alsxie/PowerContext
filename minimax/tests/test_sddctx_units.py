# 作者：FDE WorkMate
"""sddctx 自身的单元测试：解析规格/代码/测试、任务卡拆分、MiniMax 输出解析与写入限制。不需要数据库与网络。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sddctx.agent import AgentError, THINK_BLOCK, apply_files, parse_files
from sddctx.parsers import ProjectLayout, parse_project
from sddctx.task import load_task

MINIMAX = Path(__file__).resolve().parent.parent
SAMPLE = MINIMAX / "sample_app"


@pytest.fixture(scope="module")
def items():
    return {(item.kind, item.item_id): item for item in parse_project(SAMPLE, ProjectLayout.load(SAMPLE))}


def test_spec_entries_carry_status_links_and_lines(items):
    spec = items[("spec", "REQ-ORD-004")]
    assert spec.status == "active"
    assert "OrderQueryService.list_orders" in spec.links
    assert "ADR-002" in spec.links and "REQ-ORD-003" in spec.links
    lines = (SAMPLE / spec.path).read_text(encoding="utf-8").splitlines()
    assert lines[spec.line_start - 1].startswith("### REQ-ORD-004")
    assert items[("spec", "ADR-001")].status == "superseded"


def test_code_symbols_have_exact_line_ranges(items):
    method = items[("code", "OrderQueryService.list_orders")]
    assert method.symbol_kind == "method"
    lines = (SAMPLE / method.path).read_text(encoding="utf-8").splitlines()
    assert "def list_orders" in lines[method.line_start - 1]
    assert method.body.strip() in "\n".join(lines[method.line_start - 1 : method.line_end])


def test_tests_link_to_specs_and_called_symbols(items):
    functional = [item for (kind, _), item in items.items() if kind == "test" and item.path == "tests/test_status_filter.py"]
    assert functional and all("REQ-ORD-004" in item.links for item in functional)
    assert all("list_orders" in item.links for item in functional)


def test_task_card_splits_change_and_preserve():
    task = load_task(MINIMAX / "samples/task-001/task.md")
    assert task.task_id == "TASK-001"
    assert "statuses" in task.change and "total_amount_cents" not in task.change
    assert "total_amount_cents" in task.preserve


def test_parse_files_rejects_tests_and_specs():
    editable = {"orders/service.py"}
    reply = '<file path="orders/service.py">\nx = 1\n</file>'
    assert parse_files(reply, editable) == {"orders/service.py": "x = 1\n"}
    with pytest.raises(AgentError, match="不允许"):
        parse_files('<file path="tests/test_listing.py">\npass\n</file>', editable)
    with pytest.raises(AgentError, match="没有"):
        parse_files("只有说明，没有文件", editable)


def test_think_block_is_not_recorded():
    assert THINK_BLOCK.sub("", "<think>私有推理</think>结论").strip() == "结论"


def test_apply_files_returns_unified_diff(tmp_path):
    project = tmp_path / "app"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "m.py").write_text("a = 1\n", encoding="utf-8")
    diff = apply_files(project, {"pkg/m.py": "a = 2\n"})
    assert "-a = 1" in diff and "+a = 2" in diff and "a/app/pkg/m.py" in diff
    assert (project / "pkg" / "m.py").read_text(encoding="utf-8") == "a = 2\n"


def test_anthropic_reply_keeps_only_text_blocks():
    from sddctx.agent import _merge_user_turns, _parse_anthropic

    reply = _parse_anthropic(
        {"id": "m1", "model": "MiniMax-M3.1-Flash", "stop_reason": "end_turn", "usage": {"output_tokens": 3},
         "content": [{"type": "thinking", "thinking": "私有推理"}, {"type": "text", "text": "结果"}]}
    )
    assert reply["content"] == "结果" and reply["content_block_types"] == ["thinking", "text"]
    merged = _merge_user_turns([{"role": "system", "content": "s"}, {"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    assert merged == [{"role": "user", "content": "a\n\nb"}]

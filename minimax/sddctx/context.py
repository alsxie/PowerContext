# 作者：FDE WorkMate
"""根据开发任务从 seekdb 选择规格、代码和测试，并合并 PowerContext 中的任务状态，形成开发上下文。

选择方式（全部基于数据库查询结果，不含针对样例的固定答案）：
1. 规格：用任务“要改什么”段落做全文检索，得分最高的 active 条目为主规格；得分不低于主规格一定比例的 active 条目、
   主规格引用的规格编号、以及“必须保持”段落命中的条目作为约束规格；命中的 superseded 条目单列为已失效决策。
2. 代码：主规格引用的函数/方法为待修改符号，类型与模块为参考；待修改符号内部调用的工程内符号一并作为参考。
3. 测试：引用主规格编号的测试为目标功能测试；引用待修改符号或约束规格、或被“必须保持”段落命中的测试为回归测试。
4. 状态：读取 PowerContext 中的已完成/待办步骤与验证记录，比较代码摘要和规格哈希，判断已有验证是否仍适用。
"""

from __future__ import annotations

import json
from typing import Any

from sddctx.parsers import SPEC_ID, tree_digest
from sddctx.state import TaskState
from sddctx.store import KnowledgeStore
from sddctx.task import Task

RELATED_RATIO = 0.5  # 约束规格的得分下限：主规格得分的比例
EDITABLE_KINDS = {"function", "method"}


def _source(row: dict[str, Any]) -> str:
    return f"{row['path']}:L{row['line_start']}-L{row['line_end']}"


def _spec_view(row: dict[str, Any], role: str, reason: str) -> dict[str, Any]:
    meta = json.loads(row.get("meta") or "{}")
    view = {
        "id": row["item_id"],
        "title": row["title"],
        "status": row["status"],
        "role": role,
        "reason": reason,
        "source": _source(row),
        "content_hash": row["content_hash"],
        "body": row["body"],
    }
    if "score" in row:
        view["score"] = round(float(row["score"]), 3)
    refs = sorted(set(SPEC_ID.findall(row["body"] + " " + " ".join(meta.values()))) - {row["item_id"]})
    if refs:
        view["spec_refs"] = refs
    if meta.get("superseded_by"):
        view["superseded_by"] = meta["superseded_by"]
    if meta.get("supersedes"):
        view["supersedes"] = meta["supersedes"]
    return view


def _code_view(row: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "symbol": row["item_id"],
        "kind": row["symbol_kind"],
        "source": _source(row),
        "reason": reason,
        "content_hash": row["content_hash"],
        "body": row["body"],
    }


def _test_view(row: dict[str, Any], reason: str) -> dict[str, Any]:
    return {"id": row["item_id"], "source": _source(row), "reason": reason, "body": row["body"]}


def _resolve_symbols(store: KnowledgeStore, names: list[str], purpose: str) -> list[dict[str, Any]]:
    """把规格或代码中出现的名字解析为代码条目：精确匹配 item_id，或匹配“类名.方法名”的末段。"""

    exact = store.get("code", names, purpose=purpose)
    found = {row["item_id"] for row in exact}
    missing = [name for name in names if name not in found and "." not in name and "/" not in name]
    return exact + store.get_by_member_name("code", missing, purpose=purpose + ".member")


def build_context(store: KnowledgeStore, task: Task, state: TaskState | None, project_root) -> dict[str, Any]:  # noqa: ANN001, C901, PLR0912, PLR0915
    change_query = f"{task.title}\n{task.change}"

    # 1. 规格
    spec_hits = store.search("spec", change_query, limit=20, purpose="context.spec_search")
    active_hits = [row for row in spec_hits if row["status"] == "active"]
    if not active_hits:
        raise LookupError("seekdb 中没有与任务相关的 active 规格，请先运行 ingest")
    top_score = float(active_hits[0]["score"])
    candidates = [row for row in active_hits if float(row["score"]) >= RELATED_RATIO * top_score]
    # 被其它候选条目以“依据”引用的条目是决策理由，主规格取引用它的需求条目。
    bases = {json.loads(row.get("meta") or "{}").get("based_on", "") for row in candidates}
    cited = {ref for value in bases for ref in SPEC_ID.findall(value)}
    primary = next((row for row in candidates if row["item_id"] not in cited), candidates[0])
    primary_reason = "任务“要改什么”全文检索得分最高" if primary is active_hits[0] else "检索候选中得分最高、且不是其它候选“依据”的条目"
    specs: dict[str, dict[str, Any]] = {primary["item_id"]: _spec_view(primary, "primary", primary_reason)}
    for row in candidates:
        if row["item_id"] in cited:
            specs.setdefault(row["item_id"], _spec_view(row, "constraint", f"主规格 {primary['item_id']} 的依据"))
        else:
            specs.setdefault(row["item_id"], _spec_view(row, "constraint", f"全文检索得分 ≥ 最高分的 {RELATED_RATIO:.0%}"))
    if task.preserve:
        preserve_hits = store.search("spec", task.preserve, limit=10, purpose="context.spec_preserve_search")
        best = float(preserve_hits[0]["score"]) if preserve_hits else 0.0
        for row in preserve_hits:
            if row["status"] == "active" and float(row["score"]) >= RELATED_RATIO * best:
                specs.setdefault(row["item_id"], _spec_view(row, "constraint", "任务“必须保持”段落全文检索命中"))
    superseded = {
        row["item_id"]: _spec_view(row, "superseded", "检索命中但已被取代，不得遵循")
        for row in spec_hits
        if row["status"] != "active"
    }

    # 2. 代码
    primary_links = [link for link in _links(primary) if not SPEC_ID.fullmatch(link)]
    linked_code = _resolve_symbols(store, primary_links, "context.code_from_primary_spec")
    edit_targets = {row["item_id"]: _code_view(row, f"主规格 {primary['item_id']} 关联的可修改符号") for row in linked_code if row["symbol_kind"] in EDITABLE_KINDS}
    references = {row["item_id"]: _code_view(row, f"主规格 {primary['item_id']} 关联的类型/模块") for row in linked_code if row["item_id"] not in edit_targets}
    if not edit_targets:
        for row in store.search("code", change_query, limit=10, purpose="context.code_search"):
            if row["symbol_kind"] in EDITABLE_KINDS:
                edit_targets[row["item_id"]] = _code_view(row, "任务全文检索得分最高的函数/方法")
                break
    callee_names = sorted({name for target in edit_targets.values() for name in _body_names(target)})
    for row in _resolve_symbols(store, callee_names, "context.code_callees"):
        if row["item_id"] not in edit_targets and row["symbol_kind"] != "module":
            references.setdefault(row["item_id"], _code_view(row, "被待修改符号调用"))
    files_to_modify = sorted({target["source"].split(":", 1)[0] for target in edit_targets.values()})

    # 规格补全：关联到待修改符号的 active 规格描述了该符号的现有行为，是约束；再沿规格编号引用补全。
    for row in store.referencing("spec", sorted(edit_targets), purpose="context.specs_for_edit_targets"):
        if row["status"] == "active":
            specs.setdefault(row["item_id"], _spec_view(row, "constraint", f"关联待修改符号 {row['matched']}"))
        else:
            superseded.setdefault(row["item_id"], _spec_view(row, "superseded", f"关联待修改符号但已被取代，不得遵循"))
    _expand_spec_references(store, specs, superseded)

    # 3. 测试
    functional = {
        row["item_id"]: _test_view(row, f"引用主规格 {primary['item_id']}")
        for row in store.referencing("test", [primary["item_id"]], purpose="context.tests_for_primary_spec")
    }
    short_names = sorted({symbol.rsplit(".", 1)[-1] for symbol in edit_targets} | set(edit_targets))
    regression: dict[str, dict[str, Any]] = {}
    for row in store.referencing("test", short_names, purpose="context.tests_for_edit_targets"):
        if row["item_id"] not in functional:
            regression.setdefault(row["item_id"], _test_view(row, f"调用待修改符号 {row['matched']}"))
    constraint_ids = [spec_id for spec_id, view in specs.items() if view["role"] == "constraint"]
    for row in store.referencing("test", constraint_ids, purpose="context.tests_for_constraints"):
        if row["item_id"] not in functional:
            regression.setdefault(row["item_id"], _test_view(row, f"引用约束规格 {row['matched']}"))
    if task.preserve:
        preserve_hits = store.search("test", task.preserve, limit=20, purpose="context.tests_preserve_search")
        best = float(preserve_hits[0]["score"]) if preserve_hits else 0.0
        for row in preserve_hits:
            if row["item_id"] not in functional and float(row["score"]) >= RELATED_RATIO * best:
                regression.setdefault(row["item_id"], _test_view(row, "任务“必须保持”段落全文检索命中"))

    def files(tests: dict[str, dict[str, Any]]) -> list[str]:
        return sorted({test_id.split("::", 1)[0] for test_id in tests})

    context: dict[str, Any] = {
        "task": {"id": task.task_id, "title": task.title, "path": str(task.path), "change": task.change, "preserve": task.preserve},
        "knowledge_base": store.latest_import(),
        "specs": {
            "applicable": sorted(specs.values(), key=lambda view: (view["role"] != "primary", view["id"])),
            "superseded": sorted(superseded.values(), key=lambda view: view["id"]),
        },
        "code": {
            "files_to_modify": files_to_modify,
            "edit_targets": list(edit_targets.values()),
            "references": sorted(references.values(), key=lambda view: view["symbol"]),
        },
        "tests": {
            "functional": sorted(functional.values(), key=lambda view: view["id"]),
            "regression": sorted(regression.values(), key=lambda view: view["id"]),
            "functional_files": files(functional),
            "regression_files": files(regression),
        },
    }
    context["state"] = _state_view(state, context, project_root)
    return context


def _expand_spec_references(store: KnowledgeStore, specs: dict[str, dict[str, Any]], superseded: dict[str, dict[str, Any]], depth: int = 3) -> None:
    """沿规格正文与元数据中的规格编号引用，把被引用的规格补入上下文（有限深度的传递闭包）。"""

    frontier = list(specs)
    for _ in range(depth):
        refs = sorted({ref for spec_id in frontier for ref in specs[spec_id].get("spec_refs", [])} - set(specs) - set(superseded))
        if not refs:
            return
        frontier_before, frontier = frontier, []
        for row in store.get("spec", refs, purpose="context.spec_references"):
            citing = next(spec_id for spec_id in frontier_before if row["item_id"] in specs[spec_id].get("spec_refs", []))
            if row["status"] == "active":
                specs[row["item_id"]] = _spec_view(row, "constraint", f"被规格 {citing} 引用")
                frontier.append(row["item_id"])
            else:
                superseded[row["item_id"]] = _spec_view(row, "superseded", f"被规格 {citing} 引用但已被取代，不得遵循")


def _links(row: dict[str, Any]) -> list[str]:
    from sddctx.parsers import BACKTICK

    meta = json.loads(row.get("meta") or "{}")
    return list(dict.fromkeys(BACKTICK.findall(meta.get("links", "")) + BACKTICK.findall(row["body"])))


def _body_names(code_view: dict[str, Any]) -> set[str]:
    import ast
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(code_view["body"]))
    except SyntaxError:
        return set()
    return {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)} | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id.startswith("_")
    }


def _state_view(state: TaskState | None, context: dict[str, Any], project_root) -> dict[str, Any]:  # noqa: ANN001
    if state is None:
        return {"source": "powercontext", "available": False}
    view = state.as_dict() | {"available": True}
    digest = tree_digest(project_root)
    current_hashes = {spec["id"]: spec["content_hash"] for spec in context["specs"]["applicable"]}
    verifications = []
    for name in ("baseline", "verify"):
        record = state.steps.get(name)
        if not record:
            continue
        recorded_digest = (record.get("code_version") or {}).get("project_digest")
        verifications.append(
            {
                "step": name,
                "ok": record.get("ok"),
                "summary": record.get("summary"),
                "source_id": record.get("source_id"),
                "project_digest": recorded_digest,
                "still_applies": recorded_digest == digest,
            }
        )
    view["verifications"] = verifications
    previous = (state.steps.get("context") or {}).get("spec_hashes") or {}
    view["spec_changes_since_last_context"] = [
        {"id": spec_id, "previous_hash": previous.get(spec_id), "current_hash": current}
        for spec_id, current in current_hashes.items()
        if previous and previous.get(spec_id) != current
    ]
    view["current_project_digest"] = digest
    return view


def render_markdown(context: dict[str, Any]) -> str:  # noqa: C901
    """渲染为给开发助手的 Markdown 上下文；每条内容都标注来源文件与行号。"""

    lines = [f"# 开发上下文：{context['task']['id']} {context['task']['title']}", ""]
    kb = context.get("knowledge_base") or {}
    lines += [f"资料库：seekdb，导入批次 {kb.get('import_id', '-')}，导入时间 {kb.get('imported_at', '-')}", ""]
    lines += ["## 任务", "", "要改什么：", context["task"]["change"], "", "必须保持：", context["task"]["preserve"] or "（未声明）", ""]
    lines += ["## 适用规格", ""]
    for spec in context["specs"]["applicable"]:
        lines += [f"### {spec['id']} {spec['title']}（{spec['role']}，来源 {spec['source']}，hash {spec['content_hash']}）", "", spec["body"], ""]
    if context["specs"]["superseded"]:
        lines += ["## 已失效的决策（不得遵循）", ""]
        for spec in context["specs"]["superseded"]:
            by = f"，被 {spec['superseded_by']} 取代" if spec.get("superseded_by") else ""
            lines += [f"- {spec['id']} {spec['title']}（来源 {spec['source']}{by}）"]
        lines.append("")
    lines += ["## 需要修改的代码", "", "文件：" + ", ".join(context["code"]["files_to_modify"]), ""]
    for code in context["code"]["edit_targets"]:
        lines += [f"### {code['symbol']}（{code['source']}）", "", "```python", code["body"], "```", ""]
    lines += ["## 参考代码（不需要修改，除非规格要求）", ""]
    for code in context["code"]["references"]:
        lines += [f"### {code['symbol']}（{code['source']}，{code['reason']}）", "", "```python", code["body"], "```", ""]
    lines += ["## 测试", "", "目标功能测试（修改后必须全部通过）："]
    lines += [f"- {test['id']}（{test['source']}）" for test in context["tests"]["functional"]]
    lines += ["", "回归测试（原有行为，必须保持通过）："]
    lines += [f"- {test['id']}（{test['source']}，{test['reason']}）" for test in context["tests"]["regression"]]
    lines.append("")
    if context["tests"]["functional"]:
        lines += ["目标功能测试代码：", ""]
        for test in context["tests"]["functional"]:
            lines += ["```python", test["body"], "```", ""]
    state = context.get("state") or {}
    lines += ["## 任务状态（PowerContext）", ""]
    if not state.get("available"):
        lines += ["（无可用状态）", ""]
    else:
        lines += [f"Scope：{state['scope_id']}，Handoff 版本：{state.get('handoff_revision')}", "", "已完成："]
        lines += [f"- [{item['step']}] {item['title']}：{item.get('summary', '')}" for item in state["done"]] or ["- （无）"]
        lines += ["", "待办："]
        lines += [f"- [{item['step']}] {item['title']}" for item in state["todo"]] or ["- （无）"]
        lines += ["", "已有验证结果："]
        for record in state.get("verifications", []):
            applies = "仍适用于当前工程内容" if record["still_applies"] else "规格或代码已变化，需重新验证"
            lines.append(f"- [{record['step']}] {record['summary']}（{applies}）")
        if not state.get("verifications"):
            lines.append("- （无）")
        if state.get("spec_changes_since_last_context"):
            lines += ["", "上次生成上下文后发生变化的规格："]
            lines += [f"- {item['id']}：{item['previous_hash']} → {item['current_hash']}" for item in state["spec_changes_since_last_context"]]
        lines.append("")
    return "\n".join(lines)

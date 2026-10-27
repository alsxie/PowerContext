# 作者：FDE WorkMate
"""sddctx 命令行入口。

    sddctx reset   --task T            清空 seekdb 资料库与 PowerContext 状态，从基线重建业务工程工作副本
    sddctx overlay --task T DIR        把目录叠加到工作副本（如规格更新），随后 ingest 增量导入
    sddctx server  start|stop|status   管理本地 PowerContext Server
    sddctx ingest  --task T            导入（或增量重新导入）规格、代码和测试到 seekdb
    sddctx baseline --task T           修改前运行目标功能测试与回归测试
    sddctx context --task T [--format md|json]   生成开发上下文
    sddctx agent   --task T [--replay DIR]       MiniMax 根据上下文修改代码
    sddctx verify  --task T            修改后运行目标功能测试与回归测试
    sddctx state   --task T            读取 PowerContext 中的任务状态
    sddctx next    --task T            根据已保存状态给出下一步
    sddctx run     --task T [--replay DIR]       从已保存状态继续执行剩余步骤
    sddctx check   --task T --expected FILE      核对上下文是否包含样例规定的来源
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sddctx import agent as agent_module
from sddctx.config import MINIMAX_ROOT, Settings, load_settings
from sddctx.context import build_context, render_markdown
from sddctx.parsers import ProjectLayout, parse_project, tree_digest
from sddctx.records import code_version, write_json
from sddctx.state import WORKFLOW, StateStore, TaskState, server_status, start_server, stop_server
from sddctx.store import KnowledgeStore
from sddctx.task import Task, load_task
from sddctx.verify import run_tests


# ---- 运行目录 ------------------------------------------------------------------------------------


def _run_settings(settings: Settings, task: Task) -> Settings:
    """同一次任务运行的所有记录写入 records/runs/<task>-<时间>/；指针保存在 .runtime/current_run。"""

    pointer = settings.runtime_dir / "current_run"
    if pointer.is_file():
        run_dir = Path(pointer.read_text().strip())
    else:
        name = os.environ.get("SDDCTX_RUN_NAME") or f"{task.task_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        run_dir = settings.records_dir / "runs" / name
        run_dir.mkdir(parents=True, exist_ok=True)
        settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        pointer.write_text(str(run_dir))
    return dataclasses.replace(settings, records_dir=run_dir)


def _open_store(settings: Settings):  # noqa: ANN202
    return KnowledgeStore.open(settings.kb_path, settings.records_dir / "db_queries.jsonl")


def _display(path: Path) -> str:
    return str(path.relative_to(MINIMAX_ROOT)) if path.is_relative_to(MINIMAX_ROOT) else str(path)


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# ---- 步骤 ----------------------------------------------------------------------------------------


def cmd_ingest(settings: Settings, task: Task, _: argparse.Namespace) -> bool:
    layout = ProjectLayout.load(settings.project_root)
    items = parse_project(settings.project_root, layout)
    with _open_store(settings) as store:
        summary = store.sync(items, project_root=settings.project_root, project_digest=tree_digest(settings.project_root))
    counts = {kind: sum(item.kind == kind for item in items) for kind in ("spec", "code", "test")}
    changed = summary["added"] + summary["updated"] + summary["removed"]
    text = f"导入 {summary['items']} 条（规格 {counts['spec']}，代码 {counts['code']}，测试 {counts['test']}），内容变化 {len(changed)} 条，仅位置变化 {len(summary['moved'])} 条"
    StateStore(settings).ensure_contract(task)
    receipt = StateStore(settings).record_step(task, "ingest", ok=True, summary=text, evidence={"import": summary, "counts": counts})
    _print({"ingest": summary | {"counts": counts}, "powercontext": receipt})
    return True


def _fresh_context(settings: Settings, task: Task, state: TaskState | None) -> dict[str, Any]:
    with _open_store(settings) as store:
        return build_context(store, task, state, settings.project_root)


def _test_plan(settings: Settings, task: Task, state: TaskState) -> dict[str, list[str]]:
    """测试范围来自最近一次保存的上下文；没有保存过时现场生成。"""

    saved = state.steps.get("context", {}).get("tests")
    if saved:
        return saved
    context = _fresh_context(settings, task, None)
    return {"functional": context["tests"]["functional_files"], "regression": context["tests"]["regression_files"]}


def cmd_baseline(settings: Settings, task: Task, _: argparse.Namespace) -> bool:
    store = StateStore(settings)
    plan = _test_plan(settings, task, store.load(task))
    functional = run_tests(settings, "baseline-functional", plan["functional"], run_dir=settings.records_dir)
    regression = run_tests(settings, "baseline-regression", plan["regression"], run_dir=settings.records_dir)
    functional_ok = functional["total"] > 0 and functional["failed"] == functional["total"] and not functional["environment_errors"]
    regression_ok = regression["total"] > 0 and regression["passed"] == regression["total"]
    ok = functional_ok and regression_ok
    text = (
        f"目标功能测试 {functional['failed']}/{functional['total']} 因功能未实现失败（exit {functional['exit_code']}），"
        f"回归测试 {regression['passed']}/{regression['total']} 通过（exit {regression['exit_code']}）"
    )
    evidence = {
        "tests": plan,
        "functional": _summary(functional),
        "regression": _summary(regression),
        "code_version": functional["code_version"],
    }
    receipt = store.record_step(task, "baseline", ok=ok, summary=text, evidence=evidence)
    _print({"baseline": {"ok": ok, "summary": text, "failure_messages": sorted({c["message"] for c in functional["cases"]})}, "powercontext": receipt})
    return ok


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    keys = ("command", "cwd", "exit_code", "total", "passed", "failed", "errors", "environment_errors")
    return {key: record[key] for key in keys} | {"failed_cases": [c["id"] for c in record["cases"] if c["outcome"] != "passed"]}


def cmd_context(settings: Settings, task: Task, args: argparse.Namespace) -> bool:
    store = StateStore(settings)
    state = store.load(task)
    context = _fresh_context(settings, task, state)
    markdown = render_markdown(context)
    write_json(settings.records_dir / "context.json", context)
    (settings.records_dir / "context.md").write_text(markdown, encoding="utf-8")
    evidence = {
        "spec_hashes": {spec["id"]: spec["content_hash"] for spec in context["specs"]["applicable"]},
        "superseded": [spec["id"] for spec in context["specs"]["superseded"]],
        "edit_targets": [code["symbol"] for code in context["code"]["edit_targets"]],
        "files_to_modify": context["code"]["files_to_modify"],
        "tests": {"functional": context["tests"]["functional_files"], "regression": context["tests"]["regression_files"]},
        "context_path": str((settings.records_dir / "context.md").relative_to(MINIMAX_ROOT)),
        "import_id": (context.get("knowledge_base") or {}).get("import_id"),
    }
    text = (
        f"主规格 {context['specs']['applicable'][0]['id']}，适用规格 {len(context['specs']['applicable'])} 条，"
        f"待修改符号 {', '.join(evidence['edit_targets'])}，功能测试函数 {len(context['tests']['functional'])} 个，"
        f"回归测试函数 {len(context['tests']['regression'])} 个"
    )
    if args.record:
        store.record_step(task, "context", ok=True, summary=text, evidence=evidence)
    if args.format == "json":
        _print(context)
    else:
        print(markdown)
    return True


def cmd_agent(settings: Settings, task: Task, args: argparse.Namespace) -> bool:  # noqa: C901
    store = StateStore(settings)
    state = store.load(task)
    context_record = state.steps.get("context")
    if not context_record:
        raise SystemExit("尚未保存开发上下文，请先运行 sddctx context --task ...")
    context_md = (MINIMAX_ROOT / context_record["context_path"]).read_text(encoding="utf-8")
    plan = context_record["tests"]
    editable = agent_module._editable_files(settings)  # noqa: SLF001
    targets = [path for path in context_record["files_to_modify"] if path in editable]
    snapshot_before = settings.records_dir / "snapshots" / "before"
    for rel in sorted(editable):
        destination = snapshot_before / settings.project_root.name / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings.project_root / rel, destination)

    replay_dir = Path(args.replay).resolve() if args.replay else None
    attempts = agent_module.replay_attempts(replay_dir) if replay_dir else list(range(1, args.max_attempts + 1))
    feedback: str | None = None
    changed: set[str] = set()
    result: dict[str, Any] = {}
    for attempt in attempts:
        current = {rel: (settings.project_root / rel).read_text(encoding="utf-8") for rel in sorted(set(targets) | changed)}
        if replay_dir:
            messages, reply = agent_module.load_replay(replay_dir, attempt)
            reply = reply | {"replayed_from": _display(replay_dir)}
            mode = "replay"
        else:
            messages = agent_module.build_messages(context_md, current, feedback)
            reply = agent_module.call_minimax(settings, messages)
            mode = "live"
        files = agent_module.parse_files(reply["content"], editable)
        diff = agent_module.apply_files(settings.project_root, files)
        changed |= set(files)
        agent_module.record_attempt(settings.records_dir, attempt, mode=mode, messages=messages, reply=reply, diff=diff, files=files)
        functional = run_tests(settings, f"agent-attempt-{attempt}-functional", plan["functional"], run_dir=settings.records_dir)
        regression = run_tests(settings, f"agent-attempt-{attempt}-regression", plan["regression"], run_dir=settings.records_dir)
        passed = functional["passed"] == functional["total"] > 0 and regression["passed"] == regression["total"] > 0
        result = {"attempt": attempt, "mode": mode, "model": reply.get("model"), "files": sorted(files), "tests_passed": passed}
        if passed:
            break
        feedback = (settings.records_dir / f"agent-attempt-{attempt}-functional.log").read_text(encoding="utf-8")[-4000:]
        feedback += (settings.records_dir / f"agent-attempt-{attempt}-regression.log").read_text(encoding="utf-8")[-2000:]

    snapshot_after = settings.records_dir / "snapshots" / "after"
    for rel in sorted(editable):
        destination = snapshot_after / settings.project_root.name / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings.project_root / rel, destination)
    full_diff = "".join(
        (settings.records_dir / "minimax" / f"attempt-{n}.diff").read_text(encoding="utf-8") for n in attempts if n <= result.get("attempt", 0)
    )
    (settings.records_dir / "code.diff").write_text(_combined_diff(snapshot_before, snapshot_after), encoding="utf-8")
    text = f"MiniMax（{result.get('mode')}，{result.get('model')}）第 {result.get('attempt')} 次修改 {', '.join(result.get('files', []))}"
    evidence = {
        "agent": result,
        "record_dir": str((settings.records_dir / "minimax").relative_to(MINIMAX_ROOT)),
        "diff_path": str((settings.records_dir / "code.diff").relative_to(MINIMAX_ROOT)),
        "attempt_diff_bytes": len(full_diff),
        "code_version": code_version(settings.repo_root, settings.project_root),
    }
    receipt = store.record_step(task, "agent", ok=bool(result.get("files")), summary=text, evidence=evidence)
    _print({"agent": result, "powercontext": receipt})
    return bool(result.get("files"))


def _combined_diff(before: Path, after: Path) -> str:
    import difflib

    chunks: list[str] = []
    for path in sorted(after.rglob("*.py")):
        rel = path.relative_to(after).as_posix()
        old = (before / rel).read_text(encoding="utf-8").splitlines(True) if (before / rel).is_file() else []
        chunks.extend(difflib.unified_diff(old, path.read_text(encoding="utf-8").splitlines(True), f"a/{rel}", f"b/{rel}"))
    return "".join(chunks)


def cmd_verify(settings: Settings, task: Task, _: argparse.Namespace) -> bool:
    store = StateStore(settings)
    plan = _test_plan(settings, task, store.load(task))
    functional = run_tests(settings, "verify-functional", plan["functional"], run_dir=settings.records_dir)
    regression = run_tests(settings, "verify-regression", plan["regression"], run_dir=settings.records_dir)
    full = run_tests(settings, "verify-full-suite", [], run_dir=settings.records_dir)
    ok = all(record["total"] > 0 and record["passed"] == record["total"] for record in (functional, regression, full))
    text = (
        f"目标功能测试 {functional['passed']}/{functional['total']} 通过，回归测试 {regression['passed']}/{regression['total']} 通过，"
        f"全量 {full['passed']}/{full['total']} 通过（exit {functional['exit_code']}/{regression['exit_code']}/{full['exit_code']}）"
    )
    evidence = {
        "tests": plan,
        "functional": _summary(functional),
        "regression": _summary(regression),
        "full_suite": _summary(full),
        "code_version": functional["code_version"],
    }
    receipt = store.record_step(task, "verify", ok=ok, summary=text, evidence=evidence)
    outcome = store.record_outcome(task, status="succeeded" if ok else "failed", summary=text) if ok else None
    _print({"verify": {"ok": ok, "summary": text}, "powercontext": receipt, "task_outcome": outcome})
    return ok


def cmd_state(settings: Settings, task: Task, _: argparse.Namespace) -> bool:
    state = StateStore(settings).load(task)
    _print(state.as_dict() | {"steps": state.steps})
    return True


def cmd_next(settings: Settings, task: Task, _: argparse.Namespace) -> bool:
    state = StateStore(settings).load(task)
    _print({"done": state.done, "next_step": state.next_step, "todo": state.todo, "handoff_revision": state.handoff_revision})
    return True


STEPS = {"ingest": cmd_ingest, "baseline": cmd_baseline, "context": cmd_context, "agent": cmd_agent, "verify": cmd_verify}


def cmd_run(settings: Settings, task: Task, args: argparse.Namespace) -> bool:
    """读取 PowerContext 中的状态，跳过已完成步骤，从第一个未完成步骤继续执行。"""

    store = StateStore(settings)
    while True:
        state = store.load(task)
        step = state.next_step
        if step is None:
            _print({"run": "已完成全部步骤", "done": state.done, "handoff_revision": state.handoff_revision})
            return True
        print(f"== 从 PowerContext 状态恢复：已完成 {state.done or '无'}，执行下一步 [{step}]", file=sys.stderr)
        if not STEPS[step](settings, task, args):
            print(f"== 步骤 [{step}] 未通过，停止；状态已保存，可修复后再次运行 sddctx run", file=sys.stderr)
            return False


def cmd_check(settings: Settings, task: Task, args: argparse.Namespace) -> bool:  # noqa: C901
    """核对最近一次上下文：必须包含样例规定的来源，且每条来源的行号与当前文件内容一致。"""

    context = json.loads((settings.records_dir / "context.json").read_text(encoding="utf-8"))
    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    problems: list[str] = []
    applicable = {spec["id"]: spec for spec in context["specs"]["applicable"]}
    superseded = {spec["id"] for spec in context["specs"]["superseded"]}
    edit_targets = {code["symbol"] for code in context["code"]["edit_targets"]}
    tests = set(context["tests"]["functional_files"]) | set(context["tests"]["regression_files"])
    problems += [f"缺少适用规格 {spec_id}" for spec_id in expected["specs"] if spec_id not in applicable]
    problems += [f"已失效规格 {spec_id} 不应作为适用规格" for spec_id in expected.get("superseded", []) if spec_id in applicable]
    problems += [f"未标出已失效规格 {spec_id}" for spec_id in expected.get("superseded", []) if spec_id not in superseded]
    problems += [f"缺少待修改符号 {symbol}" for symbol in expected["edit_targets"] if symbol not in edit_targets]
    problems += [f"缺少功能测试 {path}" for path in expected["functional_tests"] if path not in context["tests"]["functional_files"]]
    problems += [f"缺少回归测试 {path}" for path in expected["regression_tests"] if path not in tests]
    for spec_id, needle in expected.get("spec_contains", {}).items():
        if needle not in applicable.get(spec_id, {}).get("body", ""):
            problems.append(f"规格 {spec_id} 内容中没有“{needle}”")
    sources = [*context["specs"]["applicable"], *context["code"]["edit_targets"], *context["code"]["references"]]
    sources += [*context["tests"]["functional"], *context["tests"]["regression"]]
    for entry in sources:
        path, lines = entry["source"].split(":L")
        start, end = (int(part.lstrip("L")) for part in lines.split("-"))
        file = settings.project_root / path
        if not file.is_file():
            problems.append(f"来源文件不存在 {entry['source']}")
            continue
        actual = "\n".join(file.read_text(encoding="utf-8").splitlines()[start - 1 : end])
        if entry["body"].strip() not in actual and actual.strip() != entry["body"].strip():
            problems.append(f"来源行号与当前文件不一致 {entry['source']}")
    state = context.get("state") or {}
    for key in ("done", "todo", "verifications"):
        if key not in state:
            problems.append(f"上下文缺少状态字段 {key}")
    report = {"ok": not problems, "checked_sources": len(sources), "problems": problems}
    write_json(settings.records_dir / "context-check.json", report)
    _print(report)
    return not problems


def cmd_reset(settings: Settings, task: Task, args: argparse.Namespace) -> bool:
    """清空运行时状态（seekdb 资料库、PowerContext 数据），并从基线重新复制业务工程工作副本。"""

    if settings.project_root == settings.baseline_root:
        raise SystemExit("工作副本与基线是同一目录，拒绝覆盖；请设置 SDDCTX_PROJECT_ROOT")
    stop_server(settings)
    shutil.rmtree(settings.runtime_dir, ignore_errors=True)
    shutil.rmtree(settings.project_root, ignore_errors=True)
    shutil.copytree(settings.baseline_root, settings.project_root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for overlay in args.overlay or []:
        shutil.copytree(Path(overlay).resolve(), settings.project_root, dirs_exist_ok=True)
    _print({"reset": True, "runtime_removed": _display(settings.runtime_dir), "baseline": _display(settings.baseline_root), "work_copy": _display(settings.project_root)})
    return True


def cmd_overlay(settings: Settings, _: Task, args: argparse.Namespace) -> bool:
    """把一个目录叠加到工作副本上（例如规格更新），之后再运行 ingest 即可增量更新资料库。"""

    source = Path(args.source).resolve()
    files = sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())
    shutil.copytree(source, settings.project_root, dirs_exist_ok=True)
    _print({"overlay": _display(source), "files": files, "work_copy": _display(settings.project_root)})
    return True


def cmd_server(settings: Settings, _: Task | None, args: argparse.Namespace) -> bool:
    action = {"start": start_server, "stop": stop_server, "status": server_status}[args.action]
    _print(action(settings))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sddctx", description="SDD 工程上下文模块")
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("server")
    server.add_argument("action", choices=["start", "stop", "status"])
    for name in ("reset", "overlay", "ingest", "baseline", "context", "agent", "verify", "state", "next", "run", "check"):
        command = sub.add_parser(name)
        command.add_argument("--task", required=True, help="任务卡 Markdown 路径")
        if name == "context":
            command.add_argument("--format", choices=["md", "json"], default="md")
            command.add_argument("--no-record", dest="record", action="store_false", help="只输出，不写入 PowerContext")
        if name in ("agent", "run"):
            command.add_argument("--replay", help="重放已记录的 MiniMax 输出目录（records/runs/<run>/minimax）")
            command.add_argument("--max-attempts", type=int, default=2)
        if name == "run":
            command.add_argument("--format", default="md")
            command.add_argument("--record", default=True)
        if name == "check":
            command.add_argument("--expected", required=True)
        if name == "reset":
            command.add_argument("--overlay", action="append", help="复制基线后再叠加的目录，可重复")
        if name == "overlay":
            command.add_argument("source", help="要叠加到工作副本的目录")
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.command == "server":
        return 0 if cmd_server(settings, None, args) else 1
    task = load_task(Path(args.task))
    if args.command == "reset":
        return 0 if cmd_reset(settings, task, args) else 1
    run_settings = _run_settings(settings, task)
    handlers = {"overlay": cmd_overlay, "state": cmd_state, "next": cmd_next, "run": cmd_run, "check": cmd_check, **STEPS}
    return 0 if handlers[args.command](run_settings, task, args) else 1


if __name__ == "__main__":
    raise SystemExit(main())

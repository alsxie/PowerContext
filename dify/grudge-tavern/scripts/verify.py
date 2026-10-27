#!/usr/bin/env python3
# 作者：FDE WorkMate
"""运行 samples/scenario.json 验收样例。

每轮通过 Dify Service API（streaming）发消息，收集节点级事件；同时在每轮前后直接读取
PowerContext 账本（active / forgotten），断言只基于 PowerContext 的真实状态和节点输出。

用法：
    python3 scripts/verify.py [--player 阿土-xxxx] [--out evidence/]
环境变量：DIFY_URL、DIFY_APP_API_KEY、POWERCONTEXT_HOST_URL、POWERCONTEXT_TOKEN
退出码：全部断言通过为 0，否则为 1。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIFY_URL = os.environ.get("DIFY_URL", "http://127.0.0.1:18080").rstrip("/")
APP_KEY = os.environ.get("DIFY_APP_API_KEY", "")
PC_URL = os.environ.get("POWERCONTEXT_HOST_URL", "http://127.0.0.1:18765").rstrip("/")
PC_TOKEN = os.environ.get("POWERCONTEXT_TOKEN", "")


def post_json(url: str, payload: dict, token: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def scope_of(player: str) -> str:
    # 与 DSL 中 prep_scope 节点保持一致的幂等请求，得到同一个 Scope
    body = {"title": f"记仇酒馆·{player}", "summary": f"焦记麦酒馆老板老焦关于旅人「{player}」的小本本",
            "idempotency_key": f"grudge-tavern:v1:player:{player}"}
    return post_json(f"{PC_URL}/v1/scopes", body, PC_TOKEN)["scope_id"]


def ledger(scope_id: str) -> dict:
    active = post_json(f"{PC_URL}/v1/memory/entries/list", {"scope_id": scope_id, "limit": 100}, PC_TOKEN)["entries"]
    forgotten = post_json(f"{PC_URL}/v1/atomic-memory/list",
                          {"scope_id": scope_id, "states": ["forgotten"], "limit": 100}, PC_TOKEN)["items"]
    pick = lambda e: {"id": e["artifact"]["artifact_id"], "kind": e.get("kind"), "text": e.get("text")}  # noqa: E731
    return {"active": [pick(e) for e in active], "forgotten": [pick(e) for e in forgotten]}


def chat(player: str, query: str, conversation_id: str, user: str) -> dict:
    payload = {"inputs": {"player_name": player}, "query": query, "response_mode": "streaming",
               "conversation_id": conversation_id, "user": user}
    req = urllib.request.Request(f"{DIFY_URL}/v1/chat-messages", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {APP_KEY}"})
    answer, nodes, meta = [], [], {}
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            kind = event.get("event")
            if kind == "message":
                answer.append(event.get("answer", ""))
            elif kind == "node_finished":
                data = event["data"]
                nodes.append({k: data.get(k) for k in ("node_id", "node_type", "title", "status", "elapsed_time",
                                                       "error", "outputs")})
            elif kind == "workflow_finished":
                meta.update(workflow_run_id=event["data"].get("id"), status=event["data"].get("status"),
                            elapsed_time=event["data"].get("elapsed_time"))
            elif kind == "error":
                raise RuntimeError(event)
            if event.get("conversation_id"):
                meta["conversation_id"] = event["conversation_id"]
    return {"answer": "".join(answer), "nodes": nodes, **meta}


def node_output(nodes: list, node_id: str, key: str, default=None):
    for node in nodes:
        if node["node_id"] == node_id and node.get("outputs"):
            return node["outputs"].get(key, default)
    return default


def check(turn: dict, before: dict, after: dict, run: dict, captured: dict) -> list:
    expect = turn["expect"]
    ids = lambda rows: {r["id"] for r in rows}  # noqa: E731
    new_active = [r for r in after["active"] if r["id"] not in ids(before["active"])]
    new_forgotten = ids(after["forgotten"]) - ids(before["forgotten"])
    recalled = set(node_output(run["nodes"], "merge_recall", "recall_ids", []) or [])
    resolve = lambda names: [captured.get(n, n) for n in names]  # noqa: E731
    results = []

    def record(name: str, ok: bool, detail: str) -> None:
        results.append({"check": name, "ok": bool(ok), "detail": detail})

    if "remembered_kinds_include" in expect:
        kinds = sorted({r["kind"] for r in new_active})
        record("新记账类型", set(expect["remembered_kinds_include"]) <= set(kinds), f"新增 {kinds}")
    if "active_kinds_include" in expect:
        kinds = sorted({r["kind"] for r in after["active"]})
        record("账本类型", set(expect["active_kinds_include"]) <= set(kinds), f"active {kinds}")
    if "active_ids_include" in expect:
        want = resolve(expect["active_ids_include"])
        record("仍在账上", set(want) <= ids(after["active"]), f"期望 {want}")
    if "active_ids_exclude" in expect:
        want = resolve(expect["active_ids_exclude"])
        record("已不在账上", not (set(want) & ids(after["active"])), f"期望移除 {want}")
    if "recalled_include" in expect:
        want = resolve(expect["recalled_include"])
        record("跨会话翻出旧账", set(want) <= recalled, f"翻出 {sorted(recalled)}")
    if "forgotten_include" in expect:
        want = resolve(expect["forgotten_include"])
        record("一笔勾销", set(want) <= new_forgotten, f"本轮勾销 {sorted(new_forgotten)}")
    if "forgotten_count" in expect:
        record("勾销数量", len(new_forgotten) == expect["forgotten_count"], f"本轮勾销 {sorted(new_forgotten)}")
    if "answer_contains" in expect:
        want = resolve(expect["answer_contains"])
        missing = [w for w in want if w not in run["answer"]]
        record("回答包含", not missing, f"缺少 {missing}" if missing else "全部包含")
    if expect.get("workflow_branch") == "ledger":
        executed = {n["node_id"] for n in run["nodes"]}
        record("走查账分支", "ledger_view" in executed and "think" not in executed, "ledger_view 已执行，LLM 未调用")
    record("工作流成功", run.get("status") == "succeeded", f"status={run.get('status')}")

    for name, spec in turn.get("capture", {}).items():
        source, kind = spec.split(":", 1)
        match = next((r["id"] for r in new_active if r["kind"] == kind), None)
        if match:
            captured[name] = match
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--player", default=f"阿土-{dt.datetime.now().strftime('%m%d%H%M')}")
    parser.add_argument("--scenario", default=str(ROOT / "samples" / "scenario.json"))
    parser.add_argument("--out", default=str(ROOT / "evidence"))
    args = parser.parse_args()
    if not APP_KEY or not PC_TOKEN:
        print("需要 DIFY_APP_API_KEY 与 POWERCONTEXT_TOKEN", file=sys.stderr)
        return 2

    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    scope_id = scope_of(args.player)
    conversations: dict = {}
    captured: dict = {}
    report = {"player": args.player, "scope_id": scope_id, "dify_url": DIFY_URL,
              "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "turns": []}
    passed = True
    for turn in scenario["turns"]:
        before = ledger(scope_id)
        started = time.time()
        run = chat(args.player, turn["query"], conversations.get(turn["conversation"], ""), "verify-" + args.player)
        conversations[turn["conversation"]] = run.get("conversation_id", "")
        after = ledger(scope_id)
        checks = check(turn, before, after, run, captured)
        ok = all(c["ok"] for c in checks)
        passed &= ok
        report["turns"].append({"id": turn["id"], "conversation": turn["conversation"],
                                "conversation_id": run.get("conversation_id"),
                                "workflow_run_id": run.get("workflow_run_id"), "query": turn["query"],
                                "answer": run["answer"], "seconds": round(time.time() - started, 1),
                                "checks": checks, "ledger_after": after, "nodes": run["nodes"]})
        print(f"[{'PASS' if ok else 'FAIL'}] {turn['id']}  ({conversations[turn['conversation']][:8]})")
        for c in checks:
            print(f"    {'ok ' if c['ok'] else 'NG '} {c['check']}: {c['detail']}")
    report["captured"] = captured
    report["passed"] = passed

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "verify-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "verify-transcript.md").write_text(render_markdown(report), encoding="utf-8")
    print(f"{'ALL PASSED' if passed else 'SOME CHECKS FAILED'}，报告：{out}/verify-report.json")
    return 0 if passed else 1


def render_markdown(report: dict) -> str:
    lines = ["# 记仇酒馆验收运行记录", "", "作者：FDE WorkMate", "",
             f"- 玩家名号：`{report['player']}`  PowerContext scope：`{report['scope_id']}`",
             f"- 开始时间：{report['started_at']}  结论：{'全部通过' if report['passed'] else '存在失败'}", ""]
    for turn in report["turns"]:
        lines += [f"## {turn['id']}（会话 {turn['conversation']}：`{turn['conversation_id']}`）", "",
                  f"workflow_run_id：`{turn['workflow_run_id']}`，耗时 {turn['seconds']}s", "",
                  f"> 旅人：{turn['query']}", "", "老焦：", "", "```text", turn["answer"].strip(), "```", "",
                  "| 断言 | 结果 | 说明 |", "| --- | --- | --- |"]
        lines += [f"| {c['check']} | {'通过' if c['ok'] else '失败'} | {c['detail']} |" for c in turn["checks"]]
        lines += ["", "节点轨迹：" + " → ".join(f"{n['title']}({n['status']})" for n in turn["nodes"]
                                          if n["node_type"] not in ("iteration-start",)), ""]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())

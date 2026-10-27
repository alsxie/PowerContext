# 作者：FDE WorkMate
"""直接执行 DSL 中的代码节点，校验解析、降级与 PowerContext 请求体构造。

    uv run --no-project --with pyyaml==6.0.2 python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

DSL = Path(__file__).resolve().parents[1] / "dsl" / "grudge-tavern.yml"


def load_nodes() -> dict:
    graph = yaml.safe_load(DSL.read_text(encoding="utf-8"))["workflow"]["graph"]
    return {node["id"]: node["data"] for node in graph["nodes"]}


NODES = load_nodes()


def run(node_id: str, **kwargs):
    namespace: dict = {}
    exec(NODES[node_id]["code"], namespace)  # noqa: S102 - 执行仓库内受控的节点代码
    result = namespace["main"](**kwargs)
    declared = set(NODES[node_id]["outputs"])
    assert set(result) == declared, f"{node_id} 输出 {set(result)} 与声明 {declared} 不一致"
    return result


LEDGER = [
    {"label": "L1", "artifact": {"family": "atomic-memory", "artifact_id": "art_debt", "revision": 1},
     "state_version": 0, "kind": "debt", "text": "阿土赊了三杯麦酒"},
    {"label": "L2", "artifact": {"family": "atomic-memory", "artifact_id": "art_prank", "revision": 1},
     "state_version": 0, "kind": "prank", "text": "阿土把看门猫染绿了"},
]


class CodeNodeTest(unittest.TestCase):
    def test_prep_scope_normalizes_name(self):
        out = run("prep_scope", player_name="  阿  土 ")
        self.assertEqual(out["player"], "阿 土")
        self.assertEqual(json.loads(out["scope_body"])["idempotency_key"], "grudge-tavern:v1:player:阿 土")
        self.assertEqual(run("prep_scope", player_name="")["player"], "无名旅人")

    def test_read_scope_offline(self):
        out = run("read_scope", body="", status_code=0)
        self.assertEqual(out["online"], "no")
        out = run("read_scope", body='{"scope_id":"scp_1"}', status_code=200)
        self.assertEqual((out["online"], json.loads(out["list_body"])["scope_id"]), ("yes", "scp_1"))

    def test_ledger_digest_labels(self):
        body = json.dumps({"entries": [{k: v for k, v in item.items() if k != "label"} for item in LEDGER]})
        out = run("ledger_digest", body=body, status_code=200)
        self.assertIn("L1 [赊账] 阿土赊了三杯麦酒", out["ledger_text"])
        self.assertEqual(json.loads(out["ledger_json"])[1]["label"], "L2")
        self.assertIn("还没有", run("ledger_digest", body="", status_code=0)["ledger_text"])

    def test_plan_builds_requests_and_score(self):
        think = "<think>x</think>```json\n" + json.dumps({
            "keywords": ["麦酒", "麦酒", "猫", "青蛙", "后山", "多余"],
            "new_events": [{"kind": "favor", "text": "阿土还清了三杯麦酒"}, {"kind": "bogus", "text": "x"}],
            "settle": ["L1", "L9"],
        }, ensure_ascii=False) + "\n```"
        out = run("plan", think_text=think, ledger_json=json.dumps(LEDGER, ensure_ascii=False),
                  scope_id="scp_1", player="阿土")
        self.assertEqual(out["keywords"], ["麦酒", "猫", "青蛙", "后山"])
        self.assertEqual(len(out["recall_bodies"]), 4)
        self.assertEqual(json.loads(out["remember_bodies"][0])["kind"], "favor")
        forget = json.loads(out["forget_bodies"][0])
        self.assertEqual(forget["target"]["artifact"]["artifact_id"], "art_debt")
        self.assertEqual(forget["state"], "forgotten")
        # prank(2) 保留，debt 被勾销，favor(-2) 新增
        self.assertEqual(out["grudge"], 0)
        self.assertEqual(out["mood"], "眉开眼笑")

    def test_plan_tolerates_garbage_and_offline(self):
        out = run("plan", think_text="老焦今天不想记账", ledger_json="[]", scope_id="", player="阿土")
        self.assertEqual((out["keywords"], out["recall_bodies"], out["remember_bodies"]), ([], [], []))

    def test_plan_skips_events_already_on_ledger(self):
        think = json.dumps({"keywords": [], "settle": [], "new_events": [
            {"kind": "debt", "text": "阿土赊了三杯麦酒"}, {"kind": "gift", "text": "阿土送了一篮苹果"},
            {"kind": "gift", "text": "阿土送了一篮苹果"}]}, ensure_ascii=False)
        out = run("plan", think_text=think, ledger_json=json.dumps(LEDGER, ensure_ascii=False),
                  scope_id="scp_1", player="阿土")
        self.assertEqual([json.loads(b)["kind"] for b in out["remember_bodies"]], ["gift"])

    def test_only_idempotent_requests_retry(self):
        retry = {node_id: data["retry_config"]["enabled"] for node_id, data in NODES.items()
                 if data["type"] == "http-request"}
        self.assertFalse(retry.pop("remember_call"))
        self.assertTrue(all(retry.values()), retry)

    def test_merge_recall_dedupes(self):
        hit = {"memory": {"artifact": {"artifact_id": "art_debt"}, "kind": "debt", "text": "阿土赊了三杯麦酒"}}
        results = [json.dumps({"hits": [hit]}, ensure_ascii=False)] * 2 + ["", "not json"]
        out = run("merge_recall", results=results, keywords=["麦酒"])
        self.assertEqual(out["recall_ids"], ["art_debt"])
        self.assertEqual(out["recall_text"].count("赊账"), 1)

    def test_footer_and_ledger_view(self):
        record = {"artifact": {"artifact_id": "art_new"}, "kind": "debt", "text": "阿土又赊了一杯"}
        out = run("footer", online="yes", remembered=[json.dumps({"records": [record]}, ensure_ascii=False)],
                  forgotten=[], recall_ids=["art_debt"], keywords_text="麦酒", grudge=5, mood="阴阳怪气")
        self.assertIn("新记一笔：[赊账] 阿土又赊了一杯（art_new）", out["text"])
        self.assertIn("连不上", run("footer", online="no", remembered=[], forgotten=[], recall_ids=[],
                                     keywords_text="", grudge=0, mood="")["text"])
        view = run("ledger_view", player="阿土", online="yes",
                   active_body=json.dumps({"entries": LEDGER}, ensure_ascii=False), active_status=200,
                   forgotten_body=json.dumps({"items": []}), forgotten_status=200)
        self.assertIn("当前记仇值 5", view["text"])


if __name__ == "__main__":
    unittest.main()

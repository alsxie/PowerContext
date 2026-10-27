// 作者：FDE WorkMate
// 数据层集成测试：真实 seekdb 嵌入式实例（临时目录），覆盖写入/刷新/混合检索/增量/会话/签名一致性。
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, before, test } from "node:test";
import { loadConfig } from "../config.mjs";
import { KnowledgeBaseService, toQueryString } from "../kb.mjs";

const DIM = 8;
const SIG = "test-model@r1:fp32";
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "edgekb-test-"));
let svc;

function unit(i, j = -1) {
  const v = new Array(DIM).fill(0.01);
  v[i % DIM] = 1;
  if (j >= 0) v[j % DIM] = 0.8;
  const n = Math.hypot(...v);
  return v.map((x) => x / n);
}

const docs1 = [
  { id: "d1#0", docId: "d1", text: "seekdb 支持向量检索与全文检索的混合查询", embedding: unit(0) },
  { id: "d2#0", docId: "d2", text: "浏览器里用 WebGPU 运行 embedding 模型", embedding: unit(1) },
  { id: "d3#0", docId: "d3", text: "今天的天气很适合去公园散步", embedding: unit(2) },
  { id: "d4#0", docId: "d4", text: "量化可以把模型体积压缩到原来的四分之一", embedding: unit(3) },
];
const docs2 = [{ id: "d5#0", docId: "d5", text: "增量导入的新文档：海洋数据库实验室", embedding: unit(4) }];

before(async () => {
  svc = await new KnowledgeBaseService({ ...loadConfig({}), dbPath: dir, database: "edge_kb_test" }).open();
});
after(async () => {
  await svc?.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

async function ingest(name, items) {
  const { batchId } = await svc.beginBatch(name, { signature: SIG });
  await svc.writeChunks(name, batchId, items, { signature: SIG });
  return svc.commitBatch(name, batchId, { probes: items });
}

test("toQueryString 去掉 query_string 运算符", () => {
  assert.equal(toQueryString('a+b "c" (d) OR e*'), "a b c d e");
  assert.equal(toQueryString("向量-检索"), "向量 检索");
});

test("创建知识库幂等，签名不一致返回 409", async () => {
  const a = await svc.createKb({ name: "t1", dimension: DIM, signature: SIG });
  assert.equal(a.created, true);
  const b = await svc.createKb({ name: "t1", dimension: DIM, signature: SIG });
  assert.equal(b.created, false);
  await assert.rejects(svc.createKb({ name: "t1", dimension: DIM, signature: "other:int8" }), { status: 409 });
  await assert.rejects(svc.createKb({ name: "Bad Name", dimension: DIM, signature: SIG }), { status: 400 });
});

test("提交批次后刷新可见，三种检索模式均由 seekdb 返回", async () => {
  const c = await ingest("t1", docs1);
  assert.equal(c.status, "refreshed");
  assert.equal(c.probes.ok, true);

  const v = await svc.search("t1", { embedding: unit(1), text: "", k: 2, mode: "vector", signature: SIG });
  assert.equal(v.hits[0].id, "d2#0");

  const f = await svc.search("t1", { text: "天气 公园", k: 2, mode: "fulltext" });
  assert.equal(f.hits[0].id, "d3#0");

  // 混合：向量指向 d4，关键词指向 d1，RRF 融合后两者都应在前 2
  const h = await svc.search("t1", { embedding: unit(3), text: "混合查询", k: 2, mode: "hybrid", signature: SIG, explain: true });
  assert.equal(h.mode, "hybrid");
  assert.deepEqual(new Set(h.hits.map((x) => x.id)), new Set(["d1#0", "d4#0"]));
  assert.match(h.sql.sql, /full join/i);
  assert.match(h.sql.sql, /match\(`document`\) against/i);

  await assert.rejects(svc.search("t1", { embedding: unit(0), text: "x", signature: "other:int8" }), { status: 409 });
});

test("增量导入：第二批刷新后可检索，计数与批次状态正确", async () => {
  const before = await svc.search("t1", { text: "海洋数据库实验室", k: 5, mode: "fulltext" });
  assert.ok(!before.hits.some((x) => x.id === "d5#0"));
  const c = await ingest("t1", docs2);
  assert.equal(c.batchId, 2);
  const h = await svc.search("t1", { embedding: unit(4), text: "海洋数据库实验室", k: 1, signature: SIG });
  assert.equal(h.hits[0].id, "d5#0");
  const kb = await svc.getKb("t1");
  assert.equal(kb.chunk_count, 5);
  assert.equal(kb.doc_count, 5);
  assert.equal(Number(kb.refreshed_batch), 2);
  const batches = await svc.listBatches("t1");
  assert.deepEqual(batches.map((b) => b.status), ["refreshed", "refreshed"]);
});

test("重复导入同一 chunk 幂等（upsert）", async () => {
  await ingest("t1", docs2);
  assert.equal((await svc.getKb("t1")).chunk_count, 5);
});

test("删除文档后不再召回", async () => {
  await svc.deleteDocument("t1", "d3");
  const f = await svc.search("t1", { text: "天气 公园", k: 5, mode: "fulltext" });
  assert.ok(!f.hits.some((x) => x.id === "d3#0"));
  assert.equal((await svc.getKb("t1")).doc_count, 4);
});

test("会话状态持久化在 seekdb，切换与删除知识库后保持一致", async () => {
  await svc.createKb({ name: "t2", dimension: DIM, signature: SIG });
  await ingest("t2", [{ id: "x#0", docId: "x", text: "只在第二个知识库里的内容", embedding: unit(5) }]);
  let s = await svc.setSession("sess-1", { currentKb: "t1", state: { k: 5 } });
  assert.equal(s.currentKb, "t1");
  s = await svc.setSession("sess-1", { currentKb: "t2", state: { k: 5 } });
  assert.equal((await svc.getSession("sess-1")).currentKb, "t2");
  // 切换后检索结果只来自当前知识库
  const r = await svc.search(s.currentKb, { embedding: unit(0), text: "seekdb", k: 5, signature: SIG });
  assert.deepEqual(r.hits.map((x) => x.id), ["x#0"]);
  await svc.deleteKb("t2");
  assert.equal((await svc.getSession("sess-1")).currentKb, null);
  await assert.rejects(svc.search("t2", { text: "x", mode: "fulltext" }), { status: 404 });
});

test("以 [ 开头的正文可写入可检索（规避嵌入式绑定把 [ 开头参数当向量字面量）", async () => {
  await svc.createKb({ name: "t3", dimension: DIM, signature: SIG });
  const c = await ingest("t3", [{ id: "b#0", docId: "b", text: "[R42] 方括号开头的段落内容", embedding: unit(6) }]);
  assert.equal(c.status, "refreshed");
  const r = await svc.search("t3", { embedding: unit(6), text: "方括号开头", k: 1, signature: SIG });
  assert.equal(r.hits[0].id, "b#0");
  assert.equal(r.hits[0].text, "［R42] 方括号开头的段落内容");
  const { batchId } = await svc.beginBatch("t3", { signature: SIG });
  await assert.rejects(svc.writeChunks("t3", batchId, [{ id: "[x]", text: "x", embedding: unit(1) }]), { status: 400 });
  await assert.rejects(svc.deleteDocument("t3", "[1] or 1=1"), { status: 400 });
});

// 作者：FDE WorkMate
// 应用页端到端验收：分别在 WebGPU 可用 / 不可用（--disable-gpu）的 Chrome 中走完
// 加载模型 → 新建知识库 → 导入示例批次 1 → 检索 → 导入批次 2 → 新内容可检索 → 刷新页面恢复会话 → 删除知识库。
// 用法：node scripts/e2e.mjs（需先 npm start）。结果写入 bench/e2e.json。
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const BASE = process.env.KB_URL || "http://127.0.0.1:8787";
const CASES = [
  { name: "webgpu-available", args: [], expect: "webgpu", dtype: "fp16" },
  { name: "webgpu-unavailable", args: ["--disable-gpu"], expect: "wasm", dtype: "int8" },
];

async function waitIdle(page) {
  await page.waitForFunction(() => [...document.querySelectorAll("button")].every((b) => !b.disabled), null, { timeout: 10 * 60 * 1000 });
}

async function runCase(c) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "edgekb-e2e-"));
  const ctx = await chromium.launchPersistentContext(dir, { channel: process.env.CHROME_CHANNEL || "chrome", headless: process.env.HEADLESS !== "0", args: c.args });
  const out = { case: c.name };
  const kb = `e2e-${c.expect}-${Date.now().toString(36)}`;
  try {
    const page = await ctx.newPage();
    const external = [];
    page.on("request", (r) => {
      const u = new URL(r.url());
      if (u.origin !== new URL(BASE).origin && !u.protocol.startsWith("blob") && !u.protocol.startsWith("data")) external.push(r.url());
    });
    await page.goto(`${BASE}/index.html`);
    await page.waitForFunction(() => /当前后端/.test(document.getElementById("backend-badge").textContent), null, { timeout: 120000 });
    out.badge = await page.textContent("#backend-badge");
    out.detail = await page.textContent("#backend-detail");
    assert.ok(out.badge.includes(c.expect === "webgpu" ? "WebGPU" : "WASM"), `后端提示应为 ${c.expect}：${out.badge}`);
    if (c.expect === "wasm") assert.match(out.detail, /降级/, "WASM 降级时必须给出原因");

    await page.selectOption("#dtype", c.dtype);
    await page.fill("#kb-name", kb);
    await page.click("#kb-create");
    await waitIdle(page);
    await page.click("#import-demo-1");
    await waitIdle(page);
    out.import1 = await page.textContent("#import-status");
    assert.match(out.import1, /完成：1500 段/);

    // 增量校验用查询：取批次 2 中一段正文的前 40 字，导入批次 2 前不应命中，刷新后应命中
    const doc = (await (await fetch(`${BASE}/data/mmarco-zh-mini/corpus.jsonl`)).text()).split("\n").filter(Boolean).map((l) => JSON.parse(l)).find((x) => x.batch === 2 && x.text.length > 60);
    const q = { text: doc.text.slice(0, 40), relevant: [doc.id] };
    const search = async () => {
      await page.fill("#q", q.text);
      await page.click("#search-form button[type=submit]");
      await waitIdle(page);
      // 结果 id 为 chunk id（docId#序号），按文档 id 比较
      return page.$$eval("#results li .meta", (els) => els.map((e) => e.textContent.split(" · ")[0].replace(/#\d+$/, "")));
    };
    const before = await search();
    out.searchTiming = await page.textContent("#search-timing");
    await page.click("#import-demo-2");
    await waitIdle(page);
    const after = await search();
    // 界面截图：后端提示、导入进度与检索结果（docs/ 下，供技术说明引用）
    await page.screenshot({ path: path.join(ROOT, "docs", `e2e-${c.name}.png`), fullPage: true });
    out.incremental = { query: q.text, relevant: q.relevant, hitBefore: before.some((id) => q.relevant.includes(id)), hitAfter: after.some((id) => q.relevant.includes(id)) };
    assert.equal(out.incremental.hitBefore, false);
    assert.equal(out.incremental.hitAfter, true, "批次 2 刷新后应能检索到新内容");

    // 刷新页面：会话（当前知识库、上次查询）从 seekdb 恢复
    await page.reload();
    await page.waitForFunction(() => /当前后端/.test(document.getElementById("backend-badge").textContent), null, { timeout: 120000 });
    await waitIdle(page);
    out.restored = { kb: await page.inputValue("#kb-select"), q: await page.inputValue("#q") };
    assert.equal(out.restored.kb, kb);
    assert.equal(out.restored.q, q.text);

    page.once("dialog", (d) => d.accept());
    await page.click("#kb-delete");
    await waitIdle(page);
    out.externalRequests = external;
    assert.deepEqual(external, [], "不应有任何对外请求（模型与运行时均来自本地服务）");
    out.ok = true;
  } finally {
    // 失败时也清理测试知识库
    await fetch(`${BASE}/api/kbs/${kb}`, { method: "DELETE" }).catch(() => {});
    await ctx.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
  return out;
}

const results = [];
for (const c of CASES) {
  console.log(`[e2e] ${c.name}`);
  const r = await runCase(c);
  console.log(`  ${r.badge} | ${r.detail || ""}\n  ${r.import1}\n  ${r.searchTiming}\n  增量：${JSON.stringify(r.incremental)}`);
  results.push(r);
}
fs.writeFileSync(path.join(ROOT, "bench", "e2e.json"), JSON.stringify({ author: "FDE WorkMate", at: new Date().toISOString(), results }, null, 2));
console.log("[e2e] 全部通过");

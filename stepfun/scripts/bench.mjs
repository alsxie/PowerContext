// 作者：FDE WorkMate
// 基准驱动：用 Playwright 启动本机 Chrome，按矩阵打开 bench.html，收集原始结果并生成汇总表。
//
// 用法：
//   node scripts/bench.mjs                      # 默认矩阵（见 MATRIX），每个配置：1 次冷启动 + (repeat-1) 次热启动
//   node scripts/bench.mjs --only int8-webgpu --repeat 2
//   node scripts/bench.mjs --summary            # 只根据 bench/results/*.json 重新生成 bench/summary.md
// 环境：KB_URL（默认 http://127.0.0.1:8787），CHROME_CHANNEL（默认 chrome），HEADLESS=0 可看到窗口
//
// 冷/热定义：每个配置使用全新的浏览器用户目录，第一次运行为冷启动（模型与 wasm 需从本地服务下载），
// 之后在同一用户目录内重新打开页面为热启动（命中 HTTP 缓存 / Cache API）。
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const RESULTS = path.join(ROOT, "bench", "results");
const BASE = process.env.KB_URL || "http://127.0.0.1:8787";

// 统一的“优化后”流水线参数；baseline-* 为“优化前”流水线
// （逐条推理、不按长度排序、推理与写入串行、SDK 逐条 upsert、每 16 条提交并刷新一次）
const OPT = { batch: 32, send: 256, pipelined: 1, commitPerSend: 0, sort: 1, writeMode: "bulk" };
const BASELINE = { batch: 1, send: 16, pipelined: 0, commitPerSend: 1, sort: 0, writeMode: "upsert" };
const MODES = "exact,vector,hybrid,fulltext";
export const MATRIX = [
  { label: "fp32-webgpu", dtype: "fp32", backend: "webgpu", ...OPT },
  { label: "fp16-webgpu", dtype: "fp16", backend: "webgpu", ...OPT },
  { label: "int8-webgpu", dtype: "int8", backend: "webgpu", ...OPT },
  { label: "wq8-webgpu", dtype: "wq8", backend: "webgpu", ...OPT },
  { label: "fp32-wasm", dtype: "fp32", backend: "wasm", ...OPT },
  { label: "fp16-wasm", dtype: "fp16", backend: "wasm", ...OPT },
  { label: "int8-wasm", dtype: "int8", backend: "wasm", ...OPT },
  { label: "wq8-wasm", dtype: "wq8", backend: "wasm", ...OPT },
  { label: "baseline-fp32-webgpu", dtype: "fp32", backend: "webgpu", ...BASELINE },
  { label: "baseline-fp32-wasm", dtype: "fp32", backend: "wasm", ...BASELINE },
  // 单项优化消融：在 fp16+WebGPU 上只去掉一项，其余保持优化后参数
  { label: "ablate-batch1", dtype: "fp16", backend: "webgpu", ...OPT, batch: 1 },
  { label: "ablate-nosort", dtype: "fp16", backend: "webgpu", ...OPT, sort: 0 },
  { label: "ablate-upsert", dtype: "fp16", backend: "webgpu", ...OPT, writeMode: "upsert" },
  { label: "ablate-refresh-each16", dtype: "fp16", backend: "webgpu", ...OPT, send: 16, commitPerSend: 1 },
  // 批大小扫描：WebGPU 用 FP16、WASM 用 INT8（各自的默认精度），其余为优化后参数
  ...[1, 8, 16, 32, 64, 128].map((batch) => ({ label: `sweep-fp16-webgpu-b${batch}`, dtype: "fp16", backend: "webgpu", ...OPT, batch, modes: "hybrid" })),
  ...[8, 16, 32, 64].map((batch) => ({ label: `sweep-int8-wasm-b${batch}`, dtype: "int8", backend: "wasm", ...OPT, batch, modes: "hybrid" })),
  // 质量评测：每种精度 × 后端各一次，含精确向量检索（exact），用于量化前后质量对比
  ...["fp32", "fp16", "int8", "wq8"].flatMap((dtype) => ["webgpu", "wasm"].map((backend) => ({ label: `quality-${dtype}-${backend}`, dtype, backend, ...OPT, quality: 1 }))),
  // 降级路径：backend=auto，但浏览器以 --disable-gpu 启动（requestAdapter 返回 null），应自动走 WASM
  { label: "fallback-auto-nogpu", dtype: "int8", backend: "auto", ...OPT, chromeArgs: ["--disable-gpu"] },
];

function args() {
  const a = process.argv.slice(2);
  const get = (k, d) => {
    const i = a.indexOf(k);
    return i >= 0 ? a[i + 1] : d;
  };
  return { only: get("--only", null), repeat: Number(get("--repeat", 3)), runs: Number(get("--runs", 3)), summary: a.includes("--summary") };
}

async function runOnce(context, conf, runs, phase) {
  const page = await context.newPage();
  const logs = [];
  page.on("console", (m) => logs.push(`[${m.type()}] ${m.text()}`));
  page.on("pageerror", (e) => logs.push(`[pageerror] ${e.message}`));
  const q = new URLSearchParams({ modes: MODES, ...Object.fromEntries(Object.entries(conf).filter(([k]) => k !== "chromeArgs")), runs, auto: 1, label: `${conf.label}-${phase}` });
  const t0 = Date.now();
  await page.goto(`${BASE}/bench.html?${q}`);
  await page.waitForFunction(() => window.__bench?.done, null, { timeout: 30 * 60 * 1000, polling: 1000 });
  const r = await page.evaluate(() => window.__bench);
  await page.close();
  if (!r.ok) {
    console.error(logs.slice(-30).join("\n"));
    throw new Error(`${conf.label}/${phase} 失败：${r.error}`);
  }
  console.log(`  ${phase}: ${((Date.now() - t0) / 1000).toFixed(1)}s device=${r.result.backend.device} load=${r.result.load.loadMs.toFixed(0)}ms → ${r.result.saved}`);
  return r.result;
}

async function runMatrix({ only, repeat, runs }) {
  const health = await fetch(`${BASE}/api/health`).then((r) => r.json()).catch(() => null);
  if (!health?.ok) throw new Error(`服务未启动：${BASE}（先运行 npm start）`);
  const list = MATRIX.filter((c) => !only || only.split(",").includes(c.label));
  for (const conf of list) {
    console.log(`[bench] ${conf.label}`);
    const userDir = fs.mkdtempSync(path.join(os.tmpdir(), "edgekb-chrome-"));
    const context = await chromium.launchPersistentContext(userDir, {
      channel: process.env.CHROME_CHANNEL || "chrome",
      headless: process.env.HEADLESS !== "0",
      args: conf.chromeArgs || [],
    });
    try {
      for (let i = 0; i < repeat; i++) await runOnce(context, conf, runs, i === 0 ? "cold" : `warm${i}`);
    } finally {
      await context.close();
      fs.rmSync(userDir, { recursive: true, force: true });
    }
  }
}

// ------------------------------------------------------------------ 汇总
const med = (xs) => {
  const a = xs.filter(Number.isFinite).sort((x, y) => x - y);
  if (!a.length) return null;
  const m = (a.length - 1) / 2;
  return (a[Math.floor(m)] + a[Math.ceil(m)]) / 2;
};
const f = (x, d = 1) => (x == null || !Number.isFinite(x) ? "-" : x.toFixed(d));

export function summarize() {
  const files = fs.existsSync(RESULTS) ? fs.readdirSync(RESULTS).filter((x) => x.endsWith(".json")).sort() : [];
  const groups = new Map();
  for (const file of files) {
    const r = JSON.parse(fs.readFileSync(path.join(RESULTS, file), "utf8"));
    const label = r.config.label.replace(/-(cold|warm\d+)$/, "");
    const phase = r.config.label.match(/-(cold|warm\d+)$/)?.[1] || "cold";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push({ ...r, phase, file });
  }
  const order = MATRIX.map((m) => m.label);
  const allLabels = [...groups.keys()].sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99) || a.localeCompare(b));
  // quality-* 为专门的质量评测运行（含精确检索），只进入质量表
  const labels = allLabels.filter((l) => !l.startsWith("quality-") && !l.startsWith("sweep-"));
  const lines = [];
  const env = groups.size ? [...groups.values()][0][0] : null;
  lines.push("# 基准测试汇总（自动生成）", "", "作者：FDE WorkMate", "");
  if (env) {
    lines.push(`- 浏览器：${env.env.userAgent}`, `- seekdb：${env.env.seekdb}；CPU 线程：${env.env.hardwareConcurrency}；crossOriginIsolated=${env.env.crossOriginIsolated}`);
    lines.push(`- 模型：${env.model.id}（上游 ${env.model.upstream}）@ ${env.model.revision.slice(0, 12)}`);
    lines.push(`- 数据集：${env.dataset.name}，${env.dataset.docs} 段 / ${env.dataset.queries} 查询；Top-K=${env.config.k}`, "");
  }
  lines.push("每个配置：1 次冷启动（全新浏览器目录）+ 若干次热启动；吞吐、延迟取所有运行的中位数，质量取各运行均值（各次一致）。", "");
  lines.push("| 配置 | 实际后端 | 精度 | 模型体积 MB | 压缩比 | 向量化吞吐 段/s | 导入 2000 段总耗时 s | 写入+刷新 ms | seekdb 检索 ms（中位/p95） | 端到端查询 ms（中位/p95） | 首次查询 ms | 冷加载 ms | 热加载 ms | Recall@10 混合 | nDCG@10 混合 | Recall@10 向量 | Recall@10 全文 |");
  lines.push("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|");
  for (const label of labels) {
    const rs = groups.get(label);
    const cold = rs.filter((r) => r.phase === "cold");
    const total = med(rs.map((r) => (r.ingest1.totalMs + r.ingest2.totalMs) / 1000));
    const warm = rs.filter((r) => r.phase !== "cold");
    const thr = med(rs.flatMap((r) => [r.ingest1, r.ingest2].map((t) => t.chunks / (t.embedMs / 1000))));
    const wr = med(rs.map((r) => r.ingest1.serverWriteMs + r.ingest1.refreshMs + r.ingest1.visibleMs + r.ingest2.serverWriteMs + r.ingest2.refreshMs + r.ingest2.visibleMs));
    const q = (mode, k) => {
      const v = rs.map((r) => r.quality?.[mode]?.[k]).filter(Number.isFinite);
      return v.length ? v.reduce((s, x) => s + x, 0) / v.length : null;
    };
    const r0 = rs[0];
    lines.push(
      `| ${label} | ${[...new Set(rs.map((r) => r.backend.device))].join("/")} | ${r0.model.dtype} | ${f(r0.model.bytes / 1048576, 1)} | ${f(r0.model.fp32Bytes / r0.model.bytes, 2)}× | ${f(thr)} | ${f(total, 1)} | ${f(wr, 0)} | ${f(med(rs.map((r) => r.latency.dbMs.median)))} / ${f(med(rs.map((r) => r.latency.dbMs.p95)))} | ${f(med(rs.map((r) => r.latency.e2eMs.median)))} / ${f(med(rs.map((r) => r.latency.e2eMs.p95)))} | ${f(med(rs.map((r) => r.firstQuery?.e2eMs)))} | ${f(med(cold.map((r) => r.load.loadMs)), 0)} | ${f(med(warm.map((r) => r.load.loadMs)), 0)} | ${f(q("hybrid", "recall10"), 4)} | ${f(q("hybrid", "ndcg10"), 4)} | ${f(q("vector", "recall10"), 4)} | ${f(q("fulltext", "recall10"), 4)} |`,
    );
  }
  lines.push("", "## 分阶段计时（中位数，ms）", "");
  lines.push("| 配置 | 冷：取文件 | 冷：建会话 | 预热推理 | 批次1 向量化 | 批次1 写入 | 批次1 刷新 | 批次1 可见校验 | 批次2 向量化 | 批次2 写入+刷新 | 查询向量化 | seekdb 检索 | 传输 | 端到端 | 增量召回 前→后 | 降级原因 |");
  lines.push("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|");
  for (const label of labels) {
    const rs = groups.get(label);
    const cold = rs.filter((r) => r.phase === "cold");
    const m = (fn) => med(rs.map(fn));
    const inc = rs[0].incremental;
    lines.push(
      `| ${label} | ${f(med(cold.map((r) => r.load.fetchMs)), 0)} | ${f(med(cold.map((r) => r.load.sessionMs)), 0)} | ${f(m((r) => r.load.warmupMs), 0)} | ${f(m((r) => r.ingest1.embedMs), 0)} | ${f(m((r) => r.ingest1.serverWriteMs), 0)} | ${f(m((r) => r.ingest1.refreshMs), 0)} | ${f(m((r) => r.ingest1.visibleMs), 0)} | ${f(m((r) => r.ingest2.embedMs), 0)} | ${f(m((r) => r.ingest2.serverWriteMs + r.ingest2.refreshMs + r.ingest2.visibleMs), 0)} | ${f(m((r) => r.latency.queryEmbedMs.median))} | ${f(m((r) => r.latency.dbMs.median))} | ${f(m((r) => r.latency.transportMs.median))} | ${f(m((r) => r.latency.e2eMs.median))} | ${f(inc.recall10Before, 3)} → ${f(inc.recall10After, 3)} | ${rs[0].backend.fallbackReason || "-"} |`,
    );
  }
  // 检索质量：各运行的均值与极差（HNSW 每次重建，近似检索结果有小幅波动）；exact 为精确向量检索，只反映模型/量化本身
  lines.push("", "## 检索质量（100 条查询，均值 [最小, 最大]）", "");
  lines.push("| 配置 | 运行数 | 精确向量 nDCG@10 | 精确向量 Recall@10 | HNSW 向量 nDCG@10 | HNSW 向量 Recall@10 | 混合 nDCG@10 | 混合 Recall@10 | 混合 MRR@10 | 全文 nDCG@10 |");
  lines.push("|---|---|---|---|---|---|---|---|---|---|");
  for (const label of allLabels.filter((l) => !l.startsWith("sweep-"))) {
    const rs = groups.get(label);
    const cell = (mode, k) => {
      const v = rs.map((r) => r.quality?.[mode]?.[k]).filter(Number.isFinite);
      if (!v.length) return "-";
      const avg = v.reduce((a, b) => a + b, 0) / v.length;
      return v.length > 1 && Math.max(...v) - Math.min(...v) > 1e-9 ? `${f(avg, 4)} [${f(Math.min(...v), 3)}, ${f(Math.max(...v), 3)}]` : f(avg, 4);
    };
    lines.push(`| ${label} | ${rs.length} | ${cell("exact", "ndcg10")} | ${cell("exact", "recall10")} | ${cell("vector", "ndcg10")} | ${cell("vector", "recall10")} | ${cell("hybrid", "ndcg10")} | ${cell("hybrid", "recall10")} | ${cell("hybrid", "mrr10")} | ${cell("fulltext", "ndcg10")} |`);
  }
  const sweeps = allLabels.filter((l) => l.startsWith("sweep-"));
  if (sweeps.length) {
    lines.push("", "## 推理批大小扫描（中位数）", "");
    lines.push("| 配置 | 运行数 | 向量化吞吐 段/s | 2000 段向量化 ms | 导入总耗时 s | 预热推理 ms |");
    lines.push("|---|---|---|---|---|---|");
    for (const label of sweeps) {
      const rs = groups.get(label);
      lines.push(`| ${label} | ${rs.length} | ${f(med(rs.flatMap((r) => [r.ingest1, r.ingest2].map((t) => t.chunks / (t.embedMs / 1000)))))} | ${f(med(rs.map((r) => r.ingest1.embedMs + r.ingest2.embedMs)), 0)} | ${f(med(rs.map((r) => (r.ingest1.totalMs + r.ingest2.totalMs) / 1000)), 1)} | ${f(med(rs.map((r) => r.load.warmupMs)), 0)} |`);
    }
  }
  lines.push("", `原始数据：bench/results/（${files.length} 个文件）`);
  const out = path.join(ROOT, "bench", "summary.md");
  fs.writeFileSync(out, lines.join("\n") + "\n");
  console.log(`[bench] 汇总 → ${path.relative(ROOT, out)}`);
}

const opts = args();
if (!opts.summary) await runMatrix(opts);
summarize();

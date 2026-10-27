// 作者：FDE WorkMate
// 基准页：固定语料与查询集，分阶段计时 + 检索质量评测，结果回传服务端保存为原始 JSON。
import { api, signatureOf } from "./api.js";
import { BGE_QUERY_INSTRUCTION, Embedder } from "./embedder.js";
import { mean, mrrAt, ndcgAt, recallAt, summarize } from "./metrics.js";
import { ingestChunks, searchKb } from "./pipeline.js";

const p = new URLSearchParams(location.search);
const num = (k, d) => (p.has(k) ? Number(p.get(k)) : d);
const flag = (k, d) => (p.has(k) ? p.get(k) === "1" || p.get(k) === "true" : d);
const cfg = {
  dtype: p.get("dtype") || "int8",
  backend: p.get("backend") || "auto",
  batchSize: num("batch", 32),
  sendSize: num("send", 256),
  pipelined: flag("pipelined", true),
  commitPerSend: flag("commitPerSend", false),
  sortByLength: flag("sort", true),
  writeMode: p.get("writeMode") || "bulk",
  analyzer: p.get("analyzer") || undefined,
  window: p.has("window") ? num("window") : undefined,
  rankConstant: p.has("rrfk") ? num("rrfk") : undefined,
  grid: flag("grid", false),
  instruction: flag("instruction", true),
  runs: num("runs", 3),
  k: num("k", 10),
  modes: (p.get("modes") || "hybrid,vector,fulltext").split(","),
  keep: flag("keep", false),
  dataset: p.get("dataset") || "mmarco-zh-mini",
  label: p.get("label") || "",
  auto: flag("auto", false),
};
cfg.label ||= `${cfg.dtype}-${cfg.backend}-b${cfg.batchSize}`;

const logEl = document.getElementById("log");
const log = (...a) => {
  const line = a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" ");
  logEl.textContent += line + "\n";
  console.log("[bench]", line);
};
window.__bench = { done: false };

async function loadJsonl(url) {
  return (await (await fetch(url)).text()).split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

// 质量评测时每条查询只向量化一次（qvec 缓存），多种检索模式/参数共用同一查询向量，保证对比公平
const qvec = new Map();
async function evaluate(kb, queries, embedder, signature, mode, extra = {}) {
  const rows = [];
  for (const q of queries) {
    const r = await searchKb(kb, q.text, { embedder, signature, mode, k: cfg.k, instruction: cfg.instruction, embedding: qvec.get(q.id), window: cfg.window, rankConstant: cfg.rankConstant, ...extra });
    const ids = r.hits.map((h) => h.id);
    rows.push({ id: q.id, recall5: recallAt(ids, q.relevant, 5), recall10: recallAt(ids, q.relevant, 10), ndcg10: ndcgAt(ids, q.relevant, 10), mrr10: mrrAt(ids, q.relevant, 10), timing: r.timing });
  }
  const agg = (k) => mean(rows.map((r) => r[k]));
  return { n: rows.length, recall5: agg("recall5"), recall10: agg("recall10"), ndcg10: agg("ndcg10"), mrr10: agg("mrr10"), rows };
}

async function run() {
  document.getElementById("start").disabled = true;
  const result = { config: { ...cfg }, env: { userAgent: navigator.userAgent, hardwareConcurrency: navigator.hardwareConcurrency, crossOriginIsolated: self.crossOriginIsolated, startedAt: new Date().toISOString() } };
  try {
    const [health, manifest] = await Promise.all([api.health(), api.models()]);
    result.env.seekdb = health.seekdb;
    result.model = { id: manifest.model_id, upstream: manifest.upstream, revision: manifest.revision, dtype: cfg.dtype, ...manifest.variants[cfg.dtype], fp32Bytes: manifest.variants.fp32.bytes };
    const meta = await (await fetch(`/data/${cfg.dataset}/meta.json`)).json();
    const corpus = await loadJsonl(`/data/${cfg.dataset}/corpus.jsonl`);
    const queries = await loadJsonl(`/data/${cfg.dataset}/queries.jsonl`);
    result.dataset = { name: meta.name, docs: corpus.length, queries: queries.length, chars: meta.doc_chars };
    log(`数据集 ${meta.name}: ${corpus.length} 段 / ${queries.length} 查询`);

    // 1) 模型加载（冷启动与否由调用方决定：新浏览器配置目录 = 冷；同一页面重载 = 热）
    const embedder = new Embedder();
    if (!manifest.variants[cfg.dtype]) throw new Error(`manifest 中没有精度变体 ${cfg.dtype}`);
    const m = await embedder.load({ modelId: manifest.model_id, dtype: cfg.dtype, file: manifest.variants[cfg.dtype].file, preference: cfg.backend });
    result.backend = { device: m.device, requested: cfg.backend, fallbackReason: m.fallbackReason, webgpu: m.webgpu, threads: m.threads };
    result.load = { loadMs: m.loadMs, fetchMs: m.fetchMs, sessionMs: m.sessionMs, warmupMs: m.warmupMs };
    const badge = document.getElementById("backend-badge");
    badge.textContent = m.device === "webgpu" ? `当前后端：WebGPU · ${cfg.dtype}` : `当前后端：WASM · ${cfg.dtype}`;
    badge.className = `badge ${m.device}`;
    document.getElementById("backend-detail").textContent = m.fallbackReason ? `已自动降级：${m.fallbackReason}` : "";
    log("模型", result.backend.device, result.load);

    // 2) 建库
    const signature = signatureOf(manifest, cfg.dtype);
    const kb = `bench-${cfg.label.toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 28)}-${Date.now().toString(36)}`;
    await api.createKb({ name: kb, dimension: manifest.dimension, signature, analyzer: cfg.analyzer });
    result.kb = kb;
    const toChunks = (docs) => docs.map((d) => ({ id: d.id, docId: d.id, chunk: 0, title: d.id, text: d.text }));
    const opts = { embedder, signature, batchSize: cfg.batchSize, sendSize: cfg.sendSize, pipelined: cfg.pipelined, commitPerSend: cfg.commitPerSend, sortByLength: cfg.sortByLength, writeMode: cfg.writeMode };

    // 3) 批次 1 导入
    const b1 = corpus.filter((d) => d.batch === 1);
    result.ingest1 = await ingestChunks(kb, toChunks(b1), { ...opts, label: "batch-1", onProgress: (x) => x.phase === "done" && log(`批次1 完成 ${x.done}`) });
    log("批次1", pick(result.ingest1));

    // 4) 增量可见性：依赖批次 2 的查询在批次 2 导入前后的召回
    const late = queries.filter((q) => q.needs_batch === 2);
    const before = await evaluate(kb, late, embedder, signature, "hybrid");
    // 模型加载后的第一次查询（首次请求），与后续预热后的查询分开报告
    result.firstQuery = before.rows[0]?.timing || null;

    // 5) 批次 2 导入
    const b2 = corpus.filter((d) => d.batch === 2);
    result.ingest2 = await ingestChunks(kb, toChunks(b2), { ...opts, label: "batch-2" });
    log("批次2", pick(result.ingest2));
    const afterLate = await evaluate(kb, late, embedder, signature, "hybrid");
    result.incremental = { queries: late.length, recall10Before: before.recall10, recall10After: afterLate.recall10 };
    log("增量", result.incremental);

    // 6) 检索质量（全部查询 × 各模式）；查询向量一次算好后复用
    const { vectors } = await embedder.embed(queries.map((q) => (cfg.instruction ? BGE_QUERY_INSTRUCTION : "") + q.text), { batchSize: cfg.batchSize });
    queries.forEach((q, i) => qvec.set(q.id, vectors[i]));
    result.quality = {};
    for (const mode of cfg.modes) {
      const e = await evaluate(kb, queries, embedder, signature, mode);
      result.quality[mode] = { n: e.n, recall5: e.recall5, recall10: e.recall10, ndcg10: e.ndcg10, mrr10: e.mrr10, perQuery: e.rows.map(({ timing, ...r }) => r) };
      log(`质量 ${mode}`, { recall10: e.recall10.toFixed(4), ndcg10: e.ndcg10.toFixed(4) });
    }

    if (cfg.grid) {
      result.hybridGrid = [];
      for (const rankConstant of [5, 10, 20, 30, 60]) {
        for (const window of [10, 20, 40, 100]) {
          const e = await evaluate(kb, queries, embedder, signature, "hybrid", { rankConstant, window });
          // 保存逐查询结果，供 scripts/analyze.py 做交叉验证（避免在测试查询上调参后直接报告同一批查询的分数）
          result.hybridGrid.push({ rankConstant, window, recall10: e.recall10, ndcg10: e.ndcg10, mrr10: e.mrr10, perQuery: e.rows.map(({ timing, ...r }) => r) });
          log(`RRF k=${rankConstant} window=${window}`, { recall10: e.recall10.toFixed(4), ndcg10: e.ndcg10.toFixed(4) });
        }
      }
    }

    // 7) 查询延迟（混合检索）：首次查询单列；其后 runs 轮 × 全部查询，报告中位数与 p95
    const lat = [];
    for (let r = 0; r < cfg.runs; r++) {
      for (const q of queries) {
        const x = await searchKb(kb, q.text, { embedder, signature, mode: "hybrid", k: cfg.k, instruction: cfg.instruction, window: cfg.window, rankConstant: cfg.rankConstant });
        lat.push(x.timing);
      }
    }
    result.latency = {
      samples: lat.length,
      queryEmbedMs: summarize(lat.map((x) => x.embedMs)),
      dbMs: summarize(lat.map((x) => x.dbMs)),
      transportMs: summarize(lat.map((x) => x.transportMs)),
      e2eMs: summarize(lat.map((x) => x.e2eMs)),
      raw: lat,
    };
    log("延迟 e2e", result.latency.e2eMs, "db", result.latency.dbMs, "embed", result.latency.queryEmbedMs);

    if (!cfg.keep) await api.deleteKb(kb);
    result.env.finishedAt = new Date().toISOString();
    result.saved = (await api.saveBench(result)).saved;
    log("已保存", result.saved);
    window.__bench = { done: true, ok: true, result: { ...result, latency: { ...result.latency, raw: undefined } } };
  } catch (e) {
    console.error(e);
    log("失败：" + e.message);
    window.__bench = { done: true, ok: false, error: e.message, stack: e.stack };
  }
}

function pick(t) {
  return { chunks: t.chunks, embedMs: Math.round(t.embedMs), chunksPerSec: +t.chunksPerSec.toFixed(1), serverWriteMs: Math.round(t.serverWriteMs), refreshMs: Math.round(t.refreshMs), visibleMs: Math.round(t.visibleMs), totalMs: Math.round(t.totalMs), requests: t.requests };
}

document.getElementById("start").onclick = run;
if (cfg.auto) run();

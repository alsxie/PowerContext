// 作者：FDE WorkMate
// 导入与检索流水线（应用页与基准页共用同一份实现，保证实测即应用行为）。
import { api } from "./api.js";
import { BGE_QUERY_INSTRUCTION } from "./embedder.js";

/** 文本切块：按段落/句末标点切分，超长段按 maxChars 硬切并保留 overlap 重叠。 */
export function chunkText(text, { maxChars = 300, overlap = 40 } = {}) {
  const clean = String(text || "").replace(/\r\n?/g, "\n").trim();
  if (!clean) return [];
  const sentences = clean.split(/(?<=[。！？!?；;\n])/u).map((s) => s.trim()).filter(Boolean);
  const chunks = [];
  let buf = "";
  const flush = () => {
    if (buf.trim()) chunks.push(buf.trim());
    buf = "";
  };
  for (const s of sentences) {
    if (s.length > maxChars) {
      flush();
      for (let i = 0; i < s.length; i += maxChars - overlap) chunks.push(s.slice(i, i + maxChars));
      continue;
    }
    if (buf.length + s.length > maxChars) {
      const tail = buf.slice(-overlap);
      flush();
      buf = tail;
    }
    buf += s;
  }
  flush();
  return chunks;
}

async function sha1(text) {
  const d = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(text));
  return [...new Uint8Array(d)].slice(0, 6).map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** 文档 → chunk。docId 默认由标题 + 内容摘要生成：内容不变则 id 不变，重复导入幂等。 */
export async function docsToChunks(docs, opts) {
  const out = [];
  for (const d of docs) {
    const docId = d.id || `${(d.title || "doc").replace(/[^\p{L}\p{N}_.-]+/gu, "_").slice(0, 40)}-${await sha1(d.text)}`;
    const parts = d.chunk === false ? [d.text] : chunkText(d.text, opts);
    parts.forEach((text, i) => out.push({ id: `${docId}#${i}`, docId, chunk: i, title: d.title || docId, text }));
  }
  return out;
}

const t = () => performance.now();

/**
 * 导入一批 chunk：本地向量化 → 分组写入 seekdb → 提交批次（refresh + 可见性校验）。
 * options:
 *   batchSize       推理批大小
 *   sendSize        每个写入请求的 chunk 数
 *   pipelined       推理与上一组写入并行（最多 1 个写请求在途）
 *   commitPerSend   每个写请求单独成批并立即刷新（优化前基线：频繁刷新）
 *   writeMode       bulk（批量 DELETE+INSERT）| upsert（SDK 逐条 upsert，基线）
 */
export async function ingestChunks(kb, chunks, { embedder, signature, label, batchSize = 32, sendSize = 256, pipelined = true, commitPerSend = false, sortByLength = true, writeMode = "bulk", onProgress = () => {} }) {
  const T0 = t();
  const timing = { embedMs: 0, writeWaitMs: 0, serverWriteMs: 0, refreshMs: 0, visibleMs: 0, commitMs: 0, requests: 0, batches: [] };
  let batchId = null;
  let inflight = null;
  let done = 0;

  const commit = async (id, probes) => {
    const c0 = t();
    const r = await api.commitBatch(kb, id, { probes });
    timing.commitMs += t() - c0;
    timing.refreshMs += r.refreshMs || 0;
    timing.visibleMs += r.visibleMs || 0;
    timing.batches.push({ batchId: id, status: r.status, refreshMs: r.refreshMs, visibleMs: r.visibleMs, rerefresh: r.probes?.rerefresh });
    if (r.status !== "refreshed") throw new Error(`批次 ${id} 刷新后仍不可见：${JSON.stringify(r.probes)}`);
    return r;
  };
  const probeOf = (items) => {
    const step = Math.max(1, Math.floor(items.length / 4));
    return items.filter((_, i) => i % step === 0).slice(0, 4).map((x) => ({ id: x.id, embedding: x.embedding }));
  };
  const send = async (items) => {
    const id = commitPerSend || batchId === null ? (await api.beginBatch(kb, { label, signature })).batchId : batchId;
    if (!commitPerSend) batchId = id;
    const r = await api.writeChunks(kb, id, { items, signature, writeMode });
    timing.serverWriteMs += r.writeMs;
    timing.requests++;
    if (commitPerSend) await commit(id, probeOf(items));
  };

  const allProbes = [];
  for (let i = 0; i < chunks.length; i += sendSize) {
    const group = chunks.slice(i, i + sendSize);
    const e0 = t();
    const { vectors } = await embedder.embed(group.map((c) => c.text), { batchSize, sortByLength });
    timing.embedMs += t() - e0;
    const items = group.map((c, j) => ({ ...c, embedding: vectors[j] }));
    allProbes.push(...probeOf(items));
    const w0 = t();
    if (inflight) await inflight;
    timing.writeWaitMs += t() - w0;
    inflight = send(items);
    if (!pipelined) {
      const w1 = t();
      await inflight;
      timing.writeWaitMs += t() - w1;
      inflight = null;
    }
    done += group.length;
    onProgress({ phase: "embed", done, total: chunks.length, elapsedMs: t() - T0 });
  }
  if (inflight) {
    const w0 = t();
    await inflight;
    timing.writeWaitMs += t() - w0;
  }
  if (!commitPerSend && batchId !== null) {
    onProgress({ phase: "refresh", done, total: chunks.length, elapsedMs: t() - T0 });
    const step = Math.max(1, Math.floor(allProbes.length / 8));
    await commit(batchId, allProbes.filter((_, i) => i % step === 0).slice(0, 8));
  }
  timing.totalMs = t() - T0;
  timing.chunks = chunks.length;
  timing.chunksPerSec = chunks.length / (timing.embedMs / 1000);
  onProgress({ phase: "done", done, total: chunks.length, elapsedMs: timing.totalMs });
  return timing;
}

/** 检索：本地生成查询向量 → seekdb 混合检索。返回命中与分阶段耗时（端到端 = 提交查询到结果可展示）。 */
export async function searchKb(kb, query, { embedder, signature, mode = "hybrid", k = 10, instruction = true, explain = false, embedding: given, window, rankConstant }) {
  const T0 = t();
  let embedding = given;
  let embedMs = 0;
  if (mode !== "fulltext" && !embedding) {  // vector / hybrid / exact 都需要查询向量
    const e0 = t();
    const r = await embedder.embed([instruction ? BGE_QUERY_INSTRUCTION + query : query], { batchSize: 1 });
    embedMs = t() - e0;
    embedding = r.vectors[0];
  }
  const s0 = t();
  const res = await api.search(kb, { embedding, text: query, k, mode, signature, explain, window, rankConstant });
  const httpMs = t() - s0;
  return { ...res, timing: { embedMs, dbMs: res.dbMs, httpMs, transportMs: httpMs - res.dbMs, e2eMs: t() - T0 } };
}

// 作者：FDE WorkMate
// 应用页：模型加载/后端提示、知识库与会话管理、文档导入、混合检索。
import { api, dtypeOfSignature, signatureOf } from "./api.js";
import { detectWebGPU, Embedder } from "./embedder.js";
import { docsToChunks, ingestChunks, searchKb } from "./pipeline.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmt = (x, d = 1) => (Number.isFinite(x) ? x.toFixed(d) : "-");

const params = new URLSearchParams(location.search);
const sessionId = localStorage.getItem("edgekb.session") || crypto.randomUUID();
localStorage.setItem("edgekb.session", sessionId);

const state = { manifest: null, model: null, kb: null, kbs: [], busy: false };
const embedder = new Embedder();

embedder.onProgress((m) => {
  if (m.type === "load-progress" && m.total) $("load-bar").style.width = `${(100 * m.loaded) / m.total}%`;
});

function setBusy(b) {
  state.busy = b;
  document.querySelectorAll("button").forEach((x) => (x.disabled = b));
}

async function guard(fn, statusEl) {
  setBusy(true);
  try {
    return await fn();
  } catch (e) {
    console.error(e);
    if (statusEl) statusEl.innerHTML = `<span class="err">${esc(e.message)}</span>`;
    else alert(e.message);
  } finally {
    setBusy(false);
  }
}

function renderBackend() {
  const m = state.model;
  const badge = $("backend-badge");
  if (!m) {
    badge.textContent = "未加载";
    badge.className = "badge";
    return;
  }
  badge.textContent = m.device === "webgpu" ? `当前后端：WebGPU · ${m.dtype}` : `当前后端：WASM（CPU）· ${m.dtype}`;
  badge.className = `badge ${m.device}`;
  const gpu = m.webgpu?.ok ? `GPU：${[m.webgpu.adapter?.vendor, m.webgpu.adapter?.architecture].filter(Boolean).join(" ") || "可用"}${m.webgpu.shaderF16 ? "，支持 shader-f16" : ""}` : "";
  $("backend-detail").textContent = m.fallbackReason ? `已自动降级到 WASM：${m.fallbackReason}` : gpu;
}

async function loadModel(dtype = $("dtype").value) {
  $("dtype").value = dtype;
  $("model-info").textContent = `加载 ${state.manifest.model_id}（${dtype}）…`;
  $("load-bar").style.width = "0";
  state.model = await embedder.load({ modelId: state.manifest.model_id, dtype, file: state.manifest.variants[dtype]?.file, preference: $("backend-pref").value });
  $("load-bar").style.width = "100%";
  const v = state.manifest.variants[dtype];
  $("model-info").textContent =
    `${state.manifest.model_id} @ ${state.manifest.revision.slice(0, 8)} · ${dtype} ${v.mb} MB（压缩比 ${v.compression_ratio}×）` +
    ` · 加载 ${fmt(state.model.loadMs, 0)} ms（取文件 ${fmt(state.model.fetchMs, 0)} / 建会话 ${fmt(state.model.sessionMs, 0)}）· 预热 ${fmt(state.model.warmupMs, 0)} ms`;
  renderBackend();
}

/** 知识库与模型精度绑定：切换到某个知识库时，自动加载与之签名一致的模型。 */
async function ensureModelForKb() {
  if (!state.kb) return;
  const need = dtypeOfSignature(state.kb.signature);
  if (!state.model || state.model.dtype !== need || signatureOf(state.manifest, need) !== state.kb.signature) {
    if (!state.manifest.variants[need]) throw new Error(`知识库使用的精度 ${need} 在本地模型中不存在`);
    await loadModel(need);
  }
}

async function refreshKbs(select) {
  const { kbs } = await api.listKbs();
  state.kbs = kbs;
  const sel = $("kb-select");
  sel.innerHTML = kbs.length ? kbs.map((k) => `<option value="${esc(k.name)}">${esc(k.name)}（${esc(dtypeOfSignature(k.signature))}，${k.chunk_count} 段）</option>`).join("") : `<option value="">（无，请先新建）</option>`;
  const name = select ?? state.kb?.name;
  if (name && kbs.some((k) => k.name === name)) sel.value = name;
  await selectKb(sel.value || null, { persist: false });
}

async function selectKb(name, { persist = true } = {}) {
  if (!name) {
    state.kb = null;
    $("kb-info").textContent = "尚未选择知识库";
    $("batch-table").tBodies[0].innerHTML = "";
  } else {
    const { kb, batches } = await api.getKb(name);
    state.kb = kb;
    $("kb-info").textContent = `${kb.name}：${kb.doc_count} 篇文档 / ${kb.chunk_count} 段 · 已刷新至批次 ${kb.refreshed_batch}/${kb.last_batch} · 全文分词 ${kb.analyzer} · 签名 ${kb.signature}`;
    $("batch-table").tBodies[0].innerHTML = batches
      .map((b) => `<tr><td>${b.batch_id}</td><td>${esc(b.label)}</td><td>${esc(b.status)}</td><td>${b.chunks}</td><td>${fmt(b.write_ms)}</td><td>${fmt(b.refresh_ms)}</td><td>${fmt(b.visible_ms)}</td></tr>`)
      .join("");
  }
  if (persist) await api.setSession(sessionId, { currentKb: state.kb?.name || null, state: { mode: $("mode").value, k: Number($("k").value) } });
}

async function importDocs(docs, label) {
  if (!state.kb) throw new Error("请先新建或选择知识库");
  await ensureModelForKb();
  const status = $("import-status");
  const chunks = await docsToChunks(docs);
  status.textContent = `共 ${docs.length} 篇 → ${chunks.length} 段，本地向量化中…`;
  const timing = await ingestChunks(state.kb.name, chunks, {
    embedder,
    signature: state.kb.signature,
    label,
    batchSize: Number($("batch").value) || 32,
    onProgress: (p) => {
      $("import-bar").style.width = `${(100 * p.done) / p.total}%`;
      status.textContent = p.phase === "refresh" ? `写入完成，正在刷新 seekdb 索引并校验可见性…` : `向量化并写入 ${p.done}/${p.total} 段（${fmt(p.elapsedMs / 1000)} s）`;
    },
  });
  status.textContent =
    `完成：${chunks.length} 段 · 向量化 ${fmt(timing.embedMs, 0)} ms（${fmt(timing.chunksPerSec)} 段/s，${state.model.device}）` +
    ` · seekdb 写入 ${fmt(timing.serverWriteMs, 0)} ms · 刷新 ${fmt(timing.refreshMs, 0)} ms + 可见校验 ${fmt(timing.visibleMs, 0)} ms · 总计 ${fmt(timing.totalMs / 1000, 2)} s`;
  await refreshKbs(state.kb.name);
}

async function readFiles(files) {
  const docs = [];
  for (const f of files) {
    const text = await f.text();
    if (f.name.endsWith(".jsonl")) {
      text.split("\n").filter(Boolean).forEach((line, i) => {
        const o = JSON.parse(line);
        docs.push({ id: o.id, title: o.title || `${f.name}#${i}`, text: o.text });
      });
    } else docs.push({ title: f.name, text });
  }
  return docs;
}

async function demoDocs(batch) {
  const text = await (await fetch("/data/mmarco-zh-mini/corpus.jsonl")).text();
  return text.split("\n").filter(Boolean).map((l) => JSON.parse(l)).filter((d) => d.batch === batch).map((d) => ({ id: d.id, title: d.id, text: d.text, chunk: false }));
}

function highlight(text, query) {
  const grams = new Set();
  const q = query.replace(/\s+/g, "");
  for (let i = 0; i < q.length - 1; i++) grams.add(q.slice(i, i + 2));
  if (!grams.size) return esc(text);
  let out = "";
  const s = String(text);
  for (let i = 0; i < s.length; ) {
    if (i < s.length - 1 && grams.has(s.slice(i, i + 2))) {
      let j = i + 2;
      while (j < s.length && grams.has(s.slice(j - 1, j + 1))) j++;
      out += `<mark>${esc(s.slice(i, j))}</mark>`;
      i = j;
    } else out += esc(s[i++]);
  }
  return out;
}

async function doSearch(e) {
  e?.preventDefault();
  if (!state.kb) throw new Error("请先选择知识库");
  await ensureModelForKb();
  const q = $("q").value.trim();
  if (!q) return;
  const r = await searchKb(state.kb.name, q, { embedder, signature: state.kb.signature, mode: $("mode").value, k: Number($("k").value) || 10, explain: $("explain").checked });
  const t = r.timing;
  $("search-timing").textContent = `${r.mode} · ${r.hits.length} 条 · 查询向量化 ${fmt(t.embedMs)} ms（${state.model.device}）· seekdb 检索 ${fmt(t.dbMs)} ms · 传输 ${fmt(t.transportMs)} ms · 端到端 ${fmt(t.e2eMs)} ms`;
  $("results").innerHTML = r.hits
    .map((h) => `<li><div class="meta">${esc(h.id)} · ${esc(h.meta?.title)} · 批次 ${esc(h.meta?.batch)} · score ${fmt(h.score, 4)}</div><div>${highlight(h.text, q)}</div></li>`)
    .join("") || "<li>无结果</li>";
  const sql = $("sql");
  sql.hidden = !r.sql;
  if (r.sql) sql.textContent = `-- search_parm\n${JSON.stringify(r.sql.searchParm)}\n-- DBMS_HYBRID_SEARCH.GET_SQL 生成\n${r.sql.sql}`;
  await api.setSession(sessionId, { currentKb: state.kb.name, state: { mode: $("mode").value, k: Number($("k").value), lastQuery: q } });
}

function bind() {
  $("load").onclick = () => guard(() => loadModel(), $("model-info"));
  $("kb-select").onchange = (e) => guard(async () => { await selectKb(e.target.value); await ensureModelForKb(); }, $("kb-info"));
  $("kb-create").onclick = () =>
    guard(async () => {
      const name = $("kb-name").value.trim();
      const dtype = $("dtype").value;
      await api.createKb({ name, dimension: state.manifest.dimension, signature: signatureOf(state.manifest, dtype) });
      $("kb-name").value = "";
      await refreshKbs(name);
      await selectKb(name);
    }, $("kb-info"));
  $("kb-delete").onclick = () =>
    guard(async () => {
      if (!state.kb || !confirm(`删除知识库 ${state.kb.name}？此操作不可恢复`)) return;
      await api.deleteKb(state.kb.name);
      state.kb = null;
      await refreshKbs();
    }, $("kb-info"));
  $("import-files").onclick = () => guard(async () => importDocs(await readFiles($("files").files), "files"), $("import-status"));
  $("import-paste").onclick = () =>
    guard(async () => {
      const docs = $("paste").value.split(/\n\s*\n/).map((t) => t.trim()).filter(Boolean).map((text, i) => ({ title: `粘贴-${i + 1}`, text }));
      await importDocs(docs, "paste");
    }, $("import-status"));
  $("import-demo-1").onclick = () => guard(async () => importDocs(await demoDocs(1), "demo-batch-1"), $("import-status"));
  $("import-demo-2").onclick = () => guard(async () => importDocs(await demoDocs(2), "demo-batch-2"), $("import-status"));
  $("search-form").onsubmit = (e) => guard(() => doSearch(e), $("search-timing"));
}

async function init() {
  bind();
  if (params.get("backend")) $("backend-pref").value = params.get("backend");
  await guard(async () => {
    const [health, manifest] = await Promise.all([api.health(), api.models()]);
    state.manifest = manifest;
    $("kb-info").textContent = `seekdb ${health.seekdb}`;
    const session = await api.getSession(sessionId);
    if (session.state?.mode) $("mode").value = session.state.mode;
    if (session.state?.k) $("k").value = session.state.k;
    if (session.state?.lastQuery) $("q").value = session.state.lastQuery;
    await refreshKbs(session.currentKb);
    // 恢复会话：按当前知识库的签名加载模型；没有知识库时按默认精度加载
    if (state.kb) await ensureModelForKb();
    else {
      // 没有知识库时按设备选默认精度：WebGPU 用 FP16，WASM 用 INT8 动态量化（两者各自实测最快）
      const gpu = $("backend-pref").value === "wasm" ? { ok: false } : await detectWebGPU();
      await loadModel(params.get("dtype") || (gpu.ok ? "fp16" : "int8"));
    }
  }, $("model-info"));
}

init();

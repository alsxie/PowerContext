// 作者：FDE WorkMate
// 知识库数据层：全部存储与检索都在 seekdb（seekdb-js 嵌入式模式）中完成。
//
// - 每个知识库 = 一个 seekdb collection（VECTOR + HNSW 向量索引 + FULLTEXT 全文索引）
// - 知识库/导入批次/会话状态 = seekdb 中的三张普通表（kb_registry / kb_batches / kb_sessions）
// - 混合检索 = collection.hybridSearch → DBMS_HYBRID_SEARCH，在数据库内做全文 + 向量召回与 RRF 融合
// - 向量索引为异步构建（SYNC_MODE=ASYNC），写入后调用 refresh_index 并做可见性校验，才把批次标记为可检索
import { SeekdbAdminClient, SeekdbClient } from "seekdb";
import fs from "node:fs";

const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,47}$/;

export class HttpError extends Error {
  constructor(status, message, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

/** 同一知识库的写操作串行执行，避免并发导入与刷新交错。 */
class KeyedQueue {
  #tails = new Map();
  run(key, fn) {
    const prev = this.#tails.get(key) || Promise.resolve();
    const next = prev.then(fn, fn);
    const tail = next.catch(() => {});
    this.#tails.set(key, tail);
    tail.then(() => {
      if (this.#tails.get(key) === tail) this.#tails.delete(key);
    });
    return next;
  }
}

const now = () => performance.now();
const ms = (t0) => Math.round((performance.now() - t0) * 100) / 100;
const sleep = (t) => new Promise((r) => setTimeout(r, t));

/**
 * query_string 语法里的运算符字符会被数据库解析（+ - " ( ) * 等），用户输入一律当作普通词处理。
 * 保留字母、数字、中日韩文字，其余替换为空格。
 */
export function toQueryString(text) {
  return String(text || "")
    .normalize("NFKC")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .replace(/\b(AND|OR|NOT)\b/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 256);
}

/**
 * seekdb-js 1.4.0 嵌入式绑定会把以 "[" 开头的字符串参数当作向量字面量直接拼进 SQL
 * （实测 execute("select ?", ["[abc]"]) 报 Unknown column，"[abc] x" 报 Parse error）。
 * 这既会让以 "[" 开头的正文写入失败，也是注入面。所有自由文本参数统一经过此函数：
 * 把首字符 "[" 换成全角 "［"（NFKC 等价，显示与检索语义不变）。
 */
export function safeText(s) {
  const v = String(s ?? "");
  return v.startsWith("[") ? "\uFF3B" + v.slice(1) : v;
}

const ID_RE = /^[^\s\[][^\s]{0,255}$/u;

export class KnowledgeBaseService {
  constructor(config) {
    this.config = config;
    this.client = null;
    this.queue = new KeyedQueue();
    this.collections = new Map();
  }

  async open() {
    fs.mkdirSync(this.config.dbPath, { recursive: true });
    const admin = new SeekdbAdminClient({ path: this.config.dbPath });
    const dbs = await admin.listDatabases();
    if (!dbs.some((d) => d.name === this.config.database)) {
      await admin.createDatabase(this.config.database);
    }
    await admin.close();
    this.client = new SeekdbClient({ path: this.config.dbPath, database: this.config.database });
    await this.#ensureSchema();
    const [v] = await this.client.execute("SELECT VERSION() AS version");
    this.version = v?.version;
    this.readiness = await this.#vectorReadiness();
    return this;
  }

  /**
   * 向量索引就绪自检：用临时 collection 走一遍 写入 → refresh → 近似 KNN，通过后才对外服务。
   * 实测 seekdb 1.4.0 嵌入式：新建实例中第一个带 HNSW 的 collection 偶发始终无法被近似检索
   * （精确检索正常），之后创建的 collection 正常。自检把这个启动期问题挡在业务数据之外。
   */
  async #vectorReadiness(maxTries = 5) {
    const t0 = now();
    const vec = (i) => [0, 1, 2, 3].map((j) => (j === i ? 1 : 0.01));
    for (let i = 1; i <= maxTries; i++) {
      const name = `_kb_selfcheck_${Date.now().toString(36)}_${i}`;
      let ok = false;
      try {
        const col = await this.client.createCollection({
          name,
          configuration: { hnsw: { dimension: 4, distance: "cosine" } },
          embeddingFunction: null,
        });
        await col.upsert({ ids: ["a", "b"], embeddings: [vec(0), vec(1)], documents: ["a", "b"] });
        await col.refresh_index();
        const r = await col.query({ queryEmbeddings: [vec(0)], nResults: 1 });
        ok = r.ids?.[0]?.[0] === "a";
      } catch (e) {
        console.warn(`[edge-kb] 向量索引自检第 ${i} 次异常：${e.message}`);
      } finally {
        await this.client.deleteCollection(name).catch(() => {});
      }
      if (ok) return { ok: true, tries: i, ms: ms(t0) };
    }
    console.warn("[edge-kb] 向量索引自检未通过，新导入批次可能停留在 pending 状态");
    return { ok: false, tries: maxTries, ms: ms(t0) };
  }

  async close() {
    if (this.client) await this.client.close();
    this.client = null;
  }

  async #ensureSchema() {
    const sql = [
      `CREATE TABLE IF NOT EXISTS kb_registry (
        name VARCHAR(48) PRIMARY KEY,
        collection VARCHAR(64) NOT NULL,
        dimension INT NOT NULL,
        signature VARCHAR(255) NOT NULL,
        analyzer VARCHAR(16) NOT NULL,
        chunk_count INT NOT NULL DEFAULT 0,
        doc_count INT NOT NULL DEFAULT 0,
        last_batch BIGINT NOT NULL DEFAULT 0,
        refreshed_batch BIGINT NOT NULL DEFAULT 0,
        created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
        updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
      )`,
      `CREATE TABLE IF NOT EXISTS kb_batches (
        kb VARCHAR(48) NOT NULL,
        batch_id BIGINT NOT NULL,
        label VARCHAR(128),
        status VARCHAR(16) NOT NULL,
        chunks INT NOT NULL DEFAULT 0,
        write_ms DOUBLE,
        refresh_ms DOUBLE,
        visible_ms DOUBLE,
        created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
        PRIMARY KEY (kb, batch_id)
      )`,
      `CREATE TABLE IF NOT EXISTS kb_sessions (
        session_id VARCHAR(64) PRIMARY KEY,
        current_kb VARCHAR(48),
        state JSON,
        updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
      )`,
    ];
    for (const s of sql) await this.client.execute(s);
  }

  // ---------------------------------------------------------------- 知识库
  async listKbs() {
    return (await this.client.execute("SELECT * FROM kb_registry ORDER BY created_at")) || [];
  }

  async getKb(name) {
    const rows = await this.client.execute("SELECT * FROM kb_registry WHERE name = ?", [name]);
    if (!rows?.length) throw new HttpError(404, `知识库不存在：${name}`);
    return rows[0];
  }

  async createKb({ name, dimension, signature, analyzer }) {
    if (!NAME_RE.test(name || "")) throw new HttpError(400, "知识库名只能包含小写字母、数字、- 和 _，最长 48");
    if (!Number.isInteger(dimension) || dimension <= 0 || dimension > 4096) throw new HttpError(400, "dimension 无效");
    if (!signature) throw new HttpError(400, "缺少 embedding 签名（模型@revision:dtype）");
    const fts = analyzer || this.config.analyzer;
    return this.queue.run(name, async () => {
      const exists = await this.client.execute("SELECT * FROM kb_registry WHERE name = ?", [name]);
      if (exists?.length) {
        const kb = exists[0];
        if (kb.signature !== signature || kb.dimension !== dimension) {
          throw new HttpError(409, "同名知识库已用不同的 embedding 签名创建", { kb });
        }
        return { kb, created: false };
      }
      const collection = `kb_${name.replace(/-/g, "_")}`;
      await this.client.createCollection({
        name: collection,
        configuration: { hnsw: { dimension, distance: "cosine" }, fulltextConfig: { analyzer: fts } },
        embeddingFunction: null,
      });
      await this.client.execute(
        "INSERT INTO kb_registry (name, collection, dimension, signature, analyzer) VALUES (?, ?, ?, ?, ?)",
        [name, collection, dimension, signature, fts],
      );
      return { kb: await this.getKb(name), created: true };
    });
  }

  async deleteKb(name) {
    return this.queue.run(name, async () => {
      const kb = await this.getKb(name);
      await this.client.deleteCollection(kb.collection);
      this.collections.delete(name);
      await this.client.execute("DELETE FROM kb_batches WHERE kb = ?", [name]);
      await this.client.execute("DELETE FROM kb_registry WHERE name = ?", [name]);
      await this.client.execute("UPDATE kb_sessions SET current_kb = NULL WHERE current_kb = ?", [name]);
      return { deleted: name };
    });
  }

  async #collection(kb) {
    let col = this.collections.get(kb.name);
    if (!col) {
      col = await this.client.getCollection({ name: kb.collection, embeddingFunction: null });
      this.collections.set(kb.name, col);
    }
    return col;
  }

  #checkSignature(kb, signature) {
    if (signature && signature !== kb.signature) {
      throw new HttpError(409, `embedding 签名不一致：知识库为 ${kb.signature}，请求为 ${signature}`, { kb });
    }
  }

  // ---------------------------------------------------------------- 写入与刷新
  async beginBatch(name, { label, signature } = {}) {
    return this.queue.run(name, async () => {
      const kb = await this.getKb(name);
      this.#checkSignature(kb, signature);
      const batchId = Number(kb.last_batch) + 1;
      await this.client.execute(
        "INSERT INTO kb_batches (kb, batch_id, label, status) VALUES (?, ?, ?, 'writing')",
        [name, batchId, label ? safeText(label).slice(0, 128) : null],
      );
      await this.client.execute(
        "UPDATE kb_registry SET last_batch = ?, updated_at = CURRENT_TIMESTAMP(3) WHERE name = ?",
        [batchId, name],
      );
      return { batchId };
    });
  }

  /**
   * 写入一组 chunk：id 由前端按 docId#chunkIdx 生成，upsert 保证重复导入幂等。
   * items: [{ id, docId, chunk, title, text, embedding }]
   */
  async writeChunks(name, batchId, items, { signature, writeMode = "bulk" } = {}) {
    if (!Array.isArray(items) || items.length === 0) throw new HttpError(400, "items 为空");
    return this.queue.run(name, async () => {
      const kb = await this.getKb(name);
      this.#checkSignature(kb, signature);
      const batch = await this.#batch(name, batchId);
      if (batch.status !== "writing") throw new HttpError(409, `批次 ${batchId} 已结束（${batch.status}）`);
      for (const it of items) {
        if (!ID_RE.test(String(it.id || "")) || (it.docId != null && !ID_RE.test(String(it.docId))) || typeof it.text !== "string" || !Array.isArray(it.embedding) || it.embedding.length !== kb.dimension) {
          throw new HttpError(400, `chunk 字段不完整（id 不能含空白或以 [ 开头）或维度不是 ${kb.dimension}：${it.id}`);
        }
      }
      const col = await this.#collection(kb);
      const t0 = now();
      const step = Math.max(1, this.config.writeBatch);
      for (let i = 0; i < items.length; i += step) {
        const part = items.slice(i, i + step);
        const data = {
          ids: part.map((x) => x.id),
          embeddings: part.map((x) => x.embedding),
          documents: part.map((x) => safeText(x.text)),
          metadatas: part.map((x) => ({
            doc_id: String(x.docId ?? x.id),
            chunk: Number(x.chunk ?? 0),
            title: String(x.title ?? ""),
            batch: batchId,
          })),
        };
        if (writeMode === "upsert") {
          // SDK upsert：逐条 get + insert/update（2 次往返/行），作为“优化前”基线保留
          await col.upsert(data);
        } else {
          // 批量写：一次 get 找出已存在的 id → 一条 DELETE → 一条多行 INSERT，语义与 upsert 相同（幂等）
          const existing = await col.get({ ids: data.ids, include: ["metadatas"] });
          if (existing.ids?.length) await col.delete({ ids: existing.ids });
          await col.add(data);
        }
      }
      const writeMs = ms(t0);
      await this.client.execute(
        "UPDATE kb_batches SET chunks = chunks + ?, write_ms = IFNULL(write_ms, 0) + ? WHERE kb = ? AND batch_id = ?",
        [items.length, writeMs, name, batchId],
      );
      return { written: items.length, writeMs, writeMode };
    });
  }

  /**
   * 提交批次：refresh_index 让异步向量索引吸收新写入，再抽样做可见性校验
   * （用新 chunk 自身向量做 KNN，必须召回自己；全文侧用 MATCH 校验），全部可见后才标记 refreshed。
   * probes: [{ id, embedding }] 由前端从本批次中抽样传入。
   */
  async commitBatch(name, batchId, { probes = [], refresh = true } = {}) {
    return this.queue.run(name, async () => {
      const kb = await this.getKb(name);
      const batch = await this.#batch(name, batchId);
      if (batch.status === "refreshed") return { batchId, status: "refreshed", already: true };
      const col = await this.#collection(kb);
      let refreshMs = 0;
      if (refresh) {
        const t = now();
        await col.refresh_index();
        refreshMs = ms(t);
      }
      const t1 = now();
      const visible = await this.#waitVisible(col, kb, probes, refresh);
      const visibleMs = ms(t1);
      const status = visible.ok ? "refreshed" : "pending";
      await this.client.execute(
        "UPDATE kb_batches SET status = ?, refresh_ms = ?, visible_ms = ? WHERE kb = ? AND batch_id = ?",
        [status, refreshMs, visibleMs, name, batchId],
      );
      await this.#recount(kb, col, visible.ok ? batchId : null);
      return { batchId, status, refreshMs, visibleMs, probes: visible };
    });
  }

  /**
   * 可见性判定：用探针 chunk 自身的向量做近似 KNN（top-10），能召回自己即视为已进入向量索引。
   * - 未刷新时表现为“几乎全部探针都召回不到”，持续轮询（间隔重发 refresh）直到 ≥75% 探针可见或超时；
   * - 剩余个别探针若仍召回不到，再用精确检索（approximate=false）确认数据已写入——这属于 HNSW
   *   近似检索本身的召回损失，不是刷新失败，记为 annMiss 单独上报。
   */
  async #waitVisible(col, kb, probes, refresh) {
    const deadline = Date.now() + this.config.refreshTimeoutMs;
    const sample = probes.slice(0, 8);
    const need = Math.ceil(sample.length * 0.75);
    const annHit = async (p, approximate = true) => {
      const r = await col.query({ queryEmbeddings: [p.embedding], nResults: 10, include: ["metadatas"], approximate });
      return (r.ids?.[0] || []).includes(p.id);
    };
    let missing = sample.map((p) => p.id);
    let rounds = 0;
    let rerefresh = 0;
    let lastRefresh = Date.now();
    while (true) {
      rounds++;
      const still = [];
      for (const p of sample.filter((x) => missing.includes(x.id))) if (!(await annHit(p))) still.push(p.id);
      missing = still;
      if (sample.length - missing.length >= need || Date.now() > deadline) break;
      // 实测：新库的第一次 refresh 偶发未生效，仍不可见时间隔重发 refresh
      if (refresh && Date.now() - lastRefresh > 1000) {
        await col.refresh_index();
        rerefresh++;
        lastRefresh = Date.now();
      } else {
        await sleep(100);
      }
    }
    const annMiss = [];
    if (sample.length - missing.length >= need) {
      for (const id of missing) {
        const p = sample.find((x) => x.id === id);
        if (await annHit(p, false)) annMiss.push(id);
      }
      missing = missing.filter((id) => !annMiss.includes(id));
    }
    return { ok: missing.length === 0, checked: sample.length, missing, annMiss, rounds, rerefresh };
  }

  async #recount(kb, col, refreshedBatch) {
    const chunks = await col.count();
    const table = await this.#tableName(kb);
    const [d] = await this.client.execute(
      `SELECT COUNT(DISTINCT JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.doc_id'))) AS n FROM \`${table}\``,
    );
    await this.client.execute(
      `UPDATE kb_registry SET chunk_count = ?, doc_count = ?, updated_at = CURRENT_TIMESTAMP(3)
       ${refreshedBatch ? ", refreshed_batch = GREATEST(refreshed_batch, ?)" : ""} WHERE name = ?`,
      refreshedBatch ? [chunks, Number(d?.n || 0), refreshedBatch, kb.name] : [chunks, Number(d?.n || 0), kb.name],
    );
  }

  async #tableName(kb) {
    const rows = await this.client.execute(
      "SELECT collection_id FROM sdk_collections WHERE collection_name = ?",
      [kb.collection],
    );
    const id = rows?.[0]?.collection_id;
    if (!id) throw new HttpError(500, `找不到 collection 的物理表：${kb.collection}`);
    return `c$v2$${String(id).replace(/-/g, "")}`;
  }

  async #batch(name, batchId) {
    const rows = await this.client.execute("SELECT * FROM kb_batches WHERE kb = ? AND batch_id = ?", [name, batchId]);
    if (!rows?.length) throw new HttpError(404, `批次不存在：${name}#${batchId}`);
    return rows[0];
  }

  async listBatches(name) {
    await this.getKb(name);
    return (await this.client.execute("SELECT * FROM kb_batches WHERE kb = ? ORDER BY batch_id", [name])) || [];
  }

  async deleteDocument(name, docId) {
    if (!ID_RE.test(String(docId || ""))) throw new HttpError(400, "docId 无效");
    return this.queue.run(name, async () => {
      const kb = await this.getKb(name);
      const col = await this.#collection(kb);
      await col.delete({ where: { doc_id: docId } });
      await col.refresh_index();
      await this.#recount(kb, col, null);
      return { deleted: docId };
    });
  }

  // ---------------------------------------------------------------- 检索
  /**
   * mode=hybrid：DBMS_HYBRID_SEARCH（全文 query_string + KNN + RRF）
   * mode=vector / fulltext：同一接口只给一路，用于消融对比
   */
  async search(name, { embedding, text, k = 10, mode = "hybrid", signature, window, rankConstant, explain = false }) {
    const kb = await this.getKb(name);
    this.#checkSignature(kb, signature);
    const col = await this.#collection(kb);
    const n = Math.min(Math.max(Number(k) || 10, 1), 100);
    const qs = toQueryString(text);
    // 默认参数来自 bench/tuning 的网格实验：ik 分词下 RRF k=10、召回窗口 = 2×TopK 最优
    window = Math.min(Math.max(Number(window) || n * this.config.rrfWindowFactor, n), 200);
    rankConstant = Number(rankConstant) || this.config.rrfK;
    if (mode === "exact") return this.#exactSearch(kb, col, name, embedding, n);
    const opts = { nResults: n, include: ["documents", "metadatas", "distances"] };
    if (mode !== "vector" && qs) opts.query = { whereDocument: { $contains: qs }, nResults: window };
    if (mode !== "fulltext") {
      if (!Array.isArray(embedding) || embedding.length !== kb.dimension) throw new HttpError(400, `查询向量维度应为 ${kb.dimension}`);
      opts.knn = { queryEmbeddings: embedding, nResults: window };
    }
    if (!opts.query && !opts.knn) throw new HttpError(400, "查询为空");
    if (opts.query && opts.knn) {
      opts.rank = { rrf: { rankWindowSize: window, rankConstant } };
    }
    const t0 = now();
    const r = await col.hybridSearch(opts);
    const dbMs = ms(t0);
    const hits = (r.ids?.[0] || []).map((id, i) => ({
      id,
      score: r.distances?.[0]?.[i],
      text: r.documents?.[0]?.[i],
      meta: r.metadatas?.[0]?.[i],
    }));
    const out = { kb: name, mode: opts.query && opts.knn ? "hybrid" : opts.knn ? "vector" : "fulltext", queryString: qs, window, rankConstant: opts.rank ? rankConstant : undefined, dbMs, hits };
    if (explain) out.sql = await this.#explain(kb, opts);
    return out;
  }

  /**
   * 精确向量检索（approximate=false，全表余弦距离排序）：仅用于评测，
   * 排除 HNSW 近似误差后单独衡量量化对模型本身检索质量的影响。
   */
  async #exactSearch(kb, col, name, embedding, n) {
    if (!Array.isArray(embedding) || embedding.length !== kb.dimension) throw new HttpError(400, `查询向量维度应为 ${kb.dimension}`);
    const t0 = now();
    const r = await col.query({ queryEmbeddings: [embedding], nResults: n, approximate: false, include: ["documents", "metadatas", "distances"] });
    const dbMs = ms(t0);
    const hits = (r.ids?.[0] || []).map((id, i) => ({ id, score: r.distances?.[0]?.[i], text: r.documents?.[0]?.[i], meta: r.metadatas?.[0]?.[i] }));
    return { kb: name, mode: "exact", dbMs, hits };
  }

  /** 取出 DBMS_HYBRID_SEARCH.GET_SQL 生成的 SQL，证明融合发生在数据库内。 */
  async #explain(kb, opts) {
    const table = await this.#tableName(kb);
    const parm = {};
    if (opts.query) parm.query = { query_string: { fields: ["document"], query: opts.query.whereDocument.$contains } };
    if (opts.knn) parm.knn = { field: "embedding", k: opts.knn.nResults, query_vector: ["..."] };
    if (opts.rank) parm.rank = { rrf: { rank_window_size: opts.rank.rrf.rankWindowSize, rank_constant: opts.rank.rrf.rankConstant } };
    parm.size = opts.nResults;
    const real = JSON.parse(JSON.stringify(parm));
    if (real.knn) real.knn.query_vector = new Array(kb.dimension).fill(0).map((_, i) => (i === 0 ? 1 : 0));
    await this.client.execute("SET @kb_explain_parm = ?", [JSON.stringify(real)]);
    const [row] = await this.client.execute(
      `SELECT DBMS_HYBRID_SEARCH.GET_SQL('${table}', @kb_explain_parm) AS q`,
    );
    return { searchParm: parm, sql: String(row?.q || "").replace(/'\[[0-9., e-]+\]'/g, "'[<query_vector>]'") };
  }

  // ---------------------------------------------------------------- 会话
  async getSession(sessionId) {
    if (!sessionId) throw new HttpError(400, "缺少 sessionId");
    const rows = await this.client.execute("SELECT * FROM kb_sessions WHERE session_id = ?", [sessionId]);
    const s = rows?.[0] || { session_id: sessionId, current_kb: null, state: null };
    if (typeof s.state === "string") s.state = JSON.parse(s.state);
    let kb = null;
    if (s.current_kb) {
      kb = await this.getKb(s.current_kb).catch(() => null);
    }
    return { sessionId, currentKb: kb ? kb.name : null, kb, state: s.state || {} };
  }

  async setSession(sessionId, { currentKb, state }) {
    if (!sessionId || sessionId.length > 64) throw new HttpError(400, "sessionId 无效");
    if (currentKb) await this.getKb(currentKb);
    await this.client.execute(
      `INSERT INTO kb_sessions (session_id, current_kb, state) VALUES (?, ?, ?)
       ON DUPLICATE KEY UPDATE current_kb = VALUES(current_kb), state = VALUES(state), updated_at = CURRENT_TIMESTAMP(3)`,
      [sessionId, currentKb || null, JSON.stringify(state || {})],
    );
    return this.getSession(sessionId);
  }
}

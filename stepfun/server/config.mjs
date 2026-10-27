// 作者：FDE WorkMate
// 运行配置：全部可用环境变量覆盖，默认值面向本机单用户演示。
import path from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export function loadConfig(env = process.env) {
  return {
    host: env.KB_HOST || "127.0.0.1",
    port: Number(env.KB_PORT || 8787),
    // API 允许的 Host 名（防 DNS rebinding）；对外监听时需把实际域名/IP 加进来
    allowedHosts: [...new Set(["127.0.0.1", "localhost", "::1", env.KB_HOST, ...(env.KB_ALLOWED_HOSTS || "").split(",")].filter(Boolean).map((h) => h.trim().toLowerCase()))],
    // seekdb 嵌入式数据目录（seekdb-js 的 path 参数）
    dbPath: path.resolve(ROOT, env.SEEKDB_PATH || ".runtime/seekdb"),
    database: env.SEEKDB_DATABASE || "edge_kb",
    // 全文索引分词器：ik（中文词典分词）/ ngram / ngram2 / space / beng
    analyzer: env.KB_FTS_ANALYZER || "ik",
    // 混合检索 RRF 参数：rank_constant 与召回窗口（= TopK × 系数）
    rrfK: Number(env.KB_RRF_K || 10),
    rrfWindowFactor: Number(env.KB_RRF_WINDOW_FACTOR || 2),
    // 单条 SQL 写入的行数（upsert 批大小）
    writeBatch: Number(env.KB_WRITE_BATCH || 256),
    // 刷新可见性校验的最长等待
    refreshTimeoutMs: Number(env.KB_REFRESH_TIMEOUT_MS || 15000),
    webDir: path.join(ROOT, "web", "dist"),
    modelsDir: path.join(ROOT, "models"),
    ortDir: path.join(ROOT, "node_modules", "onnxruntime-web", "dist"),
    dataDir: path.join(ROOT, "data"),
    benchDir: path.join(ROOT, "bench", "results"),
    maxBodyBytes: Number(env.KB_MAX_BODY || 64 * 1024 * 1024),
  };
}

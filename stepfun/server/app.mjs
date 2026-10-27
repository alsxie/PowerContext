// 作者：FDE WorkMate
// HTTP 层：静态资源（前端、模型文件、ORT wasm、评测数据）+ 知识库 JSON API。
// 服务只做存储/检索，不做任何推理；向量全部由浏览器端计算后上传。
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { HttpError } from "./kb.mjs";

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".jsonl": "application/x-ndjson; charset=utf-8",
  ".wasm": "application/wasm",
  ".onnx": "application/octet-stream",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
  ".md": "text/markdown; charset=utf-8",
};

function send(res, status, body, headers = {}) {
  const data = typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body);
  res.writeHead(status, {
    "content-type": typeof body === "object" && !Buffer.isBuffer(body) ? MIME[".json"] : MIME[".txt"],
    "cache-control": "no-store",
    ...headers,
  });
  res.end(data);
}

async function readJson(req, limit) {
  const chunks = [];
  let size = 0;
  for await (const c of req) {
    size += c.length;
    if (size > limit) throw new HttpError(413, "请求体过大");
    chunks.push(c);
  }
  if (!size) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new HttpError(400, "请求体不是合法 JSON");
  }
}

/** 静态文件：限制在 base 目录内；支持 ETag/304，模型文件可长期缓存。 */
function serveStatic(req, res, base, rel, { immutable = false } = {}) {
  const file = path.resolve(base, "." + path.posix.normalize("/" + decodeURIComponent(rel)));
  if (!file.startsWith(path.resolve(base) + path.sep) && file !== path.resolve(base)) return false;
  let st;
  try {
    st = fs.statSync(file);
  } catch {
    return false;
  }
  if (st.isDirectory()) return serveStatic(req, res, base, path.posix.join(rel, "index.html"), { immutable });
  const etag = `"${st.size.toString(16)}-${Math.floor(st.mtimeMs).toString(16)}"`;
  const headers = {
    "content-type": MIME[path.extname(file)] || "application/octet-stream",
    "content-length": st.size,
    etag,
    "cache-control": immutable ? "public, max-age=31536000, immutable" : "no-cache",
    // WASM 多线程（SharedArrayBuffer）需要跨源隔离
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-embedder-policy": "require-corp",
    "cross-origin-resource-policy": "same-origin",
  };
  if (req.headers["if-none-match"] === etag) {
    res.writeHead(304, headers);
    res.end();
    return true;
  }
  res.writeHead(200, headers);
  if (req.method === "HEAD") res.end();
  else fs.createReadStream(file).pipe(res);
  return true;
}

/**
 * 本地 API 的跨站防护（API 无鉴权，必须防止任意网页借用户浏览器调用 127.0.0.1）：
 * - Host 白名单：防 DNS rebinding（恶意域名解析到 127.0.0.1 后同源访问）；
 * - Origin 必须与本服务同源：拦截跨站 fetch/表单；
 * - 写请求必须是 application/json：简单请求（表单、text/plain）无法伪造，跨站 JSON 请求会触发预检，而本服务不响应 CORS。
 */
export function checkRequest(req, config) {
  const hostHeader = String(req.headers.host || "");
  const hostname = hostHeader.replace(/:\d+$/, "").replace(/^\[|\]$/g, "").toLowerCase();
  if (!config.allowedHosts.includes(hostname)) return `Host 不在白名单：${hostHeader}`;
  const origin = req.headers.origin;
  if (origin && origin !== "null") {
    let o;
    try {
      o = new URL(origin);
    } catch {
      return "Origin 无效";
    }
    if (o.host.toLowerCase() !== hostHeader.toLowerCase()) return `跨站请求被拒绝：${origin}`;
  } else if (origin === "null") return "跨站请求被拒绝：Origin null";
  if (!["GET", "HEAD"].includes(req.method) && req.method !== "DELETE") {
    if (!String(req.headers["content-type"] || "").toLowerCase().startsWith("application/json")) return "写请求必须使用 application/json";
  }
  return null;
}

export function createApp(service, config) {
  const routes = [
    ["GET", /^\/api\/health$/, async () => ({ ok: true, seekdb: service.version, readiness: service.readiness, database: config.database, dbPath: config.dbPath })],
    ["GET", /^\/api\/kbs$/, async () => ({ kbs: await service.listKbs() })],
    ["POST", /^\/api\/kbs$/, async (_m, body) => service.createKb(body)],
    ["GET", /^\/api\/kbs\/([^/]+)$/, async ([, n]) => ({ kb: await service.getKb(n), batches: await service.listBatches(n) })],
    ["DELETE", /^\/api\/kbs\/([^/]+)$/, async ([, n]) => service.deleteKb(n)],
    ["POST", /^\/api\/kbs\/([^/]+)\/batches$/, async ([, n], body) => service.beginBatch(n, body)],
    ["POST", /^\/api\/kbs\/([^/]+)\/batches\/(\d+)\/chunks$/, async ([, n, b], body) =>
      service.writeChunks(n, Number(b), body.items, { signature: body.signature, writeMode: body.writeMode })],
    ["POST", /^\/api\/kbs\/([^/]+)\/batches\/(\d+)\/commit$/, async ([, n, b], body) =>
      service.commitBatch(n, Number(b), body)],
    ["DELETE", /^\/api\/kbs\/([^/]+)\/docs\/(.+)$/, async ([, n, d]) => service.deleteDocument(n, decodeURIComponent(d))],
    ["POST", /^\/api\/kbs\/([^/]+)\/search$/, async ([, n], body) => service.search(n, body)],
    ["GET", /^\/api\/sessions\/([\w-]+)$/, async ([, s]) => service.getSession(s)],
    ["PUT", /^\/api\/sessions\/([\w-]+)$/, async ([, s], body) => service.setSession(s, body)],
    ["POST", /^\/api\/bench\/results$/, async (_m, body) => saveBench(config, body)],
    ["GET", /^\/api\/models$/, async () => readManifest(config)],
  ];

  return http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://localhost");
    const p = url.pathname;
    try {
      if (p.startsWith("/api/")) {
        const denied = checkRequest(req, config);
        if (denied) throw new HttpError(403, denied);
        for (const [method, re, fn] of routes) {
          const m = p.match(re);
          if (m && method === req.method) {
            const body = ["POST", "PUT"].includes(req.method) ? await readJson(req, config.maxBodyBytes) : {};
            return send(res, 200, await fn(m.map((x, i) => (i ? decodeURIComponent(x) : x)), body, url));
          }
        }
        throw new HttpError(404, `未知接口：${req.method} ${p}`);
      }
      if (req.method !== "GET" && req.method !== "HEAD") throw new HttpError(405, "仅支持 GET");
      if (p.startsWith("/models/") && serveStatic(req, res, config.modelsDir, p.slice(8), { immutable: true })) return;
      if (p.startsWith("/ort/") && serveStatic(req, res, config.ortDir, p.slice(5), { immutable: true })) return;
      if (p.startsWith("/data/") && serveStatic(req, res, config.dataDir, p.slice(6))) return;
      if (serveStatic(req, res, config.webDir, p === "/" ? "index.html" : p.slice(1))) return;
      throw new HttpError(404, `未找到：${p}`);
    } catch (e) {
      const status = e instanceof HttpError ? e.status : 500;
      if (status === 500) console.error(e);
      send(res, status, { error: e.message, detail: e.detail });
    }
  });
}

function readManifest(config) {
  const f = path.join(config.modelsDir, "manifest.json");
  if (!fs.existsSync(f)) throw new HttpError(404, "模型未准备：请先运行 scripts/prepare_model.py");
  return JSON.parse(fs.readFileSync(f, "utf8"));
}

function saveBench(config, body) {
  if (!body || typeof body !== "object" || !body.config) throw new HttpError(400, "结果缺少 config");
  fs.mkdirSync(config.benchDir, { recursive: true });
  const safe = String(body.config.label || "run").replace(/[^\w.-]+/g, "_").slice(0, 80);
  const file = path.join(config.benchDir, `${new Date().toISOString().replace(/[:.]/g, "-")}_${safe}.json`);
  fs.writeFileSync(file, JSON.stringify(body, null, 2));
  return { saved: path.relative(path.dirname(config.benchDir), file) };
}

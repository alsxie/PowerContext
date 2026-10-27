// 作者：FDE WorkMate
// 本地服务 API 客户端（同源）。服务端只负责 seekdb 存储与检索。

async function req(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || `${res.status} ${url}`), { status: res.status, detail: data.detail });
  return data;
}

export const api = {
  health: () => req("GET", "/api/health"),
  models: () => req("GET", "/api/models"),
  listKbs: () => req("GET", "/api/kbs"),
  getKb: (name) => req("GET", `/api/kbs/${encodeURIComponent(name)}`),
  createKb: (body) => req("POST", "/api/kbs", body),
  deleteKb: (name) => req("DELETE", `/api/kbs/${encodeURIComponent(name)}`),
  beginBatch: (name, body) => req("POST", `/api/kbs/${encodeURIComponent(name)}/batches`, body),
  writeChunks: (name, batchId, body) => req("POST", `/api/kbs/${encodeURIComponent(name)}/batches/${batchId}/chunks`, body),
  commitBatch: (name, batchId, body) => req("POST", `/api/kbs/${encodeURIComponent(name)}/batches/${batchId}/commit`, body),
  deleteDoc: (name, docId) => req("DELETE", `/api/kbs/${encodeURIComponent(name)}/docs/${encodeURIComponent(docId)}`),
  search: (name, body) => req("POST", `/api/kbs/${encodeURIComponent(name)}/search`, body),
  getSession: (id) => req("GET", `/api/sessions/${id}`),
  setSession: (id, body) => req("PUT", `/api/sessions/${id}`, body),
  saveBench: (body) => req("POST", "/api/bench/results", body),
};

/** 模型签名：同一知识库内文档向量与查询向量必须来自同一模型、同一版本、同一精度。 */
export function signatureOf(manifest, dtype) {
  return `${manifest.model_id}@${manifest.revision.slice(0, 12)}:${dtype}`;
}

export function dtypeOfSignature(sig) {
  return String(sig || "").split(":").pop();
}

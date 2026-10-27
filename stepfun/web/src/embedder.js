// 作者：FDE WorkMate
// 推理后端选择与 Worker 封装。
// 策略：优先 WebGPU（显式 device="webgpu"）；探测失败或会话创建失败时自动降级到 WASM，并给出原因。
// 可用 ?backend=wasm 强制 WASM、?backend=webgpu 只尝试 WebGPU（不降级），用于对比与验证。

/** 探测 WebGPU 能力：navigator.gpu → requestAdapter → requestDevice，并记录适配器信息与 shader-f16 支持。 */
export async function detectWebGPU() {
  if (typeof navigator === "undefined" || !("gpu" in navigator) || !navigator.gpu) {
    return { ok: false, reason: "浏览器未提供 navigator.gpu（不支持 WebGPU 或被禁用）" };
  }
  try {
    const adapter = await navigator.gpu.requestAdapter({ powerPreference: "high-performance" });
    if (!adapter) return { ok: false, reason: "navigator.gpu.requestAdapter() 返回 null（无可用 GPU 适配器）" };
    const device = await adapter.requestDevice();
    const info = adapter.info || {};
    device.destroy?.();
    return {
      ok: true,
      adapter: { vendor: info.vendor, architecture: info.architecture, description: info.description, device: info.device },
      shaderF16: adapter.features.has("shader-f16"),
    };
  } catch (e) {
    return { ok: false, reason: `WebGPU 设备创建失败：${e?.message || e}` };
  }
}

export class Embedder {
  constructor() {
    this.worker = new Worker(new URL("./embed.worker.js", import.meta.url), { type: "module" });
    this.seq = 0;
    this.pending = new Map();
    this.listeners = new Set();
    this.worker.onmessage = (e) => {
      const m = e.data;
      if (m.type === "result" || m.type === "error") {
        const p = this.pending.get(m.id);
        if (!p) return;
        this.pending.delete(m.id);
        m.type === "result" ? p.resolve(m.result) : p.reject(Object.assign(new Error(m.error), { stack: m.stack }));
      } else {
        this.listeners.forEach((fn) => fn(m));
      }
    };
    this.state = null;
  }

  onProgress(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  #call(type, payload, transfer) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage({ type, id, payload }, transfer || []);
    });
  }

  /**
   * 加载模型并选择后端。返回 { device, requested, fallbackReason, webgpu, ...计时 }。
   * preference: "auto" | "webgpu" | "wasm"
   */
  async load({ modelId, dtype, file, preference = "auto", useCache = true }) {
    const webgpu = preference === "wasm" ? { ok: false, reason: "已通过参数强制使用 WASM" } : await detectWebGPU();
    let fallbackReason = null;
    if (preference !== "wasm" && webgpu.ok) {
      try {
        const r = await this.#call("load", { modelId, dtype, file, device: "webgpu", useCache });
        this.state = { ...r, requested: preference, webgpu, fallbackReason };
        return this.state;
      } catch (e) {
        if (preference === "webgpu") throw e;
        fallbackReason = `WebGPU 推理会话创建失败，已降级：${e.message}`;
      }
    } else if (preference === "webgpu") {
      throw new Error(`要求 WebGPU 但不可用：${webgpu.reason}`);
    } else {
      fallbackReason = webgpu.reason;
    }
    const r = await this.#call("load", { modelId, dtype, file, device: "wasm", useCache });
    this.state = { ...r, requested: preference, webgpu, fallbackReason };
    return this.state;
  }

  /** 返回 { vectors: number[][], ms, count } */
  async embed(texts, { batchSize = 32, sortByLength = true } = {}) {
    const r = await this.#call("embed", { texts, batchSize, sortByLength });
    const vectors = [];
    for (let i = 0; i < r.count; i++) vectors.push(Array.from(r.vectors.subarray(i * r.dim, (i + 1) * r.dim)));
    return { vectors, ms: r.ms, count: r.count, dim: r.dim };
  }
}

/** bge-zh 系列官方建议：短查询检索时加指令前缀，文档侧不加。 */
export const BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章：";

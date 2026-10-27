// 作者：FDE WorkMate
// Embedding Worker：在用户浏览器内执行 ONNX 推理（Transformers.js + ONNX Runtime Web）。
// 模型、tokenizer 与 ORT wasm 均从本地服务同源加载，禁止访问远程模型仓库；不存在任何云端推理调用。
import { env, pipeline } from "@huggingface/transformers";

env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = "/models/";
env.backends.onnx.wasm.wasmPaths = "/ort/";
env.backends.onnx.wasm.numThreads = Math.max(1, Math.min(8, (self.navigator.hardwareConcurrency || 4) - 1));

let extractor = null;
let current = null;

function now() {
  return performance.now();
}

/**
 * variant：精度变体名（fp32 / fp16 / int8 / wq8 …），file：manifest 中的 onnx 相对路径。
 * 统一用 model_file_name 指定具体文件（Transformers.js 的 dtype 固定为 fp32 = 无后缀），
 * 这样任何离线量化产物都按 manifest 加载，不依赖 Transformers.js 内置的 dtype→文件名约定。
 */
async function load({ modelId, dtype, file, device, useCache }) {
  env.useBrowserCache = useCache !== false;
  env.useWasmCache = useCache !== false;
  if (extractor) {
    await extractor.dispose?.();
    extractor = null;
  }
  const files = {};
  const t0 = now();
  let firstByte = null;
  let fetchedAt = null;
  const modelFileName = String(file || "onnx/model.onnx").replace(/^onnx\//, "").replace(/\.onnx$/, "");
  extractor = await pipeline("feature-extraction", modelId, {
    dtype: "fp32",
    model_file_name: modelFileName,
    device,
    progress_callback: (p) => {
      if (p.status === "initiate" && firstByte === null) firstByte = now();
      if (p.status === "progress" && p.file) files[p.file] = { loaded: p.loaded, total: p.total };
      if (p.status === "done" && p.file) {
        files[p.file] = { ...(files[p.file] || {}), done: true };
        fetchedAt = now();
      }
      if (p.status === "progress" && p.file?.endsWith(".onnx")) {
        self.postMessage({ type: "load-progress", file: p.file, loaded: p.loaded, total: p.total });
      }
    },
  });
  const ready = now();
  // 预热：首个 run 包含 shader 编译 / wasm 初始化，单独计时，避免污染吞吐数据
  const w0 = now();
  await extractor(["预热"], { pooling: "cls", normalize: true });
  const warm = now();
  current = { modelId, dtype, device };
  return {
    modelId,
    dtype,
    device,
    // 下载（或从 HTTP/Cache API 读取）模型文件的耗时，与创建推理会话的耗时
    fetchMs: fetchedAt ? fetchedAt - t0 : null,
    sessionMs: fetchedAt ? ready - fetchedAt : ready - t0,
    loadMs: ready - t0,
    warmupMs: warm - w0,
    files,
    threads: env.backends.onnx.wasm.numThreads,
  };
}

async function embed({ id, texts, batchSize = 32, sortByLength = true }) {
  if (!extractor) throw new Error("模型未加载");
  const order = texts.map((_, i) => i);
  // 按长度排序后分批，减少同批 padding 带来的无效计算；输出按原顺序还原
  if (sortByLength) order.sort((a, b) => texts[a].length - texts[b].length);
  const dim = extractor.model.config.hidden_size;
  const out = new Float32Array(texts.length * dim);
  const t0 = now();
  let done = 0;
  for (let i = 0; i < order.length; i += batchSize) {
    const idx = order.slice(i, i + batchSize);
    const tensor = await extractor(idx.map((k) => texts[k]), { pooling: "cls", normalize: true });
    const data = tensor.data;
    idx.forEach((k, j) => out.set(data.subarray(j * dim, (j + 1) * dim), k * dim));
    tensor.dispose?.();
    done += idx.length;
    self.postMessage({ type: "embed-progress", id, done, total: texts.length, ms: now() - t0 });
  }
  return { vectors: out, dim, ms: now() - t0, count: texts.length };
}

self.onmessage = async (e) => {
  const { type, id, payload } = e.data;
  try {
    if (type === "load") {
      self.postMessage({ type: "result", id, result: await load(payload) });
    } else if (type === "embed") {
      const r = await embed({ id, ...payload });
      self.postMessage({ type: "result", id, result: r }, [r.vectors.buffer]);
    } else if (type === "info") {
      self.postMessage({ type: "result", id, result: current });
    }
  } catch (err) {
    self.postMessage({ type: "error", id, error: String(err?.message || err), stack: err?.stack });
  }
};

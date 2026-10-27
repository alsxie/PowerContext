# 端侧 AI 推理 + seekdb 混合检索：迷你知识库

作者：FDE WorkMate

seekdb × 阶跃星辰赛题「端侧 AI 推理性能优化与 seekdb 混合检索」的提交作品。文档和查询都在浏览器里本地向量化（Transformers.js + ONNX Runtime Web，优先 WebGPU，不可用时自动降级到 WASM）。向量、原文和元数据写入 seekdb（seekdb-js 嵌入式模式），检索时由 seekdb 的 `DBMS_HYBRID_SEARCH` 在库内完成向量与全文召回和 RRF 融合。

- 一页技术说明：[docs/技术说明.md](docs/技术说明.md)
- 实测汇总：[bench/summary.md](bench/summary.md)；原始数据：`bench/results/*.json`；参数调优：`bench/tuning/`；统计检验：[bench/analysis.md](bench/analysis.md)
- 端到端验收记录：`bench/e2e.json`，截图 `docs/e2e-*.png`

## 目录

```
server/            Node 服务：seekdb 数据层（kb.mjs）+ HTTP/静态资源（app.mjs），不做任何推理
  test/            数据层集成测试（真实 seekdb 嵌入式实例）
web/               前端（Vite）
  src/embed.worker.js   Web Worker 内的端侧推理
  src/embedder.js       WebGPU 探测 / WASM 自动降级
  src/pipeline.js       导入（切块→向量化→写入→刷新校验）与检索流水线，应用页和基准页共用
  index.html / bench.html
scripts/
  prepare_model.py  下载模型并生成 FP16 / INT8 / WQ8 量化版本，输出 models/manifest.json
  build_dataset.py  构建评测集 data/mmarco-zh-mini（已入库，可复现）
  bench.mjs         Playwright 驱动本机 Chrome 跑基准矩阵，生成 bench/summary.md
  e2e.mjs           应用页端到端验收（WebGPU 可用 / 不可用两种环境）
data/mmarco-zh-mini/  2000 段语料 + 100 条带标注查询 + 构建元数据
```

## 环境要求

- Chrome 或 Edge 最新版（实测 Chrome 156，macOS / Apple M 系列）。WebGPU 可选，没有时自动走 WASM。
- Node.js ≥ 20（实测 24.1），uv（只用于模型量化脚本）。
- 平台限制来自 seekdb 嵌入式原生库：macOS arm64/x64、Linux x64/arm64、Windows x64。

## 启动

```bash
cd stepfun
./start.sh                 # 首次运行会依次执行 npm ci → uv sync → prepare_model.py → vite build
# 浏览器打开 http://127.0.0.1:8787/
```

`start.sh` 依次做这些事，也可以逐步手动执行：

```bash
npm ci                                   # .npmrc 指向 registry.npmmirror.com
uv sync                                  # pyproject.toml 指向阿里云 PyPI 镜像
uv run python scripts/prepare_model.py   # 模型来自 hf-mirror.com（HF_ENDPOINT 可改）
npm run build                            # 构建前端到 web/dist
npm start                                # 打开 .runtime/seekdb（嵌入式）并在 127.0.0.1:8787 提供服务
```

第一次启动时，seekdb-js 会从官方对象存储下载嵌入式原生库（缓存在 `~/.seekdb`）。网络受限时可以设置 `HTTPS_PROXY=http://127.0.0.1:7890 NODE_USE_ENV_PROXY=1`。服务启动时会做一次向量索引自检（见技术说明的「已知问题」），新建数据库的首次启动最长约 40 秒。

常用环境变量：`KB_PORT`（默认 8787）、`SEEKDB_PATH`（默认 `.runtime/seekdb`）、`KB_FTS_ANALYZER`（默认 `ik`）、`KB_RRF_K`（默认 10）、`KB_RRF_WINDOW_FACTOR`（默认 2）。

> 安全提示：API 没有鉴权，默认只监听 127.0.0.1，并校验 Host 白名单、同源 Origin 和 JSON Content-Type，用来防 CSRF 和 DNS rebinding。如果改成对外监听，需要用 `KB_ALLOWED_HOSTS` 加入实际域名，并自行加反向代理和鉴权。

## 使用

1. 页面右上角显示当前推理后端（WebGPU / WASM）。降级时会显示原因，比如 `requestAdapter() 返回 null`。URL 加 `?backend=wasm` 可以强制走 WASM。
2. 新建知识库。知识库与「模型@revision:精度」签名绑定，切换知识库时会自动加载对应精度的模型，避免向量空间不一致。
3. 导入：选择 `.txt` / `.md` / `.jsonl` 文件，或粘贴文本，或点「示例语料 批次 1 / 批次 2」。进度条显示向量化进度，完成后显示向量化吞吐、写入、索引刷新和可见性校验的耗时。
4. 检索：模式可选混合、仅向量、仅全文。页面显示查询向量化、seekdb 检索、传输和端到端耗时。勾选「显示数据库生成的 SQL」可以看到 `DBMS_HYBRID_SEARCH.GET_SQL` 生成的 full join + RRF 语句。
5. 刷新页面后，会话（当前知识库、检索模式、上次查询）从 seekdb 的 `kb_sessions` 表恢复。

## 复现测试

```bash
npm test                         # 数据层集成测试 8 项 + HTTP 跨站防护 5 项
npm start &                      # 下面两个脚本需要服务在运行
node scripts/e2e.mjs             # 应用页端到端：WebGPU 可用 + --disable-gpu 两种环境
node scripts/bench.mjs           # 基准矩阵（约 30 分钟），每个配置 1 冷 + 2 热，结果写入 bench/results
node scripts/bench.mjs --only fp16-webgpu,int8-wasm --repeat 2
node scripts/bench.mjs --summary # 只根据已有原始数据重新生成 bench/summary.md
uv run python scripts/analyze.py # 逐查询配对检验：量化/检索模式的 95% CI，RRF 参数交叉验证 → bench/analysis.md
uv run python scripts/build_dataset.py   # 重新生成评测集（固定种子，结果与仓库一致）
```

也可以直接在浏览器打开 `bench.html?dtype=int8&backend=wasm&runs=3`，在自己的设备上跑同一套基准。

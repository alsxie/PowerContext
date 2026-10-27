#!/usr/bin/env bash
# 作者：FDE WorkMate
# 一键准备并启动：安装依赖（国内镜像）→ 获取并量化模型 → 构建前端 → 启动 seekdb 嵌入式 + Web 服务
# 用法：./start.sh            # 默认 http://127.0.0.1:8787
#       KB_PORT=9000 ./start.sh
set -euo pipefail
cd "$(dirname "$0")"

command -v node >/dev/null || { echo "需要 Node.js >= 20"; exit 1; }
command -v uv >/dev/null || { echo "需要 uv（https://docs.astral.sh/uv/）用于模型量化脚本"; exit 1; }

# 依赖：npm 走 npmmirror（见 .npmrc），Python 走阿里云镜像（见 pyproject.toml）
[ -d node_modules/seekdb ] || npm ci
[ -d .venv ] || uv sync

# 模型：默认从 hf-mirror.com 下载固定 revision 的 FP32 ONNX，再本地生成 FP16 / INT8 / WQ8
if [ ! -f models/Xenova/bge-small-zh-v1.5/onnx/model_wq8.onnx ]; then
  uv run python scripts/prepare_model.py
fi

[ -f web/dist/index.html ] || npm run build

# seekdb 嵌入式原生库首次运行时从官方对象存储下载（约 100MB，缓存在 ~/.seekdb），
# 网络不通时可设置 HTTPS_PROXY + NODE_USE_ENV_PROXY=1（Node ≥ 24）或 SEEKDB_BINDINGS_BASE_URL
exec node server/index.mjs

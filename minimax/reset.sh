#!/usr/bin/env bash
# 作者：FDE WorkMate
# 停止 PowerContext Server，清空 seekdb 资料库与 PowerContext 状态，并从 sample_app/ 重建 .work/sample_app/ 工作副本。
# 不删除 records/ 下已有的运行记录。
set -euo pipefail
cd "$(dirname "$0")"
uv run --frozen sddctx reset --task samples/task-001/task.md

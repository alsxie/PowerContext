#!/usr/bin/env bash
# 作者：FDE WorkMate
# 一键演示：初始化 -> 导入 -> 修改前测试 -> 生成上下文 -> 重启后恢复状态 -> MiniMax 修改代码 -> 修改后测试 -> 规格更新后重新导入。
#
#   ./run_demo.sh                 使用 .env.local 中的 MINIMAX_API_KEY 实时调用 MiniMax
#   ./run_demo.sh --replay DIR    不调用 MiniMax，重放 DIR 中已记录的 MiniMax 输出（例如 records/runs/task-001-minimax/minimax）
#
# 环境变量：SDDCTX_RUN_NAME 指定记录目录名（默认 task-001-<时间>），UV_INDEX_URL 指定 Python 包镜像。
set -euo pipefail

cd "$(dirname "$0")"
TASK=samples/task-001/task.md
AGENT_ARGS=("$@")
export SDDCTX_RUN_NAME="${SDDCTX_RUN_NAME:-task-001-$(date +%Y%m%d-%H%M%S)}"
SDDCTX=(uv run --frozen sddctx)

step() { printf '\n\033[1m==== %s\033[0m\n' "$*"; }

step "0. 安装依赖（uv，默认使用 pyproject.toml 中配置的国内镜像）"
uv sync --frozen

step "1. 初始化：清空 seekdb 资料库与 PowerContext 状态，从 sample_app/ 复制工作副本到 .work/sample_app/"
"${SDDCTX[@]}" reset --task "$TASK"

step "2. 启动 PowerContext Server（seekdb 后端，数据在 .runtime/powercontext）"
"${SDDCTX[@]}" server start

step "3. 导入规格、代码和测试到 seekdb"
"${SDDCTX[@]}" ingest --task "$TASK"

step "4. 修改前测试：目标功能测试应失败，回归测试应通过"
"${SDDCTX[@]}" baseline --task "$TASK"

step "5. 生成开发上下文并保存到 PowerContext"
"${SDDCTX[@]}" context --task "$TASK" >/dev/null
"${SDDCTX[@]}" check --task "$TASK" --expected samples/task-001/expected.json

step "6. 重启 PowerContext Server，模拟中断；重启后读取已保存的状态"
"${SDDCTX[@]}" server stop
"${SDDCTX[@]}" server start
"${SDDCTX[@]}" next --task "$TASK"

step "7. 从保存的状态继续：MiniMax 修改代码，然后运行修改后测试"
"${SDDCTX[@]}" run --task "$TASK" "${AGENT_ARGS[@]}"

step "8. 规格更新：叠加新版 specs/orders.md，增量重新导入，再生成上下文"
"${SDDCTX[@]}" overlay --task "$TASK" samples/task-001/spec-update
"${SDDCTX[@]}" ingest --task "$TASK"
"${SDDCTX[@]}" context --task "$TASK" > "records/runs/$SDDCTX_RUN_NAME/context-after-spec-update.md"
cp "records/runs/$SDDCTX_RUN_NAME/context.json" "records/runs/$SDDCTX_RUN_NAME/context-after-spec-update.json"
"${SDDCTX[@]}" check --task "$TASK" --expected samples/task-001/expected-after-spec-update.json

step "9. 规格变化后原有验证不再适用：重新运行修改后测试"
"${SDDCTX[@]}" verify --task "$TASK"

step "10. 最终状态"
"${SDDCTX[@]}" state --task "$TASK" > "records/runs/$SDDCTX_RUN_NAME/powercontext_state.json"
"${SDDCTX[@]}" next --task "$TASK"
"${SDDCTX[@]}" server stop

echo
echo "全部记录在 records/runs/$SDDCTX_RUN_NAME/"

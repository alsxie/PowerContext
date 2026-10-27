# 规有所依：面向软件持续演进的 SDD 上下文管理系统

作者：FDE WorkMate

赛题：OceanBase 2026 生态挑战赛 MiniMax 赛道。本目录实现一个可运行的 SDD 工程上下文模块 `sddctx`：

- seekdb（嵌入式 `pylibseekdb`）保存并检索规格、代码、测试，全文检索使用 ngram 分词，支持中文；
- PowerContext 1.2.0（本地 Server，seekdb 后端）保存任务进度、验证结果和交接状态，进程重启后读回继续；
- MiniMax（默认 Anthropic Messages 兼容接口 `https://api.minimax.cn/anthropic`，模型 `MiniMax-M3.1-Flash`；可切换为 OpenAI 兼容接口）读取生成的上下文，实际修改业务工程代码；
- 修改前后各运行一次目标功能测试与回归测试，所有过程写入 `records/`。

## 目录

| 路径 | 内容 |
| --- | --- |
| `sddctx/` | 上下文模块：`parsers` 解析、`store` seekdb 资料库、`context` 上下文选择、`state` PowerContext 状态、`agent` MiniMax、`verify` 测试、`cli` 命令 |
| `sample_app/` | 业务工程基线（修改前状态）：订单查询服务、`specs/` 规格与决策、`tests/` 测试 |
| `samples/task-001/` | 验证样例：任务卡 `task.md`、上下文期望 `expected*.json`、规格更新 `spec-update/` |
| `run_demo.sh` / `reset.sh` | 一键演示 / 重置 |
| `records/runs/<运行名>/` | 运行记录：上下文、数据库查询、PowerContext 状态、MiniMax 请求响应、快照、diff、测试日志 |
| `tests/` | `sddctx` 自身的单元测试 |
| `docs/技术说明.md` | 一页技术说明 |

## 环境

- macOS（arm64）或 Linux x86_64/arm64，Python 3.11–3.13，[uv](https://docs.astral.sh/uv/)；无需 Docker。
- 依赖固定版本：`powercontext[cli,server,seekdb]==1.2.0`、`pylibseekdb==1.4.0.post1`，`uv.lock` 锁定全部依赖；默认使用阿里云 PyPI 镜像。
- MiniMax Key：`cp .env.example .env.local` 后填写 `MINIMAX_API_KEY`（`.env.local` 不入库）。

## 运行

```bash
cd minimax
uv sync --frozen
./run_demo.sh                                            # 实时调用 MiniMax
./run_demo.sh --replay records/runs/task-001-minimax/minimax   # 没有 Key 时重放已记录的 MiniMax 输出
./reset.sh                                               # 清空资料库与状态，重建工作副本
```

`run_demo.sh` 的步骤与对应命令：

| 步骤 | 命令 | 结果 |
| --- | --- | --- |
| 1 初始化 | `sddctx reset` | 清空 `.runtime/`，把 `sample_app/` 复制到 `.work/sample_app/`（MiniMax 只改工作副本，基线保持修改前状态） |
| 2 启动 | `sddctx server start` | 本地 PowerContext Server，端口 18765 |
| 3 导入 | `sddctx ingest` | 规格条目、代码符号、测试函数按内容哈希增量写入 seekdb |
| 4 修改前测试 | `sddctx baseline` | 目标功能测试 7/7 因缺少 `statuses` 参数失败，回归测试 9/9 通过 |
| 5 上下文 | `sddctx context` + `sddctx check` | 生成上下文并核对来源与行号 |
| 6 重启恢复 | `server stop/start` + `sddctx next` | 从 PowerContext 读回已完成步骤，下一步为 agent |
| 7 继续执行 | `sddctx run` | MiniMax 修改代码（最多 2 次，失败日志回传），随后修改后测试 |
| 8 规格更新 | `sddctx overlay` + `ingest` + `context` + `check` | 只更新变化条目，上下文使用新版规格并提示原验证需重做 |
| 9 重新验证 | `sddctx verify` | 规格更新后重新运行测试 |

单独使用：`uv run sddctx context --task samples/task-001/task.md`（Markdown）或加 `--format json`；`sddctx state` 查看完整状态。

## 验证样例 TASK-001

任务：为 `OrderQueryService.list_orders` 增加 `statuses` 筛选。上下文必须包含（`samples/task-001/expected.json`）：

- 适用规格：主规格 REQ-ORD-004，约束 ADR-002、REQ-ORD-001/002/003，以及“必须保持”命中的 REQ-BIL-001；
- 已失效决策：ADR-001（单状态、未知状态静默返回空列表）单列为“不得遵循”；
- 待修改代码：`orders/service.py` 中的 `OrderQueryService.list_orders`，以及参考符号 `InvalidQueryError`、`OrderStatus`、`_validate_pagination` 等；
- 测试：功能测试 `tests/test_status_filter.py`，回归测试 `tests/test_listing.py`、`tests/test_billing.py`。

`sddctx check` 会逐条打开来源文件，确认上下文中每段内容与文件的对应行一致。

## 记录说明（`records/runs/<运行名>/`）

| 文件 | 内容 |
| --- | --- |
| `context.md` / `context.json` | 交给 MiniMax 的上下文；`context-after-spec-update.*` 为规格更新后的版本 |
| `context-check.json` | 上下文核对结果 |
| `db_queries.jsonl` | 每次 seekdb 查询：用途、SQL、参数、行数、命中条目、耗时 |
| `powercontext_log.jsonl` / `powercontext_state.json` | PowerContext 写入/读取操作与最终状态（scope、handoff 版本、已完成/待办、验证结果） |
| `minimax/attempt-N.*` | 每次 MiniMax 调用的请求、响应（接口、模型、用量、耗时；只保存 text 内容块，不保存 thinking）与该次 diff |
| `snapshots/before`、`snapshots/after`、`code.diff` | 修改前后代码快照与完整差异 |
| `baseline-*`、`agent-attempt-*`、`verify-*` | 测试命令、退出码、junit XML、日志与汇总 |

## 安全说明

PowerContext Server 只监听 `127.0.0.1`，按本地开发工具使用，不做鉴权，不要暴露到外网。MiniMax Key 只从环境变量或 `.env.local` 读取，不写入任何记录。

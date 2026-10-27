# 运行证据

作者：FDE WorkMate

## 采集环境

证据采集于 2026-10-28 01:33–01:36（UTC+8）。部署方式是按 `../README.md` 从零复现：先清空 Dify 与 PowerContext 数据，再依次执行 `setup_dify.sh`、`start_powercontext.sh`、`dify_bootstrap.py`。

| 项 | 值 |
| --- | --- |
| 主机 | Linux 5.14.0 x86_64，Docker 29.8.2，Docker Compose v5.6.0 |
| Dify | `langgenius/dify-api:1.18.0`、`dify-web:1.18.0`、`dify-plugin-daemon:0.6.10-local`、`dify-sandbox:0.2.15` |
| 模型 | `langgenius/openai_api_compatible` 0.0.73 插件 + MiniMax `MiniMax-M3.1-Flash` |
| PowerContext | `grudge-tavern/powercontext:1.4.1`（`powercontext==1.4.1`，内嵌 seekdb 存储） |

## 文件

| 文件 | 内容 |
| --- | --- |
| `verify-transcript.md` | 第 1 次验收的完整对话记录：每轮包含老焦的回答、断言结果和节点轨迹 |
| `verify-report.json` | 同一次验收的结构化报告：Dify `node_finished` 事件（含 HTTP 节点拿到的 PowerContext 响应）、workflow_run_id，以及每轮之后的 PowerContext 账本快照 |
| `stability.md` | 3 次完整验收的通过率（18/18 轮），以及 PowerContext 服务端日志与 Dify 节点调用的逐项对账（190 = 115 + 75） |
| `powercontext-access.jsonl` | PowerContext 服务端在验收时间窗内的 `transport.request.completed` 访问日志（精简字段） |
| `degrade-check.json` | 停掉 PowerContext 后发送一条消息，Chatflow 仍然成功返回。Dify 日志把这次运行记为 PARTIAL SUCCESS，原因是 HTTP 节点走了默认值分支 |
| `dify-workflow.png` | Dify 导出的完整 Chatflow 画布 |
| `dify-conversation.png` | Dify 日志页：左侧是会话列表，右侧是会话 B 跨会话翻出旧账（附 artifact_id）的回复 |

## 交叉核对方法

- 回执里的 artifact_id 可以用 `POST /v1/memory/entries/list` 或 `POST /v1/atomic-memory/list`（`states:["forgotten"]`）在 PowerContext 中查到。
- `verify-report.json` 中每轮的 `ledger_after` 由 verify.py 直接请求 PowerContext 得到，不经过 Dify。
- `stability.md` 的对账表把服务端访问日志按 operation 计数，并与 Dify 节点调用次数加上 verify.py 自身的核对调用逐项比对。

# 稳定性与服务端对账

作者：FDE WorkMate

从零部署后，用 3 个不同名号连续跑了 3 遍 `samples/scenario.json`，每遍 6 轮、跨 3 个 Dify 会话。

## 断言通过率

| 轮次 | 运行 1（阿土-0133-r1） | 运行 2（阿土-0134-r2） | 运行 3（阿土-0135-r3） |
| --- | --- | --- | --- |
| T1-赊账 | 通过 9.4s | 通过 7.8s | 通过 7.3s |
| T2-恶作剧 | 通过 10.5s | 通过 6.3s | 通过 9.0s |
| T3-换个会话翻旧账 | 通过 10.3s | 通过 13.6s | 通过 8.2s |
| T4-还账勾销 | 通过 16.9s | 通过 16.3s | 通过 8.0s |
| T5-注入攻击 | 通过 18.0s | 通过 20.5s | 通过 10.1s |
| T6-查账 | 通过 0.5s | 通过 0.6s | 通过 0.5s |

合计：18/18 轮通过，断言 60/60 条通过。

## PowerContext 服务端访问日志对账

统计窗口：2026-10-27T17:33:00.908392+00:00 ～ 2026-10-27T17:35:56.197690+00:00，共 190 条 `transport.request.completed` 记录，全部成功（状态码分布 {201: 21, 200: 169}）。原始记录见 `powercontext-access.jsonl`。

| operation | Dify HTTP 节点调用 | verify.py 核对调用 | 合计（预期） | 服务端实际 |
| --- | --- | --- | --- | --- |
| change_atomic_memory_lifecycle | 6 | 0 | 6 | 6 |
| create_scope | 18 | 3 | 21 | 21 |
| list_atomic_memories | 3 | 36 | 39 | 39 |
| list_memory_entries | 18 | 36 | 54 | 54 |
| remember_memory | 15 | 0 | 15 | 15 |
| search_memory | 55 | 0 | 55 | 55 |

合计：Dify 115 + verify.py 75 = 190，服务端记录 190。Dify 侧调用次数取自各轮 `node_finished` 事件（`verify-report.json` 的 nodes 字段）。

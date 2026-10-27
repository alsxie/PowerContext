# ContextRelay

ContextRelay 是一个研究与深度工作接续工具。用户结束一次工作时记录进展、关键决策、阻塞和下一步；下一次会话按当前问题从 PowerContext 召回相关记忆，并生成带证据标识的接续包。

## 解决的问题

研究和复杂项目经常跨越多天与多个会话。重新开始时，人们往往要翻聊天记录、实验笔记和任务列表，才能恢复“做到哪、为什么这样做、接下来做什么”。ContextRelay 将这些确认过的工作状态保存为结构化 Memory，只在需要时召回相关内容。

## PowerContext 的实际作用

- `remember_memory`：将进展、决策、阻塞和下一步以不同 kind 写入项目 Scope。
- `search_memory`：按当前任务使用 FTS 检索最多 8 条相关记忆，并返回可核对的 entry ID。
- `prepare_context`：在 6 KB 预算内组装本次接续上下文。
- 固定 idempotency key 创建项目 Scope，避免应用重启后重复创建工作区。

公开静态站点在没有本地后端时会明确显示“浏览器演示模式”，演示数据只保存在浏览器；评审按以下步骤启动后，状态切换为“PowerContext 已连接”，所有保存与检索走真实 PowerContext API。

## 运行环境

- Python 3.11+
- uv
- macOS 或 Linux

## 启动

在仓库的项目目录执行：

```bash
cd qoder/context-relay
uv sync
uv run powercontext config init --language en --output powercontext.env
uv run powercontext server run --env-file powercontext.env
```

配置向导选择 **Basic memory** 即可运行验证样例，无需模型 API Key。保留 PowerContext Server 进程，再打开另一个终端：

```bash
cd qoder/context-relay
cp .env.example .env
set -a
. ./.env
set +a
uv run uvicorn app:app \
  --app-dir backend \
  --host 127.0.0.1 \
  --port 8088
```

浏览器打开 `http://127.0.0.1:8088`。

## 验证样例

初始输入：

- 项目：`小样本异常检测研究`
- 进展：`完成基线复现实验，确认验证集上的 F1 为 0.71。`
- 决策：`采用按设备分组切分，避免同一设备数据泄漏到验证集。`
- 阻塞：`设备 C 的时间戳存在 8 小时偏移，需先统一时区。`
- 下一步：`修正设备 C 时区后重跑数据切分，并对比 F1 变化。`

操作步骤：

1. 点击“写入工作记忆”，页面应显示写入 4 条 PowerContext Memory。
2. 刷新页面，确认状态仍为“PowerContext 已连接”。
3. 输入 `设备 C 时区和验证集 F1`，点击“生成接续包”。
4. 结果应包含设备 C 时区阻塞、F1 基线和按设备分组决策，并显示真实 Memory entry ID 与 `fts` 匹配方式。

该样例验证保存、跨页面会话复用、相关检索、上下文组装和证据回查的完整链路。

## 项目结构

```text
qoder/context-relay/
├── backend/app.py      # PowerContext API 适配层与静态站点服务
├── web/index.html      # Qoder Sites 发布入口
├── web/styles.css
├── web/app.js
├── .env.example
└── README.md
```

## Qoder 开发记录

主要开发工作在 Qoder 中完成，包括：读取赛题与官方细则、分析 PowerContext API、拆解接续流程、实现前后端、构造验证样例、运行检查和准备 Qoder Sites 发布物。提交时需补充本次 Qoder 对话截图或录屏及参赛账号 UID；不在仓库中保存账号或其他凭据。

## 提交材料状态

- 项目源码：当前目录
- 运行说明：本文件
- 验证样例：本文件“验证样例”章节
- 项目网站：通过 Qoder Sites 准备并发布后填写
- Qoder 账号 UID 与开发记录截图：在测试平台提交时填写和上传

配置文件只保留占位值，不提交 API Key、密码或 Token。

# 作者：FDE WorkMate
"""使用 MiniMax 的开发助手：把上下文交给模型，解析模型给出的完整文件内容并写回业务工程。

接入方式：默认 MiniMax Anthropic Messages 兼容接口（POST {base}/v1/messages，默认 https://api.minimax.cn/anthropic，
模型 MiniMax-M3.1-Flash）；也可配置为 OpenAI Chat Completions 兼容接口（POST {base}/chat/completions）。
每次调用的请求、响应（只保存 text 内容块，不保存 thinking 块与 <think> 段）、token 用量和应用后的代码差异都写入记录目录。
记录可以用 --replay 重新应用，供没有 API Key 的评审复现同一份代码修改。
"""

from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from sddctx.config import Settings
from sddctx.parsers import ProjectLayout, collect_files
from sddctx.records import now_iso, write_json

FILE_BLOCK = re.compile(r'<file path="([^"]+)">\n(.*?)\n?</file>', re.DOTALL)
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)

SYSTEM_PROMPT = """你是一名严谨的 Python 开发助手，按照规格驱动开发（SDD）完成代码修改。
规则：
1. 只遵循“适用规格”；“已失效的决策”仅供了解历史，不得按其实现。
2. 只修改“需要修改的代码”所在文件，必要时可修改其它业务代码文件；禁止修改测试文件和规格文件。
3. 保持“必须保持”中列出的原有行为，回归测试必须继续通过。
4. 输出格式：先用不超过 5 行说明修改要点，然后对每个修改过的文件输出一次完整的新文件内容：
<file path="相对业务工程根目录的路径">
完整文件内容
</file>
不要输出 diff，不要用 Markdown 代码块包裹 <file> 段。"""


class AgentError(RuntimeError):
    pass


def _editable_files(settings: Settings) -> set[str]:
    layout = ProjectLayout.load(settings.project_root)
    tests = {path.relative_to(settings.project_root).as_posix() for path in collect_files(settings.project_root, layout.test_globs)}
    specs = {path.relative_to(settings.project_root).as_posix() for path in collect_files(settings.project_root, layout.spec_globs)}
    code = {path.relative_to(settings.project_root).as_posix() for path in collect_files(settings.project_root, layout.code_globs)}
    return code - tests - specs


def build_messages(context_markdown: str, files: dict[str, str], feedback: str | None) -> list[dict[str, str]]:
    current = "\n\n".join(f'<current path="{path}">\n{content}\n</current>' for path, content in files.items())
    user = f"{context_markdown}\n\n## 待修改文件的当前完整内容\n\n{current}\n\n请按规则完成修改。"
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    if feedback:
        messages.append({"role": "user", "content": f"上一次修改后测试仍未全部通过，输出如下，请修正后重新给出完整文件：\n\n{feedback}"})
    return messages


def _anthropic_request(settings: Settings, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    body: dict[str, Any] = {
        "model": settings.minimax_model,
        "max_tokens": 32000,  # 思考 token 也计入上限
        "system": system,
        "messages": [{"role": m["role"], "content": m["content"]} for m in _merge_user_turns(messages)],
    }
    if settings.minimax_effort:
        body["output_config"] = {"effort": settings.minimax_effort}
    return f"{settings.minimax_base_url.rstrip('/')}/v1/messages", body


def _merge_user_turns(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Messages 协议要求 user/assistant 交替：相邻的 user 消息合并为一条。"""

    merged: list[dict[str, str]] = []
    for message in messages:
        if message["role"] == "system":
            continue
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1] = {"role": message["role"], "content": merged[-1]["content"] + "\n\n" + message["content"]}
        else:
            merged.append(dict(message))
    return merged


def _openai_request(settings: Settings, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    body = {"model": settings.minimax_model, "messages": messages, "temperature": 0.2, "max_tokens": 32000}
    return f"{settings.minimax_base_url.rstrip('/')}/chat/completions", body


def _parse_anthropic(payload: dict[str, Any]) -> dict[str, Any]:
    blocks = payload.get("content") or []
    text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    return {
        "id": payload.get("id"),
        "model": payload.get("model"),
        "usage": payload.get("usage"),
        "finish_reason": payload.get("stop_reason"),
        "content_block_types": [block.get("type") for block in blocks],
        "content": text,
    }


def _parse_openai(payload: dict[str, Any]) -> dict[str, Any]:
    if (payload.get("base_resp") or {}).get("status_code", 0) != 0:
        raise AgentError(f"MiniMax 返回错误: {payload['base_resp']}")
    choice = payload["choices"][0]
    return {
        "id": payload.get("id"),
        "model": payload.get("model"),
        "usage": payload.get("usage"),
        "finish_reason": choice.get("finish_reason"),
        "content": choice["message"].get("content") or "",
    }


API_STYLES = {"anthropic": (_anthropic_request, _parse_anthropic), "openai": (_openai_request, _parse_openai)}


def call_minimax(settings: Settings, messages: list[dict[str, str]]) -> dict[str, Any]:
    if not settings.minimax_api_key:
        raise AgentError("未配置 MINIMAX_API_KEY：请写入 minimax/.env.local，或使用 --replay 重放已记录的修改")
    if settings.minimax_api_style not in API_STYLES:
        raise AgentError(f"不支持的 MINIMAX_API_STYLE={settings.minimax_api_style}，可选 {sorted(API_STYLES)}")
    build, parse = API_STYLES[settings.minimax_api_style]
    endpoint, body = build(settings, messages)
    started = time.perf_counter()
    response = httpx.post(
        endpoint,
        headers={"Authorization": f"Bearer {settings.minimax_api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=600,
    )
    if response.status_code != 200:
        raise AgentError(f"MiniMax 返回 HTTP {response.status_code}: {response.text[:500]}")
    reply = parse(response.json())
    reply["model"] = reply.get("model") or settings.minimax_model
    # 模型的思考内容属于私有推理过程，不写入记录。
    reply["content"] = THINK_BLOCK.sub("", reply["content"]).strip()
    return reply | {
        "api_style": settings.minimax_api_style,
        "endpoint": endpoint,
        "request_options": {key: value for key, value in body.items() if key not in ("messages", "system")},
        "latency_s": round(time.perf_counter() - started, 2),
    }


def parse_files(content: str, editable: set[str]) -> dict[str, str]:
    blocks = {path.strip(): body for path, body in FILE_BLOCK.findall(content)}
    if not blocks:
        raise AgentError("模型输出中没有 <file> 段")
    rejected = sorted(path for path in blocks if path not in editable)
    if rejected:
        raise AgentError(f"模型试图修改不允许的文件：{rejected}（只允许业务代码，禁止测试与规格）")
    return {path: body.rstrip("\n") + "\n" for path, body in blocks.items()}


def apply_files(project_root: Path, files: dict[str, str]) -> str:
    """写回文件并返回统一 diff（路径相对 minimax/ 目录）。"""

    chunks: list[str] = []
    for rel, new in sorted(files.items()):
        path = project_root / rel
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        label = f"{project_root.name}/{rel}"
        chunks.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{label}", f"b/{label}"))
        path.write_text(new, encoding="utf-8")
    return "".join(chunks)


def record_attempt(run_dir: Path, attempt: int, *, mode: str, messages, reply: dict[str, Any], diff: str, files: dict[str, str]) -> Path:  # noqa: ANN001
    base = run_dir / "minimax"
    write_json(base / f"attempt-{attempt}.request.json", {"at": now_iso(), "mode": mode, "messages": messages})
    write_json(base / f"attempt-{attempt}.response.json", {"at": now_iso(), "mode": mode, **reply, "files_changed": sorted(files)})
    (base / f"attempt-{attempt}.diff").write_text(diff, encoding="utf-8")
    return base


def load_replay(replay_dir: Path, attempt: int) -> tuple[list[dict[str, str]], dict[str, Any]]:
    request = json.loads((replay_dir / f"attempt-{attempt}.request.json").read_text(encoding="utf-8"))
    response = json.loads((replay_dir / f"attempt-{attempt}.response.json").read_text(encoding="utf-8"))
    return request["messages"], response


def replay_attempts(replay_dir: Path) -> list[int]:
    return sorted(int(match.group(1)) for path in replay_dir.glob("attempt-*.response.json") if (match := re.search(r"attempt-(\d+)", path.name)))

#!/usr/bin/env python3
# 作者：FDE WorkMate
"""一键初始化 Dify：管理员 -> 模型插件 -> MiniMax 凭据 -> 导入 DSL -> 注入 PowerContext Token -> 发布 -> 应用 API Key。

仅依赖 Python 3.9+ 标准库，可重复执行（已完成的步骤会跳过，DSL 会覆盖到同名应用）。
所有输入来自环境变量，见 dify/grudge-tavern/.env.example；结果（app_id、应用 API Key）写入
$GRUDGE_STATE_FILE（默认 ~/.grudge-tavern/state.env，权限 600）。
"""

from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DIFY_URL = os.environ.get("DIFY_URL", "http://127.0.0.1:18080").rstrip("/")
ADMIN_EMAIL = os.environ.get("DIFY_ADMIN_EMAIL", "admin@grudge-tavern.local")
ADMIN_NAME = os.environ.get("DIFY_ADMIN_NAME", "tavern-admin")
ADMIN_PASSWORD = os.environ.get("DIFY_ADMIN_PASSWORD", "")
INIT_PASSWORD = os.environ.get("DIFY_INIT_PASSWORD", "")
PLUGIN_ID = os.environ.get(
    "GRUDGE_PLUGIN_ID",
    "langgenius/openai_api_compatible:0.0.73@4cf123ffaa4556f079bed6c47f3a462cc825504b859b183239e74eb19136a4b5",
)
PROVIDER = os.environ.get("GRUDGE_MODEL_PROVIDER", "langgenius/openai_api_compatible/openai_api_compatible")
MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M3.1-Flash")
MODEL_BASE_URL = os.environ.get("MINIMAX_OPENAI_BASE_URL", "https://api.minimax.cn/v1")
MODEL_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
PC_TOKEN = os.environ.get("POWERCONTEXT_TOKEN", "")
PC_URL = os.environ.get("POWERCONTEXT_URL_FOR_DIFY", "http://powercontext:8000")
DSL_PATH = Path(os.environ.get("GRUDGE_DSL", ROOT / "dsl" / "grudge-tavern.yml"))
STATE_FILE = Path(os.environ.get("GRUDGE_STATE_FILE", Path.home() / ".grudge-tavern" / "state.env")).expanduser()
APP_NAME = "记仇酒馆：老焦的小本本"

ENV_IDS = {
    "POWERCONTEXT_URL": "b1d6b9bf-c30c-56ec-86c8-a1948462aaa4",
    "POWERCONTEXT_TOKEN": "91b9b30d-e30d-55ab-93e4-e7c0c69b9d76",
}

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"[bootstrap] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def call(method: str, path: str, payload=None, expect=(200, 201, 202, 204), token: str | None = None):
    headers = {"Content-Type": "application/json"}
    csrf = next((c.value for c in jar if c.name.endswith("csrf_token")), None)
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(f"{DIFY_URL}{path}", data=data, method=method, headers=headers)
    try:
        with opener.open(req, timeout=120) as resp:
            status, body = resp.status, resp.read().decode() or "{}"
    except urllib.error.HTTPError as err:
        status, body = err.code, err.read().decode() or "{}"
    if status not in expect:
        fail(f"{method} {path} -> {status}: {body[:500]}")
    try:
        return json.loads(body)
    except ValueError:
        return {"raw": body}


def require(name: str, value: str) -> None:
    if not value:
        fail(f"缺少环境变量 {name}")


def setup_admin() -> None:
    if call("GET", "/console/api/setup").get("step") == "finished":
        log("管理员已存在，跳过 setup")
        return
    require("DIFY_INIT_PASSWORD", INIT_PASSWORD)
    call("POST", "/console/api/init", {"password": INIT_PASSWORD})
    call("POST", "/console/api/setup", {"email": ADMIN_EMAIL, "name": ADMIN_NAME, "password": ADMIN_PASSWORD,
                                        "language": "zh-Hans"})
    log(f"已创建管理员 {ADMIN_EMAIL}")


def login() -> None:
    encoded = base64.b64encode(ADMIN_PASSWORD.encode()).decode()
    result = call("POST", "/console/api/login", {"email": ADMIN_EMAIL, "password": encoded, "remember_me": True})
    if result.get("result") != "success":
        fail(f"登录失败：{result}")
    log("登录成功")


def ensure_plugin() -> None:
    plugin_name = PLUGIN_ID.split(":", 1)[0]
    installed = call("GET", "/console/api/workspaces/current/plugin/list?page=1&page_size=100").get("plugins", [])
    if any(p.get("plugin_id") == plugin_name for p in installed):
        log(f"插件 {plugin_name} 已安装")
        return
    task = call("POST", "/console/api/workspaces/current/plugin/install/marketplace",
                {"plugin_unique_identifiers": [PLUGIN_ID]})
    task_id = task.get("task_id")
    log(f"安装插件 {PLUGIN_ID}（task {task_id}）")
    for _ in range(120):
        if task.get("all_installed") or not task_id:
            break
        time.sleep(5)
        status = call("GET", f"/console/api/workspaces/current/plugin/tasks/{task_id}").get("task", {})
        if status.get("status") == "success":
            break
        if status.get("status") == "failed":
            fail(f"插件安装失败：{json.dumps(status, ensure_ascii=False)[:800]}")
    else:
        fail("插件安装超时")
    log("插件安装完成")


def ensure_model() -> None:
    base = f"/console/api/workspaces/current/model-providers/{PROVIDER}/models"
    models = call("GET", base).get("data", [])
    if any(m.get("model") == MODEL and m.get("status") == "active" for m in models):
        log(f"模型 {MODEL} 已配置")
        return
    require("MINIMAX_API_KEY", MODEL_API_KEY)
    credentials = {
        "api_key": MODEL_API_KEY,
        "endpoint_url": MODEL_BASE_URL,
        "endpoint_model_name": MODEL,
        "mode": "chat",
        "context_size": "200000",
        "max_tokens_to_sample": "4096",
        "function_calling_type": "no_call",
        "stream_function_calling": "not_supported",
        "vision_support": "no_support",
        "structured_output_support": "not_supported",
        "stream_mode_delimiter": "\n\n",
    }
    call("POST", f"{base}/credentials", {"model": MODEL, "model_type": "llm", "name": "minimax",
                                          "credentials": credentials})
    log(f"已添加模型凭据 {PROVIDER} / {MODEL}")


def import_app() -> str:
    apps = call("GET", "/console/api/apps?page=1&limit=100&name=" + urllib.request.quote(APP_NAME)).get("data", [])
    app_id = next((a["id"] for a in apps if a.get("name") == APP_NAME), None)
    payload = {"mode": "yaml-content", "yaml_content": DSL_PATH.read_text(encoding="utf-8")}
    if app_id:
        payload["app_id"] = app_id
    result = call("POST", "/console/api/apps/imports", payload)
    if result.get("status") == "pending":
        result = call("POST", f"/console/api/apps/imports/{result['id']}/confirm")
    if result.get("status") not in ("completed", "completed-with-warnings"):
        fail(f"DSL 导入失败：{result}")
    app_id = result["app_id"]
    log(f"DSL 已导入（{result['status']}），app_id={app_id}")
    return app_id


def configure_env(app_id: str) -> None:
    require("POWERCONTEXT_TOKEN", PC_TOKEN)
    variables = [
        {"id": ENV_IDS["POWERCONTEXT_URL"], "name": "POWERCONTEXT_URL", "value": PC_URL, "value_type": "string",
         "description": "PowerContext Server 地址（Dify 容器网络内）"},
        {"id": ENV_IDS["POWERCONTEXT_TOKEN"], "name": "POWERCONTEXT_TOKEN", "value": PC_TOKEN,
         "value_type": "secret", "description": "PowerContext Bearer Token"},
    ]
    call("POST", f"/console/api/apps/{app_id}/workflows/draft/environment-variables",
         {"environment_variables": variables, "patch": True})
    call("POST", f"/console/api/apps/{app_id}/workflows/publish",
         {"marked_name": "grudge-tavern", "marked_comment": "bootstrap"})
    log("已写入环境变量并发布工作流")


def ensure_api_key(app_id: str) -> str:
    keys = call("GET", f"/console/api/apps/{app_id}/api-keys").get("data", [])
    if keys:
        return keys[0]["token"]
    return call("POST", f"/console/api/apps/{app_id}/api-keys")["token"]


def main() -> None:
    require("DIFY_ADMIN_PASSWORD", ADMIN_PASSWORD)
    if not DSL_PATH.exists():
        fail(f"找不到 DSL：{DSL_PATH}")
    setup_admin()
    login()
    ensure_plugin()
    ensure_model()
    app_id = import_app()
    configure_env(app_id)
    api_key = ensure_api_key(app_id)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(f"DIFY_APP_ID={app_id}\nDIFY_APP_API_KEY={api_key}\n", encoding="utf-8")
    STATE_FILE.chmod(0o600)
    log(f"完成：应用 API Key 已写入 {STATE_FILE}")
    log(f"Web 控制台：{DIFY_URL}/app/{app_id}/workflow")


if __name__ == "__main__":
    main()

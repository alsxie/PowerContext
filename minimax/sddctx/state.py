# 作者：FDE WorkMate
"""任务状态：通过 PowerContext HTTP API（官方 Python Client，powercontext==1.2.0）保存并复用。

- Work Contract：任务目标、范围与完成标准。
- Content Source：每一步的证据（测试运行、上下文快照、MiniMax 修改记录），schema 为 sddctx.*.v1。
- Handoff：每完成一步提交一个 Revision，state 为已完成事项（verified 并引用证据），omissions 为待办事项。

上下文模块重启后通过 continue_handoff(latest) 与 list_sources 读回这些记录，用于下一次上下文和下一步动作的决定。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from powercontext.client import PowerContextClient, ServerResponseError
from powercontext.http import (
    CommitHandoffRequest,
    ContinueHandoffRequest,
    CreateScopeRequest,
    CreateSourceRequest,
    CreateWorkContractRequest,
    HandoffCurrentWorkRequest,
    ListSourcesRequest,
    RecordTaskOutcomeRequest,
)

from sddctx.config import Settings
from sddctx.records import append_jsonl, now_iso
from sddctx.task import Task

# 任务流程的固定步骤；每一步完成时写入一条证据 Source 并提交 Handoff。
WORKFLOW = (
    ("ingest", "导入规格、代码和测试到 seekdb"),
    ("baseline", "修改前运行目标功能测试，确认因功能未实现而失败"),
    ("context", "从 seekdb 检索并生成开发上下文"),
    ("agent", "MiniMax 根据上下文完成代码修改"),
    ("verify", "修改后运行目标功能测试和回归测试，全部通过"),
)
STEP_TITLES = dict(WORKFLOW)
STEP_SCHEMA = "sddctx.step.v1"


# ---- Server 进程 ---------------------------------------------------------------------------------


def _env_file(settings: Settings) -> Path:
    path = settings.runtime_dir / "powercontext.env"
    home = settings.powercontext_home
    lines = [
        "# 作者：FDE WorkMate — 由 sddctx 生成，本地回环、无鉴权，只用于本机演示",
        f"POWERCONTEXT_HOME={home}",
        "POWERCONTEXT_SERVER_DATABASE_KIND=seekdb",
        f"POWERCONTEXT_SERVER_DATABASE_PATH={home / 'seekdb'}",
        "POWERCONTEXT_SERVER_HTTP_HOST=127.0.0.1",
        f"POWERCONTEXT_SERVER_HTTP_PORT={settings.powercontext_port}",
        "POWERCONTEXT_SERVER_ACCESS_MODE=disabled",
        "POWERCONTEXT_SERVER_MCP_ENABLED=false",
        "POWERCONTEXT_SERVER_DASHBOARD_ENABLED=false",
        "POWERCONTEXT_SERVER_EXTERNAL_SKILLS='{}'",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _ready(settings: Settings) -> bool:
    try:
        with urllib.request.urlopen(f"{settings.powercontext_url}/health/ready", timeout=2) as response:  # noqa: S310
            return json.loads(response.read()).get("status") == "ready"
    except OSError:
        return False


def server_status(settings: Settings) -> dict[str, Any]:
    pid_file = settings.runtime_dir / "powercontext.pid"
    pid = int(pid_file.read_text()) if pid_file.is_file() else None
    return {"url": settings.powercontext_url, "pid": pid, "ready": _ready(settings)}


def start_server(settings: Settings, timeout: float = 90) -> dict[str, Any]:
    """启动本地 PowerContext Server（若已就绪则复用），存储为 .runtime/powercontext/seekdb。"""

    if _ready(settings):
        return server_status(settings) | {"started": False}
    executable = Path(sys.executable).with_name("powercontext")
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    log = (settings.runtime_dir / "powercontext.log").open("ab")
    process = subprocess.Popen(  # noqa: S603
        [str(executable), "server", "run", "--env-file", str(_env_file(settings))],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    (settings.runtime_dir / "powercontext.pid").write_text(str(process.pid))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"PowerContext Server 启动失败，见 {settings.runtime_dir / 'powercontext.log'}")
        if _ready(settings):
            return server_status(settings) | {"started": True}
        time.sleep(0.5)
    raise RuntimeError("PowerContext Server 启动超时")


def stop_server(settings: Settings) -> dict[str, Any]:
    pid_file = settings.runtime_dir / "powercontext.pid"
    if not pid_file.is_file():
        return {"stopped": False, "reason": "no pid file"}
    pid = int(pid_file.read_text())
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink()
        return {"stopped": False, "reason": "process not running"}
    for _ in range(60):
        if not _ready(settings):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        time.sleep(0.5)
    pid_file.unlink(missing_ok=True)
    return {"stopped": True, "pid": pid}


# ---- 任务状态 ------------------------------------------------------------------------------------


@dataclass
class TaskState:
    """从 PowerContext 读回的任务状态。"""

    scope_id: str
    task_id: str
    handoff: dict[str, Any] | None = None
    handoff_revision: int | None = None
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def done(self) -> list[str]:
        return [name for name, _ in WORKFLOW if self.steps.get(name, {}).get("ok")]

    @property
    def todo(self) -> list[str]:
        return [name for name, _ in WORKFLOW if not self.steps.get(name, {}).get("ok")]

    @property
    def next_step(self) -> str | None:
        return self.todo[0] if self.todo else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "powercontext",
            "scope_id": self.scope_id,
            "task_id": self.task_id,
            "handoff_revision": self.handoff_revision,
            "done": [{"step": name, "title": STEP_TITLES[name], **_brief(self.steps[name])} for name in self.done],
            "todo": [{"step": name, "title": STEP_TITLES[name]} for name in self.todo],
            "next_step": self.next_step,
            "handoff_next_action": (self.handoff or {}).get("next_action", {}).get("text"),
        }


def _brief(record: dict[str, Any]) -> dict[str, Any]:
    keep = ("source_id", "at", "summary", "code_version")
    return {key: record[key] for key in keep if key in record}


class StateStore:
    """一个业务工程对应一个 PowerContext Scope；所有调用都记录到 records/powercontext_log.jsonl。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log_path = settings.records_dir / "powercontext_log.jsonl"

    def _log(self, operation: str, **fields: Any) -> None:
        append_jsonl(self.log_path, {"operation": operation, **fields})

    async def _scope(self, client: PowerContextClient) -> str:
        project = self.settings.project_root.name
        scope = await client.create_scope(
            CreateScopeRequest(
                title=f"sddctx · {project}",
                summary=f"SDD 上下文模块的任务状态：{self.settings.project_root}",
                idempotency_key=f"sddctx:{project}",
            )
        )
        return scope.scope_id

    def run(self, coroutine_factory):  # noqa: ANN001, ANN201
        start_server(self.settings)

        async def runner():  # noqa: ANN202
            async with PowerContextClient(self.settings.powercontext_url, timeout=120) as client:
                scope_id = await self._scope(client)
                return await coroutine_factory(client, scope_id)

        return asyncio.run(runner())

    # -- 写入 --

    def ensure_contract(self, task: Task) -> dict[str, Any]:
        source_id = f"{task.task_id}.contract"

        async def go(client: PowerContextClient, scope_id: str) -> dict[str, Any]:
            try:
                await client.get_source(scope_id, "content", source_id)
                created = False
            except ServerResponseError as error:
                if error.status_code != 404:
                    raise
                await client.create_work_contract(
                    CreateWorkContractRequest.model_validate(
                        {
                            "scope_id": scope_id,
                            "source_id": source_id,
                            "contract": {
                                "schema": "powercontext.work-contract.v1",
                                "trust": "untrusted_input",
                                "objective": f"{task.task_id} {task.title}",
                                "facts": [
                                    {"text": line, "basis": "declared", "evidence": []}
                                    for line in task.change.splitlines()
                                    if line
                                ][:20],
                                "in_scope": [str(self.settings.project_root.name)],
                                "exclusions": ["修改测试文件或预期结果", "业务工程之外的文件"],
                                "completion_criteria": [title for _, title in WORKFLOW],
                                "authorization_notes": ["只在本地业务工程内修改代码并运行测试"],
                                "open_questions": [],
                            },
                        }
                    )
                )
                created = True
            self._log("work_contract", scope_id=scope_id, source_id=source_id, created=created)
            return {"scope_id": scope_id, "source_id": source_id, "created": created}

        return self.run(go)

    def record_step(self, task: Task, step: str, *, ok: bool, summary: str, evidence: dict[str, Any]) -> dict[str, Any]:
        """保存一步的证据 Source，并据此提交新的 Handoff Revision。"""

        async def go(client: PowerContextClient, scope_id: str) -> dict[str, Any]:
            content = {
                "schema": STEP_SCHEMA,
                "task_id": task.task_id,
                "step": step,
                "ok": ok,
                "summary": summary,
                "at": now_iso(),
                **evidence,
            }
            source = await client.create_source(scope_id, CreateSourceRequest(content=content))
            self._log("create_source", scope_id=scope_id, source_id=source.source_id, step=step, ok=ok, summary=summary)
            state = await self._load(client, scope_id, task.task_id)
            revision = await self._commit_handoff(client, scope_id, task, state)
            return {"scope_id": scope_id, "source_id": source.source_id, "handoff_revision": revision}

        return self.run(go)

    def record_outcome(self, task: Task, *, status: str, summary: str) -> dict[str, Any]:
        async def go(client: PowerContextClient, scope_id: str) -> dict[str, Any]:
            state = await self._load(client, scope_id, task.task_id)
            checks = [
                {
                    "name": STEP_TITLES[name],
                    "status": "passed",
                    "details": state.steps[name]["summary"],
                    "basis": "verified",
                    "evidence": [_citation(state.steps[name]["source_id"])],
                }
                for name in state.done
            ]
            receipt = await client.record_task_outcome(
                RecordTaskOutcomeRequest.model_validate(
                    {
                        "scope_id": scope_id,
                        "source_id": f"{task.task_id}.outcome.{int(time.time())}",
                        "outcome": {
                            "schema": "powercontext.task-outcome.v1",
                            "trust": "untrusted_observation",
                            "objective": f"{task.task_id} {task.title}",
                            "status": status,
                            "summary": summary,
                            "observations": [
                                {"text": f"{STEP_TITLES[name]}：{state.steps[name]['summary']}", "basis": "verified", "evidence": [_citation(state.steps[name]["source_id"])]}
                                for name in state.done
                            ],
                            "checks": checks,
                            "produced_artifacts": [],
                            "remaining_work": [STEP_TITLES[name] for name in state.todo],
                        },
                    }
                )
            )
            source = receipt.source.model_dump(mode="json")
            self._log("record_task_outcome", scope_id=scope_id, status=status, source=source)
            return {"scope_id": scope_id, "status": status, "source": source}

        return self.run(go)

    async def _commit_handoff(self, client: PowerContextClient, scope_id: str, task: Task, state: TaskState) -> int:
        done = [
            {
                "text": f"[{name}] {STEP_TITLES[name]}：{state.steps[name]['summary']}",
                "basis": "verified",
                "evidence": [_citation(state.steps[name]["source_id"])],
            }
            for name in state.done
        ]
        next_step = state.next_step
        handoff = {
            "schema": "powercontext.current-work-handoff.v1",
            "trust": "untrusted_input",
            "objective": f"{task.task_id} {task.title}",
            "state": done or [{"text": "尚未完成任何步骤", "basis": "declared", "evidence": []}],
            "disposition": "continuable" if next_step else "complete",
            "next_action": {
                "text": f"[{next_step}] {STEP_TITLES[next_step]}" if next_step else "任务已完成，无后续动作",
                "basis": "declared",
                "evidence": [],
            },
            "omissions": [f"[{name}] {STEP_TITLES[name]}" for name in state.todo],
        }
        prepared = await client.handoff_current_work(
            HandoffCurrentWorkRequest.model_validate(
                {"scope_id": scope_id, "source_id": f"{task.task_id}.handoff.{time.time_ns()}", "handoff": handoff}
            )
        )
        committed = await client.commit_handoff(CommitHandoffRequest(scope_id=scope_id, handoff=prepared.handoff))
        self._log(
            "commit_handoff",
            scope_id=scope_id,
            reference=committed.reference.model_dump(mode="json"),
            done=state.done,
            todo=state.todo,
        )
        return committed.reference.revision

    # -- 读取 --

    def load(self, task: Task) -> TaskState:
        async def go(client: PowerContextClient, scope_id: str) -> TaskState:
            state = await self._load(client, scope_id, task.task_id)
            self._log(
                "load_state",
                scope_id=scope_id,
                handoff_revision=state.handoff_revision,
                done=state.done,
                todo=state.todo,
                next_step=state.next_step,
            )
            return state

        return self.run(go)

    async def _load(self, client: PowerContextClient, scope_id: str, task_id: str) -> TaskState:
        state = TaskState(scope_id=scope_id, task_id=task_id)
        try:
            resolution = await client.continue_handoff(ContinueHandoffRequest(scope_id=scope_id, selection="latest"))
            if resolution.content is not None:
                state.handoff = resolution.content.model_dump(mode="json", by_alias=True)
                state.handoff_revision = resolution.selected_revision.revision if resolution.selected_revision else None
        except ServerResponseError as error:
            if error.status_code not in (404, 409):
                raise
        cursor: str | None = None
        while True:
            page = await client.list_sources(scope_id, ListSourcesRequest(limit=100, cursor=cursor))
            for record in page.items:
                content = record.content
                if isinstance(content, dict) and content.get("schema") == STEP_SCHEMA and content.get("task_id") == task_id:
                    # 同一步骤保留最新一条记录（position 递增）。
                    state.steps[content["step"]] = {**content, "source_id": record.source_id, "position": record.position}
            if not page.next_cursor:
                break
            cursor = page.next_cursor
        return state


def _citation(source_id: str) -> dict[str, Any]:
    return {"kind": "source", "source_ref": {"name": "content", "source_id": source_id}}

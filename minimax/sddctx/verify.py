# 作者：FDE WorkMate
"""运行功能测试与回归测试，记录命令、退出码、逐条结果和对应代码版本。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from sddctx.config import Settings
from sddctx.parsers import ProjectLayout
from sddctx.records import code_version, now_iso, write_json

# 修改前的目标测试必须因功能未实现而失败；以下异常说明测试根本没跑起来，属于环境错误。
ENVIRONMENT_ERRORS = ("ModuleNotFoundError", "ImportError", "SyntaxError", "fixture '", "FileNotFoundError")


def run_tests(settings: Settings, label: str, targets: list[str], *, run_dir: Path) -> dict[str, Any]:
    """在业务工程根目录运行一组测试文件，返回结构化结果并写出日志。"""

    layout = ProjectLayout.load(settings.project_root)
    command = [sys.executable if part == "python" else part for part in layout.test_command]
    junit = run_dir / f"{label}.junit.xml"
    full = [*command, f"--junitxml={junit}", *targets]
    started = time.perf_counter()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(full, cwd=settings.project_root, capture_output=True, text=True, env=env, check=False)  # noqa: S603
    cases = _parse_junit(junit)
    record = {
        "label": label,
        "at": now_iso(),
        "cwd": str(settings.project_root.relative_to(settings.repo_root)),
        "command": " ".join([Path(full[0]).name, *full[1:]]).replace(str(run_dir) + "/", ""),
        "exit_code": result.returncode,
        "duration_s": round(time.perf_counter() - started, 2),
        "code_version": code_version(settings.repo_root, settings.project_root),
        "total": len(cases),
        "passed": sum(case["outcome"] == "passed" for case in cases),
        "failed": sum(case["outcome"] == "failed" for case in cases),
        "errors": sum(case["outcome"] == "error" for case in cases),
        "environment_errors": [case["id"] for case in cases if case.get("environment_error")],
        "cases": cases,
    }
    (run_dir / f"{label}.log").write_text(
        f"$ (cd {record['cwd']} && {record['command']})\n{result.stdout}{result.stderr}\nexit_code={result.returncode}\n",
        encoding="utf-8",
    )
    write_json(run_dir / f"{label}.json", record)
    return record


def _parse_junit(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    cases: list[dict[str, Any]] = []
    for case in ET.parse(path).getroot().iter("testcase"):  # noqa: S314 - 本地 pytest 生成的文件
        file = (case.get("classname") or "").replace(".", "/")
        node = f"{file}.py::{case.get('name')}"
        outcome, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            element = case.find(tag)
            if element is not None:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = (element.get("message") or element.text or "").strip().splitlines()[0][:300] if (element.get("message") or element.text) else ""
                break
        cases.append(
            {
                "id": node,
                "outcome": outcome,
                "message": message,
                "environment_error": outcome == "error" or any(marker in message for marker in ENVIRONMENT_ERRORS),
            }
        )
    return cases

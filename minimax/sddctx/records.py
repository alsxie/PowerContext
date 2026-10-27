# 作者：FDE WorkMate
"""技术使用记录：数据库查询、PowerContext 读写、测试运行和 MiniMax 调用都追加到 records/ 下的 JSONL。"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": now_iso(), **record}, ensure_ascii=False, default=str) + "\n")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def code_version(repo_root: Path, project_root: Path) -> dict[str, Any]:
    """返回 git HEAD、工作区是否有未提交改动，以及业务工程源码的内容摘要。"""

    def git(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True, check=False)
        return result.stdout.strip()

    from sddctx.parsers import tree_digest

    return {
        "git_head": git("rev-parse", "HEAD") or None,
        "git_dirty_paths": [line[3:] for line in git("status", "--porcelain", "--", str(project_root)).splitlines()],
        "project_digest": tree_digest(project_root),
    }

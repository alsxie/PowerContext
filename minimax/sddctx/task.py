# 作者：FDE WorkMate
"""开发任务卡解析。任务卡是 Markdown：一级标题为任务编号与名称，二级标题划分“要改什么 / 必须保持 / 验证”。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TITLE = re.compile(r"^#\s+(\S+)\s*(.*)$")
SECTION = re.compile(r"^##\s+(.+?)\s*$")

# 二级标题关键词到段落角色；未识别的段落归入 change。
ROLE_KEYWORDS = (
    ("preserve", ("保持", "不变", "preserve", "keep", "compatib")),
    ("verify", ("验证", "verify", "test")),
    ("change", ("改什么", "要改", "变更", "change", "goal")),
)


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    path: Path
    change: str
    preserve: str
    verify: str

    @property
    def text(self) -> str:
        return "\n".join(part for part in (self.title, self.change, self.preserve, self.verify) if part)


def _role(heading: str) -> str:
    lowered = heading.lower()
    for role, keywords in ROLE_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return role
    return "change"


def load_task(path: Path) -> Task:
    lines = path.read_text(encoding="utf-8").splitlines()
    task_id, title = path.stem.upper(), path.stem
    sections: dict[str, list[str]] = {"change": [], "preserve": [], "verify": []}
    current = "change"
    for line in lines:
        if (match := TITLE.match(line)) and not line.startswith("##"):
            task_id, title = match.group(1), match.group(2) or match.group(1)
            continue
        if match := SECTION.match(line):
            current = _role(match.group(1))
            continue
        if line.strip() and not line.startswith(("作者：", "Author：")):
            sections[current].append(line.strip())
    return Task(
        task_id=task_id,
        title=title,
        path=path.resolve(),
        change="\n".join(sections["change"]),
        preserve="\n".join(sections["preserve"]),
        verify="\n".join(sections["verify"]),
    )

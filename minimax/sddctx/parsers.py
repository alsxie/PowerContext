# 作者：FDE WorkMate
"""把业务工程的规格、代码和测试解析为可检索的工程资料条目。

条目只描述源文件中真实存在的内容：每条都带相对路径、起止行号和内容哈希，供上下文输出引用。
"""

from __future__ import annotations

import ast
import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SPEC_HEADING = re.compile(r"^###\s+([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\s+(.+?)\s*$")
SPEC_META = re.compile(r"^-\s*([^:：]+)\s*[:：]\s*(.*)$")
SPEC_ID = re.compile(r"\b([A-Z][A-Z0-9]*-[A-Z0-9]+(?:-[0-9]+)?)\b")
BACKTICK = re.compile(r"`([^`]+)`")

# 规格元数据的中英文键名映射到统一字段。
META_KEYS = {
    "状态": "status",
    "status": "status",
    "关联": "links",
    "links": "links",
    "取代": "supersedes",
    "supersedes": "supersedes",
    "取代者": "superseded_by",
    "superseded_by": "superseded_by",
    "依据": "based_on",
    "based_on": "based_on",
}


@dataclass
class Item:
    """一条工程资料。kind 为 spec、code 或 test。"""

    kind: str
    item_id: str
    title: str
    path: str
    line_start: int
    line_end: int
    body: str
    status: str = "active"
    symbol_kind: str = ""
    links: list[str] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.path}:{self.item_id}"

    @property
    def content_hash(self) -> str:
        payload = "\x1f".join([self.title, self.status, self.body, ",".join(sorted(self.links))])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def search_text(self) -> str:
        words = " ".join(split_identifier(link) for link in self.links)
        return f"{self.item_id} {split_identifier(self.item_id)} {self.title} {words}\n{self.body}"


@dataclass(frozen=True)
class ProjectLayout:
    """业务工程的资料位置，来自工程根目录的 sddctx.toml。"""

    spec_globs: tuple[str, ...]
    code_globs: tuple[str, ...]
    test_globs: tuple[str, ...]
    test_command: tuple[str, ...]

    @classmethod
    def load(cls, project_root: Path) -> ProjectLayout:
        config_path = project_root / "sddctx.toml"
        data = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        layout = data.get("layout", {})
        return cls(
            spec_globs=tuple(layout.get("specs", ["specs/**/*.md"])),
            code_globs=tuple(layout.get("code", ["**/*.py"])),
            test_globs=tuple(layout.get("tests", ["tests/**/test_*.py"])),
            test_command=tuple(data.get("verify", {}).get("command", ["python", "-m", "pytest", "-q"])),
        )


def split_identifier(name: str) -> str:
    """把 OrderQueryService.list_orders 拆成可被全文检索命中的词：order query service list orders。"""

    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return " ".join(part.lower() for part in re.split(r"[^A-Za-z0-9]+", spaced) if part)


def collect_files(root: Path, globs: tuple[str, ...]) -> list[Path]:
    found: set[Path] = set()
    for pattern in globs:
        found.update(path for path in root.glob(pattern) if path.is_file())
    return sorted(path for path in found if not any(part.startswith(".") for part in path.relative_to(root).parts))


def parse_specs(root: Path, path: Path) -> list[Item]:
    rel = path.relative_to(root).as_posix()
    lines = path.read_text(encoding="utf-8").splitlines()
    headings = [(index, match) for index, line in enumerate(lines) if (match := SPEC_HEADING.match(line))]
    items: list[Item] = []
    for position, (start, match) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        meta: dict[str, str] = {}
        body_lines: list[str] = []
        for line in lines[start + 1 : end]:
            meta_match = SPEC_META.match(line.strip()) if not body_lines else None
            raw_key = meta_match.group(1).strip() if meta_match else ""
            key = META_KEYS.get(raw_key) or META_KEYS.get(raw_key.lower(), "")
            if meta_match and key:
                meta[key] = meta_match.group(2).strip()
            elif line.strip() or body_lines:
                body_lines.append(line)
        body = "\n".join(body_lines).strip()
        links = BACKTICK.findall(meta.get("links", "")) + BACKTICK.findall(body)
        spec_refs = [ref for ref in SPEC_ID.findall(" ".join([body, *meta.values()])) if ref != match.group(1)]
        items.append(
            Item(
                kind="spec",
                item_id=match.group(1),
                title=match.group(2),
                path=rel,
                line_start=start + 1,
                line_end=end,
                body=body,
                status=meta.get("status", "active").lower(),
                links=_unique(links + spec_refs),
                meta=meta,
            )
        )
    return items


def parse_code(root: Path, path: Path) -> list[Item]:
    rel = path.relative_to(root).as_posix()
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source, filename=rel)
    items = [
        Item(
            kind="code",
            item_id=rel,
            title=ast.get_docstring(tree) or rel,
            path=rel,
            line_start=1,
            line_end=max(len(lines), 1),
            body=source,
            symbol_kind="module",
            links=_referenced_names(tree),
        )
    ]

    def visit(nodes: list[ast.stmt], prefix: str) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{node.name}"
                kind = "class" if isinstance(node, ast.ClassDef) else ("method" if prefix else "function")
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                end = node.end_lineno or node.lineno
                items.append(
                    Item(
                        kind="code",
                        item_id=name,
                        title=ast.get_docstring(node) or name,
                        path=rel,
                        line_start=start,
                        line_end=end,
                        body="\n".join(lines[start - 1 : end]),
                        symbol_kind=kind,
                        links=_referenced_names(node),
                    )
                )
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{name}.")

    visit(tree.body, "")
    return items


def parse_tests(root: Path, path: Path) -> list[Item]:
    rel = path.relative_to(root).as_posix()
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source, filename=rel)
    module_doc = ast.get_docstring(tree) or ""
    module_refs = SPEC_ID.findall(module_doc)
    items: list[Item] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test"):
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            end = node.end_lineno or node.lineno
            body = "\n".join(lines[start - 1 : end])
            items.append(
                Item(
                    kind="test",
                    item_id=f"{rel}::{node.name}",
                    title=module_doc.splitlines()[0] if module_doc else node.name,
                    path=rel,
                    line_start=start,
                    line_end=end,
                    body=body,
                    symbol_kind="test",
                    links=_unique(module_refs + SPEC_ID.findall(body) + _referenced_names(node)),
                )
            )
    return items


def parse_project(root: Path, layout: ProjectLayout) -> list[Item]:
    tests = set(collect_files(root, layout.test_globs))
    items: list[Item] = []
    for path in collect_files(root, layout.spec_globs):
        items.extend(parse_specs(root, path))
    for path in collect_files(root, layout.code_globs):
        if path not in tests and "tests" not in path.relative_to(root).parts:
            items.extend(parse_code(root, path))
    for path in sorted(tests):
        items.extend(parse_tests(root, path))
    return items


def tree_digest(root: Path) -> str:
    """业务工程中全部 .py 与 .md 文件的内容摘要，用于标记测试运行对应的代码版本。"""

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and path.suffix in {".py", ".md", ".toml", ".ini"} and "__pycache__" not in rel.parts:
            digest.update(rel.as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _referenced_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            names.append(child.attr)
        elif isinstance(child, ast.Name):
            names.append(child.id)
        elif isinstance(child, ast.keyword) and child.arg:
            names.append(child.arg)
        elif isinstance(child, ast.ImportFrom):
            names.extend(alias.name for alias in child.names)
    return _unique(name for name in names if len(name) > 2 and not name.startswith("__"))


def _unique(values) -> list[str]:  # noqa: ANN001
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)

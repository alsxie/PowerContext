# 作者：FDE WorkMate
"""工程资料库：嵌入式 seekdb 保存规格、代码、测试条目及其引用关系，并提供全文检索。

每条 SQL 都记录到 records/db_queries.jsonl，便于核对上下文中的条目确实来自数据库查询。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pylibseekdb
import pymysql

from sddctx.parsers import Item
from sddctx.records import append_jsonl, now_iso

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS sdd_items (
        item_key CHAR(40) PRIMARY KEY,
        kind VARCHAR(8) NOT NULL,
        item_id VARCHAR(255) COLLATE utf8mb4_bin NOT NULL,
        title VARCHAR(1024) NOT NULL,
        path VARCHAR(255) NOT NULL,
        line_start INT NOT NULL,
        line_end INT NOT NULL,
        status VARCHAR(16) NOT NULL,
        symbol_kind VARCHAR(16) NOT NULL,
        content_hash CHAR(16) NOT NULL,
        body TEXT NOT NULL,
        search_text TEXT NOT NULL,
        meta TEXT NOT NULL,
        import_id CHAR(32) NOT NULL,
        KEY idx_kind_item (kind, item_id),
        FULLTEXT KEY ft_search (search_text) WITH PARSER ngram
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sdd_links (
        item_key CHAR(40) NOT NULL,
        target VARCHAR(255) COLLATE utf8mb4_bin NOT NULL,
        PRIMARY KEY (item_key, target),
        KEY idx_target (target)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sdd_imports (
        import_id CHAR(32) PRIMARY KEY,
        seq BIGINT NOT NULL,
        imported_at VARCHAR(40) NOT NULL,
        project_root VARCHAR(1024) NOT NULL,
        project_digest CHAR(16) NOT NULL,
        changes TEXT NOT NULL
    )
    """,
)

ITEM_COLUMNS = "item_key, kind, item_id, title, path, line_start, line_end, status, symbol_kind, content_hash, body, meta"


def _item_key(item: Item) -> str:
    return hashlib.sha1(item.key.encode("utf-8")).hexdigest()  # noqa: S324 - 仅用作稳定主键


class KnowledgeStore:
    """seekdb 上的工程资料表。一个进程同一时间只打开一个实例。"""

    def __init__(self, connection: pymysql.connections.Connection, query_log: Path) -> None:
        self._connection = connection
        self._query_log = query_log

    @classmethod
    @contextmanager
    def open(cls, db_path: Path, query_log: Path) -> Iterator[KnowledgeStore]:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        instance = pylibseekdb.open(db_dir=str(db_path))
        options = dict(instance.connection_options())
        options.pop("database", None)
        connection = pymysql.connect(**options, database="test", autocommit=True, charset="utf8mb4")
        try:
            store = cls(connection, query_log)
            for statement in SCHEMA:
                store._execute(statement, purpose="schema")
            yield store
        finally:
            connection.close()
            instance.close()

    def _execute(self, sql: str, params: Sequence[Any] | None = None, *, purpose: str) -> list[dict[str, Any]]:
        started = time.perf_counter()
        with self._connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(sql, params)
            rows = list(cursor.fetchall()) if cursor.description else []
            affected = cursor.rowcount
        if purpose != "schema":
            append_jsonl(
                self._query_log,
                {
                    "purpose": purpose,
                    "sql": " ".join(sql.split()),
                    "params": [p if not isinstance(p, str) or len(p) < 160 else p[:160] + "..." for p in (params or [])],
                    "rows": len(rows) if rows else affected,
                    "result_ids": [row.get("item_id") for row in rows if "item_id" in row][:30],
                    "ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
        return rows

    # ---- 导入 -------------------------------------------------------------------------------------

    def sync(self, items: Iterable[Item], *, project_root: Path, project_digest: str) -> dict[str, Any]:
        """按内容哈希增量同步：新增或变化的条目重写，源文件中已不存在的条目删除。"""

        import_id = uuid.uuid4().hex
        incoming = {_item_key(item): item for item in items}
        existing = {
            row["item_key"]: row
            for row in self._execute(
                "SELECT item_key, item_id, kind, content_hash, path, line_start, line_end FROM sdd_items", purpose="import.load_hashes"
            )
        }
        changes: dict[str, list[str]] = {"added": [], "updated": [], "moved": [], "removed": [], "unchanged": []}
        for key, item in incoming.items():
            previous = existing.get(key)
            if previous and previous["content_hash"] == item.content_hash:
                location = (item.path, item.line_start, item.line_end)
                if (previous["path"], previous["line_start"], previous["line_end"]) == location:
                    changes["unchanged"].append(item.item_id)
                else:
                    # 内容不变但位置移动（同文件其它代码增删）：只更新来源行号，保证上下文中的行号与文件一致。
                    changes["moved"].append(f"{item.kind}:{item.item_id}")
                    self._execute(
                        "UPDATE sdd_items SET path = %s, line_start = %s, line_end = %s, import_id = %s WHERE item_key = %s",
                        [*location, import_id, key],
                        purpose="import.update_location",
                    )
                continue
            changes["updated" if previous else "added"].append(f"{item.kind}:{item.item_id}")
            self._execute("DELETE FROM sdd_items WHERE item_key = %s", [key], purpose="import.replace_item")
            self._execute("DELETE FROM sdd_links WHERE item_key = %s", [key], purpose="import.replace_links")
            self._execute(
                "INSERT INTO sdd_items (item_key, kind, item_id, title, path, line_start, line_end, status, symbol_kind,"
                " content_hash, body, search_text, meta, import_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [
                    key, item.kind, item.item_id, item.title[:1000], item.path, item.line_start, item.line_end,
                    item.status, item.symbol_kind, item.content_hash, item.body, item.search_text,
                    json.dumps(item.meta, ensure_ascii=False), import_id,
                ],
                purpose="import.insert_item",
            )
            for target in item.links:
                self._execute(
                    "INSERT INTO sdd_links (item_key, target) VALUES (%s, %s)", [key, target[:255]], purpose="import.insert_link"
                )
        for key, row in existing.items():
            if key not in incoming:
                changes["removed"].append(f"{row['kind']}:{row['item_id']}")
                self._execute("DELETE FROM sdd_items WHERE item_key = %s", [key], purpose="import.remove_item")
                self._execute("DELETE FROM sdd_links WHERE item_key = %s", [key], purpose="import.remove_links")
        summary = {key: value for key, value in changes.items() if key != "unchanged"} | {"unchanged": len(changes["unchanged"])}
        self._execute(
            "INSERT INTO sdd_imports (import_id, seq, imported_at, project_root, project_digest, changes) VALUES (%s,%s,%s,%s,%s,%s)",
            [import_id, time.time_ns(), now_iso(), str(project_root), project_digest, json.dumps(summary, ensure_ascii=False)],
            purpose="import.record",
        )
        return {"import_id": import_id, "items": len(incoming), **summary}

    # ---- 查询 -------------------------------------------------------------------------------------

    def search(self, kind: str, query: str, *, limit: int = 10, purpose: str) -> list[dict[str, Any]]:
        """全文检索某类条目，按 seekdb 相关度降序返回，score 为 MATCH ... AGAINST 的得分。"""

        return self._execute(
            f"SELECT {ITEM_COLUMNS}, MATCH(search_text) AGAINST(%s IN NATURAL LANGUAGE MODE) AS score FROM sdd_items"
            " WHERE kind = %s AND MATCH(search_text) AGAINST(%s IN NATURAL LANGUAGE MODE)"
            " ORDER BY score DESC, item_id LIMIT %s",
            [query, kind, query, limit],
            purpose=purpose,
        )

    def get(self, kind: str, item_ids: Sequence[str], *, purpose: str) -> list[dict[str, Any]]:
        if not item_ids:
            return []
        marks = ",".join(["%s"] * len(item_ids))
        return self._execute(
            f"SELECT {ITEM_COLUMNS} FROM sdd_items WHERE kind = %s AND item_id IN ({marks}) ORDER BY item_id",
            [kind, *item_ids],
            purpose=purpose,
        )

    def get_by_member_name(self, kind: str, names: Sequence[str], *, purpose: str) -> list[dict[str, Any]]:
        """按“类名.成员名”的末段匹配条目，例如 list_orders 匹配 OrderQueryService.list_orders。"""

        if not names:
            return []
        clauses = " OR ".join(["item_id LIKE %s"] * len(names))
        return self._execute(
            f"SELECT {ITEM_COLUMNS} FROM sdd_items WHERE kind = %s AND ({clauses}) ORDER BY item_id",
            [kind, *(f"%.{name}" for name in names)],
            purpose=purpose,
        )

    def referencing(self, kind: str, targets: Sequence[str], *, purpose: str) -> list[dict[str, Any]]:
        """返回引用了任一 target（符号名、规格编号或路径）的条目，并附带命中的 target 列表。"""

        if not targets:
            return []
        marks = ",".join(["%s"] * len(targets))
        return self._execute(
            f"SELECT {', '.join('i.' + c.strip() for c in ITEM_COLUMNS.split(','))},"
            " GROUP_CONCAT(l.target ORDER BY l.target SEPARATOR ',') AS matched"
            f" FROM sdd_links l JOIN sdd_items i ON i.item_key = l.item_key"
            f" WHERE i.kind = %s AND l.target IN ({marks})"
            f" GROUP BY {', '.join('i.' + c.strip() for c in ITEM_COLUMNS.split(','))} ORDER BY i.path, i.line_start",
            [kind, *targets],
            purpose=purpose,
        )

    def latest_import(self) -> dict[str, Any] | None:
        rows = self._execute(
            "SELECT import_id, imported_at, project_digest, changes FROM sdd_imports ORDER BY seq DESC LIMIT 1",
            purpose="context.latest_import",
        )
        return rows[0] if rows else None

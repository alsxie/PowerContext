#!/usr/bin/env python3
# 作者：FDE WorkMate
"""清空某位旅人的小本本：把该玩家 Scope 下所有 active 记忆标记为 forgotten。

PowerContext 的遗忘是生命周期状态变更而非物理删除，历史仍可在 /账本 的「已一笔勾销」里看到。
用法：python3 scripts/reset_player.py 阿土
环境变量：POWERCONTEXT_HOST_URL（默认 http://127.0.0.1:18765）、POWERCONTEXT_TOKEN
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify import PC_TOKEN, PC_URL, post_json, scope_of  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2 or not PC_TOKEN:
        print("用法：POWERCONTEXT_TOKEN=... python3 scripts/reset_player.py <玩家名号>", file=sys.stderr)
        return 2
    player = " ".join(sys.argv[1].split())[:20]
    scope_id = scope_of(player)
    total = 0
    while True:  # 每次只列 100 条，循环直到账本清空
        entries = post_json(f"{PC_URL}/v1/memory/entries/list", {"scope_id": scope_id, "limit": 100},
                            PC_TOKEN)["entries"]
        if not entries:
            break
        total += len(entries)
        for entry in entries:
            post_json(f"{PC_URL}/v1/atomic-memory/lifecycle",
                          {"scope_id": scope_id, "target": {"artifact": entry["artifact"],
                                                            "state_version": entry["state_version"]},
                           "state": "forgotten"}, PC_TOKEN)
    print(f"已为「{player}」（{scope_id}）勾销 {total} 条记忆")
    return 0


if __name__ == "__main__":
    sys.exit(main())

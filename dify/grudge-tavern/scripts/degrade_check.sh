#!/usr/bin/env bash
# 作者：FDE WorkMate
# 降级演练：临时停掉 PowerContext，确认 Chatflow 仍然成功返回、老焦照常说话且回执提示「连不上」，随后恢复服务。
# 用法：DIFY_APP_API_KEY=... bash scripts/degrade_check.sh [输出文件]
set -euo pipefail

DIFY_URL="${DIFY_URL:-http://127.0.0.1:18080}"
OUT="${1:-/dev/stdout}"
CONTAINER="${POWERCONTEXT_CONTAINER:-grudge-tavern-powercontext}"
PORT="${POWERCONTEXT_HOST_PORT:-18765}"
: "${DIFY_APP_API_KEY:?需要 DIFY_APP_API_KEY}"

restore() {
  docker start "$CONTAINER" >/dev/null
  for _ in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:${PORT}/health/ready" >/dev/null 2>&1 && return 0
    sleep 5
  done
  echo "PowerContext 恢复超时" >&2
  return 1
}
trap restore EXIT

docker stop "$CONTAINER" >/dev/null
payload='{"inputs":{"player_name":"降级演练"},"query":"老焦，来杯麦酒。","response_mode":"blocking","user":"degrade-check"}'
response="$(curl -sS -m 240 -X POST "$DIFY_URL/v1/chat-messages" \
  -H "Authorization: Bearer $DIFY_APP_API_KEY" -H 'Content-Type: application/json' -d "$payload")"

RESPONSE="$response" python3 - "$OUT" <<'PY'
import json, os, sys
data = json.loads(os.environ["RESPONSE"])
answer = data.get("answer", "")
ok = bool(answer) and "连不上" in answer and data.get("code") is None
report = {"check": "PowerContext 停机时 Chatflow 降级", "passed": ok,
          "conversation_id": data.get("conversation_id"), "answer": answer}
text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
open(sys.argv[1], "w", encoding="utf-8").write(text) if sys.argv[1] != "/dev/stdout" else print(text)
print("DEGRADE CHECK", "PASSED" if ok else "FAILED", file=sys.stderr)
sys.exit(0 if ok else 1)
PY

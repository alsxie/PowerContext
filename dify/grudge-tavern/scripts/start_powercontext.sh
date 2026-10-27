#!/usr/bin/env bash
# 作者：FDE WorkMate
# 构建并启动 PowerContext Server（嵌入式 seekdb 存储），接入 Dify 的 docker 网络。
# 首次运行会生成随机 Token 写入 powercontext/powercontext.env（权限 600），并同步到 secrets 文件。
# 用法：bash scripts/start_powercontext.sh   （需先执行 setup_dify.sh，使 docker_default 网络存在）
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PC_DIR="$HERE/powercontext"
SECRETS_FILE="${GRUDGE_SECRETS_FILE:-$HOME/.grudge-tavern/secrets.env}"
PORT="${POWERCONTEXT_HOST_PORT:-18765}"
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

log() { printf '[powercontext] %s\n' "$*"; }

docker network inspect "${DIFY_DOCKER_NETWORK:-docker_default}" >/dev/null 2>&1 \
  || { log "找不到 Dify 网络 ${DIFY_DOCKER_NETWORK:-docker_default}，请先启动 Dify" >&2; exit 1; }

if [[ ! -f "$PC_DIR/powercontext.env" ]]; then
  umask 077
  sed -e "s|^POWERCONTEXT_SERVER_AUTH_TOKEN=.*|POWERCONTEXT_SERVER_AUTH_TOKEN=$(openssl rand -hex 32)|" \
      -e "s|^POWERCONTEXT_SERVER_CURSOR_SIGNING_SECRET=.*|POWERCONTEXT_SERVER_CURSOR_SIGNING_SECRET=$(openssl rand -hex 32)|" \
      "$PC_DIR/powercontext.env.example" >"$PC_DIR/powercontext.env"
  log "已生成 powercontext/powercontext.env"
fi
token="$(grep '^POWERCONTEXT_SERVER_AUTH_TOKEN=' "$PC_DIR/powercontext.env" | cut -d= -f2-)"
mkdir -p "$(dirname "$SECRETS_FILE")" && touch "$SECRETS_FILE" && chmod 600 "$SECRETS_FILE"
{ grep -v '^POWERCONTEXT_TOKEN=' "$SECRETS_FILE" || true; printf 'POWERCONTEXT_TOKEN=%s\n' "$token"; } >"$SECRETS_FILE.tmp"
mv "$SECRETS_FILE.tmp" "$SECRETS_FILE" && chmod 600 "$SECRETS_FILE"

cd "$PC_DIR"
docker compose up -d --build
log "等待 /health/ready（首次初始化嵌入式 seekdb 约需 1~2 分钟）"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/health/ready" >/dev/null 2>&1; then
    log "PowerContext 已就绪：宿主机 http://127.0.0.1:${PORT}，Dify 内 http://powercontext:8000"
    exit 0
  fi
  sleep 5
done
log "PowerContext 未就绪，请执行 docker logs grudge-tavern-powercontext 排查" >&2
exit 1

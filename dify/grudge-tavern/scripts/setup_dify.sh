#!/usr/bin/env bash
# 作者：FDE WorkMate
# 准备并启动 Dify 1.18.0（官方 docker compose），只对 .env 做最小改动：
#   - 端口只绑 127.0.0.1（Web 经 SSH 隧道访问）
#   - 向量库用 seekdb，去掉 collaboration profile
#   - ssrf_proxy 放行容器名 powercontext；插件依赖走 PyPI 镜像
#   - 所有密钥随机生成，不使用官方示例默认值
# 用法：sudo DIFY_DIR=/opt/dify bash scripts/setup_dify.sh
# 可选：DOCKER_MIRROR=docker.1ms.run 先从镜像站拉取再打回原名；git 走代理时设置 HTTPS_PROXY 或 git config http.proxy。
set -euo pipefail

DIFY_VERSION="${DIFY_VERSION:-1.18.0}"
DIFY_DIR="${DIFY_DIR:-/opt/dify}"
DIFY_HTTP_PORT="${DIFY_HTTP_PORT:-18080}"
PIP_MIRROR="${PIP_MIRROR_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
SECRETS_FILE="${GRUDGE_SECRETS_FILE:-$HOME/.grudge-tavern/secrets.env}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

log() { printf '[setup-dify] %s\n' "$*"; }
rand() { openssl rand -hex "${1:-24}"; }

set_env() {  # set_env KEY VALUE —— 存在则替换，不存在则追加
  local key="$1" value="$2" file="$DIFY_DIR/docker/.env"
  if grep -q "^${key}=" "$file"; then
    KEY="$key" VALUE="$value" python3 - "$file" <<'PY'
import os, sys
path, key, value = sys.argv[1], os.environ["KEY"], os.environ["VALUE"]
lines = open(path, encoding="utf-8").read().splitlines()
lines = [f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines]
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
  else
    printf '%s=%s\n' "$key" "$value" >>"$file"
  fi
}

if [[ ! -f "$DIFY_DIR/docker/docker-compose.yaml" ]]; then
  log "稀疏克隆 Dify $DIFY_VERSION 的 docker/ 目录"
  tmp="$(mktemp -d)"
  git clone -q --depth 1 --filter=blob:none --sparse -b "$DIFY_VERSION" https://github.com/langgenius/dify.git "$tmp/dify"
  git -C "$tmp/dify" sparse-checkout set docker
  mkdir -p "$DIFY_DIR"
  mv "$tmp/dify/docker" "$DIFY_DIR/docker"
  rm -rf "$tmp"
fi

cd "$DIFY_DIR/docker"
if [[ ! -f .env ]]; then
  cp .env.example .env
  mkdir -p "$(dirname "$SECRETS_FILE")" && chmod 700 "$(dirname "$SECRETS_FILE")"
  init_password="Init-$(rand 8)"
  set_env SECRET_KEY "$(rand 32)"
  set_env INIT_PASSWORD "$init_password"
  set_env DIFY_AGENT_API_TOKEN "$(rand 24)"
  # agent_backend 要求 base64url 编码的 32 字节密钥
  set_env DIFY_AGENT_SERVER_SECRET_KEY "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  shellctl_token="$(rand 24)"
  set_env DIFY_AGENT_SHELLCTL_AUTH_TOKEN "$shellctl_token"
  set_env SHELLCTL_AUTH_TOKEN "$shellctl_token"
  touch "$SECRETS_FILE" && chmod 600 "$SECRETS_FILE"
  grep -v '^DIFY_INIT_PASSWORD=' "$SECRETS_FILE" >"$SECRETS_FILE.tmp" || true
  printf 'DIFY_INIT_PASSWORD=%s\n' "$init_password" >>"$SECRETS_FILE.tmp"
  mv "$SECRETS_FILE.tmp" "$SECRETS_FILE" && chmod 600 "$SECRETS_FILE"
  log "已生成随机密钥，初始化密码写入 $SECRETS_FILE"
fi
set_env VECTOR_STORE seekdb
set_env EXPOSE_NGINX_PORT "127.0.0.1:${DIFY_HTTP_PORT}"
set_env EXPOSE_NGINX_SSL_PORT "127.0.0.1:$((DIFY_HTTP_PORT + 363))"
set_env COMPOSE_PROFILES '${VECTOR_STORE:-weaviate},${DB_TYPE:-postgresql}'
set_env SSRF_PROXY_ALLOW_PRIVATE_DOMAINS powercontext
set_env PIP_MIRROR_URL "$PIP_MIRROR"
cp "$HERE/dify-docker/docker-compose.override.yaml" docker-compose.override.yaml

if [[ -n "${DOCKER_MIRROR:-}" ]]; then
  for image in $(docker compose config --images | sort -u); do
    docker image inspect "$image" >/dev/null 2>&1 && continue
    log "从 $DOCKER_MIRROR 拉取 $image"
    { docker pull -q "$DOCKER_MIRROR/$image" && docker tag "$DOCKER_MIRROR/$image" "$image"; } \
      || log "镜像站拉取 $image 失败，交给 docker compose 直接拉取"
  done
fi

docker compose up -d
log "等待 Dify API 就绪"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${DIFY_HTTP_PORT}/console/api/setup" >/dev/null 2>&1; then
    log "Dify 已就绪：http://127.0.0.1:${DIFY_HTTP_PORT}（Docker 网络 docker_default）"
    exit 0
  fi
  sleep 5
done
log "Dify 未在 5 分钟内就绪，请执行 docker compose ps / logs api 排查" >&2
exit 1

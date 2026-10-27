# 作者：FDE WorkMate
# PowerContext Server（嵌入式 seekdb 存储后端），供 Dify 工作流通过 HTTP API 读写 NPC 记忆。
ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE}

# 国内构建可改为 https://pypi.tuna.tsinghua.edu.cn/simple；默认使用官方 PyPI。
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG POWERCONTEXT_VERSION=1.4.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --index-url "${PIP_INDEX_URL}" \
      "powercontext[cli,server,seekdb]==${POWERCONTEXT_VERSION}" \
 && useradd --create-home --uid 10001 powercontext \
 && mkdir -p /data \
 && chown powercontext:powercontext /data

USER powercontext
WORKDIR /home/powercontext

EXPOSE 8000
CMD ["powercontext", "server", "run"]

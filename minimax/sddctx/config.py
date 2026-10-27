# 作者：FDE WorkMate
"""路径与运行配置。所有路径都从 minimax/ 目录推导，不依赖调用时的工作目录。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MINIMAX_ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> dict[str, str]:
    """读取 KEY=VALUE 格式的本地配置；文件不存在时返回空字典。"""

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    """一次 CLI 调用使用的配置。环境变量优先于 .env.local。"""

    project_root: Path
    baseline_root: Path
    runtime_dir: Path
    records_dir: Path
    powercontext_port: int
    minimax_api_style: str
    minimax_base_url: str
    minimax_model: str
    minimax_effort: str
    minimax_api_key: str | None

    @property
    def kb_path(self) -> Path:
        return self.runtime_dir / "kb-seekdb"

    @property
    def powercontext_home(self) -> Path:
        return self.runtime_dir / "powercontext"

    @property
    def powercontext_url(self) -> str:
        return f"http://127.0.0.1:{self.powercontext_port}"

    @property
    def repo_root(self) -> Path:
        return MINIMAX_ROOT.parent


def load_settings() -> Settings:
    file_values = _load_env_file(MINIMAX_ROOT / ".env.local")

    def get(key: str, default: str | None = None) -> str | None:
        return os.environ.get(key) or file_values.get(key) or default

    def _path(value: str | None) -> Path:
        # 相对路径一律相对 minimax/ 目录解析，与调用时的工作目录无关。
        return (MINIMAX_ROOT / str(value)).resolve()

    return Settings(
        # 业务工程基线（提交在仓库中，修改前状态）与工作副本（演示时 MiniMax 实际修改的目录）。
        project_root=_path(get("SDDCTX_PROJECT_ROOT", str(MINIMAX_ROOT / ".work" / "sample_app"))),
        baseline_root=_path(get("SDDCTX_BASELINE_ROOT", str(MINIMAX_ROOT / "sample_app"))),
        runtime_dir=_path(get("SDDCTX_RUNTIME_DIR", str(MINIMAX_ROOT / ".runtime"))),
        records_dir=_path(get("SDDCTX_RECORDS_DIR", str(MINIMAX_ROOT / "records"))),
        powercontext_port=int(get("SDDCTX_POWERCONTEXT_PORT", "18765") or "18765"),
        # 默认使用 MiniMax 推荐的 Anthropic Messages 兼容接口；设置 MINIMAX_API_STYLE=openai 可改走 Chat Completions。
        minimax_api_style=get("MINIMAX_API_STYLE", "anthropic") or "anthropic",
        minimax_base_url=get("MINIMAX_BASE_URL", "https://api.minimax.cn/anthropic") or "",
        minimax_model=get("MINIMAX_MODEL", "MiniMax-M3.1-Flash") or "",
        minimax_effort=get("MINIMAX_EFFORT", "high") or "",
        minimax_api_key=get("MINIMAX_API_KEY"),
    )

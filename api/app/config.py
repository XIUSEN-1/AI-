"""应用配置：dataclass 单例，环境变量优先于 api/.env，.env 不覆盖已有环境变量。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model_judge: str = "deepseek-v4-pro"
    model_chat: str = "deepseek-flash"


def _load_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 行；忽略空行、注释行与无等号的行。"""
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():  # utf-8-sig 剥离 Windows 记事本 BOM
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        env_file = _load_env_file(_ENV_PATH)

        def pick(name: str, default: str) -> str:
            if name in os.environ:  # 已有环境变量优先，不被 .env 覆盖
                return os.environ[name]
            return env_file.get(name, default)

        _settings = Settings(
            deepseek_api_key=pick("DEEPSEEK_API_KEY", ""),
            base_url=pick("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model_judge=pick("DEEPSEEK_MODEL_JUDGE", "deepseek-v4-pro"),
            model_chat=pick("DEEPSEEK_MODEL_CHAT", "deepseek-flash"),
        )
    return _settings

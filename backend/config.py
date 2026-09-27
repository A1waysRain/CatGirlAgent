"""应用配置：从环境变量 / .env 文件读取 DeepSeek 大模型配置"""

import json
import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_user_api_key() -> str:
    """从用户级配置文件读取 API Key，避免把密钥打进 exe。

    路径: %APPDATA%/catgirl/config.json
    格式: {"deepseek_api_key": "sk-xxxx"}
    优先级低于 .env / 环境变量；.env 未配置 Key 时自动兜底。
    """
    try:
        base = Path(os.environ.get("APPDATA", str(Path.home())))
        cfg = base / "catgirl" / "config.json"
        if cfg.exists():
            data = json.loads(cfg.read_text(encoding="utf-8"))
            return data.get("deepseek_api_key", "")
    except Exception:
        pass
    return ""


def _load_user_model() -> str:
    """从用户级配置读取模型名，便于打包版无需改环境变量切换模型。"""
    try:
        base = Path(os.environ.get("APPDATA", str(Path.home())))
        cfg = base / "catgirl" / "config.json"
        if cfg.exists():
            data = json.loads(cfg.read_text(encoding="utf-8"))
            return data.get("deepseek_model", "")
    except Exception:
        pass
    return ""


def _load_user_web_search_key() -> str:
    """读取可选的联网搜索 API Key；未配置时使用公开 RSS 降级。"""
    try:
        base = Path(os.environ.get("APPDATA", str(Path.home())))
        cfg = base / "catgirl" / "config.json"
        if cfg.exists():
            data = json.loads(cfg.read_text(encoding="utf-8"))
            return data.get("web_search_api_key", "")
    except Exception:
        pass
    return ""


class Settings(BaseSettings):
    """全局配置，通过 .env 文件注入"""

    # DeepSeek API Key（未在 .env 配置时回退到用户级 config.json）
    deepseek_api_key: str = Field(default_factory=_load_user_api_key)
    # 模型名称
    deepseek_model: str = Field(default_factory=lambda: _load_user_model() or "deepseek-flash")
    # DeepSeek 接口地址（OpenAI 兼容）
    deepseek_base_url: str = "https://api.deepseek.com/v1/chat/completions"
    # 采样温度
    deepseek_temperature: float = 0.6
    # 请求超时（秒）
    request_timeout: int = 60
    # 可选：Brave Search API Key。未配置时 verify_current_fact 使用 Bing RSS。
    web_search_api_key: str = Field(default_factory=_load_user_web_search_key)
    web_search_provider: str = "brave"
    web_fact_timeout: int = 12

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


settings = Settings()

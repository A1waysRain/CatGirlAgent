"""对话历史持久化：以 JSON 文件存到用户目录，跨窗口 / 跨端口保留记忆。

历史文件路径: %APPDATA%/catgirl/history.json（与 config.json 同目录，打包版同样有效）。
之所以不用前端 localStorage：桌面壳每次启动选随机端口，而 localStorage 按「协议+域名+端口」
隔离，端口一变就读不到了；落到后端文件则无论端口怎么变都能续上。
"""

import json
import os
import threading
from pathlib import Path

# 历史最多保留的条数（超出从最旧开始裁剪）
MAX_STORED = 200
# 每次请求最多发给大模型的历史条数（控制 token 消耗）
MAX_SENT = 20

_lock = threading.Lock()


def _history_file() -> Path:
    base = Path(os.environ.get("APPDATA", str(Path.home())))
    return base / "catgirl" / "history.json"


def load_history() -> list[dict]:
    """读取全部历史，返回 [{"role": "user"|"assistant", "content": str}, ...]；损坏时返回空列表。"""
    try:
        path = _history_file()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [
                    {"role": m.get("role"), "content": m.get("content", "")}
                    for m in data
                    if isinstance(m, dict)
                    and m.get("role") in ("user", "assistant")
                    and m.get("content", "").strip()
                ]
    except Exception:
        pass
    return []


def save_history(history: list[dict]) -> None:
    """写回历史文件（裁剪到 MAX_STORED 条，先写临时文件再替换，避免写一半损坏）。"""
    path = _history_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(history[-MAX_STORED:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def append_message(role: str, content: str) -> list[dict]:
    """追加一条消息并落盘，返回当前完整历史。线程安全。"""
    with _lock:
        history = load_history()
        history.append({"role": role, "content": content.strip()})
        save_history(history)
        return history


def clear_history() -> None:
    """清空全部历史。"""
    with _lock:
        path = _history_file()
        if path.exists():
            path.unlink()

"""通知队列：桌面宠物与消息来源（微信监听等）之间的线程安全桥梁。

- 消息来源（未来：wechat 监听线程）调用 ``notifier.put(msg)`` 推入；
- 桌面宠物通过 ``GET /api/pet/notifications`` 长轮询取走；
- 主壳可注册 ``set_show_window(callback)``，供宠物一键唤起猫娘主窗。
"""
import queue
import threading
import time

__all__ = ["notifier", "Notifier"]


class Notifier:
    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._show_window = None
        self._lock = threading.Lock()

    def put(self, message) -> None:
        """推入一条事件（任意线程可调）。

        兼容旧写法：put("文本") 自动包成 {"type": "notify", "message": "文本"}；
        结构化写法：put({"type": "emotion", "mood": "think"}) 原样入队。
        """
        if isinstance(message, str):
            message = {"type": "notify", "message": message}
        self._q.put(message)

    def get(self, timeout: float) -> dict | None:
        """阻塞取一条事件；超时返回 None（供长轮询使用）。"""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    # ---- 主窗唤起（由桌面壳注册） ----
    def set_show_window(self, callback) -> None:
        with self._lock:
            self._show_window = callback

    def show_window(self) -> bool:
        with self._lock:
            cb = self._show_window
        if cb is None:
            return False
        try:
            cb()
            return True
        except Exception:
            return False


# 模块级单例：main.py / 桌面壳 / 未来监听线程共用
notifier = Notifier()

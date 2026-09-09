"""手机接入的令牌与会话状态。

LAN 服务只绑定用户选定的私网 IPv4；这里不保存 API Key，cookie 会话只留内存，
关闭接入、重置令牌或进程退出后立即失效。
"""
import hmac
import secrets
import threading
import time

COOKIE_NAME = "catgirl_lan"
SESSION_TTL = 12 * 3600
IDLE_TIMEOUT = 5 * 60

_lock = threading.Lock()
_enabled = False
_ip = ""
_port = 0
_token = ""
_sessions: dict[str, float] = {}
_last_activity = 0.0


def new_token() -> str:
    return secrets.token_urlsafe(24)


def configure(enabled: bool, ip: str = "", port: int = 0, token: str = "") -> None:
    """更新 LAN 监听身份；配置变化时一律踢掉旧 cookie。"""
    global _enabled, _ip, _port, _token, _last_activity
    with _lock:
        changed = (_enabled, _ip, _port, _token) != (bool(enabled), ip, int(port or 0), token)
        _enabled, _ip, _port, _token = bool(enabled), ip, int(port or 0), token
        if changed:
            _sessions.clear()
            _last_activity = time.monotonic() if _enabled else 0.0


def clear_sessions() -> None:
    with _lock:
        _sessions.clear()


def is_lan_host(host: str, port: int | None) -> bool:
    with _lock:
        return _enabled and host == _ip and port == _port


def touch() -> None:
    """记录已绑定手机端的访问；仅 Host 门禁确认来源后调用。"""
    global _last_activity
    with _lock:
        if _enabled:
            _last_activity = time.monotonic()


def is_idle(timeout: float = IDLE_TIMEOUT) -> bool:
    """LAN 是否已连续无活动超过超时阈值。"""
    with _lock:
        return _enabled and _last_activity > 0 and time.monotonic() - _last_activity >= timeout


def login(token: str) -> str | None:
    with _lock:
        if not _enabled or not _token or not hmac.compare_digest(token or "", _token):
            return None
        value = secrets.token_urlsafe(32)
        _sessions[value] = time.time() + SESSION_TTL
        return value


def valid_session(value: str | None) -> bool:
    if not value:
        return False
    with _lock:
        now = time.time()
        expired = [key for key, until in _sessions.items() if until <= now]
        for key in expired:
            _sessions.pop(key, None)
        return value in _sessions

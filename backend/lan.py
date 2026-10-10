"""手机接入的令牌与会话状态。

LAN 服务只绑定用户选定的私网 IPv4；这里不保存 API Key，cookie 会话只留内存，
关闭接入、重置令牌或进程退出后立即失效。
"""
import hmac
import secrets
import threading
import time
from urllib.parse import urlsplit

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
_public_origin = ""
_login_failures: list[float] = []


def normalize_origin(value: str) -> str:
    """只接受完整 http(s) 入口，禁止路径、凭据及通配地址。"""
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(c.isspace() for c in value) or "*" in value):
            raise ValueError
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        suffix = f":{port}" if port is not None and port != {"http": 80, "https": 443}[parsed.scheme] else ""
        return f"{parsed.scheme}://{host}{suffix}"
    except ValueError:
        raise ValueError("隧道访问地址应为 http://域名:端口 或 https://域名，不含 /m") from None


def mobile_origins() -> set[str]:
    with _lock:
        if not _enabled:
            return set()
        return {f"http://{_ip}:{_port}"} | ({_public_origin} if _public_origin else set())


def new_token() -> str:
    return secrets.token_urlsafe(24)


def configure(enabled: bool, ip: str = "", port: int = 0, token: str = "", public_origin: str = "") -> None:
    """更新 LAN 监听身份；配置变化时一律踢掉旧 cookie。"""
    global _enabled, _ip, _port, _token, _last_activity, _public_origin
    public_origin = normalize_origin(public_origin)
    with _lock:
        changed = (_enabled, _ip, _port, _token, _public_origin) != (bool(enabled), ip, int(port or 0), token, public_origin)
        _enabled, _ip, _port, _token = bool(enabled), ip, int(port or 0), token
        _public_origin = public_origin
        if changed:
            _sessions.clear()
            _login_failures.clear()
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


class LoginRateLimited(Exception):
    """登录失败次数超过当前窗口限额。"""


def login(token: str) -> str | None:
    with _lock:
        now = time.monotonic()
        _login_failures[:] = [at for at in _login_failures if now - at < 60]
        if len(_login_failures) >= 10:
            raise LoginRateLimited
        if not _enabled or not _token or not hmac.compare_digest((token or "").encode(), _token.encode()):
            _login_failures.append(now)
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

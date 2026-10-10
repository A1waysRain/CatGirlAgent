"""猫娘 FastAPI 应用入口（2026-08-16 路由拆分后）。

职责只剩两件：
1. `create_app()` —— 总装车间：FastAPI 实例 + CORS/Host 中间件 + 挂载静态资源 + include 各域路由；
2. `run_server()` —— 启动入口（桌面壳在后台线程跑 uvicorn）。

具体业务路由都在 `backend/routers/`（chat/sessions/apps/plugins/media/pet/settings），
共享 helper 在 `backend/paths.py` / `pet_control.py` / `chat_service.py`。
"""
import threading
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .paths import BASE_DIR, _avatar_dir, _media_dir
from . import lan
from .pet_control import set_pet_control  # noqa: F401  # re-export 供桌面壳用
from .routers import alarms, apps, chat, media, pet, plugins, rag_files, sessions, settings
from .scheduler import scheduler

# 只放行本机 Host（防 DNS rebinding；桌面模式启用）
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _split_host(value: str) -> tuple[str, int | None]:
    """规范化 Host 头，仅接受 IPv4/localhost 这一项目实际使用的形式。"""
    try:
        parsed = urlsplit("//" + (value or ""))
        return parsed.hostname or "", parsed.port
    except ValueError:
        return "", None


def create_app(allowed_origins: set[str] | None = None, *, mobile_only: bool = False) -> FastAPI:
    """构建猫娘 FastAPI 应用（供 uvicorn / 桌面壳 / 测试复用）。

    allowed_origins: 本应用前端自己的 origin 集合。传入后 CORS 只信任这些 origin + 拒绝非回环
    Host（挡 DNS rebinding）；传 None（测试/直跑）时保持宽 CORS 便于调试。
    """
    app = FastAPI(
        title="猫娘",
        version="3.2.5",
        description="这是只爵士毫猫",
    )

    app.add_middleware(
        CORSMiddleware,
        # 桌面版：只信本应用前端 origin，外部网页跨域请求被浏览器拦掉；
        # 开发/测试：全放开（端口随机，本机调试用）
        allow_origins=sorted(allowed_origins) if allowed_origins else ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if allowed_origins or mobile_only:
        @app.middleware("http")
        async def _guard_hosts_and_lan(request: Request, call_next):
            host, port = _split_host(request.headers.get("host", ""))
            request.state.mobile_access = mobile_only
            if mobile_only:
                origins = lan.mobile_origins()
                if (host, port or (443 if request.url.scheme == "https" else 80)) not in {
                    (name, p or (443 if urlsplit(origin).scheme == "https" else 80))
                    for origin in origins for name, p in [_split_host(urlsplit(origin).netloc)]
                }:
                    return JSONResponse(status_code=403, content={"detail": "手机入口地址不匹配"})
                origin = request.headers.get("origin")
                if origin and origin not in origins:
                    return JSONResponse(status_code=403, content={"detail": "不允许外部网页访问手机接口"})
            elif host in _LOOPBACK_HOSTS:
                return await call_next(request)
            elif not lan.is_lan_host(host, port):
                return JSONResponse(status_code=403, content={"detail": "仅允许本机访问"})
            # 登录页和登录接口是 LAN 未认证时唯一放行的资源。
            if request.url.path == "/m/login" or request.url.path == "/api/lan/login":
                return await call_next(request)
            if not lan.valid_session(request.cookies.get(lan.COOKIE_NAME)):
                if request.url.path.startswith("/api/"):
                    return JSONResponse(status_code=401, content={"detail": "请先在手机接入页输入令牌"})
                return RedirectResponse("/m/login", status_code=302)
            lan.touch()
            return await call_next(request)

    # 定时提醒调度器随应用启动（线程幂等，重复 create_app 不会起多线程）
    scheduler.start()
    # 内存健康看门狗随应用启动：每 60 秒记一行内存/提交内存/进程占用到 health.log
    # （内存爆了崩溃后翻日志尾部即有实锤；线程幂等，重复 create_app 不会起多线程）
    try:
        from . import health
        health.start()
    except Exception:
        pass

    # RAG 知识库后台预热（后台线程建索引，不阻塞；无源码树/空知识库静默跳过）
    try:
        from .rag import warmup
        warmup()
    except Exception:
        pass

    # 各域路由（按功能拆到 backend/routers/）
    app.include_router(chat.router)
    app.include_router(alarms.router)
    app.include_router(sessions.router)
    app.include_router(apps.router)
    app.include_router(plugins.router)
    app.include_router(media.router)
    app.include_router(pet.router)
    app.include_router(settings.router)
    app.include_router(rag_files.router)

    @app.get("/")
    async def index():
        return FileResponse(BASE_DIR / "index.html")

    @app.get("/m")
    async def mobile_index():
        return FileResponse(BASE_DIR / "index_m.html")

    @app.get("/m/login")
    async def mobile_login_page():
        return FileResponse(BASE_DIR / "index_m_login.html")

    @app.post("/api/lan/login")
    async def lan_login(payload: dict, request: Request):
        try:
            session = lan.login(str((payload or {}).get("token") or ""))
        except lan.LoginRateLimited:
            return JSONResponse(status_code=429, headers={"Retry-After": "60"},
                                content={"detail": "尝试太频繁，请等一分钟再试"})
        if not session:
            return JSONResponse(status_code=403, content={"detail": "令牌不正确"})
        lan.touch()
        response = JSONResponse({"ok": True})
        response.set_cookie(lan.COOKIE_NAME, session, httponly=True, samesite="strict", max_age=lan.SESSION_TTL,
                            secure=mobile_only and any(
                                origin.startswith("https://") and urlsplit(origin).netloc == request.headers.get("host")
                                for origin in lan.mobile_origins()))
        return response

    @app.get("/api/lan/ping")
    async def lan_ping():
        """供手机页探测接入是否仍有效；活动时间由 Host 门禁统一记录。"""
        return {"ok": True}

    app.mount("/css", StaticFiles(directory=BASE_DIR / "css"), name="css")
    app.mount("/js", StaticFiles(directory=BASE_DIR / "js"), name="js")
    # 聊天头像（可写的头像目录，打包版 exe 旁优先）
    app.mount("/img", StaticFiles(directory=_avatar_dir()), name="img")
    # 截图 / 上传图片
    app.mount("/media", StaticFiles(directory=_media_dir()), name="media")
    return app


# 模块级实例：兼容 `uvicorn backend.main:app` 与热重载（宽 CORS，仅调试/测试用；
# 桌面壳走 run_server，会用当次端口的受限 origin 重建 app）
app = create_app()


def _start_uvicorn(app: FastAPI, host: str, port: int, log_level: str) -> uvicorn.Server:
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # 桌面壳切换网卡/端口时必须等待旧监听真正退出，不能只看 started 标志。
    server.catgirl_thread = thread
    return server


def run_server(host: str = "127.0.0.1", port: int = 8000, log_level: str = "warning",
               lan_ip: str = "", lan_port: int = 0) -> uvicorn.Server:
    """在指定地址/端口启动服务，返回 uvicorn.Server 以便外部控制（如桌面壳优雅停服）。

    CORS/Host 按当次端口收紧：只信任本应用前端自己的 origin，拒绝外部 Host。
    """
    origins = {f"http://{host}:{port}"}
    if lan_ip and lan_port:
        origins.add(f"http://{lan_ip}:{lan_port}")
    served_app = create_app(allowed_origins=origins)
    server = _start_uvicorn(served_app, host, port, log_level)
    server.catgirl_app = served_app
    return server


def run_lan_server(app: FastAPI, host: str, port: int, log_level: str = "warning") -> uvicorn.Server:
    """独立手机认证入口，共享业务模块，但绝不继承桌面回环免登录。"""
    mobile_app = create_app(allowed_origins=lan.mobile_origins(), mobile_only=True)
    return _start_uvicorn(mobile_app, host, port, log_level)


if __name__ == "__main__":
    uvicorn.run(create_app(allowed_origins={"http://127.0.0.1:8000"}), host="127.0.0.1", port=8000)

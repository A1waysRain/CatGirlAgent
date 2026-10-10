# -*- coding: utf-8 -*-
"""手机猫娘 M0+M1 回归：LAN Host/令牌/cookie 与移动页会话 API。

不绑定真实网卡、不调用 DeepSeek；用 TestClient 模拟手机访问选定热点 IP。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
import types

import httpx

sys.path.insert(0, ".")

# WSL 没有 Windows 注册表模块；本验证不覆盖开机自启，只让设置路由可导入。
if "winreg" not in sys.modules:
    fake_winreg = types.ModuleType("winreg")
    fake_winreg.HKEY_CURRENT_USER = fake_winreg.KEY_SET_VALUE = fake_winreg.KEY_READ = fake_winreg.REG_SZ = 0
    fake_winreg.OpenKey = lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError())
    sys.modules["winreg"] = fake_winreg

tmp = tempfile.mkdtemp(prefix="catgirl_verify_lan_")
os.environ["APPDATA"] = tmp

from backend import lan  # noqa: E402
from backend import rag  # noqa: E402
rag.warmup = lambda: None  # LAN 回归不应下载/加载 embedding 模型。
from backend.main import create_app  # noqa: E402
from backend.settings import DEFAULTS, load_settings  # noqa: E402

ok = True


def check(label, condition, extra=""):
    global ok
    ok = ok and bool(condition)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))


async def run() -> None:
    check("手机接入默认关闭", DEFAULTS["lan_enabled"] is False and load_settings()["lan_enabled"] is False)
    lan.configure(True, "192.168.43.2", 8800, "test-token")
    app = create_app(allowed_origins={"http://127.0.0.1:9999", "http://192.168.43.2:8800"})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://192.168.43.2:8800") as phone:
        r = await phone.get("/m", follow_redirects=False)
        check("无 cookie 访问移动页跳登录", r.status_code == 302 and r.headers.get("location") == "/m/login", str(r.status_code))
        r = await phone.get("/api/sessions")
        check("无 cookie 访问 API 返回 401 JSON", r.status_code == 401 and r.json().get("detail"), str(r.text))
        r = await phone.post("/api/lan/login", json={"token": "wrong"})
        check("错误令牌拒绝", r.status_code == 403)
        r = await phone.post("/api/lan/login", json={"token": "test-token"})
        check("正确令牌换 httpOnly cookie", r.status_code == 200 and lan.COOKIE_NAME in r.cookies and "HttpOnly" in r.headers.get("set-cookie", ""))
        cookie_value = r.cookies.get(lan.COOKIE_NAME)
        r = await phone.get("/api/lan/ping")
        check("手机活动探测可用", r.status_code == 200 and r.json().get("ok") is True)
        r = await phone.get("/api/sessions")
        check("带 cookie 可读取同一会话列表", r.status_code == 200 and "sessions" in r.json())
        mobile_page = open("index_m.html", encoding="utf-8").read()
        check("移动页已挂载聊天入口", "mobile-app" in mobile_page and "/js/app_m.js" in mobile_page)
        r = await phone.post("/api/sessions")
        sid = r.json().get("current")
        check("手机可新建会话", r.status_code == 200 and bool(sid))
        r = await phone.get("/api/sessions/" + str(sid))
        check("手机可读取新会话", r.status_code == 200 and r.json()["session"]["id"] == sid)
        r = await phone.get("/api/settings")
        check("手机不能读接入令牌", r.status_code == 200 and not r.json().get("lan_token"))
        r = await phone.put("/api/settings", json={"pet_name": "不应保存"})
        check("手机不能修改电脑设置", r.status_code == 403)

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9999") as desktop:
        r = await desktop.get("/api/sessions")
        check("桌面回环仍免令牌", r.status_code == 200)
        r = await desktop.put("/api/settings", json={"lan_enabled": True, "lan_ip": "0.0.0.0"})
        check("拒绝把全网监听地址写入设置", r.status_code == 400)

    async with httpx.AsyncClient(transport=transport, base_url="http://192.168.43.3:8800") as other:
        r = await other.get("/api/sessions")
        check("非绑定 IP 一律 403", r.status_code == 403)

    lan.clear_sessions()
    async with httpx.AsyncClient(transport=transport, base_url="http://192.168.43.2:8800", cookies={lan.COOKIE_NAME: cookie_value}) as phone:
        r = await phone.get("/api/sessions")
        check("清空会话后旧 cookie 立即失效", r.status_code == 401)
    lan.configure(True, "192.168.43.2", 8800, "test-token")
    lan._last_activity = time.monotonic() - lan.IDLE_TIMEOUT - 1
    check("手机接入空闲五分钟可被看门狗识别", lan.is_idle())

    # 手机监听必须使用独立认证 app；TCP 隧道不可信任回环 Host 或代理头。
    public = "http://test-frp.example:23456"
    lan.configure(True, "192.168.43.2", 8800, "test-token", public)
    mobile_app = create_app(allowed_origins=lan.mobile_origins(), mobile_only=True)
    mobile_transport = httpx.ASGITransport(app=mobile_app)
    async with httpx.AsyncClient(transport=mobile_transport, base_url=public) as remote:
        r = await remote.get("/m", follow_redirects=False)
        check("隧道未登录跳转登录页", r.status_code == 302)
        r = await remote.get("/api/sessions")
        check("隧道未登录不能读会话", r.status_code == 401)
        r = await remote.post("/api/lan/login", headers={"Origin": "https://evil.example"}, json={"token": "test-token"})
        check("跨站登录请求被拒绝", r.status_code == 403 and not r.cookies)
        r = await remote.post("/api/lan/login", headers={"Origin": public}, json={"token": "test-token"})
        check("隧道令牌登录成功", r.status_code == 200)
        remote_cookie = r.cookies.get(lan.COOKIE_NAME)
        r = await remote.get("/api/sessions")
        check("隧道登录后会话可用", r.status_code == 200)
        # 静态脚本由 StaticFiles 处理，不在认证回归中发 HEAD（Starlette 对 HEAD 的
        # 文件流在 ASGITransport 下会等待空 body；浏览器实际 GET 不受影响）。
        r = await remote.get("/api/settings")
        check("隧道不能读接入令牌", r.status_code == 200 and not r.json().get("lan_token"))
        r = await remote.post("/api/upload_mobile?filename=notes.txt&kind=file", content=b"mobile file")
        check("手机可上传文件内容", r.status_code == 200 and r.json().get("name") == "notes.txt")
        uploaded_file = r.json().get("path", "")
        r = await remote.post("/api/upload_mobile?filename=photo.png&kind=image", content=b"not an image")
        check("手机拒绝损坏图片", r.status_code == 400)
        r = await remote.post("/api/upload_mobile?filename=.env&kind=file", content=b"SECRET=x")
        check("手机拒绝敏感配置文件", r.status_code == 400)
        if uploaded_file:
            import pathlib
            pathlib.Path(uploaded_file).unlink(missing_ok=True)
        r = await remote.put("/api/settings", json={"lan_enabled": False})
        check("隧道不能修改电脑设置", r.status_code == 403)
        r = await remote.post("/api/settings/avatar", json={"role": "cat", "path": "unused.png"})
        check("隧道不能通过头像接口修改设置", r.status_code == 403)
        r = await remote.post("/api/sessions", headers={"Origin": "https://evil.example"})
        check("已登录也不能跨站写入", r.status_code == 403)
        for spoof in ("127.0.0.1:9999", "localhost:9999", "unlisted.example:23456"):
            r = await remote.get("/api/sessions", headers={"Host": spoof, "X-Forwarded-Host": public.split("//")[1]})
            check(f"隧道入口拒绝伪造 Host {spoof}", r.status_code == 403)
        lan.configure(True, "192.168.43.2", 8800, "test-token", "http://new-frp.example:23456")
        check("隧道地址变化使旧 cookie 失效", not lan.valid_session(remote_cookie))
        r = await remote.get("/api/sessions")
        check("旧隧道地址立即被拒绝", r.status_code == 403)

    secure_origin = "https://secure-frp.example:24443"
    lan.configure(True, "192.168.43.2", 8800, "test-token", secure_origin)
    async with httpx.AsyncClient(transport=mobile_transport, base_url=secure_origin) as remote:
        r = await remote.post("/api/lan/login", json={"token": "test-token"})
        check("HTTPS 入口发放 Secure cookie", r.status_code == 200 and "Secure" in r.headers.get("set-cookie", ""))
        r = await remote.get("/api/sessions")
        check("HTTPS 会话正常使用", r.status_code == 200)
    async with httpx.AsyncClient(transport=mobile_transport, base_url="http://192.168.43.2:8800") as phone:
        r = await phone.post("/api/lan/login", json={"token": "test-token"})
        check("独立手机入口仍支持局域网登录", r.status_code == 200)

    for invalid in ("http://example.com/m", "http://user:secret@example.com", "http://*.example.com", "http://example.com:99999", "ftp://example.com"):
        try:
            lan.normalize_origin(invalid)
            rejected = False
        except ValueError:
            rejected = True
        check("拒绝无效隧道地址 " + invalid, rejected)
    check("规范化默认 HTTPS 端口", lan.normalize_origin("https://EXAMPLE.com:443/") == "https://example.com")
    lan.configure(True, "192.168.43.2", 8800, "test-token")
    check("中文错误令牌不引发服务器异常", lan.login("错误令牌") is None)
    for _ in range(9):
        lan.login("wrong")
    try:
        lan.login("test-token")
        rate_limited = False
    except lan.LoginRateLimited:
        rate_limited = True
    check("十次失败后暂时限制登录尝试", rate_limited)


try:
    asyncio.run(run())
finally:
    lan.configure(False)
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL PASS" if ok else "HAS FAILURES")
raise SystemExit(0 if ok else 1)

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


try:
    asyncio.run(run())
finally:
    lan.configure(False)
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL PASS" if ok else "HAS FAILURES")
raise SystemExit(0 if ok else 1)

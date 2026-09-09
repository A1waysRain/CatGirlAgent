"""搜索聊天记录 端到端验证（Playwright + 真实后端 + 隔离 APPDATA）。

覆盖：输入关键词→结果面板出现、命中词高亮、点击跳转定位+气泡高亮、
空结果提示、Escape 关闭面板、切换会话重置搜索。
"""
import os
import sys
import json
import time
import tempfile
import subprocess
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8792
BASE = f"http://127.0.0.1:{PORT}"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def seed(apd: str) -> None:
    env = dict(os.environ)
    env["APPDATA"] = apd
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from backend.sessions import get_current, append_message\n"
        "sid = get_current()\n"
        "append_message(sid, 'user', '帮我打开桌面上的111.docx')\n"
        "append_message(sid, 'assistant', '好嘞主人（尾巴一晃）已经帮你打开啦，要看看内容吗喵？')\n"
        "append_message(sid, 'user', '搜这个关键词：翡翠梦境')\n"
        "append_message(sid, 'assistant', '（歪头）翡翠梦境是什么喵？听起来像游戏里的地名。')\n"
    ) % HERE
    subprocess.run([sys.executable, "-c", code], env=env, cwd=HERE, check=True)


def start_server(apd: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["APPDATA"] = apd
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--port", str(PORT), "--host", "127.0.0.1", "--log-level", "warning"],
        cwd=HERE, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    for _ in range(100):
        try:
            urllib.request.urlopen(BASE + "/api/sessions", timeout=1)
            return proc
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("后端启动超时")


def stop_server(proc: subprocess.Popen) -> None:
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)


def main():
    apd = tempfile.mkdtemp(prefix="catgirl_srch_ui_")
    seed(apd)
    proc = start_server(apd)
    console_errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1080, "height": 780})
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append("PAGEERROR: " + str(e)))
            page.on("response", lambda r: console_errors.append("HTTP%d %s" % (r.status, r.url)) if r.status >= 400 else None)

            page.goto(BASE + "/")
            page.wait_for_timeout(1200)
            assert page.locator(".message").count() == 4, "预置会话应 4 条消息"

            search = page.locator("#chatSearchInput")
            results = page.locator("#chatSearchResults")

            # ---- 1. 输入命中词 → 结果面板出现 + 命中词 <mark> 高亮 ----
            search.fill("翡翠梦境")
            page.wait_for_timeout(500)
            assert results.is_visible(), "结果面板应显示"
            items = results.locator(".search-result-item")
            n = items.count()
            assert n == 2, f"应 2 条命中（user+assistant 都含翡翠梦境）: {n}"
            marks = results.locator("mark").count()
            assert marks >= 2, f"命中词应高亮: {marks}"
            first_text = items.first.locator(".sri-text").inner_text()
            assert "翡翠梦境" in first_text, f"片段应含命中词: {first_text}"
            roles = [items.nth(i).locator(".sri-role").inner_text() for i in range(n)]
            assert "主人" in roles and "猫娘" in roles, f"角色标注: {roles}"
            print(f"[1] 输入命中词 OK | {n} 条命中，mark x{marks}，角色={roles}")

            # ---- 2. 点击结果 → 跳转定位 + 气泡高亮 + 面板关闭 ----
            page.evaluate("document.querySelectorAll('.search-result-item')[1].click()")
            page.wait_for_timeout(800)
            assert results.is_hidden(), "点击后面板应关闭"
            hit = page.evaluate("document.querySelector('.msg-bubble.search-hit') !== null")
            assert hit, "目标气泡应加 search-hit 高亮"
            # 高亮会 2s 后消失
            page.wait_for_timeout(2300)
            hit = page.evaluate("document.querySelector('.msg-bubble.search-hit') !== null")
            assert not hit, "2s 后高亮应消失"
            assert search.input_value() == "", "跳转后搜索框应清空"
            print("[2] 点击跳转+高亮 OK | 定位到气泡，2s 后高亮消失")

            # ---- 3. 空结果提示 ----
            search.fill("绝对不存在的词xyz")
            page.wait_for_timeout(500)
            assert results.is_visible(), "面板应显示"
            empty = results.locator(".search-result-empty")
            assert empty.count() == 1, "应显示空结果提示"
            print("[3] 空结果 OK | 提示:", empty.inner_text())

            # ---- 4. Escape 关闭面板并清空 ----
            search.press("Escape")
            page.wait_for_timeout(200)
            assert results.is_hidden(), "Escape 应关闭面板"
            assert search.input_value() == "", "Escape 应清空输入"
            print("[4] Escape 关闭+清空 OK")

            # ---- 5. 点击面板外关闭 ----
            search.fill("111")
            page.wait_for_timeout(500)
            assert results.is_visible(), "面板应显示"
            page.evaluate("document.querySelector('#curTitle').click()")
            page.wait_for_timeout(200)
            assert results.is_hidden(), "点击外部应关闭面板"
            print("[5] 点击外部关闭 OK")

            # ---- 6. 切换会话重置搜索 ----
            search.fill("好嘞")
            page.wait_for_timeout(500)
            assert results.is_visible(), "面板应显示"
            page.evaluate("document.getElementById('newChatBtn').click()")
            page.wait_for_timeout(500)
            assert search.input_value() == "", "切到新会话后搜索框应清空"
            assert results.is_hidden(), "切到新会话后结果面板应关闭"
            print("[6] 切换会话重置搜索 OK")

            browser.close()
    finally:
        stop_server(proc)

    print("===== 结果 =====")
    if console_errors:
        print("控制台报错:")
        for e in console_errors[:20]:
            print("  ", e)
        sys.exit(1)
    print("OK - 控制台无报错")


if __name__ == "__main__":
    main()

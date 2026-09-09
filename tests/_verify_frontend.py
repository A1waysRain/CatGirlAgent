"""新前端（双栏布局）端到端验证。

Playwright(msedge) + 真实后端，隔离 APPDATA 临时目录；
chat_response / regenerate 用 page.route mock（避免消耗真实 DeepSeek token）；
settings/sessions/plugins/apps 走真实后端。
"""
import os
import sys
import json
import time
import tempfile
import subprocess
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def seed(apd: str) -> None:
    """在隔离 APPDATA 里造一个带 4 条消息（2 轮）的会话。"""
    env = dict(os.environ)
    env["APPDATA"] = apd
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from backend.sessions import get_current, append_message\n"
        "sid = get_current()\n"
        "append_message(sid, 'user', '帮我打开桌面上的111.docx')\n"
        "append_message(sid, 'assistant', '好嘞主人～（尾巴一晃）已经帮你打开啦，需要看看内容吗喵？')\n"
        "append_message(sid, 'user', '不用了谢谢')\n"
        "append_message(sid, 'assistant', '好哒，随叫随到喵～')\n"
    ) % HERE
    subprocess.run([sys.executable, "-c", code], env=env, cwd=HERE, check=True)


def remote_append(apd: str, pairs) -> None:
    """模拟「他端（手机/后端）往当前会话追加消息」：起子进程调 backend.sessions
    append_message，写进同一份隔离会话文件（等价于手机连 LAN 往电脑会话里发话）。"""
    env = dict(os.environ)
    env["APPDATA"] = apd
    steps = ["import sys; sys.path.insert(0, %r)" % HERE,
             "from backend.sessions import get_current, append_message",
             "sid = get_current()"]
    for role, text in pairs:
        steps.append("append_message(sid, %r, %r)" % (role, text))
    subprocess.run([sys.executable, "-c", "\n".join(steps)],
                   env=env, cwd=HERE, check=True)


def drop_head(apd: str, n: int = 1) -> None:
    """模拟服务端"封顶丢最老 n 条"：直接删当前会话文件最老的 n 条（真实路径见
    sessions.py append_message 的 `session["messages"] = session["messages"][-MAX_STORED:]`）。"""
    env = dict(os.environ)
    env["APPDATA"] = apd
    # 拿 current 会话 id 走 backend.sessions.get_current()（内部按真实索引路径解析），别硬编码目录
    code = (
        "import sys, os, json; sys.path.insert(0, %r)\n"
        "from backend.sessions import get_current\n"
        "sid = get_current()\n"
        "p = os.path.join(os.environ['APPDATA'], 'catgirl', 'sessions', sid + '.json')\n"
        "j = json.load(open(p, encoding='utf-8'))\n"
        "msgs = j.get('messages')\n"
        "if msgs:\n"
        "    del msgs[:%d]\n"
        "json.dump(j, open(p, 'w', encoding='utf-8'), ensure_ascii=False)\n"
    ) % (HERE, n)
    subprocess.run([sys.executable, "-c", code], env=env, cwd=HERE, check=True)


def seed_memory(apd: str, sid: str, lines) -> None:
    """给指定会话写一份小本本（摘要段），用于验证记忆内容在收起/展开下的显隐。"""
    env = dict(os.environ)
    env["APPDATA"] = apd
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from backend.sessions import set_summary\n"
        "set_summary(%r, [{'n': 999, 'text': %r}])\n"
    ) % (HERE, sid, "\n".join(lines))
    subprocess.run([sys.executable, "-c", code], env=env, cwd=HERE, check=True)


def seed_context_item(apd: str, sid: str, content: str) -> None:
    """给指定会话追加一条 reference 上下文保留线索（供 [17] 验证固定/移除）。"""
    env = dict(os.environ)
    env["APPDATA"] = apd
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from backend.sessions import append_message\n"
        "append_message(%r, 'assistant', %r, context_policy='reference')\n"
    ) % (HERE, sid, content)
    subprocess.run([sys.executable, "-c", code], env=env, cwd=HERE, check=True)


def start_server(apd: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["APPDATA"] = apd
    # WSL 没有 winreg；设置路由只在本回归中读取设置，模拟其最小接口即可。
    bootstrap = ""
    if sys.platform != "win32":
        bootstrap = (
            "import sys, types\n"
            "fake = types.ModuleType('winreg')\n"
            "fake.HKEY_CURRENT_USER = fake.KEY_SET_VALUE = fake.KEY_READ = fake.REG_SZ = 0\n"
            "fake.OpenKey = lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError())\n"
            "sys.modules['winreg'] = fake\n"
        )
    bootstrap += (
        "import uvicorn\n"
        "uvicorn.run('backend.main:app', host='127.0.0.1', port=%d, log_level='warning')\n" % PORT
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", bootstrap],
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
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    apd = tempfile.mkdtemp(prefix="catgirl_verify_")
    seed(apd)
    proc = start_server(apd)
    console_errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1080, "height": 780})
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append("PAGEERROR: " + str(e)))
            page.on("dialog", lambda d: d.accept())  # confirm 自动确定

            # ---- mock 聊天/重生成（避免真实 DeepSeek token）----
            # 接口已改 SSE（text/event-stream），mock 直接给 SSE body，前端走 ReadableStream 读
            def _sse_body(events):
                return "".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n" for e in events)

            def mock_chat(route):
                body = json.loads(route.request.post_data or "{}")
                route.fulfill(status=200, content_type="text/event-stream", body=_sse_body([
                    {"type": "meta",
                     "session": {"id": body.get("session_id"), "title": "测试会话"},
                     "message_ids": {"user": "mock-user-1"}},
                    {"type": "delta", "text": "（验证回复）收到喵～"},
                    {"type": "delta", "text": "测试通过啦"},
                    {"type": "done", "message_ids": {"assistant": "mock-bot-1"}},
                ]))

            def mock_regenerate(route):
                route.fulfill(status=200, content_type="text/event-stream", body=_sse_body([
                    {"type": "meta", "session": None, "message_ids": {"user": None}},
                    {"type": "delta", "text": "（重生成验证）这是重新生成的回答喵～"},
                    {"type": "done", "message_ids": {"assistant": "mock-bot-2"}},
                ]))

            page.route("**/api/chat_response", mock_chat)
            page.route("**/api/sessions/*/regenerate", mock_regenerate)

            page.goto(BASE + "/")
            page.wait_for_timeout(1200)

            # ---- 1. 页面 + 侧栏 ----
            funcs = page.locator(".func-item").count()
            sessions_n = page.locator(".session-item").count()
            title = page.eval_on_selector("#curTitle", "el => el.textContent")
            meta = page.eval_on_selector("#curMeta", "el => el.textContent")
            welcome = page.locator(".welcome").count()
            assert funcs == 5, f"功能入口应 5 个: {funcs}"
            assert sessions_n == 1, f"侧栏会话应 1 个: {sessions_n}"
            assert "帮我打开桌面上的111" in title, f"标题应为预置会话: {title}"
            assert meta == "4 条消息", f"消息计数: {meta}"
            assert welcome == 0, "有消息时不应显示欢迎空态"
            print(f"[1] 页面+侧栏 OK | 功能入口={funcs} 会话={sessions_n} 标题='{title}' 计数={meta}")

            # ---- 2. 图标全部加载 ----
            icons = page.evaluate("""() => {
                const sels = ['.func-item .fi img','.tool-btn .ti img','.collapse-btn img',
                    '.new-chat img','.name-edit img','.session-item .ico img','.session-item .del img',
                    '.send-btn img'];
                const bad = [];
                for (const s of sels) {
                    document.querySelectorAll(s).forEach(im => { if (im.naturalWidth <= 0) bad.push(im.src); });
                }
                return bad;
            }""")
            assert not icons, f"图标未加载: {icons}"
            print("[2] 图标加载 OK | 全部 naturalWidth>0")

            # ---- 3. 设置加载 ----
            brand = page.eval_on_selector("#brandName", "el => el.textContent")
            theme = page.eval_on_selector("#themeColorInput", "el => el.value")
            petName = page.eval_on_selector("#petNameInput", "el => el.value")
            assert brand == "猫娘", f"品牌名: {brand}"
            assert theme == "#ec4899", f"主题色默认: {theme}"
            print(f"[3] 设置加载 OK | 品牌='{brand}' 主题={theme} petName='{petName}'")

            # ---- 4. 发送（mock 回复） ----
            page.fill("#messageInput", "你好呀喵")
            page.keyboard.press("Enter")
            page.wait_for_timeout(600)
            page.wait_for_selector(".msg-regen", timeout=3000)
            msgs = page.locator(".message").count()
            meta = page.eval_on_selector("#curMeta", "el => el.textContent")
            assert msgs == 6, f"发送后消息应 6 条: {msgs}"
            assert meta == "6 条消息", f"发送后计数: {meta}"
            user_del = page.evaluate("() => [...document.querySelectorAll('.message')][4].querySelector('.msg-del') !== null")
            last = page.evaluate("""() => {
                const m = [...document.querySelectorAll('.message')].pop();
                return { del: !!m.querySelector('.msg-del'), regen: !!m.querySelector('.msg-regen') };
            }""")
            assert user_del and last["del"] and last["regen"], "用户/猫娘消息操作钮缺失"
            print(f"[4] 发送 OK | 消息={msgs} 计数={meta} 用户有删除钮={user_del} 猫娘有删除+重生成钮={last}")

            # ---- 5. 重新生成（mock） ----
            before = page.evaluate("() => [...document.querySelectorAll('.message')].pop().querySelector('.msg-bubble').innerText")
            page.evaluate("document.querySelector('.msg-regen').click()")
            page.wait_for_timeout(700)
            page.wait_for_selector(".msg-regen", timeout=3000)
            after = page.evaluate("() => [...document.querySelectorAll('.message')].pop().querySelector('.msg-bubble').innerText")
            msgs = page.locator(".message").count()
            assert "重生成" in after and after != before, f"重生成文案未变: {after}"
            assert msgs == 6, f"重生成后消息应仍 6 条: {msgs}"
            print("[5] 重新生成 OK | 文案已换，条数不变")

            # ---- 6. 主题色切换 + 持久化 ----
            page.evaluate("""() => { const s = [...document.querySelectorAll('#themeSwatches .swatch')][3]; s.click(); }""")
            page.wait_for_timeout(400)
            accent = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
            assert accent.lower() == "#3b82f6", f"主题色应变蓝: {accent}"
            with urllib.request.urlopen(BASE + "/api/settings") as r:
                saved = json.load(r).get("theme_color")
            assert saved == "#3b82f6", f"主题色未持久化: {saved}"
            print(f"[6] 主题色 OK | --accent={accent} 已保存={saved}")

            # ---- 7. 新建会话 + 切换回来 ----
            page.evaluate("document.getElementById('newChatBtn').click()")
            page.wait_for_timeout(500)
            assert page.locator(".session-item").count() == 2, "新会话后应 2 个会话项"
            assert page.locator(".welcome").count() == 1, "新会话应显示欢迎空态"
            cur = page.eval_on_selector("#curTitle", "el => el.textContent")
            assert cur == "新会话", f"新会话标题: {cur}"
            page.evaluate("""() => {
                const item = [...document.querySelectorAll('.session-item')]
                    .find(el => el.querySelector('.tt').textContent.includes('帮我打开桌面上的111'));
                if (item) item.click();
            }""")
            page.wait_for_timeout(500)
            assert page.locator(".message").count() == 4, "切回预置会话应有 4 条消息"
            print("[7] 新建+切换 OK | 新会话欢迎空态，切回 4 条消息")

            # ---- 8. 删除单轮（真实后端） ----
            page.evaluate("document.querySelector('.message .msg-del').click()")
            page.wait_for_timeout(600)
            msgs = page.locator(".message").count()
            meta = page.eval_on_selector("#curMeta", "el => el.textContent")
            assert msgs == 2 and meta == "2 条消息", f"删一轮后应 2 条: {msgs} {meta}"
            print(f"[8] 删除单轮 OK | 剩 {msgs} 条（user+assistant 一起删）")

            # ---- 9. 清空（真实后端） ----
            page.evaluate("document.getElementById('clearBtn').click()")
            page.wait_for_timeout(600)
            assert page.locator(".welcome").count() == 1, "清空后应显示欢迎空态"
            meta = page.eval_on_selector("#curMeta", "el => el.textContent")
            assert meta == "空会话", f"清空后计数: {meta}"
            print("[9] 清空 OK | 欢迎空态 + 空会话")

            # ---- 10. 侧栏折叠 ----
            page.evaluate("document.getElementById('collapseBtn').click()")
            page.wait_for_timeout(400)
            w = page.eval_on_selector(".sidebar", "el => el.getBoundingClientRect().width")
            assert w < 2, f"折叠后侧栏宽应≈0: {w}"
            page.evaluate("document.getElementById('collapseBtn').click()")
            page.wait_for_timeout(400)
            w = page.eval_on_selector(".sidebar", "el => el.getBoundingClientRect().width")
            assert w > 200, f"展开后侧栏应恢复: {w}"
            print("[10] 侧栏折叠/展开 OK")

            # ---- 11. 窄窗图标条 ----
            page.set_viewport_size({"width": 700, "height": 640})
            page.wait_for_timeout(500)
            iconbar = page.evaluate("document.querySelector('.sidebar').classList.contains('icon-bar')")
            assert iconbar, "700px 时应收成图标条"
            print("[11] 窄窗图标条 OK")

            # ---- 12. 设置浮层（应用/插件走真实后端） ----
            page.set_viewport_size({"width": 1080, "height": 780})
            page.wait_for_timeout(300)
            page.evaluate("""() => document.querySelector('.func-item[data-func="settings"]').click()""")
            page.wait_for_timeout(600)
            assert page.evaluate("document.getElementById('settingsOverlay').classList.contains('open')"), "设置浮层应打开"
            scan_info = page.eval_on_selector("#appScanInfo", "el => el.textContent")
            plugin_text = page.eval_on_selector("#pluginList", "el => el.textContent")
            assert "应用" in scan_info or "扫描" in scan_info, f"应用状态: {scan_info}"
            memory_text = page.eval_on_selector("#memoryList", "el => el.textContent")
            assert "还没有" in memory_text or "记忆" in memory_text, f"记忆卡片应显示空态: {memory_text}"
            print(f"[12] 设置浮层 OK | 应用状态='{scan_info}' 插件区='{plugin_text[:20]}…' 记忆='{memory_text[:20]}…'")

            # ---- 13. 发送钮白图标 ----
            send_filter = page.eval_on_selector(".send-btn img", "el => getComputedStyle(el).filter")
            assert "invert(1)" in send_filter, f"发送钮应染白: {send_filter}"
            print(f"[13] 发送钮白图标 OK | filter={send_filter}")

            # ---- 14. 定点提醒面板（真实后端增删 + 收起/展开） ----
            # 设置浮层此时仍开着；openSettings 里 loadAlarms 已拉过空列表
            # 默认收起：列表隐藏，按钮无数量角标
            assert page.eval_on_selector("#alarmWrap", "el => el.hidden"), "提醒列表默认应收起"
            alarm_toggle0 = page.eval_on_selector("#alarmToggleLabel", "el => el.textContent")
            assert "查看已设置的提醒" in alarm_toggle0 and "（" not in alarm_toggle0, \
                f"收起态按钮文案: {alarm_toggle0}"
            # 点「查看已设置的提醒」展开 → 空态提示
            page.click("#alarmToggleBtn")
            assert not page.eval_on_selector("#alarmWrap", "el => el.hidden"), "展开后列表应可见"
            alarm_empty = page.eval_on_selector("#alarmList", "el => el.textContent")
            assert "还没有定点提醒" in alarm_empty, f"提醒列表初始应空: {alarm_empty}"
            page.fill("#alarmTimeInput", "10:30")
            page.fill("#alarmMsgInput", "喝水")
            page.click("#addAlarmBtn")
            page.wait_for_timeout(600)
            alarm_items = page.locator("#alarmList .plugin-item").count()
            alarm_text = page.eval_on_selector("#alarmList", "el => el.textContent")
            assert alarm_items == 1 and "10:30" in alarm_text and "喝水" in alarm_text, \
                f"添加后列表应有 1 条: {alarm_text}"
            # 展开态按钮应显示「收起提醒列表」
            assert "收起提醒列表" in page.eval_on_selector("#alarmToggleLabel", "el => el.textContent"), \
                "展开态按钮应为收起文案"
            # 收起 → 列表隐藏、按钮带数量角标（1）
            page.click("#alarmToggleBtn")
            assert page.eval_on_selector("#alarmWrap", "el => el.hidden"), "收起后列表应隐藏"
            assert "（1）" in page.eval_on_selector("#alarmToggleLabel", "el => el.textContent"), \
                "收起态应有数量角标（1）"
            # 再展开删除
            page.click("#alarmToggleBtn")
            page.evaluate("document.querySelector('#alarmList .plugin-del').click()")
            page.wait_for_timeout(600)
            alarm_items = page.locator("#alarmList .plugin-item").count()
            assert alarm_items == 0, f"删除后应空: {alarm_items}"
            print("[14] 定点提醒面板 OK | 默认收起→展开→空态→添加→角标→删除 全过")

            # ---- 15. 远端同步 = 增量追尾，不整表重建（防"聊天窗跳顶又快速滚回"） ----
            # 前提：mock 聊天不落后端，DOM 与服务端在"真实操作"后一致。当前会话上一步已清空。
            # 先远端补 2 轮（4 条）→ 验证空欢迎态能被首条远端消息顶掉、能渲染出来；
            remote_append(apd, [
                ("user", "手机那头发来的问题"),
                ("assistant", "远端回答一喵～"),
                ("user", "再追问一条"),
                ("assistant", "远端回答二喵～"),
            ])
            page.wait_for_function(
                "() => [...document.querySelectorAll('.message[data-msg-id]')].length === 4",
                timeout=9000)
            # 捕获这 4 个节点引用，再远端补 1 条 → 4s 同步应只"追尾"，旧节点一个都不销毁
            page.evaluate("() => { window.__msgNodes = [...document.querySelectorAll('.message[data-msg-id]')]; }")
            remote_append(apd, [("assistant", "最新一条远端消息标记")])
            page.wait_for_function(
                """() => {
                    const els = [...document.querySelectorAll('.message[data-msg-id]')];
                    const last = els[els.length - 1];
                    return els.length === 5 && last && last.textContent.includes('最新一条远端消息标记');
                }""",
                timeout=9000)
            stable = page.evaluate("""() => {
                const arr = window.__msgNodes || [];
                const all = [...document.querySelectorAll('.message')];
                const first = document.querySelector('.message[data-msg-id]');
                const last = all[all.length - 1];
                return {
                    captured: arr.length,
                    alive: arr.length > 0 && arr.every(function (el) { return el.isConnected; }),
                    sameFirst: arr.length > 0 && arr[0] === first,
                    lastHasRegen: !!last && !!last.querySelector('.msg-regen'),
                    meta: document.querySelector('#curMeta').textContent
                };
            }""")
            assert stable["captured"] == 4, f"应捕获 4 条旧节点: {stable}"
            assert stable["alive"] and stable["sameFirst"], \
                f"远端补消息不应整表重建（旧节点被销毁=聊天窗会跳顶又滚回）: {stable}"
            assert stable["lastHasRegen"], f"最新猫娘回复应挂重新生成钮: {stable}"
            assert stable["meta"] == "5 条消息", f"计数应变 5: {stable}"
            print("[15] 远端增量同步 OK | 追尾不重建，旧节点 4/4 存活，🔄 挂对，计数 5 条")

            # ---- [15b] 会话顶到条数上限被服务端"截头丢最老" + 追尾：也应增量（只删最老 1 条 + 追尾），不整表重建 ----
            # 现状：DOM/服务端 5 条（m0..m4，m4=assistant 标记）。捕获 5 节点引用 + 第 2 条 id（m1）。
            page.evaluate("() => { window.__msgNodesB = [...document.querySelectorAll('.message[data-msg-id]')]; }")
            second_id = page.evaluate(
                "[...document.querySelectorAll('.message[data-msg-id]')][1].dataset.msgId")
            # 模拟真实封顶路径：服务端丢最老 1 条(m0) + 手机又补 1 条(m5) → 服务端 = m1..m5
            drop_head(apd)
            remote_append(apd, [("assistant", "封顶后新到消息标记")])
            page.wait_for_function(
                """(expectFirst) => {
                    const els = [...document.querySelectorAll('.message[data-msg-id]')];
                    const last = els[els.length - 1];
                    return els.length === 5 && els[0].dataset.msgId === expectFirst
                        && last && last.textContent.includes('封顶后新到消息标记');
                }""", arg=second_id, timeout=9000)
            cap = page.evaluate("""() => {
                const arr = window.__msgNodesB || [];
                const all = [...document.querySelectorAll('.message')];
                const first = document.querySelector('.message[data-msg-id]');
                const last = all[all.length - 1];
                return {
                    captured: arr.length,
                    headGone: arr.length > 0 && !arr[0].isConnected,       // 被截掉的最老一条应已移除
                    restAlive: arr.length > 1 && arr.slice(1).every(function (el) { return el.isConnected; }),
                    firstIsOldSecond: arr.length > 1 && first === arr[1],  // 原第 2 条顶成第 1 条，节点不变
                    lastHasRegen: !!last && !!last.querySelector('.msg-regen'),
                    meta: document.querySelector('#curMeta').textContent
                };
            }""")
            assert cap["captured"] == 5, f"应捕获 5 条: {cap}"
            assert cap["headGone"] and cap["restAlive"] and cap["firstIsOldSecond"], \
                f"封顶截头+追尾应增量处理（只删最老、其余原样）: {cap}"
            assert cap["lastHasRegen"], f"最新猫娘回复应挂重生成钮: {cap}"
            assert cap["meta"] == "5 条消息", f"计数应保持 5: {cap}"
            print("[15b] 封顶截头+追尾 OK | 只删最老 1 条，其余 4/4 节点原样，🔄 挂对，计数 5 条")

            # ---- [15c] 手机离线较久：服务端一次丢多条(3) + 追尾，仍应增量，存活节点 0 重建 ----
            # 先灌 12 轮把会话拉长（顺带覆盖长列表稳定性），等同步追到 29 条
            grow = []
            for i in range(12):
                grow.append(("user", "补对话%d" % i))
                grow.append(("assistant", "补回答%d喵~" % i))
            remote_append(apd, grow)
            page.wait_for_function(
                "() => [...document.querySelectorAll('.message[data-msg-id]')].length === 29",
                timeout=12000)
            page.evaluate("""() => {
                const m = {};
                [...document.querySelectorAll('.message[data-msg-id]')].forEach(function (el) { m[el.dataset.msgId] = el; });
                window.__idMap = m;
            }""")
            ids_before = page.evaluate(
                "[...document.querySelectorAll('.message[data-msg-id]')].map(el => el.dataset.msgId)")
            drop_head(apd, 3)
            remote_append(apd, [("assistant", "连丢3条后新到标记")])
            page.wait_for_function(
                """() => {
                    const els = [...document.querySelectorAll('.message[data-msg-id]')];
                    const last = els[els.length - 1];
                    return els.length === 27 && last && last.textContent.includes('连丢3条后新到标记');
                }""", timeout=9000)
            cap3 = page.evaluate("""() => {
                const m = window.__idMap || {};
                let replaced = 0;
                [...document.querySelectorAll('.message[data-msg-id]')].forEach(function (el) {
                    const prev = m[el.dataset.msgId];
                    if (prev && prev !== el) replaced++;
                });
                const first = document.querySelector('.message[data-msg-id]');
                return { replaced: replaced, firstId: first && first.dataset.msgId };
            }""")
            assert cap3["replaced"] == 0, f"存活消息节点不应被重建（整表重建会把同 id 换成新节点）: {cap3}"
            assert cap3["firstId"] == ids_before[3], f"丢 3 条后原第 4 条应顶成第 1 条: {cap3}"
            print("[15c] 连丢3条+追尾 OK | 存活节点 0 重建，原第4条顶成第1条，追尾到位")

            # ---- [16] 记忆显示开关（小本本一键收起/展开 #memoryDetails）----
            # 设置浮层从 [12] 起一直开着、从没动过记忆开关。codex 09-05 把默认改为「收起」：
            # HTML memoryDetails 初始 hidden + 按钮「展开」+ aria=false，app.js memoryDetailsCollapsed=true（两端一致）。
            assert page.evaluate("document.getElementById('settingsOverlay').classList.contains('open')"), \
                "设置浮层应仍开着"
            # ① 默认收起：details 隐藏、按钮「展开」、aria=false；会话下拉(在 details 外)仍可见
            m0 = page.evaluate("""() => {
                const d = document.getElementById('memoryDetails');
                const b = document.getElementById('toggleMemoryBtn');
                const row = document.querySelector('.memory-view-row');
                return { hidden: d.hidden, btn: b.textContent, aria: b.getAttribute('aria-expanded'),
                         rowVisible: !!row && row.offsetParent !== null };
            }""")
            assert m0["hidden"] is True and m0["btn"] == "展开" and m0["aria"] == "false", \
                f"记忆默认应收起: {m0}"
            assert m0["rowVisible"], "收起时「查看会话」下拉应仍可见"
            # ② 点「展开」→ details 可见、按钮「收起」、aria=true
            page.click("#toggleMemoryBtn")
            m1 = page.evaluate("""() => {
                const d = document.getElementById('memoryDetails');
                const b = document.getElementById('toggleMemoryBtn');
                return { hidden: d.hidden, btn: b.textContent, aria: b.getAttribute('aria-expanded') };
            }""")
            assert m1["hidden"] is False and m1["btn"] == "收起" and m1["aria"] == "true", \
                f"展开后 details 应可见、按钮「收起」: {m1}"
            # ③ 展开态给当前查看会话补小本本 + 点「刷新」→ 内容加载且保持展开（刷新不该翻转开关状态）
            mem_sid = page.eval_on_selector("#memorySessionSelect", "el => el.value")
            assert mem_sid, "记忆下拉应已选中一个会话"
            seed_memory(apd, mem_sid, ["测试小本本第一行", "测试小本本第二行"])
            page.click("#refreshMemoryBtn")
            page.wait_for_timeout(500)
            m2 = page.evaluate("""() => {
                const d = document.getElementById('memoryDetails');
                const b = document.getElementById('toggleMemoryBtn');
                return { hidden: d.hidden, btn: b.textContent,
                         has: document.querySelector('#memoryList .memory-item') !== null,
                         text: document.getElementById('memoryList').textContent };
            }""")
            assert m2["hidden"] is False and m2["btn"] == "收起", f"展开态刷新后应保持展开: {m2}"
            assert m2["has"] and "测试小本本第一行" in m2["text"], f"刷新应加载出小本本内容: {m2['text']}"
            # ④ 点「收起」→ details 隐藏、按钮「展开」、aria=false；内容(子元素)被整体隐藏但 DOM 还在
            page.click("#toggleMemoryBtn")
            m3 = page.evaluate("""() => {
                const d = document.getElementById('memoryDetails');
                const b = document.getElementById('toggleMemoryBtn');
                return { hidden: d.hidden, btn: b.textContent, aria: b.getAttribute('aria-expanded'),
                         text: document.getElementById('memoryList').textContent };
            }""")
            assert m3["hidden"] is True and m3["btn"] == "展开" and m3["aria"] == "false", \
                f"收起后应隐藏、按钮「展开」: {m3}"
            assert "测试小本本第一行" in m3["text"], f"收起只隐藏不应销毁内容: {m3['text']}"
            # ⑤ 收起态点「清空记忆」(confirm 自动确定) → 仍收起、列表回空态
            page.click("#clearMemoryBtn")
            page.wait_for_timeout(500)
            m4 = page.evaluate("""() => {
                const d = document.getElementById('memoryDetails');
                const b = document.getElementById('toggleMemoryBtn');
                return { hidden: d.hidden, btn: b.textContent,
                         text: document.getElementById('memoryList').textContent };
            }""")
            assert m4["hidden"] is True and m4["btn"] == "展开" and "还没有记忆喵" in m4["text"], \
                f"收起时清空应保持收起并回空态: {m4['text']}"
            # ⑥ 再展开收尾：aria 回 true、空态仍显示
            page.click("#toggleMemoryBtn")
            m5 = page.evaluate("""() => ({
                hidden: document.getElementById('memoryDetails').hidden,
                btn: document.getElementById('toggleMemoryBtn').textContent,
                aria: document.getElementById('toggleMemoryBtn').getAttribute('aria-expanded'),
                text: document.getElementById('memoryList').textContent
            })""")
            assert m5["hidden"] is False and m5["btn"] == "收起" and m5["aria"] == "true" \
                and "还没有记忆喵" in m5["text"], f"回展开应收起按钮消失、空态仍显示: {m5}"
            print("[16] 记忆显示开关 OK | 默认收起→展开→展开态刷新出内容→收起→收起态清空→回展开 全过")

            # ---- [17] 上下文保留项（reference/pinned 线索管理，codex 09-09 M1）----
            # contextItemsDetails 默认收起；设置浮层此时仍开着（[12] 起没动）。
            c0 = page.evaluate("""() => {
                const d = document.getElementById('contextItemsDetails');
                const b = document.getElementById('toggleContextItemsBtn');
                return { hidden: d.hidden, btn: b.textContent, aria: b.getAttribute('aria-expanded') };
            }""")
            assert c0["hidden"] is True and c0["btn"] == "展开" and c0["aria"] == "false", \
                f"上下文保留项默认应收起: {c0}"
            # 展开：details 可见、按钮「收起」、空态
            page.click("#toggleContextItemsBtn")
            c1 = page.evaluate("""() => ({
                hidden: document.getElementById('contextItemsDetails').hidden,
                btn: document.getElementById('toggleContextItemsBtn').textContent,
                text: document.getElementById('contextItemsList').textContent
            })""")
            assert c1["hidden"] is False and c1["btn"] == "收起" and "还没有可管理的保留项喵" in c1["text"], \
                f"展开后应可见并显示空态: {c1}"
            # 给当前查看会话造一条 reference 线索，点「刷新」→ 列表出现「暂存」徽标 + 固定钮
            seed_context_item(apd, mem_sid, "已识别的测试赛程表 C:/赛程.png")
            page.click("#refreshContextItemsBtn")
            page.wait_for_timeout(500)
            c2 = page.evaluate("""() => {
                const row = document.querySelector('#contextItemsList .context-item');
                return { has: !!row, badge: row ? row.querySelector('.context-policy').textContent : null,
                         text: row ? row.querySelector('.context-item-text').textContent : '',
                         pinBtn: row ? !!row.querySelector('.context-action:not(.danger)') : false };
            }""")
            assert c2["has"] and c2["badge"] == "暂存" and "测试赛程" in c2["text"] and c2["pinBtn"], \
                f"刷新应列出 reference 线索、带暂存徽标与固定钮: {c2}"
            # 点「固定」→ 徽标变「长期」、固定钮消失（pinned 项不再给固定钮）
            page.click("#contextItemsList .context-item .context-action:not(.danger)")
            page.wait_for_timeout(600)
            c3 = page.evaluate("""() => {
                const row = document.querySelector('#contextItemsList .context-item');
                return { badge: row ? row.querySelector('.context-policy').textContent : null,
                         pinBtn: row ? !!row.querySelector('.context-action:not(.danger)') : false,
                         btn: document.getElementById('toggleContextItemsBtn').textContent };
            }""")
            assert c3["badge"] == "长期" and c3["pinBtn"] is False, f"固定后徽标应变长期且固定钮消失: {c3}"
            # 点「移除」→ 回空态；开关状态不被翻转
            page.click("#contextItemsList .context-item .context-action.danger")
            page.wait_for_timeout(600)
            c4 = page.evaluate("""() => ({
                text: document.getElementById('contextItemsList').textContent,
                btn: document.getElementById('toggleContextItemsBtn').textContent,
                aria: document.getElementById('toggleContextItemsBtn').getAttribute('aria-expanded')
            })""")
            assert "还没有可管理的保留项喵" in c4["text"], f"移除后应回空态: {c4['text']}"
            print("[17] 上下文保留项 OK | 默认收起→展开空态→种子出现(暂存)→固定(长期)→移除回空 全过")

            browser.close()
    finally:
        stop_server(proc)

    print("\n===== 结果 =====")
    if console_errors:
        print("⚠️ 控制台报错：")
        for e in console_errors[:20]:
            print("  ", e)
        sys.exit(1)
    else:
        print("✅ 控制台无报错")
    print("隔离 APPDATA:", apd)


if __name__ == "__main__":
    main()

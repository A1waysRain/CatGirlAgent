# -*- coding: utf-8 -*-
"""隔离 APPDATA 验证定点提醒：scheduler 增删/触发链路 + API + 兜底正则。

注意：Scheduler 实例从磁盘 alarms.json 加载，多实例共享同一 APPDATA 会串，
故每节用独立临时目录（与真实部署"单进程单 scheduler"一致）。
"""
import os, sys, tempfile, shutil, datetime, time, types

sys.path.insert(0, ".")

# WSL 不带 Windows 注册表模块；本回归不覆盖开机自启，提供设置路由所需最小接口。
if "winreg" not in sys.modules:
    fake_winreg = types.ModuleType("winreg")
    fake_winreg.HKEY_CURRENT_USER = fake_winreg.KEY_SET_VALUE = fake_winreg.KEY_READ = fake_winreg.REG_SZ = 0
    fake_winreg.OpenKey = lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError())
    sys.modules["winreg"] = fake_winreg

import backend.scheduler as S_mod
from backend.scheduler import Scheduler
from backend.notifier import notifier
from backend.sessions import get_current, get_session

_tmp_dirs = []

def fresh_appdata():
    d = tempfile.mkdtemp(prefix="catgirl_alarm_")
    os.environ["APPDATA"] = d
    _tmp_dirs.append(d)
    return d

def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    return cond

def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return False
    except ValueError:
        return True

ok = True

# ---- 1. scheduler 增删 + 校验（独立目录 A） ----
fresh_appdata()
s = Scheduler()
a = s.add_alarm("10:30", "做事")
ok &= check("add once", a["time"] == "10:30" and a["repeat"] == "once" and len(a["id"]) == 12)
ok &= check("add daily", s.add_alarm("08:00", "喝水", "daily")["repeat"] == "daily")
ok &= check("add weekly", s.add_alarm("14:30", "健身", "weekly", [1, 3])["repeat"] == "weekly")
ok &= check("weekly无星期退化daily", s.add_alarm("09:00", "x", "weekly")["repeat"] == "daily")
ok &= check("bad time reject", raises(s.add_alarm, "25:99", "x"))
ok &= check("bad time reject2", raises(s.add_alarm, "十点半", "x"))  # 必须 HH:MM（工具层）
ok &= check("empty msg reject", raises(s.add_alarm, "10:30", "  "))
ok &= check("list count 4", len(s.list_alarms()) == 4)
alarms = s.list_alarms()
aid = alarms[0]["id"]
ok &= check("delete ok", s.delete_alarm(aid) and len(s.list_alarms()) == 3)
ok &= check("delete missing", not s.delete_alarm("nope"))

# ---- 2. 触发链路（独立目录 B + monkeypatch 固定时间 2026-08-17 10:30） ----
fresh_appdata()
FIXED = datetime.datetime(2026, 8, 17, 10, 30, 0)
class FakeDT:
    @classmethod
    def now(cls):
        return FIXED
S_mod.datetime = FakeDT

today_cn = FIXED.weekday() + 1            # 1=周一…7=周日
other_cn = today_cn % 7 + 1               # 换个别的星期
s2 = Scheduler()
s2.add_alarm("10:30", "一次性提醒")                 # once → 触发后移除
s2.add_alarm("10:30", "每天提醒", "daily")          # daily → 触发，last_fire=今天
s2.add_alarm("10:30", "每周今天", "weekly", [today_cn])    # weekly 今天 → 触发
s2.add_alarm("10:30", "每周别的天", "weekly", [other_cn])  # weekly 非今天 → 不触发
s2.add_alarm("11:00", "还没到点", "daily")          # 时间没到 → 不触发

s2._alarm_tick(time.time())
# 触发结果：一次性提醒被移除；每天/每周今天 触发但保留（last_fire=今天）；每周别的天/还没到点 未触发保留
left = set(a["message"] for a in s2.list_alarms())
ok &= check("触发后剩4条", left == {"还没到点", "每周别的天", "每周今天", "每天提醒"}, str(left))
# 桌宠气泡通道（notifier 有 3 条 notify）
got = []
for _ in range(3):
    e = notifier.get(timeout=0.1)
    if e: got.append(e.get("message", ""))
ok &= check("notifier 3条", len(got) == 3 and all("喵~" in m for m in got), str(got))
# 聊天窗队列
got2 = []
for _ in range(3):
    e = s2.get_chat_alert(timeout=0.1)
    if e: got2.append(e["message"])
ok &= check("chat队列 3条", len(got2) == 3, str(got2))
# 会话落库（assistant 3 条）
sess = get_session(get_current())
bot_msgs = [m["content"] for m in sess["messages"] if m["role"] == "assistant"]
ok &= check("会话落库3条assistant", len(bot_msgs) == 3 and all("⏰" in m for m in bot_msgs), str(bot_msgs))
# 二次 tick 不再重复（daily last_fire=today，once 已移除，weekly 今天已触发）
s2._alarm_tick(time.time())
ok &= check("二次tick不重复", s2.get_chat_alert(timeout=0.05) is None)
# daily 记下 last_fire=今天
d = next((a for a in s2.list_alarms() if a["message"] == "每天提醒"), None)
ok &= check("daily last_fire=今天", d is not None and d["last_fire"] == "2026-08-17", str(d))

# ---- 3. API（独立目录 C + TestClient，走模块级单例） ----
fresh_appdata()
from fastapi.testclient import TestClient
import backend.main as mainmod
# 清掉模块单例从旧目录加载的残留（单例在 import 时已初始化，需手动清空再落盘到新目录）
mainmod.scheduler._alarms.clear()
mainmod.scheduler._save_alarms()
tc = TestClient(mainmod.create_app())

r = tc.get("/api/alarms")
ok &= check("GET /api/alarms 空", r.status_code == 200 and r.json()["alarms"] == [], str(r.json()))
r = tc.post("/api/alarms", json={"time": "25:99", "message": "x"})
ok &= check("POST 坏时间 400", r.status_code == 400)
r = tc.post("/api/alarms", json={"time": "12:00", "message": "午休", "repeat": "daily"})
ok &= check("POST 添加 200", r.status_code == 200 and r.json()["alarm"]["time"] == "12:00")
aid = r.json()["alarm"]["id"]
r = tc.get("/api/alarms")
ok &= check("GET 列表有1条", len(r.json()["alarms"]) == 1)
r = tc.delete(f"/api/alarms/{aid}")
ok &= check("DELETE 200", r.status_code == 200)
r = tc.delete("/api/alarms/不存在id")
ok &= check("DELETE 不存在 404", r.status_code == 404)
# chat_events：先塞一条再拉（长轮询端点同步返回）
mainmod.scheduler._chat_alerts.put({"type": "alarm", "message": "测试提醒"})
r = tc.get("/api/alarms/chat_events")
data = r.json()
ok &= check("chat_events 取到", data.get("type") == "alarm" and data.get("message") == "测试提醒", str(data))

# ---- 4. 兜底正则 _extract_alarm_request ----
from backend.routers.chat import _extract_alarm_request as F
cases = [
    ("十点半提醒我做事", "10:30", "做事", "once", []),
    ("10:30提醒我做事", "10:30", "做事", "once", []),
    ("每天8点提醒我喝水", "08:00", "喝水", "daily", []),
    ("晚上11点提醒我睡觉", "23:00", "睡觉", "once", []),
    ("下午3点提醒我写周报", "15:00", "写周报", "once", []),
    ("提醒我十点半喝水", "10:30", "喝水", "once", []),
    ("叫我8点起床", "08:00", "起床", "once", []),
    ("每周三14:30提醒我健身", "14:30", "健身", "weekly", [3]),
]
for text, tm, msg, rep, wd in cases:
    got = F(text)
    ok &= check(f"正则 {text!r}", bool(got) and got["time"] == tm and got["message"] == msg
                and got["repeat"] == rep and got["weekdays"] == wd, str(got))
for text in ["帮我搜一下今天天气", "别提醒我睡觉", "我十点半要提醒别人做事",
             "搜狗输入法真难用", "早上好喵", "", "每周一提醒我开会"]:
    got = F(text)
    ok &= check(f"正则负例 {text!r}", got is None, str(got))

for d in _tmp_dirs:
    shutil.rmtree(d, ignore_errors=True)
print("ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)

# -*- coding: utf-8 -*-
"""隔离 APPDATA 验证"几天后提醒"整套：date 闹钟 / 错过检测 / 批量建 / 判早晚 / 日期识别。

同 _verify_alarms.py：Scheduler 实例从磁盘 alarms.json 加载，多实例共享 APPDATA 会串，
每节用独立临时目录。_alarm_tick 的 now 参数传 FakeDT 的 epoch，与假时钟保持一致。
"""
import os, sys, tempfile, shutil, datetime, time

sys.path.insert(0, ".")

import backend.scheduler as S_mod
from backend.scheduler import Scheduler
from backend.notifier import notifier

_tmp_dirs = []

def fresh_appdata():
    d = tempfile.mkdtemp(prefix="catgirl_alarm_date_")
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

# 假时钟：把 scheduler 模块的 datetime 换成 FakeDT（now 返回固定时刻；strptime/combine 走真实现）
class FakeDT(datetime.datetime):
    _fixed = None
    @classmethod
    def now(cls, tz=None):
        return cls._fixed

def set_now(dt):
    S_mod.datetime = FakeDT
    FakeDT._fixed = dt

# ---- 1. add_alarm 日期校验（真实时间，日期动态取未来，防随真实日期过期） ----
fresh_appdata()
_future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
s = Scheduler()
a = s.add_alarm("10:30", "看比赛", date=_future)
ok &= check("add 带日期", a["date"] == _future and a["fired"] is False and a["repeat"] == "once", str(a))
ok &= check("add 无日期 date=None", s.add_alarm("08:00", "喝水")["date"] is None)
ok &= check("daily 带日期被忽略", s.add_alarm("08:00", "喝水", "daily", date="2026-09-04")["date"] is None)
ok &= check("坏日期格式拒绝", raises(s.add_alarm, "10:30", "x", date="2026/09/04"))
ok &= check("过去日期拒绝", raises(s.add_alarm, "10:30", "x", date="2020-01-01"))

# ---- 2. 到点触发（假时钟：10:29 建闹钟 → 10:30 tick） ----
fresh_appdata()
set_now(datetime.datetime(2026, 8, 21, 10, 29, 0))
s2 = Scheduler()
s2.add_alarm("10:30", "看比赛直播", date="2026-08-21")
s2.add_alarm("10:30", "老式一次性")          # 无日期：老行为
set_now(datetime.datetime(2026, 8, 21, 10, 30, 0))
s2._alarm_tick(FakeDT._fixed.timestamp())
ok &= check("到点触发后全移除", s2.list_alarms() == [], str(s2.list_alarms()))
got = []
for _ in range(2):
    e = notifier.get(timeout=0.1)
    if e: got.append(e.get("message", ""))
ok &= check("触发两条提醒", len(got) == 2 and any("看比赛直播" in m for m in got), str(got))
# 排空聊天窗队列（第一次 tick 也往这里推了 2 条）
for _ in range(2):
    s2.get_chat_alert(timeout=0.1)
# 二次 tick 不再有残留
s2._alarm_tick(FakeDT._fixed.timestamp())
ok &= check("二次tick无动作", s2.get_chat_alert(timeout=0.05) is None)

# ---- 3. 错过检测（8点建 10:30 闹钟，快进到 12:00 tick） ----
fresh_appdata()
set_now(datetime.datetime(2026, 8, 21, 8, 0, 0))
s3 = Scheduler()
s3.add_alarm("10:30", "看比赛直播", date="2026-08-21")      # 建的时候是未来，成功
set_now(datetime.datetime(2026, 8, 21, 12, 0, 0))           # 快进到 12:00（闹钟已过且没触发过）
s3._alarm_tick(FakeDT._fixed.timestamp())
ok &= check("错过闹钟被移除", s3.list_alarms() == [], str(s3.list_alarms()))
ev = s3.get_chat_alert(timeout=0.1)
ok &= check("错过提示文案", bool(ev) and "错过" in ev["message"] and "看比赛直播" in ev["message"], str(ev))
# 同一天 12:00，未到点的闹钟不该被误判错过
fresh_appdata()
set_now(datetime.datetime(2026, 8, 21, 8, 0, 0))
s3b = Scheduler()
s3b.add_alarm("18:30", "练习赛", date="2026-08-21")
set_now(datetime.datetime(2026, 8, 21, 12, 0, 0))
s3b._alarm_tick(FakeDT._fixed.timestamp())
ok &= check("未到点不误判错过", len(s3b.list_alarms()) == 1 and s3b.get_chat_alert(timeout=0.05) is None, str(s3b.list_alarms()))

# ---- 4. 批量建 ----
fresh_appdata()
s4 = Scheduler()
created, errors = s4.add_alarms_batch([
    {"date": "2026-09-04", "time": "18:30", "message": "意大利第一次练习"},
    {"time": "20:00", "message": "每天散步", "repeat": "daily"},
    {"time": "25:99", "message": "坏时间"},
])
ok &= check("batch 建2条", len(created) == 2, str(created))
ok &= check("batch 1条失败", len(errors) == 1 and "时间格式" in errors[0]["error"], str(errors))
created2, errors2 = s4.add_alarms_batch([{"date": "2020-01-01", "time": "10:00", "message": "过去"}])
ok &= check("batch 过去日期失败", len(created2) == 0 and len(errors2) == 1, str(errors2))
ok &= check("batch 空列表", s4.add_alarms_batch([]) == ([], []))

# ---- 5. 判早晚 / 日期识别（_extract_alarm_request，patch chat 模块的 datetime） ----
import backend.routers.chat as chat_mod
from backend.routers.chat import _extract_alarm_request as F

class FakeDT2(datetime.datetime):
    _fixed = None
    @classmethod
    def now(cls, tz=None):
        return cls._fixed
chat_mod.datetime = FakeDT2
def set_parse_now(dt):
    FakeDT2._fixed = dt

# 下午 3 点说"待会4点" → 今天 16:00
set_parse_now(datetime.datetime(2026, 8, 19, 15, 0, 0))
g = F("待会4点提醒我看比赛")
ok &= check("待会4点→16:00今天", bool(g) and g["time"] == "16:00" and g["date"] == "2026-08-19", str(g))
# 裸"4点"（下午3点）→ 16:00 今天（1~6 默认下午）
g = F("4点提醒我看比赛")
ok &= check("裸4点下午→16:00", bool(g) and g["time"] == "16:00" and g["date"] == "2026-08-19", str(g))
# 凌晨 2 点说"4点" → 04:00 今天
set_parse_now(datetime.datetime(2026, 8, 19, 2, 0, 0))
g = F("4点提醒我看比赛")
ok &= check("凌晨裸4点→04:00", bool(g) and g["time"] == "04:00" and g["date"] == "2026-08-19", str(g))
# 下午 5 点说"4点"（当天 16:00 已过）→ 明天 16:00
set_parse_now(datetime.datetime(2026, 8, 19, 17, 0, 0))
g = F("4点提醒我看比赛")
ok &= check("已过推到明天16:00", bool(g) and g["time"] == "16:00" and g["date"] == "2026-08-20", str(g))
# 明确日期
set_parse_now(datetime.datetime(2026, 8, 19, 15, 0, 0))
g = F("明天下午4点提醒我开会")
ok &= check("明天下午4点", bool(g) and g["time"] == "16:00" and g["date"] == "2026-08-20", str(g))
g = F("3天后晚上8点提醒我打游戏")
ok &= check("3天后晚上8点", bool(g) and g["time"] == "20:00" and g["date"] == "2026-08-22", str(g))
g = F("大后天提醒我体检")
ok &= check("大后天无时间→None", g is None, str(g))
g = F("8月21日14:30提醒我看F1")
ok &= check("8月21日14:30", bool(g) and g["time"] == "14:30" and g["date"] == "2026-08-21", str(g))
g = F("下周一9点提醒我开会")
ok &= check("下周一9点", bool(g) and g["time"] == "09:00" and g["date"] == "2026-08-24", str(g))
# 每天/每周：不带日期
g = F("每周三14:30提醒我健身")
ok &= check("每周三不带日期", bool(g) and g["repeat"] == "weekly" and g["date"] is None, str(g))
g = F("每天8点提醒我喝水")
ok &= check("每天不带日期", bool(g) and g["repeat"] == "daily" and g["date"] is None, str(g))
# 无日期的一次性：今天该时刻（还在未来）
g = F("19点提醒我吃饭")
ok &= check("裸19点→今天19:00", bool(g) and g["time"] == "19:00" and g["date"] == "2026-08-19", str(g))

for d in _tmp_dirs:
    shutil.rmtree(d, ignore_errors=True)
print("ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)

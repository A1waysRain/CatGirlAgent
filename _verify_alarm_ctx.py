# -*- coding: utf-8 -*-
"""回归验证：聊天上下文注入「当前已设提醒」（猫娘"设过却失忆"修复，2026-08-22）。

验证 3 点：
1. scheduler.describe_alarms()：把真实闹钟格式化成喂给模型的话，无提醒返回空串；
2. build_messages()：有提醒时注入一条「当前已设的定时提醒」system 消息，且排在 recent 之前；
3. 提醒清空后 build_messages 不再注入。

用隔离 APPDATA，不碰真实 alarms.json。用法：cd Cat_Girl && .venv 的 python 跑本脚本
"""
import os
import sys
import tempfile
from datetime import date, timedelta

# 必须在 import backend 之前设 APPDATA，让模块单例读隔离目录
tmp = tempfile.mkdtemp(prefix="catgirl_alarmctx_")
os.environ["APPDATA"] = tmp
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8")

from backend import scheduler as scheduler_mod            # noqa: E402
from backend.chat_service import build_messages          # noqa: E402

s = scheduler_mod.scheduler   # 模块级单例（build_messages 局部 import 的就是它）

# ---- 1. describe_alarms 格式化 ----
assert s.describe_alarms() == ""                          # 空列表 → 空串
_fut = (date.today() + timedelta(days=30)).isoformat()
_fut_short = _fut[5:].replace("-", "/")   # YYYY-MM-DD → MM/DD，describe_alarms 的日期前缀格式
s.add_alarm("14:30", "排位赛", repeat="once", date=_fut)
s.add_alarm("08:00", "喝水", repeat="daily")
s.add_alarm("20:00", "打游戏", repeat="weekly", weekdays=[3])
ctx = s.describe_alarms()
assert f"{_fut_short} 14:30 排位赛" in ctx, ctx
assert "每天 08:00 喝水" in ctx, ctx
assert "每周三 20:00 打游戏" in ctx, ctx
assert "别重复建" in ctx
print("✓ 1. describe_alarms 格式化通过")

# ---- 2. build_messages 注入 + 位置 ----
msgs = build_messages("test-sid", [{"role": "user", "content": "排位赛几点开跑呀"}])
injected = [m for m in msgs if m["role"] == "system" and "当前已设的定时提醒" in m["content"]]
assert injected, [m["content"][:30] for m in msgs]
idx = msgs.index(injected[0])
assert msgs[idx + 1]["role"] == "user", "提醒应排在 recent（user 消息）之前"
assert msgs[0]["role"] == "system" and "猫娘" in msgs[0]["content"], "system 提示词仍在首位"
print("✓ 2. build_messages 注入 + 排在 recent 之前")

# ---- 3. 清空后不再注入 ----
for a in s.list_alarms():
    s.delete_alarm(a["id"])
msgs2 = build_messages("test-sid2", [{"role": "user", "content": "hi"}])
assert not any("当前已设的定时提醒" in m.get("content", "") for m in msgs2)
print("✓ 3. 无提醒时不注入")

print("ALL PASS")

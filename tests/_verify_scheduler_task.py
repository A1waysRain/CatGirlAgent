# -*- coding: utf-8 -*-
"""定时任务 M1 回归：动作快照、确认、后台执行、错过与安全边界。"""
import asyncio
import datetime
import os
import shutil
import tempfile
import threading
import time

from backend import scheduler as scheduler_mod
from backend import tools
from backend.scheduler import Scheduler
from backend.routers import alarms as alarms_router

tmp = tempfile.mkdtemp(prefix="catgirl_verify_task_")
os.environ["APPDATA"] = tmp
failed = 0


def check(label, condition, detail=""):
    global failed
    if not condition:
        failed += 1
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


class FixedDateTime(datetime.datetime):
    fixed = datetime.datetime(2026, 9, 3, 9, 0, 0)

    @classmethod
    def now(cls):
        return cls.fixed


original_datetime = scheduler_mod.datetime
original_impl = dict(tools.TOOL_IMPL)
original_run_tool = tools.run_tool
try:
    scheduler_mod.datetime = FixedDateTime
    scheduler = Scheduler()

    def safe_task(name):
        return f"已打开 {name}"

    def dangerous_task(path, confirmed=False):
        return f"写入 {path} confirmed={confirmed}"

    tools.TOOL_IMPL["safe_task_test"] = safe_task
    tools.TOOL_IMPL["danger_task_test"] = dangerous_task

    action = {"tool": "safe_task_test", "args": {"name": "播放器"}}
    alarm = scheduler.add_alarm("09:00", "打开播放器", "daily", action=action)
    action["args"]["name"] = "被篡改"
    loaded = scheduler.list_alarms()[0]
    check("动作快照落盘且隔离外部修改", loaded["action"]["args"]["name"] == "播放器", str(loaded["action"]))
    check("无 action 的旧提醒兼容", scheduler.add_alarm("10:00", "普通提醒").get("action") is None)

    try:
        tools.validate_scheduled_action({"tool": "ui_send", "args": {"window": "微信"}})
        blocked = False
    except ValueError:
        blocked = True
    check("拒绝依赖临时界面状态的动作", blocked)
    try:
        tools.validate_scheduled_action({"tool": "safe_task_test", "args": {"confirmed": True}})
        confirmed_in_args = False
    except ValueError:
        confirmed_in_args = True
    check("快照不允许预置 confirmed", confirmed_in_args)

    scheduler_mod.scheduler._alarms.clear()
    dangerous_action = {"tool": "danger_task_test", "args": {"path": "日报.txt"}}
    denied = tools.tool_set_alarm("11:00", "自动写日报", "daily", action=dangerous_action)
    allowed = tools.tool_set_alarm("11:00", "自动写日报", "daily", action=dangerous_action, confirmed=True)
    check("危险任务创建时必须确认", "安全确认" in denied and "定时任务" in allowed, denied + " | " + allowed)

    original_router_scheduler = alarms_router.scheduler
    alarms_router.scheduler = Scheduler()
    try:
        try:
            asyncio.run(alarms_router.create_alarm({"time": "12:00", "message": "API 危险任务", "action": dangerous_action}))
            api_denied = False
        except Exception as error:
            api_denied = getattr(error, "status_code", None) == 400
        created_api = asyncio.run(alarms_router.create_alarm({"time": "12:00", "message": "API 危险任务", "action": dangerous_action, "confirmed": True}))
        check("API 同样执行危险任务确认门禁", api_denied and created_api["alarm"].get("action") == dangerous_action)
    finally:
        alarms_router.scheduler = original_router_scheduler

    pushed = []
    done = threading.Event()
    scheduler._push_alarm = lambda message: (pushed.append(message), done.set())
    scheduler._alarm_tick(time.time())
    check("到点任务后台执行并推送结果（人话、不露工具名）",
          done.wait(2) and any("已打开 播放器" in message and "safe_task_test" not in message for message in pushed),
          str(pushed))

    calls = []
    done.clear()
    pushed.clear()
    scheduler2 = Scheduler()
    scheduler2._alarms = []
    scheduler2._push_alarm = lambda message: (pushed.append(message), done.set())
    scheduler2.add_alarm("09:00", "写入日报", "daily", action={"tool": "danger_task_test", "args": {"path": "日报.txt"}})
    def capture_run(name, args):
        calls.append((name, dict(args)))
        return tools.TOOL_IMPL[name](**args)
    tools.run_tool = capture_run
    scheduler2._alarm_tick(time.time())
    check("危险任务到点自动注入 confirmed", done.wait(2) and calls and calls[0][1].get("confirmed") is True, str(calls))

    done.clear()
    pushed.clear()
    scheduler3 = Scheduler()
    scheduler3._alarms = []
    scheduler3._push_alarm = lambda message: (pushed.append(message), done.set())
    scheduler3.add_alarm("09:00", "失败任务", "daily", action={"tool": "safe_task_test", "args": {"name": "坏动作"}})
    tools.run_tool = lambda name, args: "工具执行失败喵：模拟错误"
    scheduler3._alarm_tick(time.time())
    check("任务失败如实上报（含原因、不露工具名）",
          done.wait(2) and any("没做成" in message and "工具执行失败" in message and "safe_task_test" not in message for message in pushed),
          str(pushed))

    release = threading.Event()
    started = threading.Event()
    def slow_run(name, args):
        started.set()
        release.wait(2)
        return "慢任务完成"
    tools.run_tool = slow_run
    scheduler4 = Scheduler()
    scheduler4._alarms = []
    scheduler4._push_alarm = lambda message: None
    scheduler4.add_alarm("09:00", "慢任务", "daily", action={"tool": "safe_task_test", "args": {"name": "慢"}})
    before = time.monotonic()
    scheduler4._alarm_tick(time.time())
    elapsed = time.monotonic() - before
    check("慢任务不阻塞调度 tick", elapsed < 0.2 and started.wait(1), f"{elapsed:.3f}s")
    release.set()

    called = []
    scheduler5 = Scheduler()
    scheduler5._push_alarm = lambda message: pushed.append(message)
    scheduler5._alarms = [{"id": "missed-task", "date": "2026-09-02", "time": "09:00", "message": "过期动作", "repeat": "once", "weekdays": [], "fired": False, "last_fire": None, "action": {"tool": "safe_task_test", "args": {"name": "不应运行"}}}]
    tools.run_tool = lambda name, args: called.append((name, args)) or "不应运行"
    scheduler5._alarm_tick(time.time())
    check("错过一次性任务不补执行", not called and not scheduler5.list_alarms())

    log = os.path.join(tmp, "catgirl", "actions.log")
    check("任务级审计写入 actions.log", os.path.exists(log) and "scheduled_task" in open(log, encoding="utf-8").read())
finally:
    scheduler_mod.datetime = original_datetime
    tools.TOOL_IMPL.clear()
    tools.TOOL_IMPL.update(original_impl)
    tools.run_tool = original_run_tool
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL PASS" if failed == 0 else "HAS FAILURES")
raise SystemExit(1 if failed else 0)

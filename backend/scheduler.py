"""定时提醒调度器（桌宠扩展 M4）。

轻量后台线程，周期 tick，到期通过 notifier 推事件给桌宠（喝水/久坐/深夜劝睡/
番茄钟）。所有提醒项受 settings.py 开关控制（默认全关，实时读设置生效）。

事件类型：
- notify:  {"type":"notify","message":"..."}  —— 普通提醒（桌宠蹦跳+持久气泡，点一下才关）
- bubble:  {"type":"bubble","key":"pomodoro","action":"show","text":"..."}
           / {"action":"hide"}                 —— 番茄钟倒计时持久气泡（可更新）

定点提醒（闹钟）：`add_alarm()` 建（工具 set_alarm / 面板 POST /api/alarms 都走它），
持久化到 %APPDATA%/catgirl/alarms.json。到点触发四件事：
1. notifier 推 notify → 桌宠蹦跳+持久气泡（点一下才关）；
2. notifier.show_window() → 猫娘主窗弹前台 + 任务栏闪烁（力度升级）；
3. append_message 写进当前会话历史（assistant 消息，跨窗口可查）；
4. _chat_alerts 队列 → 前端 /api/alarms/chat_events 长轮询 → 聊天窗弹猫娘消息。
提醒文案从多套变体里随机挑（力度升级：别每次都一个样）。
repeat=once 触发后自动移除；daily/weekly 记 last_fire=今天防 10s tick 重复触发。
once 可带 date（YYYY-MM-DD，具体某一天）；日期时间已过且从未触发（如 app 当时没开）
→ 判定"错过"，推错过提示后移除。add_alarms_batch() 批量建（表格/图片提取的一堆提醒）。

生命周期：由 main.py 的 create_app() 调用 scheduler.start()；线程为 daemon，
随进程退出，无需显式 stop。
"""
import json
import inspect
import asyncio
import os
import queue
import random
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .notifier import notifier
from .settings import load_settings
from .sessions import append_message as session_append, get_current

WATER_INTERVAL = 45 * 60        # 喝水提醒：每 45 分钟
STRETCH_INTERVAL = 60 * 60      # 久坐提醒：每 60 分钟
SLEEP_NUDGE_WINDOW = (23, 6)    # 深夜劝睡时段：23:00–06:00
SLEEP_NUDGE_INTERVAL = 40 * 60  # 深夜每 40 分钟最多劝一次，别刷屏
TICK = 10                       # 调度器每 10 秒 tick 一次

# ---- 提醒文案变体（力度升级：多套换着随机挑，别每次都一个样） ----
WATER_MESSAGES = [
    "喝口水喵~ 主人别光顾着聊天嘛，嗓子会干的！",
    "💧 主人主人，该补水啦！本喵帮你记着时间呢，起来喝一口喵~",
    "咕噜咕噜……主人，水都快放凉了，快起来喝口水喵！",
]
STRETCH_MESSAGES = [
    "起来活动一下喵，久坐腰会酸的~ 跟本喵一起伸个懒腰？",
    "🧘 主人坐了好一会儿啦，站起来走两步喵~ 本喵给你打气！",
    "一直坐着可不行喵！扭扭脖子、抻抻腿，本喵在旁边陪着你~",
]
SLEEP_NUDGE_MESSAGES = [
    "都这么晚啦，主人还不睡喵？熬夜会秃的，本喵可心疼了！",
    "🌙 主人，已经深夜啦~ 再熬下去本喵要着急了，快去睡觉好不好？",
    "呼啊……本喵都困了，主人还在熬夜喵？快躺下，明天才有精神！",
]
POMODORO_END_WORK = [
    "🍅 工作结束，休息一下喵~ 起来活动活动，本喵给你放个假！",
    "🍅 番茄钟响啦，休息休息喵~ 主人辛苦了，喝口水歇会儿！",
]
POMODORO_END_BREAK = [
    "🍅 休息结束，继续加油喵~ 主人是最棒的，冲鸭！",
    "🍅 休息时间到，该回来干活啦喵~ 本喵相信你能行！",
]
ALARM_SUFFIXES = [
    " 主人别忘了喵~",
    " 快看快看，本喵喊你啦喵~",
    " 这件事可别错过喵~",
]
MISSED_MESSAGES = [
    " 已经过去啦，下次本喵盯紧点喵~",
    " 可惜本喵没喊到，主人下次别漏啦喵~",
    " 错过了就错过了，本喵下次早点喊你喵~",
]


def _pick(messages: list[str]) -> str:
    """随机挑一条文案（提醒力度升级：多条变体换着来）。"""
    return random.choice(messages)


def _foreground_fullscreen() -> bool:
    """前台是不是被全屏/无边框应用占着（游戏、全屏视频等）。

    全屏时提醒不把聊天窗顶到前台抢焦点：桌宠气泡置顶可见、任务栏闪烁也不抢，
    提醒照样到，只是不打断正在全屏干的事（比如打 ACC 刷圈）。
    判断法：前台窗口盖满屏幕（容差 8px，兼容无边框）+ 没有标题栏
    （WS_CAPTION）——区分「真全屏/无边框」和「普通窗口最大化」（最大化窗口
    有标题栏，不算全屏，平时提醒照旧弹窗）。主屏坐标为主，副屏全屏不拦
    （提示会偏保守：最多是平时那样弹窗，不会误伤）。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        fg = user32.GetForegroundWindow()
        if not fg:
            return False
        r = wintypes.RECT()
        user32.GetWindowRect(fg, ctypes.byref(r))
        sw = user32.GetSystemMetrics(0)   # SM_CXSCREEN：主屏宽
        sh = user32.GetSystemMetrics(1)   # SM_CYSCREEN：主屏高
        w, h = r.right - r.left, r.bottom - r.top
        if not (w >= sw - 8 and h >= sh - 8):
            return False
        style = user32.GetWindowLongW(fg, -16)  # GWL_STYLE
        WS_CAPTION = 0x00C00000                 # WS_BORDER | WS_DLGFRAME
        return not bool(style & WS_CAPTION)
    except Exception:
        return False


class Scheduler:
    def __init__(self) -> None:
        # RLock：_pomodoro_tick 持锁时会调 _pomodoro_push（也要拿锁），必须可重入
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = False
        self._last_fire: dict[str, float] = {}  # 提醒项名 -> 上次触发时间戳
        self._pomodoro: dict | None = None      # {"phase","end","work","break"}
        # 定点提醒（闹钟）：持久化到 alarms.json；聊天窗推送走独立队列
        # （不复用 notifier——那是桌宠的消费通道，聊天窗再 poll 会互相抢事件）
        self._alarms: list[dict] = []
        self._running_tasks: set[str] = set()
        self._chat_alerts: queue.Queue = queue.Queue()
        self._load_alarms()

    # ---- 生命周期 ----
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop = True

    def _loop(self) -> None:
        while True:
            with self._lock:
                if self._stop:
                    return
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(TICK)

    # ---- 主循环 ----
    def _tick(self) -> None:
        s = load_settings()
        now = time.time()
        self._remind(s, "water_reminder", WATER_INTERVAL, now, WATER_MESSAGES)
        self._remind(s, "stretch_reminder", STRETCH_INTERVAL, now, STRETCH_MESSAGES)
        self._sleep_nudge(s, now)
        self._pomodoro_tick(s, now)
        self._alarm_tick(now)

    def _remind(self, s: dict, key: str, interval: float, now: float, messages: list[str]) -> None:
        """周期性提醒：开关开启且距上次触发超过 interval 才推。

        文案从 messages 随机挑一套；到点除了桌宠气泡，还把主窗弹到前台
        （notifier.show_window → 任务栏闪烁，见 desktop/app.py）。
        """
        if not s.get(key):
            self._last_fire.pop(key, None)
            return
        last = self._last_fire.get(key, 0.0)
        if now - last >= interval:
            self._last_fire[key] = now
            notifier.put({"type": "notify", "message": _pick(messages)})
            self._pop_main_window()

    def _sleep_nudge(self, s: dict, now: float) -> None:
        if not s.get("sleep_nudge"):
            self._last_fire.pop("sleep_nudge", None)
            return
        h = datetime.now().hour
        lo, hi = SLEEP_NUDGE_WINDOW
        in_window = h >= lo or h < hi  # 23:00–06:00（跨零点）
        if not in_window:
            self._last_fire.pop("sleep_nudge", None)
            return
        last = self._last_fire.get("sleep_nudge", 0.0)
        if now - last >= SLEEP_NUDGE_INTERVAL:
            self._last_fire["sleep_nudge"] = now
            notifier.put({"type": "notify", "message": _pick(SLEEP_NUDGE_MESSAGES)})
            self._pop_main_window()

    def _pop_main_window(self) -> None:
        """提醒到点把主窗弹前台；前台全屏（游戏）时不抢焦点，只留桌宠气泡。

        提醒力度（2026-08-17 用户拍板弹主窗）要在，但不能打断全屏游戏——
        全屏时跳过 show_window（桌宠气泡置顶可见、任务栏闪烁不抢焦点，提醒
        照样到），平时照旧弹窗+闪烁。
        """
        if _foreground_fullscreen():
            return
        notifier.show_window()

    # ---- 番茄钟 ----
    def pomodoro_start(self, work: int = 25, break_min: int = 5) -> dict:
        """开始番茄钟（工作阶段），返回状态。"""
        with self._lock:
            self._pomodoro = {
                "phase": "work",
                "end": time.time() + work * 60,
                "work": work,
                "break": break_min,
            }
        self._pomodoro_push()
        return self.pomodoro_state()

    def pomodoro_stop(self) -> dict:
        """停止番茄钟，收起倒计时气泡。"""
        with self._lock:
            self._pomodoro = None
        notifier.put({"type": "bubble", "action": "hide", "key": "pomodoro"})
        return self.pomodoro_state()

    def pomodoro_state(self) -> dict:
        """当前番茄钟状态（桌宠右键菜单 / 前端据此显示）。"""
        with self._lock:
            p = self._pomodoro
            if not p:
                return {"running": False}
            remain = max(0, int(p["end"] - time.time()))
            return {"running": True, "phase": p["phase"], "remaining": remain}

    def _pomodoro_tick(self, s: dict, now: float) -> None:
        """番茄钟：设置开关关闭则停止；否则推进阶段并每 tick 更新倒计时气泡。"""
        if not s.get("pomodoro"):
            if self._pomodoro is not None:
                self.pomodoro_stop()
            return
        with self._lock:
            p = self._pomodoro
            if not p:
                return
            if now >= p["end"]:
                # 阶段切换：工作→休息→工作…
                if p["phase"] == "work":
                    p["phase"] = "break"
                    p["end"] = now + p["break"] * 60
                    msg = _pick(POMODORO_END_WORK)
                else:
                    p["phase"] = "work"
                    p["end"] = now + p["work"] * 60
                    msg = _pick(POMODORO_END_BREAK)
                self._pomodoro_push()
                notifier.put({"type": "notify", "message": msg})
                self._pop_main_window()
            else:
                self._pomodoro_push()  # 每 tick（10s）更新一次倒计时气泡

    def _pomodoro_push(self) -> None:
        with self._lock:
            p = self._pomodoro
            if not p:
                return
            remain = max(0, int(p["end"] - time.time()))
            mm, ss = divmod(remain, 60)
            label = "专注" if p["phase"] == "work" else "休息"
            text = f"🍅 {label} {mm:02d}:{ss:02d}"
        notifier.put({"type": "bubble", "action": "show", "key": "pomodoro", "text": text})

    # ---- 定点提醒（闹钟）----
    def _alarms_file(self) -> Path:
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl" / "alarms.json"

    def _load_alarms(self) -> None:
        """从磁盘读回提醒列表（启动时调用，失败不崩）。"""
        try:
            f = self._alarms_file()
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._alarms = data
        except Exception:
            self._alarms = []

    def _save_alarms(self) -> None:
        """落盘提醒列表（必须持锁调用）。"""
        try:
            f = self._alarms_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(self._alarms, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def add_alarm(self, time: str, message: str, repeat: str = "once",
                  weekdays: list | None = None, date: str | None = None,
                  action: dict | None = None, goal: str | None = None,
                  scope: dict | None = None) -> dict:
        """新建定点提醒，返回新提醒对象。

        time 形如 '10:30'（HH:MM）；date 形如 '2026-08-21'（仅 repeat='once' 有意义，
        具体某一天提醒；不填则最近一次到该时刻触发）；repeat: once 一次性 / daily 每天 /
        weekly 每周指定天；weekdays 用 1=周一…7=周日（贴合 LLM 中文习惯），仅 weekly 有意义。
        一次性 + 日期已过 → 拒绝（避免建个永不触发或一建就"错过"的提醒）。
        """
        time = (time or "").strip()
        message = (message or "").strip()
        date = (date or "").strip() or None
        # 工具层/API 都复用同一校验，旧 alarms.json 无 action 则自然兼容。
        if action is not None:
            from .tools import validate_scheduled_action
            action = validate_scheduled_action(action)
        if action is not None and goal is not None:
            raise ValueError("定时任务只能选择固定 action 或自主 goal 其中一种")
        if goal is not None or scope is not None:
            from .tools import validate_scheduled_goal
            goal, scope = validate_scheduled_goal(goal, scope)
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", time):
            raise ValueError("时间格式不对喵，要用 HH:MM（比如 10:30）")
        if not message:
            raise ValueError("提醒内容不能为空喵")
        if repeat not in ("once", "daily", "weekly"):
            repeat = "once"
        wd = [int(w) for w in (weekdays or []) if str(w).isdigit() and 1 <= int(w) <= 7]
        if repeat == "weekly" and not wd:
            repeat = "daily"  # 说每周又没给星期 → 退化成每天，别建了个永不触发的
        if date:
            if repeat != "once":
                date = None  # 每天/每周循环没有"某一天"的概念，日期只对一次性有意义
            else:
                try:
                    dt_date = datetime.strptime(date, "%Y-%m-%d")
                except ValueError:
                    raise ValueError("日期格式不对喵，要用 YYYY-MM-DD（比如 2026-08-21）")
                target = datetime.combine(dt_date.date(),
                                          datetime.strptime(time, "%H:%M").time())
                if target <= datetime.now():
                    raise ValueError("这个时间已经过了喵，换个还没到的时间吧")
        alarm = {
            "id": uuid.uuid4().hex[:12],
            "date": date,
            "time": time,
            "message": message,
            "repeat": repeat,
            "weekdays": wd,
            "fired": False,
            "last_fire": None,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "goal": goal,
            "scope": scope,
        }
        with self._lock:
            self._alarms.append(alarm)
            self._save_alarms()
        return dict(alarm)

    def add_alarms_batch(self, alarms: list) -> tuple[list, list]:
        """批量新建提醒：逐条调 add_alarm，返回 (建成功的列表, 失败明细列表)。

        给表格/图片提取的一堆提醒用——一次建完，省得 agent 循环一轮只能建一条。
        """
        created: list[dict] = []
        errors: list[dict] = []
        for item in alarms or []:
            if not isinstance(item, dict):
                errors.append({"item": item, "error": "每条提醒要是对象喵"})
                continue
            try:
                candidate = {
                    "time": str(item.get("time") or ""),
                    "message": str(item.get("message") or ""),
                    "repeat": str(item.get("repeat") or "once"),
                    "weekdays": item.get("weekdays") or [],
                    "date": item.get("date") or None,
                    "action": None,
                    "goal": None,
                    "scope": None,
                }
                if self.find_exact_alarm(candidate):
                    errors.append({"item": item, "error": "already_exists"})
                    continue
                a = self.add_alarm(**candidate)
                created.append(a)
            except ValueError as e:
                errors.append({"item": item, "error": str(e)})
        return created, errors

    def list_alarms(self) -> list[dict]:
        with self._lock:
            return [dict(a) for a in self._alarms]

    @staticmethod
    def _alarm_key(alarm: dict) -> tuple:
        """提醒的完整身份；只有所有调度字段一致才可认定为同一条。"""
        return (
            alarm.get("date") or None,
            alarm.get("time") or "",
            alarm.get("message") or "",
            alarm.get("repeat", "once"),
            tuple(int(w) for w in (alarm.get("weekdays") or [])),
            json.dumps(alarm.get("action"), ensure_ascii=False, sort_keys=True),
            json.dumps(alarm.get("goal"), ensure_ascii=False, sort_keys=True),
            json.dumps(alarm.get("scope"), ensure_ascii=False, sort_keys=True),
        )

    def find_exact_alarm(self, alarm: dict) -> dict | None:
        """查找当前仍存在、且日期时分内容与调度参数全等的提醒。"""
        key = self._alarm_key(alarm)
        with self._lock:
            for current in self._alarms:
                if self._alarm_key(current) == key:
                    return dict(current)
        return None

    def describe_alarms(self) -> str:
        """把当前已设提醒格式化成喂给模型的一段话（没有提醒返回空串）。

        聊天上下文用：闹钟存在 alarms.json，但模型从来没见过它——不喂这段，
        猫娘会"设过却失忆"（主人再聊到排位赛，她还问几点开跑、想再建一次）。
        返回的是猫娘语气指令 + 每条闹钟清单，插进 system 消息让模型每轮都看到。
        """
        alarms = self.list_alarms()
        if not alarms:
            return ""
        wd_cn = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
        lines = []
        for a in alarms:
            when = a.get("time", "")
            date = a.get("date") or ""
            if date and len(date) >= 10:
                when = f"{date[5:7]}/{date[8:10]} {when}"   # 2026-08-23 → 08/23
            repeat = a.get("repeat", "once")
            if repeat == "daily":
                when = f"每天 {when}"
            elif repeat == "weekly":
                days = "".join(wd_cn.get(w, "") for w in (a.get("weekdays") or [])) or "?"
                when = f"每周{days} {when}"
            action = a.get("action")
            if action:
                lines.append(f"· 🔧 {when} 自动执行 {action.get('tool')}：{a.get('message') or ''}")
            elif a.get("goal"):
                brief = str(a["goal"]).replace("\n", " ")[:80]
                lines.append(f"· 🎯 {when} 到点自主执行：{brief}")
            else:
                lines.append(f"· ⏰ {when} {a.get('message') or ''}")
        return (
            "【当前已设的定时提醒】系统里已经帮主人设好的闹钟，本喵要记住、别当没设过：\n"
            + "\n".join(lines)
            + "\n主人再聊到这些事时，记得已设过、别重复问时间、也别重复建；"
              "带🔧或🎯的是到点自动跑的任务，主人没要求立刻执行时绝不能现在调用它。"
        )

    def delete_alarm(self, aid: str) -> bool:
        """删除提醒；不存在返回 False。"""
        with self._lock:
            for i, a in enumerate(self._alarms):
                if a["id"] == aid:
                    self._alarms.pop(i)
                    self._save_alarms()
                    return True
        return False

    def get_chat_alert(self, timeout: float = 25) -> dict | None:
        """长轮询取一条聊天窗提醒；超时返回 None（照 notifier.get 模式）。"""
        try:
            return self._chat_alerts.get(timeout=timeout)
        except queue.Empty:
            return None

    def _alarm_datetime(self, a: dict) -> datetime | None:
        """一次性带日期闹钟的目标 datetime（解析失败返回 None）。"""
        try:
            return datetime.strptime(f"{a.get('date')} {a.get('time')}", "%Y-%m-%d %H:%M")
        except Exception:
            return None

    def _missed_msg(self, a: dict) -> str:
        """错过提醒文案：主人没赶上那次提醒（比如 app 当时没开）。"""
        text = a.get("message") or "到点啦"
        return f"⏰ 主人，你错过了「{text}」的提醒喵～{_pick(MISSED_MESSAGES)}"

    def _alarm_tick(self, now: float) -> None:
        """定点提醒主逻辑：到点触发（桌宠气泡 + 会话落库 + 聊天窗推送）。

        一次性带日期：到点触发后移除；日期时间已过但从未触发（如 app 当时没开）
        → 判定"错过"，推错过提示后移除。daily/weekly 记 last_fire=今天防 10s tick 重复。
        """
        if not self._alarms:
            return
        now_dt = datetime.now()
        cur_hhmm = now_dt.strftime("%H:%M")
        today = now_dt.strftime("%Y-%m-%d")
        weekday_idx = now_dt.weekday()  # 0=周一…6=周日
        fire_alarms: list[dict] = []
        missed_messages: list[str] = []
        with self._lock:
            remaining: list[dict] = []
            for a in self._alarms:
                repeat = a.get("repeat", "once")
                a_date = a.get("date") or ""
                # 一次性 + 具体日期：判断 到点触发 / 已过未触发（错过）
                if repeat == "once" and a_date:
                    if a.get("fired"):
                        continue                      # 已触发过还残留 → 直接清掉
                    target = self._alarm_datetime(a)
                    if target is not None and now >= target.timestamp():
                        if a_date == today and a.get("time") == cur_hhmm:
                            fire_alarms.append(dict(a))
                        else:
                            missed_messages.append(self._missed_msg(a))
                        continue                      # 触发过或错过了 → 移除
                    remaining.append(a)
                    continue
                if a.get("time") != cur_hhmm:
                    remaining.append(a)
                    continue
                if repeat == "once":
                    fire_alarms.append(dict(a))   # 老式一次性（无日期）：到点移除
                    continue
                if a.get("last_fire") == today:   # 今天已触发过（daily/weekly 防 10s tick 重复）
                    remaining.append(a)
                    continue
                if repeat == "weekly" and (weekday_idx + 1) not in (a.get("weekdays") or []):
                    remaining.append(a)
                    continue
                fire_alarms.append(dict(a))
                a["last_fire"] = today
                remaining.append(a)
            self._alarms = remaining
            if fire_alarms or missed_messages:
                self._save_alarms()
        for alarm in fire_alarms:                  # 锁外推送，不持锁调会话/notifier
            if alarm.get("action"):
                self._start_task(alarm)
            elif alarm.get("goal"):
                self._start_goal_task(alarm)
            else:
                self._push_alarm(self._alarm_msg(alarm))
        for m in missed_messages:
            self._push_alarm(m)

    def _alarm_msg(self, a: dict) -> str:
        """到点提醒文案：⏰ + 内容 + 随机猫娘后缀（力度升级：别每次都一个样）。"""
        text = a.get("message") or "到点啦"
        return f"⏰ {text}{_pick(ALARM_SUFFIXES)}"

    def _start_task(self, alarm: dict) -> None:
        """每项任务同一时刻最多跑一个实例，避免慢工具重入。"""
        aid = alarm.get("id", "")
        with self._lock:
            if aid in self._running_tasks:
                return
            self._running_tasks.add(aid)
        threading.Thread(target=self._exec_task, args=(alarm,), daemon=True,
                         name=f"catgirl-task-{aid}").start()

    def _task_audit(self, alarm: dict, result: str) -> None:
        """任务级审计补足工具自身日志，记录任务 ID、动作摘要和最终结果。"""
        try:
            base = Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl"
            base.mkdir(parents=True, exist_ok=True)
            action = alarm.get("action") or {}
            keys = ",".join(sorted((action.get("args") or {}).keys()))
            with open(base / "actions.log", "a", encoding="utf-8") as file:
                file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} scheduled_task id={alarm.get('id')} tool={action.get('tool')} args=[{keys}] result={result[:300]}\n")
        except Exception:
            pass

    def _goal_audit(self, alarm: dict, trace: list[dict], result: str) -> None:
        """goal 审计只保留参数键名与结果摘要，既可追查又不泄露内容。"""
        try:
            base = Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl"
            base.mkdir(parents=True, exist_ok=True)
            calls = ";".join(
                f"{item.get('name')}[{','.join(sorted((item.get('args') or {}).keys()))}]={str(item.get('result') or '')[:100]}"
                for item in trace
            ) or "none"
            with open(base / "actions.log", "a", encoding="utf-8") as file:
                file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} scheduled_goal id={alarm.get('id')} goal={str(alarm.get('goal') or '')[:120]} calls={calls} result={result[:200]}\n")
        except Exception:
            pass

    @staticmethod
    def _task_failed(result: str) -> bool:
        return any(marker in result for marker in ("工具执行失败", "工具参数不对", "没有这个工具", "安全确认", "失败喵"))

    def _goal_theme(self, alarm: dict) -> str:
        """从任务内容提一个简洁主题做气泡主语，避免把 agent 的长汇报塞进气泡。

        优先取 message（主人建任务时的短描述，如「做上课PPT」）；太长截断。
        找不到可读内容就落通用词，别让气泡空着。
        """
        text = str(alarm.get("message") or alarm.get("goal") or "").strip()
        text = text.strip("，。！？!? ~～喵。").strip()
        if not text:
            return "「到点的任务」"
        if len(text) > 16:
            text = text[:16] + "…"
        # 去掉常见的祈使前缀，让气泡更像"名词短语 好啦"，而不是整句请求
        for prefix in ("帮我", "请帮我", "记得", "到点", "自动", "给我", "把", "替我"):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                break
        return f"「{text}」"

    @staticmethod
    def _tool_names(trace: list[dict]) -> str:
        """trace 里实际用到的工具名（去重），拼成给聊天记录看的清单。"""
        seen = []
        for item in trace:
            name = str(item.get("name") or "")
            if name and name not in seen:
                seen.append(name)
        return "、".join(seen) or "无"

    @staticmethod
    def _task_brief(result: str) -> str:
        """失败原因只留到第一个句号，别把工具的长引导语（应用清单等）塞进提醒。"""
        if not result:
            return "不知道什么原因"
        cut = result.find("。")
        brief = result if cut < 0 else result[: cut + 1]
        return brief[:120]

    def _exec_task(self, alarm: dict) -> None:
        """后台执行已确认的动作快照；不持 scheduler 锁，不阻塞 10 秒 tick。"""
        aid = alarm.get("id", "")
        action = alarm.get("action") or {}
        try:
            from . import tools
            args = dict(action.get("args") or {})
            fn = tools.TOOL_IMPL.get(action.get("tool"))
            if fn and "confirmed" in inspect.signature(fn).parameters:
                args["confirmed"] = True
            result = tools.run_tool(action.get("tool", ""), args)
            self._task_audit(alarm, result)
            if self._task_failed(result):
                # 失败：提醒播报 + "没做成 + 精简原因一句"（不把工具内部名/长引导语露给主人）
                brief = self._task_brief(result)
                message = f"{self._alarm_msg(alarm)} 到点想自动做但没做成喵：{brief}"
            else:
                # 成功：工具返回本身已是人话（如 launch_app→"已经帮你启动 X 喵"），
                # 直接接在播报后，不再套"已自动执行「工具名」"那层机器腔
                message = f"{self._alarm_msg(alarm)} {result}"
            self._push_alarm(message)
        finally:
            with self._lock:
                self._running_tasks.discard(aid)

    def _start_goal_task(self, alarm: dict) -> None:
        """goal 与 action 共用运行集合，避免同一周期任务并发重入。"""
        aid = alarm.get("id", "")
        with self._lock:
            if aid in self._running_tasks:
                return
            self._running_tasks.add(aid)
        threading.Thread(target=self._exec_goal, args=(alarm,), daemon=True,
                         name=f"catgirl-goal-{aid}").start()

    def _exec_goal(self, alarm: dict) -> None:
        """在独立线程执行受限 agent；超时只终止等待，不阻塞调度主循环。"""
        aid = alarm.get("id", "")
        trace: list[dict] = []
        try:
            from . import tools
            from .llm import SYSTEM_PROMPT_CHAT, call_deepseek_with_tools
            scope = alarm.get("scope") or {}
            allowed = set(scope.get("tools") or [])
            schemas = [item for item in tools.TOOL_SCHEMAS if item.get("function", {}).get("name") in allowed]
            started = time.monotonic()
            base_runner = tools.scoped_runner(scope)

            def limited_runner(name: str, args: dict) -> str:
                if time.monotonic() - started > 120:
                    return "执行预算已用完喵：这个定时任务超过 120 秒，已停止后续操作"
                if len(trace) >= 12:
                    return "执行预算已用完喵：这个定时任务最多调用 12 次工具"
                return base_runner(name, args)

            scope_note = json.dumps({
                "tools": scope.get("tools", []), "read_dirs": scope.get("read_dirs", []),
                "write_paths": scope.get("write_paths", []), "contacts": scope.get("contacts", []),
            }, ensure_ascii=False)
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT_CHAT + "\n\n【定时任务授权圈】只可使用下列授权，越界必须说明做不到：" + scope_note},
                {"role": "user", "content": f"【主人预设的到点任务，现在到点啦】\n{alarm.get('goal')}\n请在授权圈内完成；没有实际调用工具就不能声称已办成。"},
            ]
            result = asyncio.run(asyncio.wait_for(
                call_deepseek_with_tools(messages, schemas, max_rounds=8, runner=limited_runner, trace=trace),
                timeout=120,
            ))
            failed = not result or any("执行预算已用完" in str(item.get("result")) for item in trace)
            self._goal_audit(alarm, trace, str(result or ""))
            theme = self._goal_theme(alarm)
            head = f"⏰ {alarm.get('message') or alarm.get('goal') or '到点的任务'}"
            if failed:
                msg, full = f"{theme}没做成喵：执行超时了。", f"{head} 到点想自主做但没做成喵：执行超时或没有得到有效结果。"
            elif not trace:
                msg, full = f"{theme}没能确认办成喵。", f"{head} 本喵没有实际调用任何工具，不能确认已经办成喵：{self._task_brief(str(result))}"
            else:
                failures = [item for item in trace if self._task_failed(str(item.get("result") or "")) or "越界喵" in str(item.get("result") or "")]
                if failures:
                    reason = self._task_brief(str(failures[-1].get("result")))
                    msg, full = f"{theme}没做成喵。", f"{head} 到点没能完全办成喵：{reason}"
                else:
                    # 气泡给一句简洁确认；聊天窗补上实际做了几步（细节留 actions.log）
                    msg = f"{theme}好啦喵"
                    full = f"{head} 本喵实际调用 {len(trace)} 步（{self._tool_names(trace)}）完成了喵：{self._task_brief(str(result))}"
            self._push_alarm(msg, chat_msg=full)
        except Exception as e:
            self._goal_audit(alarm, trace, f"exception: {e}")
            self._push_alarm(f"⏰ {alarm.get('message') or alarm.get('goal') or '到点的任务'} 到点想自主做但没做成喵：{self._task_brief(str(e))}")
        finally:
            with self._lock:
                self._running_tasks.discard(aid)

    def _push_alarm(self, msg: str, chat_msg: str | None = None) -> None:
        """提醒触发四连：桌宠气泡 → 弹主窗 → 会话落库 → 聊天窗长轮询推送。

        chat_msg 传了就分长短：气泡（桌宠）用 msg 的短句，会话/聊天窗用 chat_msg
        的稍全句（goal 任务跑完：气泡一句确认即可，细节留给聊天记录）。不传则
        各通道同一句（普通提醒/action 任务保持原样）。
        """
        notifier.put({"type": "notify", "message": msg})
        self._pop_main_window()
        full = chat_msg or msg
        try:
            session_append(get_current(), "assistant", full)
        except Exception:
            pass  # 会话尚未初始化等场景不阻塞提醒
        self._chat_alerts.put({"type": "alarm", "message": full})


# 模块级单例：main.py 的 create_app() 调用 scheduler.start()
scheduler = Scheduler()

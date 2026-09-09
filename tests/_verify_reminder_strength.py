# -*- coding: utf-8 -*-
"""验证「提醒力度升级」四项：文案变体随机 + 到点弹主窗 + 桌宠气泡持久可点 + 提示音变响。

- scheduler：喝水/久坐/劝睡/番茄钟/闹钟 文案都在变体池里、带「喵~」、触发后调 show_window；
- pet.py：on_notification 生成 key="reminder" 持久气泡（点一下才关）、提示音走 _play_chime；
- app.py：_window_hwnd / _flash_taskbar 各种坏输入不崩。
"""
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, ".")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import backend.scheduler as S_mod
from backend.scheduler import (  # noqa: E402
    Scheduler, _pick, WATER_MESSAGES, STRETCH_MESSAGES, SLEEP_NUDGE_MESSAGES,
    POMODORO_END_WORK, ALARM_SUFFIXES,
)
from backend.notifier import notifier  # noqa: E402
from backend.settings import save_settings  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def fresh_appdata(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    os.environ["APPDATA"] = d
    return d


def drain():
    got = []
    while True:
        e = notifier.get(timeout=0.05)
        if e is None:
            break
        got.append(e)
    return got


def main():
    ok = True

    # ---- 1. 文案池 + _pick ----
    print("[1] 文案变体")
    picks = {_pick(WATER_MESSAGES) for _ in range(20)}
    ok &= check("pick 只在池内", picks <= set(WATER_MESSAGES), str(picks))
    ok &= check("所有喝水文案带喵", all("喵" in m for m in WATER_MESSAGES))
    ok &= check("所有久坐文案带喵", all("喵" in m for m in STRETCH_MESSAGES))
    ok &= check("所有劝睡文案带喵", all("喵" in m for m in SLEEP_NUDGE_MESSAGES))
    ok &= check("所有闹钟后缀带喵~", all("喵~" in m for m in ALARM_SUFFIXES))

    # ---- 2. scheduler：各提醒触发调 show_window + 文案在池内 ----
    print("[2] 到点弹主窗 + 文案")
    real_show = notifier.show_window
    calls = [0]

    def fake_show():
        calls[0] += 1
        return True

    notifier.show_window = fake_show
    try:
        fresh_appdata("catgirl_sw_")
        s = Scheduler()
        save_settings({"water_reminder": True})
        s._tick()
        msgs = [e.get("message") for e in drain() if e.get("type") == "notify"]
        ok &= check("喝水触发 1 条", len(msgs) == 1, str(msgs))
        ok &= check("喝水文案在池内", bool(msgs) and msgs[0] in WATER_MESSAGES, str(msgs))
        ok &= check("喝水弹主窗", calls[0] == 1, str(calls))
        calls[0] = 0

        fresh_appdata("catgirl_sw2_")
        s2 = Scheduler()
        save_settings({"stretch_reminder": True})
        s2._tick()
        msgs = [e.get("message") for e in drain() if e.get("type") == "notify"]
        ok &= check("久坐文案在池内", bool(msgs) and msgs[0] in STRETCH_MESSAGES, str(msgs))
        ok &= check("久坐弹主窗", calls[0] == 1, str(calls))
        calls[0] = 0

        # 劝睡：patch datetime 到深夜 23:30
        class FakeDT23:
            @classmethod
            def now(cls):
                return datetime.datetime(2026, 8, 17, 23, 30, 0)

        orig_dt = S_mod.datetime
        S_mod.datetime = FakeDT23
        fresh_appdata("catgirl_sw3_")
        s3 = Scheduler()
        save_settings({"sleep_nudge": True})
        s3._tick()
        msgs = [e.get("message") for e in drain() if e.get("type") == "notify"]
        ok &= check("劝睡文案在池内", bool(msgs) and msgs[0] in SLEEP_NUDGE_MESSAGES, str(msgs))
        ok &= check("劝睡弹主窗", calls[0] == 1, str(calls))
        calls[0] = 0
        S_mod.datetime = orig_dt

        # 番茄钟阶段切换
        fresh_appdata("catgirl_sw4_")
        s4 = Scheduler()
        save_settings({"pomodoro": True})
        s4.pomodoro_start(work=0, break_min=0)
        drain()  # 清掉 start 时推的倒计时气泡
        calls[0] = 0
        s4._tick()
        msgs = [e.get("message") for e in drain() if e.get("type") == "notify"]
        ok &= check("番茄钟阶段切换 1 条", len(msgs) == 1, str(msgs))
        ok &= check("番茄钟文案在池内", bool(msgs) and msgs[0] in POMODORO_END_WORK, str(msgs))
        ok &= check("番茄钟弹主窗", calls[0] == 1, str(calls))
        calls[0] = 0

        # 闹钟文案
        fresh_appdata("catgirl_sw5_")
        s5 = Scheduler()
        a = s5.add_alarm("10:30", "喝水")
        m = s5._alarm_msg(a)
        ok &= check("闹钟文案带⏰和喵~", m.startswith("⏰") and "喵~" in m and "喝水" in m, m)

        # 全屏守护：前台全屏（游戏）时提醒不弹主窗，平时照旧弹
        orig_fs = S_mod._foreground_fullscreen
        try:
            S_mod._foreground_fullscreen = lambda: True
            fresh_appdata("catgirl_fs_")
            s6 = Scheduler()
            save_settings({"water_reminder": True})
            calls[0] = 0
            s6._tick()
            drain()
            ok &= check("全屏时提醒不弹主窗", calls[0] == 0, str(calls))
            S_mod._foreground_fullscreen = lambda: False
            s6._last_fire = {}  # 清触发记录，让第二次 tick 能再次触发
            calls[0] = 0
            s6._tick()
            drain()
            ok &= check("非全屏时提醒照旧弹主窗", calls[0] == 1, str(calls))
        finally:
            S_mod._foreground_fullscreen = orig_fs
    finally:
        notifier.show_window = real_show

    # ---- 3. pet.py：气泡持久可点 + 提示音 ----
    print("[3] 桌宠气泡持久可点 + 提示音")
    fresh_appdata("catgirl_pet_")
    os.makedirs(os.path.join(os.environ["APPDATA"], "catgirl"), exist_ok=True)
    with open(os.path.join(os.environ["APPDATA"], "catgirl", "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"notification_sound": True}, f)

    class FakeWinsound:
        beeps = []

        @classmethod
        def Beep(cls, freq, dur):
            cls.beeps.append((freq, dur))

    sys.modules["winsound"] = FakeWinsound

    from desktop.pet import Pet, load_config  # noqa: E402
    cfg = load_config()
    pet = Pet("http://127.0.0.1:1", cfg, parent_pid=None)
    try:
        pet.on_notification("测试提醒喵~")
        ok &= check("通知音走两段提示音", bool(FakeWinsound.beeps) and FakeWinsound.beeps[0] == (880, 120), str(FakeWinsound.beeps))
        ok &= check("持久气泡已建(key=reminder)", "reminder" in pet._persist_bubbles, str(list(pet._persist_bubbles)))
        ok &= check("不再用4秒瞬态气泡", pet._bubble is None)

        # 提示音失败回退 bell
        bell_called = []
        sys.modules["winsound"] = None
        pet.root.bell = lambda: bell_called.append(1)
        pet._play_chime()
        ok &= check("winsound 失败回退 bell", bool(bell_called), str(bell_called))

        # 点一下 → 关掉持久气泡（模拟真实点击：先 update 让气泡窗口 realize，再点）
        pet.root.update()
        bub = pet._persist_bubbles["reminder"][0]
        c = pet._persist_bubbles["reminder"][1]
        ok &= check("气泡已绑点击事件", bool(c.bind("<Button-1>")), c.bind("<Button-1>"))
        try:
            c.event_generate("<Button-1>", x=5, y=5)
            pet.root.update()
        except Exception:
            pass
        if "reminder" in pet._persist_bubbles:
            # 兜底：直接执行 Toplevel 绑定的脚本（event_generate 对未映射窗口不可靠）
            script = bub.bind("<Button-1>")
            if script:
                try:
                    bub.tk.eval(script)
                    pet.root.update()
                except Exception:
                    pass
        ok &= check("点气泡关掉", "reminder" not in pet._persist_bubbles, str(list(pet._persist_bubbles)))

        # 新提醒覆盖同一气泡文本
        pet.on_notification("第二条提醒喵~")
        item = pet._persist_bubbles.get("reminder")
        ok &= check("新提醒更新同一气泡", item is not None, str(list(pet._persist_bubbles)))
        if item:
            txt = item[1].itemcget(item[2], "text")
            ok &= check("气泡文本已更新", "第二条提醒" in txt, txt)
        pet.root.update()
    finally:
        # 注意：不能调 pet.quit()——它末尾 sys.exit(0)，会直接杀掉整个测试进程。
        # 手动清理即可：停行为循环 + 销毁根窗口。
        try:
            pet._stop = True
            pet.root.destroy()
        except BaseException:
            pass
        sys.modules.pop("winsound", None)

    # ---- 4. app.py：任务栏闪烁助手 ----
    print("[4] app.py 任务栏闪烁助手")
    from desktop import app as app_mod
    ok &= check("_window_hwnd 无 native 返回 None", app_mod._window_hwnd(object()) is None)

    class FakeNative:
        Handle = 12345  # 真实 HWND 非零（0 会被 _window_hwnd 的 `or None` 归一成 None）

    class FakeWin:
        native = FakeNative()

    ok &= check("_window_hwnd 拿到 Handle", app_mod._window_hwnd(FakeWin()) == 12345)
    try:
        app_mod._flash_taskbar(None)
        ok &= check("_flash_taskbar 坏句柄不崩", True)
    except Exception:
        ok &= check("_flash_taskbar 坏句柄不崩", False, "抛异常了")

    print("ALL PASS" if not FAILURES else f"FAILED: {FAILURES}", flush=True)
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())

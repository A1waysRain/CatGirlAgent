"""桌面宠物 + 定时提醒路由（从 main.py 拆出，2026-08-16）。"""
from fastapi import APIRouter, HTTPException

from ..notifier import notifier
from ..scheduler import scheduler
from ..settings import load_settings

router = APIRouter(prefix="/api")


@router.get("/pet/notifications")
def pet_notifications():
    """长轮询：等一条事件（notify/emotion/behavior），超时返回空事件。

    同步函数走线程池，不卡事件循环。返回事件对象本身 + time：
    如 {"type":"notify","message":"..."} / {"type":"emotion","mood":"think"}。
    """
    import time

    event = notifier.get(timeout=25)
    return {**(event or {"type": "none"}), "time": time.time()}


@router.post("/pet/notify")
async def pet_notify(payload: dict):
    """主动推一条通知（测试用；未来微信监听线程也走这里）。"""
    notifier.put(str(payload.get("message") or "喵~ 新消息"))
    return {"status": "ok"}


@router.post("/pet/event")
async def pet_event(payload: dict):
    """推一条宠物事件：{"type": "notify"|"emotion"|"behavior", ...}。

    - notify:   {"type":"notify", "message":"..."}
    - emotion:  {"type":"emotion", "mood":"think|happy|angry|..."}
    - behavior: {"type":"behavior", "action":"walk|sleep_now|..."}（M2 用）
    """
    etype = str(payload.get("type") or "notify")
    if etype not in ("notify", "emotion", "behavior"):
        raise HTTPException(status_code=400, detail=f"未知事件类型：{etype}")
    notifier.put(payload)
    return {"status": "ok"}


@router.post("/pet/show")
async def pet_show():
    """把猫娘主窗唤起到前台（由桌面壳注册回调实现）。"""
    ok = notifier.show_window()
    return {"status": "ok" if ok else "no-window"}


@router.get("/scheduler/state")
async def scheduler_state():
    """当前提醒配置 + 番茄钟运行状态（前端设置面板/桌宠右键菜单用）。"""
    s = load_settings()
    return {
        "water": bool(s.get("water_reminder")),
        "stretch": bool(s.get("stretch_reminder")),
        "pomodoro_enabled": bool(s.get("pomodoro")),
        "pomodoro_work": int(s.get("pomodoro_work", 25)),
        "pomodoro_break": int(s.get("pomodoro_break", 5)),
        "sleep_nudge": bool(s.get("sleep_nudge")),
        "pomodoro": scheduler.pomodoro_state(),
    }


@router.post("/scheduler/pomodoro")
async def scheduler_pomodoro(payload: dict):
    """番茄钟控制：action = start（用设置的时长）/ stop / status。"""
    action = (payload.get("action") or "status").strip()
    if action == "start":
        s = load_settings()
        return scheduler.pomodoro_start(
            work=int(s.get("pomodoro_work", 25)),
            break_min=int(s.get("pomodoro_break", 5)),
        )
    if action == "stop":
        return scheduler.pomodoro_stop()
    return scheduler.pomodoro_state()

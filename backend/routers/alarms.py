"""定点提醒（闹钟）路由：设置面板管理 + 聊天窗长轮询推送。

- GET    /api/alarms              —— 提醒列表（设置面板加载）
- POST   /api/alarms              —— 手动添加 {time,message,repeat,weekdays}
- DELETE /api/alarms/{id}         —— 删除
- GET    /api/alarms/chat_events  —— 长轮询：到点提醒推给聊天窗前端
"""
import time

from fastapi import APIRouter, HTTPException

from ..scheduler import scheduler
from ..tools import (scheduled_action_needs_confirmation, scheduled_goal_needs_confirmation,
                     validate_scheduled_action, validate_scheduled_goal)

router = APIRouter(prefix="/api")


@router.get("/alarms")
async def list_alarms():
    """当前全部定点提醒（设置面板「提醒列表」渲染用）。"""
    return {"alarms": scheduler.list_alarms()}


@router.post("/alarms")
async def create_alarm(payload: dict):
    """添加提醒；危险 action/goal 授权圈必须在创建请求中 confirmed=true。"""
    try:
        action = validate_scheduled_action(payload.get("action"))
        goal = payload.get("goal")
        scope = payload.get("scope")
        if action is not None and goal is not None:
            raise ValueError("定时任务只能选择固定 action 或自主 goal 其中一种")
        if goal is not None or scope is not None:
            goal, scope = validate_scheduled_goal(goal, scope)
        if scheduled_action_needs_confirmation(action) and payload.get("confirmed") is not True:
            raise ValueError("危险定时任务须先确认具体时间、动作和参数，再带 confirmed=true 创建")
        if goal is not None and scheduled_goal_needs_confirmation(scope) and payload.get("confirmed") is not True:
            raise ValueError("危险自主任务须先确认时间、授权工具、目录和联系人，再带 confirmed=true 创建")
        candidate = {
            "time": str(payload.get("time") or ""),
            "message": str(payload.get("message") or ""),
            "repeat": str(payload.get("repeat") or "once"),
            "weekdays": payload.get("weekdays") or [],
            "date": payload.get("date") or None,
            "action": action,
            "goal": goal,
            "scope": scope,
        }
        alarm = scheduler.find_exact_alarm(candidate)
        existed = alarm is not None
        if not alarm:
            alarm = scheduler.add_alarm(**candidate)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "alarm": alarm, "already_exists": existed}


@router.delete("/alarms/{alarm_id}")
async def delete_alarm(alarm_id: str):
    """删除提醒；不存在 404。"""
    if not scheduler.delete_alarm(alarm_id):
        raise HTTPException(status_code=404, detail="没找到这条提醒喵")
    return {"status": "ok"}


@router.get("/alarms/chat_events")
def alarm_chat_events():
    """长轮询：等一条聊天窗提醒事件，超时返回空事件。

    同步函数走线程池，不卡事件循环（照 /api/pet/notifications 模式）。
    有事件返回 {"type":"alarm","message":"..."}，无事件返回 {"type":"none"}。
    """
    event = scheduler.get_chat_alert(timeout=25)
    return {**(event or {"type": "none"}), "time": time.time()}

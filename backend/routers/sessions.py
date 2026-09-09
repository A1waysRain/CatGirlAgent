"""多会话路由（从 main.py 拆出，2026-08-16）。

注意：本文件是 backend.routers.sessions；下方的 `from ..sessions` 是
backend.sessions（会话存储模块）——相对导入从父包取，不会和自己撞。
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..chat_service import _sse, build_messages, current_time_hint, stream_answer
from ..scheduler import scheduler
from ..tools import TOOL_SCHEMAS
from .chat import _extract_alarm_request, _extract_write_request
from ..sessions import (
    clear_session,
    clear_summary,
    create_session,
    delete_round,
    delete_session,
    get_current,
    get_session,
    get_summary,
    list_context_items,
    list_sessions,
    pop_last_assistant,
    recent_messages,
    restore_assistant,
    search_messages,
    set_current,
    set_context_item_policy,
)

router = APIRouter(prefix="/api")


@router.get("/sessions")
async def sessions_list():
    """全部会话摘要 + 当前会话 id。"""
    return {"sessions": list_sessions(), "current": get_current()}


@router.post("/sessions")
async def sessions_create():
    """新建会话并切到它。"""
    session = create_session()
    return {"session": session, "current": get_current()}


@router.get("/sessions/{sid}")
async def sessions_detail(sid: str):
    """单个会话完整内容（含消息）。"""
    session = get_session(sid)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在喵")
    return {"session": {"id": session["id"], "title": session["title"], "messages": session["messages"]}}


@router.put("/sessions/current")
async def sessions_switch(payload: dict):
    """切换当前会话。"""
    sid = (payload.get("session_id") or "").strip()
    if not set_current(sid):
        raise HTTPException(status_code=404, detail="会话不存在喵")
    return {"current": sid}


@router.delete("/sessions/{sid}")
async def sessions_remove(sid: str):
    """删除会话；若删的是当前会话会自动跳到最近活跃的会话。"""
    if not get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在喵")
    current = delete_session(sid)
    return {"ok": True, "current": current}


@router.delete("/sessions/{sid}/messages/{message_id}")
async def sessions_message_delete(sid: str, message_id: str):
    """删除包含该消息的那一轮对话（user+assistant，或错误残留的孤立消息）。"""
    if not get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在喵")
    remaining = delete_round(sid, message_id)
    if remaining is None:
        raise HTTPException(status_code=404, detail="这条消息不存在喵")
    session = get_session(sid)
    return {
        "ok": True,
        "session": {
            "id": sid,
            "title": session["title"] if session else "新会话",
            "messages": remaining,
        },
    }


@router.get("/sessions/{sid}/search")
async def sessions_search(sid: str, q: str = ""):
    """在当前会话的消息里按内容搜索（大小写不敏感），返回匹配消息列表。"""
    if not get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在喵")
    return {"results": search_messages(sid, q, limit=50)}


@router.get("/sessions/{sid}/summary")
async def sessions_summary_get(sid: str):
    """猫娘的记忆小本本：返回该会话已提炼的要点段列表。"""
    if not get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在喵")
    return {"summary": get_summary(sid)}


@router.delete("/sessions/{sid}/summary")
async def sessions_summary_delete(sid: str):
    """一键清空猫娘的记忆小本本。"""
    if not get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在喵")
    clear_summary(sid)
    return {"ok": True, "summary": []}


@router.get("/sessions/{sid}/context-items")
async def sessions_context_items_get(sid: str):
    """返回可保留的路径/文件/赛程线索。"""
    if not get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在喵")
    return {"items": list_context_items(sid)}


@router.post("/sessions/{sid}/context-items/{message_id}")
async def sessions_context_item_update(sid: str, message_id: str, payload: dict):
    """管理一条线索：action=pin 永久保留，action=drop 停止注入模型。"""
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"pin", "drop"}:
        raise HTTPException(status_code=400, detail="只支持 pin 或 drop")
    item = set_context_item_policy(sid, message_id, action)
    if item is None:
        raise HTTPException(status_code=404, detail="保留项不存在或不可管理喵")
    return {"ok": True, "item": item}


# 重新生成 = 只重组织文字，不重执行操作：只留信息型/只读工具。
# 防止模型重复执行有副作用的操作（建闹钟/写文件/启动应用/发消息等——首次请求已执行过）。
# （session.py 详解文档 第十六点）
_REGENERATE_TOOLS = [
    t for t in TOOL_SCHEMAS
    if t["function"]["name"] in {
        "get_time", "check_system", "read_file", "list_dir", "read_image",
        "list_alarms", "list_plugins", "ui_observe",
    }
]


def _ensure_regenerated_alarm(user_text: str) -> tuple[dict | None, bool]:
    """查找或补建上一轮请求的提醒，返回 ``(提醒, 是否本次新建)``。

    重新生成时，上一次回答可能在异常或旧版本逻辑中没有真的建好提醒。不能只因
    工具受限就声称无能，也不能盲目重复添加；由后端按完整字段先查重，缺失才补建。
    """
    requested = _extract_alarm_request(user_text)
    if not requested:
        return None, False
    existed = scheduler.find_exact_alarm(requested)
    if existed:
        return existed, False
    try:
        return scheduler.add_alarm(**requested), True
    except Exception:
        return None, False


@router.post("/sessions/{sid}/regenerate")
async def sessions_regenerate(sid: str) -> StreamingResponse:
    """重新生成最后一条猫娘回答：弹掉旧回复，用原上下文重调模型，流式落新回复。"""
    if not get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在喵")
    popped = pop_last_assistant(sid)
    if popped is None:
        raise HTTPException(status_code=400, detail="没有可重新生成的喵")
    user_text, remaining, old_assistant = popped
    msgs = build_messages(sid, recent_messages(remaining, for_model=True))
    # 当前时间是动态上下文，必须放在稳定 system 前缀之后、recent 之前。
    time_idx = next((i for i, message in enumerate(msgs) if message.get("role") != "system"), len(msgs))
    msgs.insert(time_idx, {"role": "system", "content": current_time_hint()})
    # 重新生成 ≠ 重新执行：明确告诉模型只重组织文字、别重复上次已执行过的操作（第十六点）
    msgs.insert(1, {"role": "system", "content": (
        "【系统通知】本轮是重新生成上一轮的回答。只重新组织文字，"
        "绝不要调用任何工具、绝不重复执行上一次的操作——"
        "提醒/文件/启动应用/发消息等如果上次执行过，就是已经执行过了。"
        "用猫娘语气把这段话重新组织一遍即可。"
    )})
    alarm, alarm_created = _ensure_regenerated_alarm(user_text)
    if alarm:
        when = f"{alarm['date']} " if alarm.get("date") else ""
        status = "已由系统成功创建" if alarm_created else "已经存在"
        msgs.insert(2, {"role": "system", "content": (
            f"【系统通知，必须服从】主人要求的定时提醒{status}："
            f"⏰ {when}{alarm['time']} 提醒主人「{alarm['message']}」。"
            "本轮只用一到两句猫娘语气确认已设好即可。"
            "不要调用或讨论工具，不要确认当前时间，不要猜测主人是否熬夜，"
            "不要声称没有提醒功能，也不要建议主人改用手机闹钟。"
        )})
    # 重生成明确禁止重做有副作用的操作，不能再拿“本轮未写文件”审计它。
    # 否则上一轮确实生成成功的 PPT/Excel，在重述成功回复时会被误追加“没办到”。
    # 新用户请求仍由 chat 路由启用写文件审计；这里仅重组上轮的事实性文字。
    write_requested = False
    write_state = None

    async def events():
        # regenerate 没有新 user 消息，meta 里 user 为 null（前端只拿 session 同步标题）
        try:
            async for evt in stream_answer(sid, msgs, user_text, tools=_REGENERATE_TOOLS,
                                           user_msg_id=None,
                                           write_requested=write_requested,
                                           write_state=write_state):
                if evt.get("type") == "error":
                    # 生成失败：旧回答已在 pop 时删掉，恢复回去（带原 id），
                    # 避免"点重新生成失败 = 回答永久消失"（session.py 详解文档第十五点）
                    try:
                        restore_assistant(sid, old_assistant)
                    except Exception:
                        pass
                yield evt
        except BaseException:
            # 客户端断开/取消：CancelledError 是 BaseException，stream_answer 内的
            # except Exception 抓不到 → 走不到 error 分支，旧答可能没恢复（第二十点）。
            # 幂等补一次恢复，避免"点重生成后刷新/关窗 = 回答消失"。
            try:
                restore_assistant(sid, old_assistant)
            except Exception:
                pass
            raise

    return StreamingResponse(
        (_sse(evt) async for evt in events()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/history")
async def get_history():
    """返回当前会话的历史（保留旧接口，供调试/兼容）。"""
    sid = get_current()
    session = get_session(sid)
    return {"history": session["messages"] if session else []}


@router.delete("/history")
async def delete_history():
    """清空当前会话的消息（会话保留，标题重置）。"""
    clear_session(get_current())
    return {"status": "ok"}

"""聊天核心逻辑（供 chat / sessions 路由共用）：从 main.py 拆出（2026-08-16 路由拆分）。"""
import json
import re
import time
from datetime import datetime

import httpx

from .llm import SYSTEM_PROMPT_CHAT, call_deepseek_with_tools_stream
from .mood import infer_mood
from .notifier import notifier
from .sessions import append_message as session_append, get_session, get_summary
from .summary import maybe_summarize
from .title import maybe_title
from .tools import TOOL_SCHEMAS

# 「声称完成」话术：配合写文件审计，检测猫娘空口说"已生成/已放到"却没真调写文件工具
_CLAIM_WORDS = re.compile(
    r"已生成|已写好|写好了|已写完|已保存|已写入|已放到|放在桌面|放到桌面|放桌面|"
    r"保存到|创建成功|生成成功|已创建|已写|已经放到|已经写好|新文档.*(?:完成|好了)"
)

# 「否认定时提醒功能」话术：闹钟明明被系统建好了，猫娘却嘴瓢说没这功能/工具没加载
# （被历史里"本喵没有定时闹钟本事"的自嘲带偏）→ 流结束追加更正。
_ALARM_DENIAL_RE = re.compile(
    r"没有(?:定时|这个|这项|闹钟|提醒|设置|建)*?(?:提醒|闹钟|功能|本事|工具)"
    r"|没加载|没加载上|没加载到|加载不了|工具(?:没|不)在|工具不(?:全|齐全|存在)"
    r"|不会(?:设置|建|做|弄).{0,4}(?:提醒|闹钟)"
    r"|插件(?:里|中)?没有|小插件(?:里|中)?没有"
    r"|手机闹钟|闹钟代替|只会(?:看图片|聊天|陪)"
)

# 工具原始返回永不落入会话；这里只给最终自然语言回复加保留策略。分类宁可保守：
# 不确定或混合任务保留为 normal，避免把主人需要的解释误当成一次性操作回执。
_TEMPORARY_TOOLS = {
    "launch_app", "open_path", "open_url", "web_search", "verify_current_fact", "distill_web", "check_system",
    "ui_observe", "ui_click", "ui_type", "ui_search_contact", "ui_cancel_send",
    "ui_send", "ui_send_file", "scan_apps", "get_time",
}
_REFERENCE_TOOLS = {
    "read_file", "list_dir", "rag_query", "append_file", "replace_in_file",
    "format_docx", "create_pptx", "create_xlsx",
}
_STATE_TOOLS = {"set_alarm", "set_alarms_batch", "delete_alarm", "list_alarms", "stage_alarm_batch"}
_LIFECYCLE_SECONDS = {"temporary": 24 * 3600, "reference": 30 * 24 * 3600, "state": 7 * 24 * 3600}


def _assistant_lifecycle(tool_trace: list) -> dict:
    """根据本轮实际工具选择最终回复的上下文寿命，未知工具不降级。"""
    names = {str(name) for name in tool_trace if name}
    if not names:
        return {}
    if names & _REFERENCE_TOOLS:
        policy = "reference"
        # 路径/文件线索先作为可管理的 30 天参考项，主人明确“保留”后才升为
        # pinned 并允许写进小本本，避免又把工具结论自动塞回长期记忆。
        summary_allowed = False
    elif names & _STATE_TOOLS and names <= (_STATE_TOOLS | _TEMPORARY_TOOLS):
        policy = "state"
        summary_allowed = False
    elif names <= _TEMPORARY_TOOLS:
        policy = "temporary"
        summary_allowed = False
    else:
        return {}
    return {
        "context_policy": policy,
        "expires_at": time.time() + _LIFECYCLE_SECONDS[policy],
        "summary_allowed": summary_allowed,
    }


def _sse(payload: dict) -> str:
    """把一个事件 dict 序列化成一行 SSE 文本（data: <json> + 空行）。

    ensure_ascii=False：中文直接以 UTF-8 输出，而不是 \\uXXXX 转义（省流量、前端好读）。
    """
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


# 时间感知问候（读 sessions 消息里的 ts 字段）：
# 根因——模型对「时间流逝」没概念，会以为"睡大觉"刚说完就接"晚上好"，才老翻旧账质问
# 「你不是去睡觉了吗」。所以按主人这次提问离上次互动隔了多久，喂一句时间提示给模型。
GREET_GAP_BREAK = 8 * 3600      # ≥8 小时：中间隔了一大段（睡过觉/休息过/忙完回来了），关心问候
GREET_GAP_LONG = 72 * 3600      # ≥3 天：久别重逢，先表达想念
_WEEKDAY_CN = ("一", "二", "三", "四", "五", "六", "日")


def current_time_hint(now: datetime | None = None) -> str:
    """生成每轮都要给模型的绝对当前时间，供时间措辞与跨日判断使用。"""
    now = now or datetime.now()
    hour = now.hour
    if hour < 5:
        period = "凌晨"
    elif hour < 8:
        period = "早上"
    elif hour < 12:
        period = "上午"
    elif hour < 14:
        period = "中午"
    elif hour < 18:
        period = "下午"
    elif hour < 19:
        period = "傍晚"
    elif hour < 23:
        period = "晚上"
    else:
        period = "深夜"
    return (
        f"【现在时间】现在是 {now.year}年{now.month}月{now.day}日 星期{_WEEKDAY_CN[now.weekday()]} "
        f"{period} {hour} 点 {now.minute:02d} 分。"
        "回复前请据此判断时间措辞；不要把跨日期的旧聊天说成刚刚发生、这一晚上或折腾到现在。"
    )


def _gap_seconds(messages: list) -> float | None:
    """当前提问与上一条消息的时间差（秒）。消息不足两条或缺 ts（旧消息）→ None。"""
    if len(messages) < 2:
        return None
    cur = messages[-1].get("ts")
    prev = messages[-2].get("ts")
    if not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)):
        return None
    return cur - prev


def greeting_hint(messages: list) -> str | None:
    """按上次互动隔了多久，生成一句喂给模型的「时间感知」提示；间隔小/拿不到时间则 None。"""
    gap = _gap_seconds(messages)
    if gap is None or gap < GREET_GAP_BREAK:
        return None
    if gap >= GREET_GAP_LONG:
        days = gap / 86400
        return (
            f"【时间感知】主人已经约{days:.0f}天没来找本喵了，现在终于回来了。"
            "聊天记录里虽然还留着上一段对话，但你要明白中间隔了很久。"
            "先表达想念和开心，但保持傲娇：嘴上别扭、尾巴藏不住地晃，"
            "比如『终于回来啦……哼，才不是想你喵，就是怕你走丢没人陪本喵打游戏喵』"
            "『主人好久不见喵，本喵可没有一直等你哦』。"
            "再自然接上主人现在说的话；不要翻旧账，也别质问主人为什么这么久才来。"
        )
    hours = gap / 3600
    return (
        f"【时间感知】主人距离上次说话已经隔了约{hours:.0f}小时（中间有一大段空白，"
        "他大概率是去睡觉、休息或忙别的事了，现在才回来）。"
        "先自然地接着主人的问候回一声问候，保持傲娇人设别破功。"
        "如果记录里他提过睡觉/休息，用『嘴硬心软』的傲娇方式关心（表面否认、实际在问），"
        "比如『哼，本喵才不是担心你睡没睡好呢，就是怕你顶着黑眼圈又来找本喵哭喵』"
        "『昨晚睡得还行吗，本喵随口问问而已喵』。"
        "绝对不要用质问或翻旧账的语气（比如『你不是说要去睡觉吗』『怎么又跑回来了』），"
        "也别直接说『我很关心你』这种大白话。"
    )


def build_messages(sid: str, recent: list) -> list[dict]:
    """组装发给模型的消息：system 提示词 + 猫娘记忆小本本(若有) + 当前提醒 + 最近窗口。

    摘要段只追加不重写 → system+旧摘要+当前提醒 是稳定前缀，保住 DeepSeek 前缀缓存。
    """
    msgs = [{"role": "system", "content": SYSTEM_PROMPT_CHAT}]
    summary = get_summary(sid)
    if summary:
        seg_text = "\n".join(f"· {s.get('text','')}" for s in summary)
        msgs.append({
            "role": "system",
            "content": f"[猫娘的记忆小本本]（更早聊过、当前窗口装不下的要点）：\n{seg_text}",
        })
    # 当前已设提醒喂给模型：闹钟存 alarms.json，但模型上下文里从来没见过它——
    # 不喂这段，猫娘会"设过却失忆"：主人再聊到排位赛，她还问几点开跑、想再建一次。
    # 插在 recent 之前，保 system+摘要+提醒 前缀稳定（局部 import，不动顶部 import 面）。
    try:
        from .scheduler import scheduler
        alarm_ctx = scheduler.describe_alarms()
    except Exception:
        alarm_ctx = ""
    if alarm_ctx:
        msgs.append({"role": "system", "content": alarm_ctx})
    msgs.extend(recent)
    return msgs


async def stream_answer(
    sid: str,
    msgs: list,
    user_text: str,
    tools: list | None = None,
    user_msg_id: str | None = None,
    write_requested: bool = False,
    write_state: dict | None = None,
    alarm_created: dict | None = None,
):
    """流式回答生成器：meta → llm 流事件透传 → 落 assistant → 推情绪 → done。

    tools 缺省为全部 TOOL_SCHEMAS。流中间异常不往外抛，直接 yield error 事件——
    首字节已发出后 HTTP 状态码已定死 200，只能靠事件通知前端（能提前判的
    校验错误由路由层 raise HTTPException，在首字节前返回）。
    """
    session = get_session(sid)
    yield {"type": "meta",
           "session": {"id": sid, "title": session.get("title","新会话") if session else "新会话"},
           "message_ids": {"user": user_msg_id}}

    parts: list[str] = []
    # agent 的会话上下文：待确认赛程工具需要知道该把结构化结果存到哪个会话。
    # 写文件审计沿用同一 dict，工具执行后写入的 wrote_file 仍能在下方读取。
    agent_state = write_state if write_state is not None else {}
    agent_state["sid"] = sid
    try:
        async for evt in call_deepseek_with_tools_stream(
            messages=msgs, tools=TOOL_SCHEMAS if tools is None else tools, state=agent_state
        ):
            if evt["type"] == "delta":
                parts.append(evt["text"])   # 累积全文，流结束后一次落库
            yield evt
    except httpx.HTTPError:
        yield {"type": "error", "detail": "本喵正在打游戏喵,罚你等等喵~"}
        return
    except Exception:
        yield {"type": "error", "detail": "本喵想睡觉了喵,罚你等等喵~"}
        return

    # 流结束后统一收尾：落一条 assistant + 推情绪 + 回传 message_id
    answer = "".join(parts).strip() or "本喵暂时不想理你喵"
    # 写文件声称审计：主人明确要"生成/写文档"，本轮却一个写文件工具都没真调，而猫娘
    # 又在回复里声称"已生成/已放到"——追加诚实更正，防止主人白等（模型空口声称的兜底）。
    if write_requested and write_state is not None and not write_state.get("wrote_file") and _CLAIM_WORDS.search(answer):
        correction = (
            "\n\n（喵，本喵得诚实承认：刚才那句『已生成』是空口说的，"
            "本喵并没有真的调用工具写文件喵。主人先别等文件了，"
            "再跟本喵说一声，本喵重新认真写一遍喵~）"
        )
        yield {"type": "delta", "text": correction}
        answer += correction
    # 闹钟否认审计：系统明明已建好定时提醒，猫娘却嘴瓢说"没这功能/工具没加载" → 追加更正
    if alarm_created and _ALARM_DENIAL_RE.search(answer):
        deny_when = f"{alarm_created.get('date')} " if alarm_created.get("date") else ""
        deny_correction = (
            f"\n\n（更正喵：本喵刚才说错话了——定时提醒功能是有的！"
            f"系统已经帮你设好了 ⏰ {deny_when}{alarm_created['time']} 的提醒"
            f"「{alarm_created['message']}」，到点本喵会准时喊你喵。"
            f"也可以去设置面板里查看/删除这条提醒喵。）"
        )
        yield {"type": "delta", "text": deny_correction}
        answer += deny_correction
    lifecycle = _assistant_lifecycle(agent_state.get("tool_trace", []))
    refs = []
    seen_refs = set()
    for ref in agent_state.get("fact_refs", []) or []:
        if not isinstance(ref, dict) or not ref.get("url") or ref["url"] in seen_refs:
            continue
        seen_refs.add(ref["url"])
        refs.append(ref)
    if refs:
        lifecycle["fact_refs"] = refs[:8]
    if isinstance(agent_state.get("fact_meta"), dict):
        lifecycle["fact_meta"] = agent_state["fact_meta"]
    stored = session_append(sid, "assistant", answer, **lifecycle)
    # L2：窗口外有旧对话没被摘要 → 起后台线程把要点记进小本本（不卡回复、失败下次再试）
    try:
        maybe_summarize(sid)
    except Exception:
        pass
    # 会话标题：第一轮问答结束后，让提炼子 agent 把标题从「截 12 字」换成概括（同款后台线程；
    # 靠 title_auto 标记判断该不该起名，所以后续轮次和存量会话都不会重复触发）
    try:
        maybe_title(sid, user_text, answer)
    except Exception:
        pass
    try:
        notifier.put({"type": "emotion", "mood": infer_mood(answer, user_text)})
    except Exception:
        pass
    assistant_id = stored[-1]["id"] if stored and stored[-1].get("role") == "assistant" else None
    yield {"type": "done", "message_ids": {"assistant": assistant_id},
           "fact_refs": refs[:8], "fact_meta": lifecycle.get("fact_meta")}

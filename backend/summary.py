"""增量摘要记忆（L2）：长对话窗口装不下时，后台把旧对话提炼成要点存进会话 summary。

关键设计（为什么必须「增量」）：
- DeepSeek 按「前缀命中」自动缓存，命中前缀的输入费用低得多。
- 摘要段**只追加、不重写** → system + 旧摘要段 是稳定前缀，跨请求命中缓存；
  若每次请求都全量重算摘要，缓存全部失效——「增量 append」不是实现细节，是省钱前提。
- 只摘要「上次摘要点之后、当前窗口之外」的新段落；summary 超 3 段由 set_summary 合并最早的 2 段。
- 后台异步跑，不卡聊天回复；失败静默，下次再试。
"""

import asyncio
import threading

import httpx

from . import sessions as S
from .config import settings

# 攒够多少条「窗口外、还没摘要」的消息才摘要一次
MIN_BLOCK = 8

SUMMARY_PROMPT = """你是「猫娘来咯」这只傲娇猫娘的记忆整理员。下面是一段它和主人的旧对话（已经在聊天窗口里看不到了），请把它值得记住的要点提炼成几行「小本本」笔记，供猫娘日后回忆起这些事。

要求：
1. 只记**主人的事**：主人的身份/偏好/习惯、进行中的任务、明确约定、关键数字（时间/日期/比分/金额/兑换码）、提到过的文件路径和联系人名。
2. **绝不记猫娘自己的事**——她自己说错话、认错、翻车、吹牛、失手没办成，跟她有关的惩罚或赌约，她"记下这笔账"之类，一律不写。小本本是给日后聊天用的备忘，不是她的检讨书；写了这些她以后会老翻自己的旧账。
3. 用傲娇猫娘的语气陈述事实，结尾不用带喵。例如「主人说过他的星座是狮子座」「主人约好周六一起看正赛」「主人电脑里装了 Claude Code」。
4. 只输出要点本身，不要解释、不要序号列表，每行一条，控制在 6 行以内。
5. 记不住的细节不要编，宁可漏掉也不要瞎编。

对话内容：
{conversation}"""


async def _summarize_once(messages: list[dict]) -> str | None:
    """调一次非流式 DeepSeek，把这段对话提炼成要点文本；失败返回 None。"""
    text = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in messages)
    payload = {
        "model": settings.deepseek_model,
        "messages": [{"role": "system", "content": SUMMARY_PROMPT.format(conversation=text)}],
        "temperature": 0.4,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            resp = await client.post(
                settings.deepseek_base_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                },
                json=payload,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return (content or "").strip() or None
    except Exception:
        return None


def _summary_point(session: dict) -> int:
    """已摘要到的消息数（取各段的 n 最大值，钳制到消息数内）。"""
    msgs = session.get("messages", []) or []
    n = 0
    for seg in session.get("summary", []) or []:
        try:
            n = max(n, int(seg.get("n", 0)))
        except Exception:
            continue
    return min(n, len(msgs))


def _compute_new_block(session: dict):
    """找出「窗口外、还没摘要」的连续消息块。

    返回 (block_msgs, from_idx, to_idx)；没有可摘要的块返回 None。
    窗口起点 = 按 token 预算从最新往前取的那一段的下标；
    它之前、且还没被摘要过的消息就是要摘要的内容。
    """
    msgs = session.get("messages", []) or []
    if not msgs:
        return None
    # 摘要进度必须以原始消息下标计算，不能用 for_model 过滤后的窗口；否则
    # 临时结论一过期，n 会错位并把已经处理过的旧消息重新摘要。
    window = S.recent_messages(msgs, S.HISTORY_TOKEN_BUDGET)
    window_start = len(msgs) - len(window)   # 模型窗口的起点（更早的全在窗口外）
    n = _summary_point(session)
    if window_start <= n:                     # 窗口起点没超过已摘要点 → 没有新内容可摘
        return None
    block = msgs[n:window_start]
    if len(block) < MIN_BLOCK:
        return None
    return block, n, window_start


def _run_summarize(sid: str) -> None:
    """后台执行一次摘要（daemon 线程里跑）；任何异常都吞掉，下次再试。"""
    try:
        session = S.get_session(sid)
        if not session:
            return
        block = _compute_new_block(session)
        if not block:
            return
        block_msgs, from_idx, to_idx = block
        # 执行结论可留在聊天历史审计，但不能污染小本本。即使本段全被过滤，
        # 也要把摘要进度推进到 to_idx，避免每次后台任务都重复扫描同一段。
        summary_msgs = [m for m in block_msgs if S.is_summary_eligible(m)]
        if not summary_msgs:
            session = S.get_session(sid)
            if session:
                segs = list(session.get("summary", []) or [])
                segs.append({"n": to_idx, "text": "", "skip": True})
                S.set_summary(sid, segs)
            return
        # 网络调用在锁外做（慢，别占着会话锁）
        text = None
        try:
            text = asyncio.run(_summarize_once(summary_msgs))
        except Exception:
            text = None
        if not text:
            return
        # 再读一次最新 session（期间可能又追加了新消息），把新段追加到末尾
        session = S.get_session(sid)
        if not session:
            return
        segs = list(session.get("summary", []) or [])
        segs.append({"n": to_idx, "text": text})
        S.set_summary(sid, segs)
    except Exception:
        pass


def maybe_summarize(sid: str) -> None:
    """猫娘回复落库后调用：有窗口外未摘要的块就起后台线程摘要，不卡回复。"""
    try:
        session = S.get_session(sid)
        if session and _compute_new_block(session):
            threading.Thread(target=_run_summarize, args=(sid,), daemon=True).start()
    except Exception:
        pass

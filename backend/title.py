"""会话标题自动命名：第一轮问答完成后，用提炼子 agent 给会话起个好名字。

背景：新会话的标题原本是「首条用户消息去换行截 12 字」（sessions.append_message）——
「帮我在微信和王小明发一句xxx」会被截成「帮我在微信和王小明发一」。这里在回复落库后
起一个后台线程，让子 agent 把这一轮对话概括成标题覆盖掉那个占位标题。

关键设计（与 summary.py 同款，逐条对应它有注释的坑）：
- **认领在锁内做**（S.claim_title 先把 title_auto 置 False 才返回）→ 并发的第二次
  触发必然认领失败，**去重是白送的**，不会出现两个线程抢着写标题。
- **网络调用在会话锁外做** —— 提炼要几秒，绝不能占着会话锁（summary.py:115 同款理由）。
- **落笔前比对快照**（S.apply_title(sid, title, expected_prev)）—— 提炼这几秒里主人
  可能手动改名、清空会话、删掉会话；快照对不上就放弃，绝不拿旧对话的标题糊到新状态上。
- **失败把认领权放回去**（S.release_title_claim）—— 不然一次网络抖动就让这个会话
  永远拿不到智能标题；放回去后下一轮还能重试。
- 全程吞异常：起标题是锦上添花，绝不能因为它影响聊天。
"""

import asyncio
import threading

from . import sessions as S
from .agents import distill_title


def maybe_title(sid: str, user_text: str, answer: str) -> None:
    """猫娘回复落库后调用：能认领到自动起名权就起后台线程去提炼，不卡回复。

    claim_title 返回的「认领当时的标题」作为快照一路传给线程，落笔时用来判断
    这几秒里标题有没有被主人动过。
    """
    try:
        expected_prev = S.claim_title(sid)
        if expected_prev is None:
            return  # 已经起过名 / 主人改过名 / 会话不存在
        threading.Thread(
            target=_run_title,
            args=(sid, user_text, answer, expected_prev),
            daemon=True,
        ).start()
    except Exception:
        pass


def _run_title(sid: str, user_text: str, answer: str, expected_prev: str) -> None:
    """后台执行一次起名（daemon 线程里跑）；任何异常都吞掉。

    独立成函数是为了能被测试直接同步调用（照 summary._run_summarize 的做法）。
    """
    try:
        title = asyncio.run(distill_title(user_text, answer))
    except Exception:
        title = None
    try:
        if title:
            S.apply_title(sid, title, expected_prev)
        else:
            S.release_title_claim(sid, expected_prev)
    except Exception:
        pass

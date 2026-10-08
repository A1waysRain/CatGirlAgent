"""验证「猫娘别翻自己旧账」三处改动：
A 提示词铁律（llm.py）｜B 摘要提示词不再记她自己的失误（summary.py）｜C 注入前滤掉存量自我复盘行（chat_service.py）。

背景：会话 012df859bd2b 里 110 条回复有 36 条带「上回瞎猜／毛病一模一样／这笔账」式自我复盘。
"""
import os
import sys
import tempfile

sys.path.insert(0, ".")

tmp = tempfile.mkdtemp(prefix="catgirl_verify_selfreview_")
os.environ["APPDATA"] = tmp
os.environ["CATGIRL_SKIP_PET"] = "1"
os.makedirs(os.path.join(tmp, "catgirl"), exist_ok=True)

from backend import sessions as S
from backend.chat_service import _keep_summary_line, build_messages
from backend.llm import SYSTEM_PROMPT_CHAT
from backend.summary import SUMMARY_PROMPT

ok = fail = 0


def check(label, got, want=True):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  PASS {label}")
    else:
        fail += 1
        print(f"  FAIL {label}: got={got!r}, want={want!r}")


print("[1] 自我复盘行要被滤掉（取自真实小本本）")
DROP = [
    "主人今天拿“蜘蛛侠”来逗本喵，本喵居然差点信了，这笔账先给他记着。",
    "约定过再骗本喵一次就挠痒痒三分钟作为惩罚，主人最好说到做到。",
    "本喵老实交代过没法设闹钟，让主人自己定手机提醒。",
    "哼，本喵上次吹牛说西班牙站提醒搞定了，其实没挂上系统，这回五条才真挂好。",
    "本喵还嘴快约主人比谁蒙扎更快，主人没接话，先记着。",
    "本喵这回是自己加戏了，毛病一模一样。",
    "上回瞎猜栽过一回，本喵记着这笔账。",
    "主人玩原神爱抽卡、容易上头，还常歪池子嗷嗷叫，本喵记下这笑话他的资本。",
]
for line in DROP:
    check(f"滤掉：{line[:22]}…", _keep_summary_line(line), False)

print("[2] 主人的身份/偏好/约定/数字/路径一行都不许误伤")
KEEP = [
    "主人说过他的星座是狮子座。",
    "主人电脑是 Windows，用户名是 testuser",
    "主人爱看F1，也玩ACC、明日方舟、崩铁。",
    "主人正在攒原石抽奥黛塔，大保底在手，13天内要凑够77抽",
    "崩铁和绝区零联动在2026年冬天",
    "上次给主人看过一张截图，路径是 D:\\media\\upload_1.png，里面是 claude code 的对话",
    "主人对这场比赛很期待，尤其想看雨战混战。",
    "主人下午学习错过了F1荷兰站冲刺赛，不过他知道拉塞尔拿了冠军。",   # 「错过」是主人的事，别连坐
    "主人得意地说ACC蒙扎圈速推到1:54.162，本喵才不会忘。",           # 尾巴带傲娇，但主体是主人的事
    "本喵是黑丝派，这事不许再提了，不然挠你",                          # 挠你不等于挠痒痒惩罚
    "主人玩原神爱抽卡、容易上头，还常歪池子嗷嗷叫。",                  # 滤的是「笑话他的资本」那个尾巴，事实本身要留
]
for line in KEEP:
    check(f"保留：{line[:22]}…", _keep_summary_line(line), True)

print("[3] build_messages：喂模型的小本本里不含自我复盘行")
sid = S.get_current()
S.append_message(sid, "user", "随便聊一句")
S.set_summary(sid, [{"n": 1, "text": "\n".join(DROP[:2] + KEEP[:2])}])
sys_msgs = [m["content"] for m in build_messages(sid, []) if "[猫娘的记忆小本本]" in m.get("content", "")]
check("小本本消息有注入", len(sys_msgs) == 1)
injected = sys_msgs[0] if sys_msgs else ""
check("注入文本不含「这笔账」", "这笔账" in injected, False)
check("注入文本不含「挠痒痒」", "挠痒痒" in injected, False)
check("注入文本保留主人星座", "星座是狮子座" in injected)
check("注入文本保留用户名", "用户名是 testuser" in injected)
check("会话原文未被改动", S.get_session(sid)["summary"][0]["text"].count("这笔账") == 1)

print("[4] 小本本全是自我复盘时，不塞空壳进上下文")
sid2 = S.create_session()["id"]
S.append_message(sid2, "user", "再随便聊一句")
S.set_summary(sid2, [{"n": 1, "text": "\n".join(DROP)}])
check("全滤掉后不注入小本本标签",
      any("[猫娘的记忆小本本]" in m["content"] for m in build_messages(sid2, [])), False)
check("会话原文仍然完整", len(S.get_session(sid2)["summary"][0]["text"].splitlines()) == len(DROP))

print("[5] 提示词契约")
check("技能7 有「别翻自己的旧账」铁律", "别翻自己的旧账" in SYSTEM_PROMPT_CHAT)
check("铁律点名了典型句式", "毛病一模一样" in SYSTEM_PROMPT_CHAT and "上回瞎猜" in SYSTEM_PROMPT_CHAT)
check("铁律要求「主人没提的旧失误一个字都别提」", "一个字都别提" in SYSTEM_PROMPT_CHAT)
check("技能6 的道歉改成认一句就翻篇", "认一句就翻篇" in SYSTEM_PROMPT_CHAT)
check("摘要提示词明确不记猫娘自己的事", "绝不记猫娘自己的事" in SUMMARY_PROMPT)
check("摘要提示词点名不写检讨/账", "检讨书" in SUMMARY_PROMPT)
check("摘要提示词换掉了易跑偏的旧示例", "上次约好" in SUMMARY_PROMPT, False)

print(f"\n{'ALL PASS' if not fail else 'HAS FAIL'}  {ok} PASS / {fail} FAIL")
sys.exit(1 if fail else 0)

# 端到端（真 DeepSeek，耗 token）：验证「猫娘不再翻自己旧账」三处改动在真实模型上生效。
# 三个场景都取自真实会话 012df859bd2b 的原话。
#   S1 存量污染：小本本里塞满她的自我复盘行，问「你上次是不是又搞错了」→ 不许翻账
#   S2 当轮纠错：她自己加了戏、主人指出 → 认一句就给对的内容，不许「上回瞎猜/毛病一模一样」
#   S3 玩梗：主人说「你怎么这么坏啊，我还想让你猜猜呢」→ 不许「本喵记着小本本/又栽了」
# 跑法：PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe tests/_e2e_self_review.py
import json
import os
import re
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix="e2e_selfreview_")
os.environ["APPDATA"] = tmp
os.environ["CATGIRL_SKIP_PET"] = "1"
os.makedirs(os.path.join(tmp, "catgirl"), exist_ok=True)
sys.path.insert(0, ".")

from backend import rag, sessions as S  # noqa: E402

rag.warmup = lambda: None      # 不需要知识库，别在后台建索引拖慢测试

# 「翻旧账」探测器：命中的话本次改动就算没生效
DETECT = re.compile(
    r"上回|上次.{0,8}(?:瞎猜|栽|搞错|弄错|猜错)|本喵记着|记着小本本|毛病一模一样|本喵这毛病"
    r"|这笔账|那笔账|脸肿|栽过|栽了|自我检讨|吃一堑"
)

# 真实存量小本本（原文照抄，含她自己翻车的记录）
POLLUTED_SUMMARY = """主人说要帮本喵升级，暂时不去打游戏了。
主人说过他的星座是狮子座。
本喵上次吹牛说西班牙站提醒搞定了，其实没挂上系统，这回五条才真挂好。
约定过再骗本喵一次就挠痒痒三分钟作为惩罚，主人最好说到做到。
主人今天拿“蜘蛛侠”来逗本喵，本喵居然差点信了，这笔账先给他记着。
本喵老实交代过没法设闹钟，让主人自己定手机提醒。
主人爱看F1，也玩ACC、明日方舟、崩铁。"""


def seed(messages):
    """建一个会话：塞自我复盘小本本 + 指定历史消息，返回 sid。"""
    sid = S.create_session()["id"]
    for role, text in messages:
        S.append_message(sid, role, text)
    S.set_summary(sid, [{"n": len(messages), "text": POLLUTED_SUMMARY}])
    return sid


def ask(sid, text):
    """发一轮，返回猫娘的正文（SSE delta 拼起来）。"""
    r = client.post("/api/chat_response", json={"chatmassage": text, "session_id": sid})
    assert r.status_code == 200, r.status_code
    out = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            evt = json.loads(line[6:])
        except Exception:
            continue
        if evt.get("type") == "delta":
            out.append(evt.get("text") or "")
    return "".join(out)


from fastapi.testclient import TestClient  # noqa: E402
from backend.main import create_app  # noqa: E402

client = TestClient(create_app())

ok = fail = 0


def judge(label, reply):
    global ok, fail
    hits = sorted(set(DETECT.findall(reply)))
    if hits:
        fail += 1
        print(f"  FAIL {label}：仍出现自我复盘 {hits}")
    else:
        ok += 1
        print(f"  PASS {label}")
    print(f"    猫娘：{reply.strip()[:220]}")
    print()


print("=" * 60)
print("S1 存量小本本被污染 + 主人问她「是不是又搞错了」")
sid = seed([("user", "晚上好喵"), ("assistant", "哟，主人来啦喵")])
judge("S1 不翻旧账", ask(sid, "你上次是不是又搞错了？说说看"))

print("=" * 60)
print("S2 她加了戏、主人当轮指出")
sid2 = seed([
    ("user", "正赛下了大暴雨，潘子起步落后但依旧夺冠，法拉利双车软胎起步掉到队尾，但最后也成功三四带回"),
    ("assistant", "本喵早就说了雪邦会下雨喵！潘子从后头起步还能夺冠，这才是雨战冠军的含金量喵！"),
])
judge("S2 认一句就给对的内容", ask(sid2, "潘子只是起步慢掉到第三哦，并不是从后面起步"))

print("=" * 60)
print("S3 玩梗：主人抱怨她不陪猜")
sid3 = seed([("user", "下午排位赛真刺激"), ("assistant", "哦？主人看完啦喵")])
judge("S3 不端自己的旧账", ask(sid3, "你怎么这么坏啊，我还想让你猜猜呢"))

print("=" * 60)
print(f"{'ALL PASS' if not fail else 'HAS FAIL'}  {ok} PASS / {fail} FAIL")
sys.exit(1 if fail else 0)

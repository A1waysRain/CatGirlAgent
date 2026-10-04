"""验证 L1（token 预算取历史 + 工具结果截断）+ L2（增量摘要记忆）后端逻辑。

摘要调用打桩（不真调 DeepSeek）；会话/API 走隔离 APPDATA。
"""
import os
import sys
import tempfile

sys.path.insert(0, ".")

_tmp = tempfile.mkdtemp(prefix="catgirl_verify_compress_")
os.environ["APPDATA"] = _tmp
os.environ["CATGIRL_SKIP_PET"] = "1"
os.makedirs(os.path.join(_tmp, "catgirl"), exist_ok=True)

ok = 0
fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  ✓ {label}")
    else:
        fail += 1
        print(f"  ✗ {label}\n      got  {got!r}\n      want {want!r}")


# ================= L1 =================
from backend.sessions import est_tokens, recent_messages, get_current, append_message, get_summary, set_summary
from backend.llm import _TOOL_RESULT_MAX, _fmt_tool_result

print("== L1: est_tokens / recent_messages ==")
check("中文≈1字/token", 4 <= est_tokens("你好世界") <= 6, True)
check("英文≈4字符/token", est_tokens("hello world") <= 10, True)
check("空串=0", est_tokens(""), 0)

msgs = [{"role": "user", "content": "你好"} for _ in range(200)]
r = recent_messages(msgs)
check("最新一条必含", r[-1] is msgs[-1], True)
check("短消息能装超过 20 条", len(r) > 20, True)
big = [{"role": "user", "content": "汉" * 20000}]
check("单条超预算也保留", recent_messages(big)[0] is big[0], True)

print("== L1: 工具结果截断 ==")
long_res = "x" * (_TOOL_RESULT_MAX + 1000)
f = _fmt_tool_result(long_res)
check("超长截断加提示",
      len(f) < len(long_res) and f.startswith("x" * _TOOL_RESULT_MAX) and "截断" in f, True)
check("短结果原样", _fmt_tool_result("短"), "短")


# ================= L2 =================
import backend.summary as summary_mod

sid = get_current()

print("== L2: set_summary 段数上限合并 ==")
from backend.sessions import create_session
sid_merge = create_session()["id"]
for i in range(25):
    append_message(sid_merge, "user" if i % 2 == 0 else "assistant", f"第 {i} 条消息")
set_summary(sid_merge, [{"n": 5, "text": "甲"}, {"n": 10, "text": "乙"}, {"n": 15, "text": "丙"}, {"n": 20, "text": "丁"}])
segs = get_summary(sid_merge)
check("超 3 段合并最早的 2 段", len(segs), 3)
check("合并后第一段含甲乙、n 取靠后", segs[0]["text"] == "甲\n乙" and segs[0]["n"] == 10, True)
check("顺序保持", [s["text"] for s in segs], ["甲\n乙", "丙", "丁"])

print("== L2: _compute_new_block 窗口外未摘要块 ==")
# 清掉摘要，重造消息：长消息让窗口外有内容
set_summary(sid, [])
# 会话已空，灌 50 条长消息（每条 ~200 字 ≈ 200 token，50 条 > 8000 预算 → 窗口外有旧消息）
for i in range(50):
    append_message(sid, "user" if i % 2 == 0 else "assistant", f"这是一条用来填满上下文窗口的长消息，内容编号 {i}，" + "啊" * 180)
session = __import__("backend.sessions", fromlist=["get_session"]).get_session(sid)
block = summary_mod._compute_new_block(session)
check("窗口外有块可摘要", block is not None, True)
if block:
    bmsgs, from_idx, to_idx = block
    check("块起点=0（还没摘要过）", from_idx, 0)
    check("块长度>=MIN_BLOCK", len(bmsgs) >= 8, True)
    check("块终点=窗口起点", to_idx > 0, True)

print("== L2: _run_summarize 增量写入 ==")
calls = []


async def fake_summarize(messages):
    calls.append(len(messages))
    return "主人说过他的星座是狮子座，最近在找工作。约定周末一起打原神。"

summary_mod._summarize_once = fake_summarize
summary_mod._run_summarize(sid)   # 同步跑，不真调网络
segs = get_summary(sid)
check("摘要段已写入", len(segs), 1)
check("摘要点推进到窗口起点", segs[0]["n"] > 0, True)
check("摘要文本是猫娘语气要点", "狮子座" in segs[0]["text"], True)
check("摘要调用收到的消息条数>0", calls and calls[-1] > 0, True)

# 再跑一次：窗口外没有新的未摘要块 → 不追加
summary_mod._run_summarize(sid)
check("无新块不重复追加", len(get_summary(sid)), 1)

print("== L2: build_messages 组装 ==")
from backend.chat_service import build_messages
m = build_messages(sid, [{"role": "user", "content": "还记得我之前说的吗"}])
check("第一句是系统提示词", m[0]["role"], "system")
# 位置断言改成语义断言：2026-10-04 起默认档会在系统提示词之后插一条「联网搜索策略」
# system 消息，小本本不再恒等于 m[1]。真正要守的是"小本本在、且在最近窗口之前"。
_sum_idx = next((i for i, x in enumerate(m) if "猫娘的记忆小本本" in x.get("content", "")), -1)
check("有小本本（有摘要时）", _sum_idx > 0, True)
check("小本本排在最近窗口之前", 0 < _sum_idx < len(m) - 1, True)
check("摘要内容带要点", "狮子座" in m[_sum_idx]["content"], True)
check("最近窗口在最后", m[-1]["content"], "还记得我之前说的吗")

print("== L2: 无摘要时不插小本本 ==")
sid2 = __import__("backend.sessions", fromlist=["create_session"]).create_session()["id"]
m2 = build_messages(sid2, [{"role": "user", "content": "hi"}])
check("无摘要时全程不含小本本", any("猫娘的记忆小本本" in x.get("content", "") for x in m2), False)
check("无摘要时窗口仍在最后", m2[-1]["content"], "hi")


print("== L2: API GET/DELETE summary ==")
from fastapi.testclient import TestClient
from backend import main
client = TestClient(main.create_app(allowed_origins=None))
r = client.get(f"/api/sessions/{sid}/summary")
check("GET summary 200 带摘要", r.status_code == 200 and len(r.json()["summary"]) == 1, True)
r = client.delete(f"/api/sessions/{sid}/summary")
check("DELETE summary 清空", r.status_code == 200 and r.json()["summary"] == [], True)
check("清空后 get_summary 为空", get_summary(sid), [])
r = client.get("/api/sessions/不存在/summary")
check("不存在会话 404", r.status_code, 404)


print(f"\n结果：{ok} 通过，{fail} 失败")
sys.exit(1 if fail else 0)

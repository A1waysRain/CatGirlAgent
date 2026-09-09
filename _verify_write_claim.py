# 验证：写文件声称审计兜底（2026-08-18 修复清单②）
#  1) _extract_write_request 意图检测用例
#  2) stream_answer 审计链路：空口声称→更正；真写了→不更正；无意图→不更正；无声称词→不更正
#  3) llm.py 流式 agent 对写文件工具的 state 标记
import asyncio
import sys

sys.path.insert(0, "backend")
import backend.llm as llm  # noqa: E402
import backend.chat_service as cs  # noqa: E402
import backend.tools as tools  # noqa: E402
from backend.routers.chat import _extract_write_request  # noqa: E402

PASS = FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


# ---- 1. 意图检测 ----
print("== 1. _extract_write_request ==")
intent_cases = [
    ("那你直接生成一篇新的文档放在桌面吧", True),
    ("帮我把内容写成一个Word文档", True),
    ("生成一份PPT放桌面", True),
    ("新建一个txt文件保存到桌面", True),
    ("重新生成一份个人心得放到桌面", True),
    ("把家乡换成东莞", False),
    ("帮我分析一下这个文档", False),
    ("我还没写作业", False),
    ("可以", False),
    ("你再看看上面说的内容", False),
]
for text, want in intent_cases:
    got = _extract_write_request(text)
    ok(f"「{text}」→ {got}", got == want, f"want {want}")

# ---- 2. stream_answer 审计链路 ----
print("== 2. stream_answer 审计 ==")


def patch(fake_llm):
    cs.get_session = lambda sid: {"title": "测试会话"}
    cs.session_append = lambda sid, role, content: (calls.append((role, content)) or [{"role": "assistant", "id": "m1"}])
    cs.notifier.put = lambda *a, **k: None
    cs.infer_mood = lambda *a, **k: "happy"
    cs.call_deepseek_with_tools_stream = fake_llm


async def run_stream(write_requested, write_state, text="本喵已经生成新文档放桌面了喵"):
    evts = []
    async for e in cs.stream_answer("s1", [], "生成文档放桌面",
                                    write_requested=write_requested, write_state=write_state):
        evts.append(e)
    return evts


# 场景 A：空口声称（意图命中、没真调工具、答案含"已生成"）→ 应追加更正
calls = []


async def fake_llm_no_write(messages, tools, state=None, max_rounds=5):
    yield {"type": "delta", "text": "本喵已经生成新文档放桌面了喵"}


patch(fake_llm_no_write)
evts = asyncio.run(run_stream(True, {}))
corr = [e for e in evts if e["type"] == "delta" and "诚实" in e["text"]]
ok("A1 空口声称→追加更正 delta", len(corr) == 1, str(evts))
ok("A2 落库内容含更正", any("诚实承认" in c for _, c in calls), str(calls))

# 场景 B：真调了写文件工具 → 不应更正
calls = []


async def fake_llm_wrote(messages, tools, state=None, max_rounds=5):
    state["wrote_file"] = True
    yield {"type": "delta", "text": "本喵已经生成新文档放桌面了喵"}


patch(fake_llm_wrote)
evts = asyncio.run(run_stream(True, {}))
corr = [e for e in evts if e["type"] == "delta" and "诚实" in e["text"]]
ok("B1 真写文件→不更正", len(corr) == 0, str(evts))
ok("B2 落库为原文（不含更正）", all("诚实承认" not in c for _, c in calls), str(calls))

# 场景 C：无写文件意图 → 不更正
calls = []
patch(fake_llm_no_write)
evts = asyncio.run(run_stream(False, None))
corr = [e for e in evts if e["type"] == "delta" and "诚实" in e["text"]]
ok("C1 无意图→不更正", len(corr) == 0, str(evts))

# 场景 D：答案不含声称词 → 不更正
calls = []


async def fake_llm_no_claim(messages, tools, state=None, max_rounds=5):
    yield {"type": "delta", "text": "本喵正在准备，还差一步喵"}


patch(fake_llm_no_claim)
evts = asyncio.run(run_stream(True, {}))
corr = [e for e in evts if e["type"] == "delta" and "诚实" in e["text"]]
ok("D1 无声称词→不更正", len(corr) == 0, str(evts))

# ---- 3. llm.py state 标记 ----
print("== 3. 流式 agent 写文件工具标记 ==")
stream_calls = []


async def fake_stream(client, payload):
    stream_calls.append(payload)
    yield {"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": "call_1",
        "function": {"name": "append_file", "arguments": "{}"},
    }]}}]}
    yield {"choices": [{"delta": {}}]}
    yield {"choices": [{"delta": {"content": "改好了喵"}}]}


llm._stream_chat_json = fake_stream
tools.run_tool = lambda name, args: "已经写入喵"
state = {}


async def run_llm():
    async for _ in llm.call_deepseek_with_tools_stream(
        messages=[{"role": "user", "content": "写文件"}], tools=[{}], state=state
    ):
        pass


asyncio.run(run_llm())
ok("llm 标记 append_file 为 wrote_file", state.get("wrote_file") is True, str(state))

# 对照：非写文件工具（get_time）不应标记
state2 = {}
tools.run_tool = lambda name, args: "时间"


async def fake_stream2(client, payload):
    yield {"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": "c2",
        "function": {"name": "get_time", "arguments": "{}"},
    }]}}]}
    yield {"choices": [{"delta": {}}]}
    yield {"choices": [{"delta": {"content": "好了喵"}}]}


llm._stream_chat_json = fake_stream2


async def run_llm2():
    async for _ in llm.call_deepseek_with_tools_stream(
        messages=[{"role": "user", "content": "问时间"}], tools=[{}], state=state2
    ):
        pass


asyncio.run(run_llm2())
ok("非写文件工具不标记", state2.get("wrote_file") is None, str(state2))

print(f"\n结果: {PASS} 过 / {FAIL} 挂")
sys.exit(1 if FAIL else 0)

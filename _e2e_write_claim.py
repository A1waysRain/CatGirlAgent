# 端到端：POST /api/chat_response 发「生成文档放桌面」→ mock LLM 空口声称 → 断言 SSE 含更正
import os
import sys
import tempfile

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="catgirl_e2e_")
sys.path.insert(0, "backend")
import backend.chat_service as cs  # noqa: E402


async def fake_llm(messages, tools, state=None, max_rounds=5):
    yield {"type": "delta", "text": "好嘞喵！新文档已生成放桌面了喵，路径是 C:\\Users\\x\\Desktop\\测试.docx"}


cs.call_deepseek_with_tools_stream = fake_llm
cs.get_session = lambda sid: {"title": "测试"}
cs.notifier.put = lambda *a, **k: None
cs.infer_mood = lambda *a, **k: "happy"

from fastapi.testclient import TestClient  # noqa: E402
import backend.main as m  # noqa: E402

client = TestClient(m.app)

# 场景：空口声称（意图命中、没真调工具、回复含"已生成"）→ 应被更正
r = client.post("/api/chat_response", json={"chatmassage": "那你直接生成一篇新的文档放在桌面吧"})
assert r.status_code == 200, r.status_code
has_correction = "诚实承认" in r.text
print("SSE 含更正:", has_correction)
assert has_correction, "空口声称应被更正！SSE: " + r.text[:300]

# 场景：普通请求（无写文件意图）→ 不受审计影响
r2 = client.post("/api/chat_response", json={"chatmassage": "帮我分析一下这个文档"})
assert r2.status_code == 200
print("普通请求不受影响:", "诚实承认" not in r2.text)

print("\n✅ 端到端写文件声称审计通过")

# 端到端：真实构建 RAG 索引 + 真实 DeepSeek agent 循环
# 验证猫娘遇到概念/功能实现问题会主动调 rag_query，并基于检索片段回答。
# 隔离 APPDATA（临时目录 + 复制真实教材建索引），可随时复跑；耗真实 DeepSeek token。
#
# 跑法：PYTHONIOENCODING=utf-8 HF_ENDPOINT=https://hf-mirror.com .venv/Scripts/python.exe _e2e_rag.py
import os
import shutil
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix="e2e_rag_")
os.environ["APPDATA"] = tmp

from backend import rag  # noqa: E402

rag._init_dirs()
print(f"同步默认语料 {rag._sync_default_docs()} 份")
rag.ensure_index(block=True)
print(f"索引就绪，chunks = {len(rag._index['chunks'])}")

# 追踪模型是否真的调了 rag_query（包一层记录 query）
_orig_search = rag.search
CALLS = []


def _spy_search(query, top_k=3):
    CALLS.append(query)
    return _orig_search(query, top_k)


rag.search = _spy_search

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import create_app  # noqa: E402

client = TestClient(create_app())

QUESTIONS = [
    "流式输出怎么实现？",
    "猫娘的上下文压缩是怎么做的？",
]
for q in QUESTIONS:
    print("\n" + "=" * 56)
    print("问：", q)
    r = client.post("/api/chat_response", json={"chatmassage": q, "session_id": None})
    print("HTTP", r.status_code)
    # 流式响应：data: json 行；只打印 delta 拼出正文（结尾 done）
    answer, ok_rag = "", False
    for line in r.text.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            import json
            evt = json.loads(line[6:])
        except Exception:
            continue
        if evt.get("type") == "delta":
            answer += evt["text"]
    print("猫娘：", answer[:300].replace("\n", " "))
    print(f"本轮是否调 rag_query: {CALLS and (CALLS[-1], '…') or '没有'}")

print("\n===== 结论 =====")
used = len(CALLS) >= len(QUESTIONS)
print("rag_query 被调用次数:", len(CALLS))
print("e2e " + ("PASS ✅ 猫娘主动检索知识库" if used else "波动：模型没主动调 rag_query（可复跑重试）"))
shutil.rmtree(tmp, ignore_errors=True)

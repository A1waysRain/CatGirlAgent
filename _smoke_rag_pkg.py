# -*- coding: utf-8 -*-
"""打包版 RAG 冒烟：临时 APPDATA + 放测试文档 → 启动 exe → 聊天触发 rag_query，
验证 exe 里 fastembed 进包、知识库检索全链路可用（空库/有文档两种）。
隔离临时目录，用后自删；taskkill /T 干净退出。"""
import os
import sys
import time
import json
import shutil
import tempfile
import subprocess
import urllib.request
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "猫娘来咯.exe")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PASS = FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


def listening_ports():
    out = subprocess.run(["netstat", "-ano"], capture_output=True,
                         creationflags=CREATE_NO_WINDOW).stdout.decode("gbk", errors="ignore")
    ports = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            try:
                addr, port = parts[1].rsplit(":", 1)
            except ValueError:
                continue
            if addr in ("127.0.0.1", "0.0.0.0", "[::]"):
                ports.append(int(port))
    return ports


def find_backend():
    for port in listening_ports():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions", timeout=2) as r:
                data = json.load(r)
                if "sessions" in data and "current" in data:
                    return f"http://127.0.0.1:{port}"
        except Exception:
            continue
    return None


def chat(base, q):
    """POST /api/chat_response，返回流式文本（delta 拼出）。"""
    body = json.dumps({"chatmassage": q, "session_id": None}).encode("utf-8")
    req = urllib.request.Request(base + "/api/chat_response", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        text = r.read().decode("utf-8")
    ans = ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            evt = json.loads(line[6:])
        except Exception:
            continue
        if evt.get("type") == "delta":
            ans += evt["text"]
    return ans


def main():
    assert os.path.exists(EXE), f"缺少 exe: {EXE}"
    tmp = tempfile.mkdtemp(prefix="smoke_rag_pkg_")
    # 启动前放好测试文档（知识库非空 → warmup 会建索引）
    doc_dir = os.path.join(tmp, "catgirl", "rag_data", "docs")
    os.makedirs(doc_dir, exist_ok=True)
    with open(os.path.join(doc_dir, "test_rag.md"), "w", encoding="utf-8") as f:
        f.write("# 猫娘打包测试\n打包版知识库冒烟测试通过标志词：喵喵火箭发射 12345。\n" * 5)

    env = dict(os.environ)
    env["APPDATA"] = tmp
    env["HF_ENDPOINT"] = "https://hf-mirror.com"
    env["HF_HUB_DISABLE_XET"] = "1"
    proc = subprocess.Popen([EXE], cwd=HERE, env=env)
    try:
        base = None
        for _ in range(60):
            base = find_backend()
            if base:
                break
            time.sleep(0.5)
        ok("exe 后端启动", base is not None)
        if not base:
            print("✗ 未找到后端端口"); sys.exit(1)
        print("  后端端口:", base)

        # 等 warmup 建好索引（sig.json 出现即构建完成）
        sig = os.path.join(tmp, "catgirl", "rag_data", "index", "sig.json")
        for _ in range(60):
            if os.path.exists(sig):
                break
            time.sleep(1)
        ok("warmup 已建索引", os.path.exists(sig))

        # 问题不带知识库特定词（防模型复述问题词造成假命中），只问"文档里记录的标志词"
        q = "用知识库检索功能查一下：知识库文档里记录的那个标志词是什么"
        ans = chat(base, q)
        print("  问:", q)
        print("  猫娘回复片段:", ans[:200].replace("\n", " "))
        hit = ("喵喵火箭" in ans) or ("12345" in ans) or ("test_rag" in ans)
        ok("聊天触发 rag_query 检索到测试文档", hit)

        if not hit:
            # 再问一次换表述，减少模型自觉波动
            ans2 = chat(base, "请检索知识库，告诉我文档里写的标志词是什么")
            print("  重试回复片段:", ans2[:200].replace("\n", " "))
            ok("重试检索命中", ("喵喵火箭" in ans2) or ("12345" in ans2) or ("test_rag" in ans2))

        print(f"\n===== RAG 打包冒烟：PASS {PASS} / FAIL {FAIL} =====")
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
        time.sleep(1)
        shutil.rmtree(tmp, ignore_errors=True)
        print("已 taskkill /T 退出，临时目录已清理")
        sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

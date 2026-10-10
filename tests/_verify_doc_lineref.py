# 校验教材里所有「文件:行号」引用是否落在真实代码的合理位置（行号不越界、文件存在）。
# 会把每处引用对应的真实行首打出来，方便人工扫一遍有没有指错位置。
# 用法（从 Cat_Girl/ 目录）：
#   PYTHONIOENCODING=utf-8 .venv\Scripts\python _verify_doc_lineref.py            # 默认扫流式+RAG 两份主教材
#   PYTHONIOENCODING=utf-8 .venv\Scripts\python _verify_doc_lineref.py --all      # 再加 LangChain/FastAPI 大白话
#   PYTHONIOENCODING=utf-8 .venv\Scripts\python _verify_doc_lineref.py <md路径...>  # 指定教材
# 说明：脚本只查「文件:行号」文本引用是否落在代码文件范围内；引用语义对不对（指没指错函数）
#       靠下面打印的真实行首人工核对。改 rag.py/tools.py/llm.py 等代码后记得跑一遍。
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 测试脚本在 Cat_Girl/tests/，教材在仓库根目录资料/；使用跨平台路径，
# 不再把 Windows 反斜杠当成 Linux 文件名。
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(HERE)
DEFAULT_MDS = [
    os.path.join(REPO_ROOT, "资料", "2.流式输出streaming+SSE", "流式输出-大白话对号猫娘.md"),
    os.path.join(REPO_ROOT, "资料", "4.RAG检索增强生成", "RAG-大白话对号猫娘.md"),
]
# --all 时追加的教材
ALL_MDS = [
    os.path.join(REPO_ROOT, "资料", "3.LangChain和LangGraph", "LangChain和LangGraph-大白话对号猫娘.md"),
    os.path.join(REPO_ROOT, "资料", "1.FastAPI+Python异步", "FastAPI与Python异步-大白话对号猫娘.md"),
]


def resolve(fname: str) -> str:
    """把教材里的裸文件名映射到真实路径（相对 Cat_Girl/）。"""
    if fname.startswith("Cat_Girl/"):
        fname = fname[len("Cat_Girl/"):]
    if fname.startswith(("backend/", "js/", "desktop/")):
        return fname
    if fname.startswith("routers/"):
        return "backend/" + fname
    if fname == "app.js":
        return "js/app.js"
    if fname == "chat.py":
        return "backend/routers/chat.py"
    return f"backend/{fname}"


REF_RE = re.compile(r"((?:[\w.-]+/)*[\w\-]+\.(?:py|js)):(\d+)")


def check(md: str):
    """扫一份教材，返回 (problems, seen)。seen: {file:行号 -> 真实行首}。"""
    try:
        content = open(md, encoding="utf-8").read()
    except FileNotFoundError:
        return [f"{md}  FILE_NOT_FOUND（路径写错？）"], {}
    problems, seen = [], {}
    for f, ln in REF_RE.findall(content):
        ln = int(ln)
        try:
            lines = open(os.path.join(HERE, resolve(f)), encoding="utf-8").read().splitlines()
        except FileNotFoundError:
            problems.append(f"{f}:{ln}  FILE_NOT_FOUND（{resolve(f)}）")
            continue
        if ln < 1 or ln > len(lines):
            problems.append(f"{f}:{ln}  行号越界（{resolve(f)} 共 {len(lines)} 行）")
            continue
        seen[(f, ln)] = lines[ln - 1].strip()
    return problems, seen


def main() -> int:
    args = sys.argv[1:]
    if "--all" in args:
        args.remove("--all")
        mds = DEFAULT_MDS + ALL_MDS
    elif args:
        mds = args
    else:
        mds = DEFAULT_MDS

    all_problems, total = [], 0
    for md in mds:
        problems, seen = check(md)
        all_problems += problems
        total += len(seen)
        print(f"\n===== {os.path.basename(md)}（{len(seen)} 处引用）=====")
        for (f, ln), text in sorted(seen.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            print(f"{f}:{ln}  ->  {text[:76]}")

    print("\n--- 硬错误 ---" if all_problems else f"\n--- 全部通过（{total} 处引用行号都在文件范围内） ---")
    for p in all_problems:
        print(p)
    return 1 if all_problems else 0


if __name__ == "__main__":
    sys.exit(main())

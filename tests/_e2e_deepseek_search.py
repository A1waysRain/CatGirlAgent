# -*- coding: utf-8 -*-
"""DeepSeek Search / Fetch 真机端到端（会花钱，约 2 次成功请求 ≈ 0.01 元）。

跑法（必须在 Cat_Girl 目录，才能读到 .env 里的真实 Key）：
    PYTHONIOENCODING=utf-8 PYTHONPATH="$(pwd)" ./.venv/Scripts/python.exe tests/_e2e_deepseek_search.py

只调用一次真检索 + 一次真抓取，验证：
- 真实响应字段与我们映射的一致（正文 / 日期 / rerank / authority）
- tool_verify_current_fact 真能拿到正文并判级
- Fetch API 真能返回 Markdown 正文
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.config import settings  # noqa: E402
from backend import tools  # noqa: E402

PASS, FAIL = 0, 0


def check(label, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"[PASS] {label} {extra}")
    else:
        FAIL += 1
        print(f"[FAIL] {label} {extra}")


if not (settings.deepseek_api_key or "").strip():
    print("[SKIP] 没读到 DeepSeek API Key（请在 Cat_Girl 目录跑，或配置 .env / config.json）")
    raise SystemExit(0)

if (settings.web_search_provider or "").lower() != "deepseek":
    print(f"[WARN] 当前 provider={settings.web_search_provider}，本脚本临时切到 deepseek")
    settings.web_search_provider = "deepseek"

print("查询：F1 2026 赛历（max_sources=4）")
report = json.loads(tools.tool_verify_current_fact("F1 2026 赛历", max_sources=4, force_refresh=True))

check("走的是 DeepSeek 检索", report.get("search_provider") == "deepseek", str(report.get("search_provider")))
check("没有整体失败", report.get("status") != "failed", str(report.get("status")))
check("至少一条正文证据", int((report.get("evidence") or {}).get("full", 0)) >= 1,
      json.dumps(report.get("evidence"), ensure_ascii=False))
check("正文来自 Search（不是逐页抓取）",
      int((report.get("evidence") or {}).get("content_from_search", 0)) >= 1)
check("有独立来源域名", int((report.get("evidence") or {}).get("independent_domains", 0)) >= 1)

sources = report.get("sources") or []
if sources:
    first = sources[0]
    print("首个来源：", first.get("domain"), first.get("grade"),
          f"{len(first.get('extract') or '')} 字", first.get("published"))
    check("来源带日期", bool(first.get("published")))
    check("来源带 rerank/authority", first.get("rerank_score") is not None or first.get("authority") is not None)

brief = tools.fact_brief("F1 2026 赛历", report=report)
check("fact_brief 有内容", bool(brief.strip()))
if brief:
    print("brief 预览：", brief[:180].replace("\n", " "))

# Fetch API 单独验证一次（用第一条结果的 URL）
if sources and sources[0].get("url"):
    url = sources[0]["url"]
    print("Fetch 测试：", url)
    text, err = tools._fact_fetch_deepseek(url)
    check("Fetch API 返回正文", bool(text.strip()), err or "")
    if text:
        print("Fetch 正文预览：", text[:160].replace("\n", " "))

print(f"\n真机结果：{PASS} PASS / {FAIL} FAIL；耗时 {report.get('elapsed_s')}s；约花费 0.01 元")
raise SystemExit(1 if FAIL else 0)

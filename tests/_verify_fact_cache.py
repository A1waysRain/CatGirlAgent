# -*- coding: utf-8 -*-
"""验证事实核验 M2：TTL 缓存、强制刷新和会话内来源引用。"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = tempfile.mkdtemp(prefix="catgirl_fact_cache_")
os.environ["APPDATA"] = ROOT
os.environ["CATGIRL_SKIP_PET"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend import tools
from backend.chat_service import _assistant_lifecycle
from backend.sessions import append_message, create_session

passed = failed = 0


def check(label, condition, extra=""):
    global passed, failed
    if condition:
        passed += 1
        print("[PASS]", label, extra)
    else:
        failed += 1
        print("[FAIL]", label, extra)


calls = {"search": 0, "fetch": 0}


def fake_search(query, max_sources, freshness="current"):
    calls["search"] += 1
    return [{"title": "近期官方资料", "url": "https://example.com/fact",
             "snippet": "摘要", "published": "2026-09-28"}]


def fake_fetch(url, lang):
    calls["fetch"] += 1
    return "M2缓存测试：这是足够长的当前事实正文，包含问题关键词和一段可靠说明。" * 40, None


real_search, real_fetch = tools._fact_search, tools._fact_fetch
tools._fact_search, tools._fact_fetch = fake_search, fake_fetch
try:
    first = json.loads(tools.tool_verify_current_fact("M2缓存测试", max_sources=2))
    second = json.loads(tools.tool_verify_current_fact("M2缓存测试", max_sources=2))
    check("首次核验写入缓存", first.get("cache") == "miss")
    check("重复核验命中缓存", second.get("cache") == "hit")
    check("缓存命中不重复搜索", calls["search"] == 1 and calls["fetch"] == 1, str(calls))

    forced = json.loads(tools.tool_verify_current_fact("M2缓存测试", max_sources=2, force_refresh=True))
    check("force_refresh 绕过缓存", forced.get("cache") == "miss" and calls["search"] == 2)

    cache_path = Path(ROOT) / "catgirl" / "fact_cache.json"
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    for item in data["entries"].values():
        item["expires_at"] = time.time() - 1
    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    expired = json.loads(tools.tool_verify_current_fact("M2缓存测试", max_sources=2))
    check("过期缓存自动重新核验", expired.get("cache") == "miss" and calls["search"] == 3)

    before_fetch = calls["fetch"]
    tools._fact_search = lambda *args, **kwargs: [
        {"title": "同一来源A", "url": "https://example.com/fact#top", "snippet": "摘要", "published": "2026-09-28"},
        {"title": "同一来源B", "url": "https://example.com/fact", "snippet": "摘要", "published": "2026-09-28"},
    ]
    tools.tool_verify_current_fact("M2缓存测试去重", max_sources=2, force_refresh=True)
    check("相同 URL 去重后只抓取一次", calls["fetch"] == before_fetch + 1)
    tools._fact_search = fake_search

    refs = tools.fact_references(first)
    check("来源引用只保留元数据", refs and refs[0]["url"] == "https://example.com/fact" and "正文" not in json.dumps(refs, ensure_ascii=False))
    check("引用包含来源等级", refs and refs[0].get("grade") == "full")

    sid = create_session()["id"]
    lifecycle = _assistant_lifecycle(["verify_current_fact"])
    lifecycle["fact_refs"] = refs
    messages = append_message(sid, "assistant", "依据当前来源回答喵", **lifecycle)
    saved = messages[-1]
    check("会话消息保存 fact_refs", saved.get("fact_refs") == refs)
    check("事实核验仍是临时结论", saved.get("context_policy") == "temporary" and saved.get("summary_allowed") is False)
finally:
    tools._fact_search, tools._fact_fetch = real_search, real_fetch

schema = next(t for t in tools.TOOL_SCHEMAS if t["function"]["name"] == "verify_current_fact")
check("工具 schema 暴露 force_refresh", "force_refresh" in schema["function"]["parameters"]["properties"])

print(f"结果：{passed} PASS, {failed} FAIL")
raise SystemExit(1 if failed else 0)

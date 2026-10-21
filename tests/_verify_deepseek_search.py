# -*- coding: utf-8 -*-
"""DeepSeek Search / Fetch 接入测试（2026-10-20）。

不打桩 `_fact_search` / `_fact_fetch` 本体，只打桩 I/O 边界
（`urllib.request.urlopen`），确保真实代码路径被执行——这是 `_verify_web_fact.py`
留下的教训（打桩打过头会漏掉 NameError 这种真 bug）。

覆盖：
- provider=deepseek 时请求端点 / 鉴权 / 请求体
- 时效、域名操作符与 128 字符截断
- 搜索结果字段映射（正文 / 日期 / rerank / authority）
- DeepSeek 失败或空结果时降级 Bing RSS
- need_content 正文直接参与判级，不再逐页抓取
- Search 没给正文时走 DeepSeek Fetch，再失败才走自建抓取
- 缓存键纳入 DeepSeek Key 状态
"""
import datetime
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp(prefix="catgirl_ds_search_")
os.environ["APPDATA"] = _TMP
os.environ["CATGIRL_SKIP_PET"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend import tools  # noqa: E402

TOTAL = [0, 0]
TODAY = datetime.date.today().isoformat()
HOST_A = "https://93.184.216.34"
HOST_B = "https://93.184.216.35"
HOST_C = "https://93.184.216.36"

F1_BODY = ("2026 F1 赛历已经公布。2026赛季F1赛历共24站：巴林大奖赛、阿塞拜疆大奖赛、"
           "日本大奖赛等。F1 赛历的具体时间以官方公告为准。") * 8


def check(label, condition, extra=""):
    TOTAL[0] += 1
    if condition:
        print(f"[PASS] {label}")
    else:
        TOTAL[1] += 1
        print(f"[FAIL] {label} {extra}")


def section(title):
    print(f"\n=== {title} ===")


class _Resp:
    def __init__(self, body: bytes, ctype="application/json"):
        self._body = body
        self.headers = {"Content-Type": ctype}

    def read(self, n=None):
        return self._body if n is None else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Settings:
    def __init__(self, deepseek_key="sk-test", provider="deepseek"):
        self.deepseek_api_key = deepseek_key
        self.deepseek_base_url = "https://api.deepseek.com/v1/chat/completions"
        self.web_search_provider = provider
        self.web_search_api_key = ""
        self.web_fact_timeout = 5


class _Net:
    """按 URL 分发 DeepSeek Search / Fetch、Bing RSS 与普通网页。"""

    def __init__(self):
        self.deepseek_results = []
        self.deepseek_fetch = ""
        self.raise_deepseek = False
        self.max_deepseek = 99
        self.rss_items = []
        self.pages = {}
        self.seen = []
        self.search_bodies = []
        self.fetch_bodies = []

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", str(req))
        self.seen.append(url)
        if "tools/search" in url:
            self.search_bodies.append(json.loads(req.data.decode("utf-8")))
            if self.raise_deepseek:
                raise urllib.error.URLError("deepseek down")
            if len(self.search_bodies) > self.max_deepseek:
                return _Resp(b'{"results": []}')
            return _Resp(json.dumps({"results": self.deepseek_results}, ensure_ascii=False).encode("utf-8"))
        if "tools/fetch" in url:
            self.fetch_bodies.append(json.loads(req.data.decode("utf-8")))
            return _Resp(json.dumps({"title": "抓取页", "content": self.deepseek_fetch},
                                    ensure_ascii=False).encode("utf-8"))
        if "format=rss" in url:
            items = "".join(
                "<item><title>{title}</title><link>{url}</link>"
                "<description>{snippet}</description><pubDate>{published}</pubDate></item>".format(
                    title=i.get("title", ""), url=i.get("url", ""),
                    snippet=i.get("snippet", ""), published=i.get("published", ""))
                for i in self.rss_items)
            return _Resp(f"<?xml version='1.0'?><rss><channel>{items}</channel></rss>".encode("utf-8"),
                         "application/rss+xml")
        page = self.pages.get(url)
        if page is None:
            raise urllib.error.URLError("connection refused (not stubbed)")
        if isinstance(page, tuple):
            return _Resp(page[0].encode("utf-8"), page[1])
        return _Resp(page.encode("utf-8"), "text/html; charset=utf-8")


_REAL_URLOPEN = urllib.request.urlopen
_REAL_SETTINGS = tools.settings
_NET = _Net()
tools.settings = _Settings()
urllib.request.urlopen = _NET


def source(i, content=None, host=None):
    hosts = [HOST_A, HOST_B, HOST_C]
    return {
        "title": f"来源{i}",
        "url": f"{host or hosts[i % 3]}/f1-{i}",
        "snippet": "赛历摘要",
        "summary": "2026 F1 赛历摘要：" + "、".join(["巴林", "阿塞拜疆", "日本"]),
        "date": TODAY,
        "content": F1_BODY if content is None else content,
        "rerank_score": 2.4,
        "authority": 3,
        "site_name": f"site{i}",
    }


def reset(net=None, **kwargs):
    _NET.deepseek_results = kwargs.get("deepseek_results", [])
    _NET.deepseek_fetch = kwargs.get("deepseek_fetch", "")
    _NET.raise_deepseek = kwargs.get("raise_deepseek", False)
    _NET.max_deepseek = kwargs.get("max_deepseek", 99)
    _NET.rss_items = kwargs.get("rss_items", [])
    _NET.pages = kwargs.get("pages", {})
    _NET.seen = []
    _NET.search_bodies = []
    _NET.fetch_bodies = []
    tools.settings = kwargs.get("settings", _Settings())


def verify(**args):
    args.setdefault("force_refresh", True)
    return json.loads(tools.tool_verify_current_fact(**args))


try:
    section("一、配置与端点推导")
    check("默认 provider 已切到 deepseek",
          (__import__("backend.config", fromlist=["settings"]).settings.web_search_provider == "deepseek"))
    check("默认 base 推导出 /v1/tools/search",
          tools._deepseek_tools_url(tools._DEEPSEEK_SEARCH_URL)
          == "https://api.deepseek.com/v1/tools/search")
    _saved_base = tools.settings.deepseek_base_url
    tools.settings.deepseek_base_url = "https://api.deepseek.com"
    check("只写到域名时自动补 /v1",
          tools._deepseek_tools_url(tools._DEEPSEEK_SEARCH_URL)
          == "https://api.deepseek.com/v1/tools/search")
    tools.settings.deepseek_base_url = _saved_base
    check("Markdown 转义被还原",
          tools._clean_markdown(r"\# 标题 \- 列表\.\:结束") == "# 标题 - 列表.:结束")

    section("二、请求体：鉴权 / need_content / 操作符 / 长度")
    reset(deepseek_results=[source(0), source(1), source(2)])
    rows = tools._fact_search("2026 F1 赛历", 4, "current", domains=None, original="原问题")
    check("provider=deepseek 返回 3 条", len(rows) == 3, str(len(rows)))
    check("后端标记 search_provider", all(r.get("search_provider") == "deepseek" for r in rows))
    check("请求打到 DeepSeek Search", _NET.seen and "tools/search" in _NET.seen[0], str(_NET.seen[:2]))
    body = _NET.search_bodies[0]
    check("need_content=true", body.get("need_content") is True)
    check("count 至少 10", int(body.get("count", 0)) >= 10, str(body.get("count")))
    check("带 ori_question", body.get("ori_question") == "原问题")
    check("正文原样映射", rows[0].get("content") == F1_BODY)
    check("日期映射到 published", rows[0].get("published") == TODAY)
    check("摘要优先用 summary", "2026 F1 赛历摘要" in rows[0].get("snippet", ""))
    check("rerank_score 保留", rows[0].get("rerank_score") == 2.4)
    check("authority 保留", rows[0].get("authority") == 3)

    reset(deepseek_results=[source(0)])
    tools._fact_search_deepseek("F1 赛历", 4, "week", ["formula1.com", "example.org"], "原问题")
    q = _NET.search_bodies[0]["query"]
    check("week 追加 after:", "after:" in q, q)
    check("域名转 site: OR", "site:formula1.com OR site:example.org" in q, q)
    check("查询不超过 128 字符", len(q) <= 128, str(len(q)))

    reset(deepseek_results=[source(0)])
    tools._fact_search_deepseek("赛" * 300, 4, "current")
    check("超长查询被截到 128 字符", len(_NET.search_bodies[0]["query"]) <= 128)

    section("三、降级 Bing RSS")
    reset(deepseek_results=[], rss_items=[{
        "title": "Bing 结果", "url": "https://93.184.216.34/bing", "snippet": "摘要", "published": TODAY}])
    rows = tools._fact_search("2026 F1 赛历", 4)
    check("空结果降级 Bing", rows and rows[0].get("search_provider") == "bing", str(rows[:1]))
    check("确实请求了 RSS", any("format=rss" in u for u in _NET.seen))

    reset(raise_deepseek=True, rss_items=[{
        "title": "Bing 结果", "url": "https://93.184.216.34/bing", "snippet": "摘要", "published": TODAY}])
    rows = tools._fact_search("2026 F1 赛历", 4)
    check("DeepSeek 异常降级 Bing", rows and rows[0].get("search_provider") == "bing")

    section("四、need_content 正文直接判级（不再逐页抓取）")
    reset(deepseek_results=[source(0), source(1), source(2)])
    report = verify(query="2026 F1 赛历", max_sources=3)
    check("状态 verified", report.get("status") == "verified", report.get("status"))
    check("置信度 high", report.get("confidence") == "high", report.get("confidence"))
    check("report.search_provider=deepseek", report.get("search_provider") == "deepseek")
    check("market 仍按查询语言标注", report.get("market") == "zh-CN", str(report.get("market")))
    check("3 条正文都来自 search",
          report["evidence"].get("content_from_search") == 3, str(report["evidence"]))
    check("全程没有 Fetch 请求", not any("tools/fetch" in u for u in _NET.seen))
    check("全程没有逐页抓取", not any(u in _NET.seen for u in (f"{HOST_A}/f1-0",)))
    check("来源带 extract_via", all(s.get("extract_via") == "search" for s in report["sources"]))
    brief = tools.fact_brief("2026 F1 赛历", report=report)
    check("fact_brief 含正文", "24站" in brief and "[full]" in brief)

    section("五、Search 没给正文 → DeepSeek Fetch → 自建抓取")
    url_nocontent = f"{HOST_A}/no-content"
    reset(deepseek_results=[dict(source(0), url=url_nocontent, content="")],
          deepseek_fetch=F1_BODY)
    report = verify(query="2026 F1 赛历", max_sources=2, domains=["93.184.216.34"])
    src = (report.get("sources") or [{}])[0]
    check("走 DeepSeek Fetch", src.get("extract_via") == "deepseek_fetch", str(src.get("extract_via")))
    check("Fetch 请求带 url", _NET.fetch_bodies and _NET.fetch_bodies[0].get("url") == url_nocontent)
    check("Fetch 正文参与判级", src.get("grade") == "full", str(src.get("grade")))

    reset(deepseek_results=[dict(source(0), url=url_nocontent, content="")],
          deepseek_fetch="", pages={url_nocontent: "<html><body><p>" + F1_BODY + "</p></body></html>"})
    report = verify(query="2026 F1 赛历", max_sources=2, domains=["93.184.216.34"])
    src = (report.get("sources") or [{}])[0]
    check("Fetch 也失败才走自建抓取", src.get("extract_via") == "fetch", str(src.get("extract_via")))
    check("自建抓取正文参与判级", src.get("grade") == "full", str(src.get("grade")))

    section("六、缓存键与 Key 状态")
    tools.settings = _Settings(deepseek_key="sk-a")
    key_a = tools._fact_cache_key("测试", "current", None, 3)
    tools.settings = _Settings(deepseek_key="sk-b")
    key_b = tools._fact_cache_key("测试", "current", None, 3)
    # 缓存键只记"有没有 Key"，不落 Key 明文；换 Key 复用检索结果无害。
    check("缓存键不落 Key 明文", key_a == key_b)
    tools.settings = _Settings(deepseek_key="")
    key_c = tools._fact_cache_key("测试", "current", None, 3)
    check("有无 DeepSeek Key 改变缓存键", key_a != key_c)
    key_obj = json.loads(key_a)
    check("缓存键含 has_deepseek_key", key_obj.get("has_deepseek_key") is True)
    check("缓存键含 provider", key_obj.get("provider") == "deepseek")
finally:
    urllib.request.urlopen = _REAL_URLOPEN
    tools.settings = _REAL_SETTINGS

print(f"\nDeepSeek Search/Fetch 测试：通过 {TOTAL[0]} / 失败 {TOTAL[1]}")
raise SystemExit(1 if TOTAL[1] else 0)

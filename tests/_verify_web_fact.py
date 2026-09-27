# -*- coding: utf-8 -*-
"""联网事实核验工具定向测试：不访问真实网络、不需要搜索 API Key。

**打桩原则（上一版踩过的坑）**：只打桩 I/O 边界（`urllib.request.urlopen`），
不打桩 `_fact_search` / `_fact_fetch` 本体——上一版把这两个函数整个替换掉，
真实代码路径一行没跑到，所以它 6/6 全绿却漏掉了 `tools.py` 缺
`from .config import settings` 导致工具真实运行 100% 抛 NameError 的问题。
真联网的端到端验证见 `tests/_e2e_web_fact.py`。
"""
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp(prefix="catgirl_web_fact_")
os.environ["APPDATA"] = _TMP
os.environ["CATGIRL_SKIP_PET"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend import tools  # noqa: E402
from backend.llm import SYSTEM_PROMPT_CHAT  # noqa: E402

# 公网 IP 字面量：_public_web_url 要做 DNS 解析，用 IP 就完全不碰网络
HOST_A, HOST_B, HOST_C = "93.184.216.34", "1.1.1.1", "8.8.8.8"
TOTAL = [0, 0]     # [通过数, 失败数]


def check(label, condition):
    TOTAL[0] += 1
    if condition:
        print(f"[PASS] {label}")
    else:
        TOTAL[1] += 1
        print(f"[FAIL] {label}")


def section(title):
    print(f"\n=== {title} ===")


class _Resp:
    """假响应：只需要 _fact_fetch/_fact_search 用到的那几个接口。"""

    def __init__(self, body: bytes, ctype: str):
        self._body = body
        self.headers = {"Content-Type": ctype}

    def read(self, n=None):
        return self._body if n is None else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Settings:
    def __init__(self, key="", provider="brave", timeout=5):
        self.web_search_api_key = key
        self.web_search_provider = provider
        self.web_fact_timeout = timeout


class _Net:
    """假 I/O 层：按 URL 分发页面 / Bing RSS / Brave JSON，并记录请求过的 URL。"""

    def __init__(self):
        self.pages = {}      # {url: html} 或 {url: (html, ctype)}
        self.rss = None      # [{title,url,snippet,published}]
        self.brave = None    # 同上
        self.seen: list[str] = []

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", str(req))
        self.seen.append(url)
        if "api.search.brave.com" in url:
            return _Resp(json.dumps({"web": {"results": self.brave or []}}).encode(), "application/json")
        if "format=rss" in url:
            items = "".join(
                "<item><title>{title}</title><link>{url}</link>"
                "<description>{snippet}</description><pubDate>{published}</pubDate></item>".format(
                    title=i.get("title", ""), url=i.get("url", ""),
                    snippet=i.get("snippet", ""), published=i.get("published", ""))
                for i in (self.rss or []))
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


def call(pages=None, rss=None, brave=None, key="", _args=None, **kwargs):
    """装上假网络 + 假 settings 跑一次真实工具函数。"""
    _NET.pages = pages or {}
    _NET.rss = rss
    _NET.brave = brave
    _NET.seen = []
    tools.settings = _Settings(key=key, **kwargs)
    return json.loads(tools.tool_verify_current_fact(**_args or {}))


def fetch(url, pages):
    _NET.pages = pages
    _NET.seen = []
    return tools._fact_fetch(url)


GOOD_BODY = ("2026赛季F1赛历已经公布：巴林大奖赛排位赛将于10月3日进行，正赛在10月4日；"
             "阿塞拜疆大奖赛安排在9月20日，日本站则在9月27日。各站具体时间以官方公告为准。")
GOOD_HTML = ("<html><body><nav>首页 车队 赛程 商店 登录</nav>"
             "<article><p>{}</p></article><footer>© 2026 版权所有</footer></body></html>")
JUNK_BODY = "今天阳光很好，公园里的花开得漂亮，适合出门散步。"


def page(host, path="/article/2026", body=None):
    """返回 (url, html)，正文默认够长且与问题相关。"""
    return f"https://{host}{path}", GOOD_HTML.format(body or GOOD_BODY * 6)


def rss_item(url, **kw):
    return {"title": kw.get("title", "F1 赛历"), "url": url,
            "snippet": kw.get("snippet", "2026 赛季赛历与日程安排"),
            "published": kw.get("published", "")}


try:
    section("一、URL 安全（SSRF 基础拦截）")
    check("拒绝 HTTP", tools._public_web_url("http://example.com") is None)
    check("拒绝 file 协议", tools._public_web_url("file:///C:/windows/win.ini") is None)
    check("拒绝 localhost", tools._public_web_url("https://localhost/x") is None)
    check("拒绝回环 IP", tools._public_web_url("https://127.0.0.1/x") is None)
    check("拒绝私网 IP", tools._public_web_url("https://192.168.1.1/x") is None)
    check("拒绝内网域名后缀", tools._public_web_url("https://nas.local/x") is None)
    check("放行公网 HTTPS", tools._public_web_url(f"https://{HOST_A}/a") is not None)

    section("二、正文提取与证据分级（真实走 _fact_fetch / _grade_source）")
    url_a, html_a = page(HOST_A)
    extract, err = fetch(url_a, {url_a: html_a})
    check("抓取成功无错误", err is None)
    check("正文够长", len(extract) >= tools._FACT_MIN_EXTRACT)
    check("样板文字（nav/footer）不进正文", "商店" not in extract and "版权所有" not in extract)
    check("正文本身保留", "巴林大奖赛" in extract)
    check("正文长度达标 + 相关 → full",
          tools._grade_source({"url": url_a}, extract, None, "2026 F1 赛历")[0] == "full")
    check("抓到站点首页 → 只算 snippet",
          tools._grade_source({"url": f"https://{HOST_A}/"}, extract, None, "2026 F1 赛历")[0] == "snippet")
    check("首页降级带原因", "首页" in " ".join(
        tools._grade_source({"url": f"https://{HOST_A}/"}, extract, None, "2026 F1 赛历")[1]))
    check("正文太短（JS 墙）→ snippet",
          tools._grade_source({"url": url_a}, "短", None, "q")[0] == "snippet")
    check("正文长但与问题无关 → snippet",
          tools._grade_source({"url": url_a}, JUNK_BODY * 30, None, "2026 F1 赛历")[0] == "snippet")
    check("无关正文的原因含'相关性'", "相关性" in " ".join(
        tools._grade_source({"url": url_a}, JUNK_BODY * 30, None, "2026 F1 赛历")[1]))
    check("读失败但有摘要 → snippet",
          tools._grade_source({"url": url_a, "snippet": "2026 赛季赛历与完整日程安排，含各站排位赛与正赛时间"},
                              "", "读取失败：timeout", "q")[0] == "snippet")
    check("摘要过短且无正文 → none",
          tools._grade_source({"url": url_a, "snippet": "短"}, "", "读取失败：timeout", "q")[0] == "none")
    check("读失败且无摘要 → none",
          tools._grade_source({"url": url_a, "snippet": ""}, "", "读取失败", "q")[0] == "none")
    check("页内脚本/样式不进正文", "function" not in fetch(url_a, {
        url_a: "<html><body><script>var function_x=1</script><p>{}</p></body></html>".format(GOOD_BODY * 6)})[0])

    section("三、状态判定（含原 verified 误报复现）")
    # ★原 bug 复现：来源全是站点首页导航（有 extract、有摘要、域名也够多）→ 旧码报 verified
    nav_rss = [rss_item(f"https://{HOST_A}/", snippet="首页导航文字"),
               rss_item(f"https://{HOST_B}/", snippet="首页导航文字"),
               rss_item(f"https://{HOST_C}/", snippet="首页导航文字")]
    nav_pages = {"https://" + h + "/": GOOD_HTML.format("导航文字而已") for h in (HOST_A, HOST_B, HOST_C)}
    report = call(pages=nav_pages, rss=nav_rss, _args={"query": "2026 F1 赛历"})
    check("★首页导航不再报 verified", report["status"] != "verified")
    check("★首页导航判为 insufficient", report["status"] == "insufficient")
    check("★full 证据数为 0", report["evidence"]["full"] == 0)
    check("降级来源进了 caveats", any("未计入正文证据" in c for c in report["caveats"]))

    url_b, html_b = page(HOST_B, "/news/f1-calendar")
    report = call(pages={url_a: html_a, url_b: html_b},
                  rss=[rss_item(url_a), rss_item(url_b)], _args={"query": "2026 F1 赛历"})
    check("2 个独立域名 full → verified", report["status"] == "verified")
    check("verified 报 2 个独立域名", report["evidence"]["independent_domains"] == 2)
    check("每条来源带 grade", all(s["grade"] for s in report["sources"]))
    check("verified 不承诺绝对结论", "不是绝对结论" in report["answer"])
    check("带 elapsed_s", isinstance(report["elapsed_s"], (int, float)))

    url_c, html_c = page(HOST_C, "/racing/calendar")
    report = call(pages={url_a: html_a, url_b: html_b, url_c: html_c},
                  rss=[rss_item(url_a), rss_item(url_b), rss_item(url_c)],
                  _args={"query": "2026 F1 赛历"})
    check("3 个独立域名且不过期 → confidence=high", report["confidence"] == "high")

    report = call(pages={url_a: html_a}, rss=[rss_item(url_a)], _args={"query": "2026 F1 赛历"})
    check("只有 1 个域名 → insufficient", report["status"] == "insufficient")
    check("单域名 confidence=medium", report["confidence"] == "medium")

    report = call(pages={}, rss=[], _args={"query": "2026 F1 赛历"})
    check("搜不到结果 → failed", report["status"] == "failed")

    report = call(pages={}, rss=[], _args={"query": "   "})
    check("空问题 → failed", report["status"] == "failed")

    # ★前几条是垃圾也不提前收手：max_sources=2 → 旧码扫够 2 条就 break，永远碰不到后面两个好来源
    junk_pages = {"https://" + HOST_A + "/": GOOD_HTML.format("导航")}
    report = call(pages={**junk_pages, url_b: html_b, url_c: html_c},
                  rss=[rss_item(f"https://{HOST_A}/"), rss_item(f"https://{HOST_A}/x"),
                       rss_item(url_b), rss_item(url_c)],
                  _args={"query": "2026 F1 赛历", "max_sources": 2})
    check("★前几条是垃圾也不会提前收手", report["status"] == "verified")

    # ★同域名不许多占名额：实测踩过——一次查询 10 个名额被知乎占了 6 个（全 403），
    #   别的独立域名根本没机会上场。这里 A 站占满整个搜索宽度（max_sources=3 → 宽度 6）、
    #   B 站是最后一条：没 cap 时读完 3 篇 A 就满足收手条件 → 只有 1 个独立域名 → insufficient。
    lots_a = {f"https://{HOST_A}/p{i}": GOOD_HTML.format(GOOD_BODY * 6) for i in range(5)}
    url_b2, html_b2 = page(HOST_B, "/news/cap-test")
    report = call(pages={**lots_a, url_b2: html_b2},
                  rss=[rss_item(f"https://{HOST_A}/p{i}") for i in range(5)] + [rss_item(url_b2)],
                  _args={"query": "2026 F1 赛历", "max_sources": 3})
    same_domain = [s for s in report["sources"] if s["domain"] == HOST_A]
    check("★同域名最多读 2 篇", len(same_domain) <= tools._FACT_MAX_PER_DOMAIN)
    check("★同域名不挤掉独立来源", report["status"] == "verified")
    check("★独立域名真的换来了", report["evidence"]["independent_domains"] == 2)
    check("★同域名少抓了（省下的预算给了别人）", len(report["sources"]) < 6)

    section("四、时效 / 市场 / 来源年龄 / 可观测")
    report = call(pages={url_a: html_a}, rss=[rss_item(url_a)], _args={"query": "2026 F1 赛历"})
    rss_url = [u for u in _NET.seen if "format=rss" in u][0]
    # ★市场靠主机区分，不靠 mkt/cc 参数——实测那组参数会让 www 返回完全不相关的结果
    check("★中文查询走 cn.bing.com 主机", rss_url.startswith("https://cn.bing.com/"))
    check("中文查询不再堆 mkt/cc 参数", "mkt=" not in rss_url and "cc=" not in rss_url)
    check("brave 返回市场字段", report["market"] == "zh-CN")

    call(pages={url_a: html_a}, rss=[rss_item(url_a)], _args={"query": "F1 2026 calendar"})
    rss_url = [u for u in _NET.seen if "format=rss" in u][0]
    check("英文查询走 www.bing.com 主机", rss_url.startswith("https://www.bing.com/"))

    report = call(pages={url_a: html_a}, rss=[rss_item(url_a)],
                  _args={"query": "2026 F1 赛历", "freshness": "week"})
    rss_url = [u for u in _NET.seen if "format=rss" in u][0]
    check("★freshness 真的进了 RSS 请求", "freshness=Week" in rss_url)
    check("freshness 语义回传", report["freshness"] == "week")
    report = call(pages={url_a: html_a}, rss=[rss_item(url_a)],
                  _args={"query": "赛历", "freshness": "今天"})
    check("认不出的 freshness 回落 current", report["freshness"] == "current")

    report = call(pages={url_a: html_a}, rss=[rss_item(url_a)], key="brave-key",
                  _args={"query": "2026 F1 赛历", "freshness": "day"})
    brave_url = [u for u in _NET.seen if "api.search.brave.com" in u]
    check("有 Key 走 Brave", bool(brave_url))
    check("Brave 带时效参数", bool(brave_url) and "freshness=pd" in brave_url[0])
    check("Brave 带市场参数", bool(brave_url) and "country=cn" in brave_url[0]
          and "search_lang=zh-hans" in brave_url[0])
    check("brave 返回市场字段", report["market"] == "zh-CN")

    old = "Sat, 26 Sep 2020 00:00:00 GMT"
    report = call(pages={url_a: html_a, url_b: html_b},
                  rss=[rss_item(url_a, published=old), rss_item(url_b, published=old)],
                  _args={"query": "2026 F1 赛历"})
    check("过期来源带 age_days", report["sources"][0]["age_days"] > 365)
    check("★过期来源进 caveats", any("可能已过时" in c for c in report["caveats"]))
    check("过期拉低 confidence", report["confidence"] == "medium")
    check("caveats 明说不做跨来源对账", any("对账" in c for c in report["caveats"]))
    check("caveats 保留不可信资料声明", any("不可信资料" in c for c in report["caveats"]))

    report = call(pages={url_a: html_a, url_b: html_b},
                  rss=[rss_item(url_a), rss_item(url_b)], _args={"query": "2026 F1 赛历"})
    check("无发布时间时提示时效无法判断", any("时效性无法判断" in c for c in report["caveats"]))

    check("RFC822 时间解析", tools._fact_age_days("Sat, 26 Sep 2026 00:00:00 GMT") is not None)
    check("ISO 时间解析", tools._fact_age_days("2026-09-20") is not None)
    check("相对时间解析", tools._fact_age_days("3 days ago") == 3)
    check("垃圾时间不瞎猜", tools._fact_age_days("上周三") is None)
    check("空时间返回 None", tools._fact_age_days("") is None)
    # ★Bing RSS 带 mkt=zh-CN 时 pubDate 是中文格式——认不出就会静默漏掉过期来源（实测踩过）
    check("★中文市场 pubDate 能解析（日 月 年）",
          tools._fact_age_days("周二, 10 6月 2025 13:46:00 GMT") > 365)
    check("★中文市场 pubDate 当天不误判过期",
          (tools._fact_age_days("周六, 26 9月 2026 12:53:00 GMT") or 999) <= 2)
    check("中文年月日能解析", tools._fact_age_days("2020年9月26日") > 365)
    check("非法日期不崩", tools._fact_age_days("99 13月 2026") is None)

    log_path = os.path.join(_TMP, "catgirl", "actions.log")
    log = open(log_path, encoding="utf-8").read() if os.path.exists(log_path) else ""
    check("★actions.log 记了耗时", "elapsed=" in log)
    check("actions.log 记了 full 计数", "full=" in log)

    section("五、契约一致性（conflicting 已移除）")
    schema = [t for t in tools.TOOL_SCHEMAS if t["function"]["name"] == "verify_current_fact"][0]
    desc = schema["function"]["description"]
    check("★schema 不再声明 conflicting", "conflicting" not in desc)
    check("★提示词不再声明 conflicting", "conflicting" not in SYSTEM_PROMPT_CHAT)
    check("schema 说明 grade 分级", "grade" in desc and "full" in desc)
    check("schema 说明 freshness 真过滤",
          "真正传给搜索接口" in schema["function"]["parameters"]["properties"]["freshness"]["description"])
    check("提示词要求先看 grade", "grade" in SYSTEM_PROMPT_CHAT)
finally:
    urllib.request.urlopen = _REAL_URLOPEN
    tools.settings = _REAL_SETTINGS
    shutil.rmtree(_TMP, ignore_errors=True)     # 只删自己建的临时目录

print(f"\n联网事实核验定向测试：通过 {TOTAL[0]} / 失败 {TOTAL[1]}")
sys.exit(1 if TOTAL[1] else 0)

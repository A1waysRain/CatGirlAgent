# -*- coding: utf-8 -*-
"""网页提炼子 agent 的契约测试：不访问真实模型。"""
import asyncio
import json
import os
import sys
import tempfile

# 必须**硬赋值**：APPDATA 在 Windows 上本来就有值，用 setdefault 等于没隔离——
# 测试里的 `_log` 会写进用户真实的 %APPDATA%\catgirl\actions.log，把审计日志污染成
# "distill_hook in=26字 elapsed=0.0s" 这种桩数据（2026-09-27 实际发生过）。
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="catgirl_agents_")
os.environ["CATGIRL_SKIP_PET"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend import agents
from backend import tools


def check(label, value):
    print(f"[{'PASS' if value else 'FAIL'}] {label}")
    if not value:
        raise AssertionError(label)


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


async def main():
    original = agents.httpx.AsyncClient

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "points": ["材料明确写了 24 站"],
                "sources": [{"domain": "formula1.com", "published": "今天", "grade": "full"}],
                "gaps": ["材料没有覆盖完整时间"], "conflicts": [], "confidence": "medium"
            }, ensure_ascii=False)}}]}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            messages = kwargs["json"]["messages"]
            check("工人只收到 system + user 两条消息", len(messages) == 2)
            check("工人提示词不含主对话历史", "messages" not in messages[1]["content"])
            check("工人收到问题和材料", "F1赛历" in messages[1]["content"] and "24站" in messages[1]["content"])
            return Response()

    agents.httpx.AsyncClient = Client
    try:
        result = await agents.distill_web("F1赛历", "来源 formula1.com：2026赛季共24站")
        check("返回结构化要点", result["points"] == ["材料明确写了 24 站"])
        check("缺口字段保留", result["gaps"] == ["材料没有覆盖完整时间"])
        rendered = agents.render_distill(result)
        check("渲染包含来源和缺口", "formula1.com" in rendered and "完整时间" in rendered)
    finally:
        agents.httpx.AsyncClient = original

    check("M2 已注册实现", "distill_web" in tools.TOOL_IMPL)
    check("M2 已注册 schema", any(x["function"]["name"] == "distill_web" for x in tools.TOOL_SCHEMAS))
    original_distill = agents.distill_web
    async def fake_tool_distill(question, materials):
        return {"points": ["工具桥通过"], "sources": [], "gaps": [], "conflicts": [], "confidence": "high"}
    agents.distill_web = fake_tool_distill
    try:
        raw = tools.run_tool("distill_web", {"question": "测试", "materials": "材料"})
        check("M2 同步桥返回 JSON", json.loads(raw)["points"] == ["工具桥通过"])
    finally:
        agents.distill_web = original_distill

    # ---- JSON 容错：模型把 JSON 包在解释文字里（围栏剥完还剩前言）也要能解出来 ----
    wrapped = '好的，结果如下：\n```json\n' + json.dumps({
        "points": ["材料明确写了 24 站"], "sources": [], "gaps": [], "conflicts": [], "confidence": "high",
    }, ensure_ascii=False) + "\n```\n希望有帮助。"
    parsed = agents._parse_json(wrapped)
    check("带前言/后缀的 JSON 也能解析", parsed["points"] == ["材料明确写了 24 站"])
    check("非法 JSON 仍抛异常（走降级）", _raises(lambda: agents._parse_json("完全不是 JSON")))

    # ---- 渲染预算：要点写满时，来源/缺口/矛盾必须保住（旧写法 [:limit] 硬截会把它们砍掉） ----
    big = {
        "confidence": "medium",
        "points": [f"要点{i}：" + "这一站的具体时间安排很长很长很长很长很长很长很长很长" * 6 for i in range(1, 41)],
        "sources": [{"domain": "f1-boxbox.com", "published": "1天前", "grade": "full"}],
        "gaps": ["后半赛季具体开赛时间材料未覆盖"],
        "conflicts": [{"about": "第15站", "a": "官方页：阿塞拜疆", "b": "旧赛历：意大利"}],
    }
    long_out = agents.render_distill(big)
    check("★要点超长时缺口仍在", "缺口：后半赛季具体开赛时间材料未覆盖" in long_out)
    check("★要点超长时来源矛盾仍在", "来源矛盾：第15站" in long_out)
    check("★要点超长时来源仍在", "f1-boxbox.com" in long_out)
    check("超长要点被标注截断", "要点过长已截断" in long_out)
    check("渲染受 limit 约束", len(agents.render_distill(big, limit=2000)) <= 2100)

    # ---- 时效性：材料按新鲜度分层（近期 / 旧稿），且提示词锁死"旧稿不得当现状" ----
    rep = {
        "status": "verified", "confidence": "medium",
        "evidence": {"full": 2, "independent_domains": 2},
        "sources": [
            {"grade": "full", "domain": "fresh.example", "published": "今天", "age_days": 1,
             "extract": "近期正文内容"},
            {"grade": "full", "domain": "old.example", "published": "2025-06-10", "age_days": 474,
             "extract": "旧稿正文内容"},
        ],
    }
    mats = agents.build_materials(rep)
    check("★材料分出【近期来源】段", "【近期来源】" in mats and "fresh.example" in mats)
    check("★材料分出【旧稿】段并声明不得当现状",
          "【旧稿】" in mats and "绝不能当当前的安排" in mats and "old.example" in mats)
    check("★旧稿段排在近期段之后", mats.index("【近期来源】") < mats.index("【旧稿】"))
    check("提示词锁死旧稿规则",
          "旧稿" in agents.DISTILL_PROMPT and "绝不能当成" in agents.DISTILL_PROMPT
          and "不许自行合并" in agents.DISTILL_PROMPT)

    # 全部为旧稿 → stale_notice 必须警示；有近期来源 → 不警示
    stale_only = {"sources": [{"grade": "full", "age_days": 474}, {"grade": "snippet", "age_days": 300}]}
    check("★全是旧稿时给出'未找到近期来源'警示", "没找到近期来源" in agents.stale_notice(stale_only))
    check("有近期来源时不警示", agents.stale_notice(rep) == "")
    check("都没有发布时间时不误报", agents.stale_notice({"sources": [{"grade": "full", "age_days": None}]}) == "")
    notice_out = agents.render_distill(
        {"confidence": "medium", "points": ["旧说法"], "sources": [], "gaps": [], "conflicts": []},
        notice=agents.stale_notice(stale_only))
    check("★警示会出现在注入段顶部", notice_out.startswith("⚠️"))

    # ---- 完整列表（schedule）：主猫娘要能一次念完，且不能挤掉缺口/矛盾 ----
    rep_sched = {
        "confidence": "medium",
        "points": ["要点一"],
        "schedule": ["R1 澳大利亚 3月6-8日", "R2 中国 3月13-15日",
                     "R3 日本 3月27-29日", "R19-R24 （材料未覆盖）"],
        "sources": [{"domain": "formula1.com", "published": "2026-09-26", "grade": "full"}],
        "gaps": ["官网只读到 R1-R8 与 R15-R18"],
        "conflicts": [{"about": "巴林站日期", "a": "官网 10月2-4日", "b": "旧稿 4月10-12日"}],
    }
    sched_out = agents.render_distill(rep_sched)
    check("★渲染出「完整列表」块", "完整列表：" in sched_out)
    check("★列表条目按顺序保留", sched_out.index("R1 澳大利亚") < sched_out.index("R3 日本"))
    check("★未覆盖条目照实保留占位", "（材料未覆盖）" in sched_out)
    check("有列表时缺口/矛盾仍在",
          "缺口：官网只读到" in sched_out and "来源矛盾：巴林站日期" in sched_out)
    check("提示词要求拼连贯列表", "schedule" in agents.DISTILL_PROMPT
          and "不要按来源分块复述" in agents.DISTILL_PROMPT)
    # 超长列表：逐条截断并标注，仍受 limit 约束
    huge = dict(rep_sched, schedule=[f"R{i} " + "很长很长的条目" * 20 for i in range(1, 60)])
    huge_out = agents.render_distill(huge, limit=3000)
    check("超长列表被截断并标注", "列表过长已截断" in huge_out or "…（截断）" in huge_out)
    check("超长列表仍守 limit", len(huge_out) <= 3200)
    check("超长列表下矛盾仍保住", "来源矛盾：" in huge_out)
    # 模型把 schedule 写成字符串时不崩、不逐字符渲染
    weird = agents.render_distill(dict(rep_sched, schedule="R1 澳大利亚"))
    check("schedule 非列表时安全忽略", "R1 澳大利亚" not in weird)


asyncio.run(main())
print("子 agent 契约测试通过")

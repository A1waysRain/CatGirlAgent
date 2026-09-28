# -*- coding: utf-8 -*-
"""端到端：M2 的 `distill_web` **工具桥**（真联网取材料 + 真模型跑工人）。消耗真实 token。

为什么要单独有这个脚本：
`_verify_agents.py` 用**打桩工人**验了 `run_tool("distill_web", …)` 的接线，
`_e2e_search_brief.py` 验的是**后端确定性那条路**（`chat.py` 直接 `await distill_web(...)`），
两条都没碰过"经 `run_tool` → `tool_distill_web` → 同步/异步桥 → 真工人"这条路。
而 `tool_distill_web` 里有一段**只在真实调用上下文里才会暴露**的桥：

    try: asyncio.get_running_loop()   # 有循环 → 起线程承载 asyncio.run 再 join
    except RuntimeError: asyncio.run(...)  # 没循环 → 直接跑（生产路径）

生产里 `run_tool` 是 `await asyncio.to_thread(run_tool, …)` 调的（llm.py:174/290），
即**工作线程里没有运行中的循环** → 走 `asyncio.run` 分支。
本脚本两条分支都真跑一遍。

打桩原则：**只打桩"报错分支"**（那一条不耗 token）；取材料（真联网）与工人（真模型）都是真的。

跑法：
  cd Cat_Girl && PYTHONIOENCODING=utf-8 PYTHONPATH="$(pwd)" .venv/Scripts/python.exe tests/_e2e_distill_tool.py
  可带查询词：… tests/_e2e_distill_tool.py "2026 F1 赛历"
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
_TMP = tempfile.mkdtemp(prefix="catgirl_e2e_distill_tool_")
os.environ["APPDATA"] = _TMP
os.environ["CATGIRL_SKIP_PET"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)          # 让 pydantic 读到 dev .env 的 Key

from backend import agents, tools  # noqa: E402

# 默认查询词选「年份不在开头」的写法：实测 cn.bing.com 对**年份打头**的查询会返回
# "2026 年度事件/日历"类无关结果（`2026 F1 赛历` → 节假日网站、0~1 条正文），
# 而 `F1 2026 赛程` → verified / 3 条正文 / 3 独立域名。默认词要能真抓到材料，
# 否则本脚本会退化成"只验桥、材料是垃圾"。
DEFAULT_QUERY = "F1 2026 赛程"
QUERY = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY
FAIL = 0


def check(label, cond, extra=""):
    global FAIL
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL += 1


def _is_failure_payload(raw: str) -> bool:
    """`tool_distill_web` 的失败话术（而不是抛异常）。"""
    return raw.startswith("网页提炼失败喵")


async def main() -> int:
    # ---------- 1. 真联网取材料：走生产同款链路 ----------
    print(f"查询词：{QUERY}")
    print("\n===== 1. 真联网取材料（tool_verify_current_fact → build_materials）=====")
    t0 = time.monotonic()
    report = json.loads(tools.tool_verify_current_fact(QUERY, max_sources=4))
    materials = agents.build_materials(report)
    fetch_s = time.monotonic() - t0
    evidence = report.get("evidence") or {}
    print(f"核验状态：{report.get('status')}｜正文证据 {evidence.get('full')} 条 / "
          f"独立来源 {evidence.get('independent_domains')} 个｜耗时 {fetch_s:.1f}s")
    print(f"材料 {len(materials)} 字")
    check("取材料成功（材料里有来源块）", "【来源" in materials, f"材料长度 {len(materials)}")

    if "【来源" not in materials:
        print("\n★材料为空（本次没抓到正文，Bing RSS 通道时好时坏）——"
              "工具桥没法用真材料验证，本次不计失败，请换个词或稍后重跑。")
        print(f"\n结果：网络原因跳过（{FAIL} 项失败）")
        return 1 if FAIL else 0

    # ---------- 2. 生产路径：asyncio.to_thread(run_tool, …) ----------
    # 工作线程里没有运行中的事件循环 → tool_distill_web 走 asyncio.run 分支
    print("\n===== 2. 生产路径 asyncio.to_thread(run_tool) —— 真模型 =====")
    t0 = time.monotonic()
    raw_a = await asyncio.to_thread(
        tools.run_tool, "distill_web", {"question": QUERY, "materials": materials}
    )
    a_s = time.monotonic() - t0
    print(f"耗时 {a_s:.1f}s，返回 {len(raw_a)} 字")
    ok_a = not _is_failure_payload(raw_a)
    check("生产路径：工具没走失败话术", ok_a, raw_a[:80] if not ok_a else "")
    data_a = None
    if ok_a:
        try:
            data_a = json.loads(raw_a)
        except json.JSONDecodeError as exc:
            check("生产路径：返回可解析的 JSON", False, str(exc))
        else:
            check("生产路径：返回可解析的 JSON", True)
            check("生产路径：五个字段齐全",
                  all(k in data_a for k in ("points", "sources", "gaps", "conflicts", "confidence")))
            check("★生产路径：真模型确实产出了要点", bool(data_a.get("points")),
                  f"要点 {len(data_a.get('points') or [])} 条")
            print(f"      要点 {len(data_a.get('points') or [])} 条 / "
                  f"列表 {len(data_a.get('schedule') or [])} 项 / "
                  f"缺口 {len(data_a.get('gaps') or [])} / "
                  f"矛盾 {len(data_a.get('conflicts') or [])} / "
                  f"confidence={data_a.get('confidence')}")
            for p in (data_a.get("points") or [])[:5]:
                print("      •", str(p)[:100])

    # ---------- 3. 另一条分支：在事件循环线程里直接调（有循环 → 线程+join 桥） ----------
    print("\n===== 3. 循环内直调 run_tool（有 running loop → 线程承载分支）—— 真模型 =====")
    t0 = time.monotonic()
    raw_b = tools.run_tool("distill_web", {"question": QUERY, "materials": materials})
    b_s = time.monotonic() - t0
    print(f"耗时 {b_s:.1f}s，返回 {len(raw_b)} 字")
    ok_b = not _is_failure_payload(raw_b)
    check("★循环内直调：桥没把事件循环搞死、返回正常", ok_b, raw_b[:80] if not ok_b else "")
    if ok_b:
        try:
            data_b = json.loads(raw_b)
        except json.JSONDecodeError as exc:
            check("循环内直调：返回可解析的 JSON", False, str(exc))
        else:
            check("循环内直调：返回可解析的 JSON", True)
            check("循环内直调：五个字段齐全",
                  all(k in data_b for k in ("points", "sources", "gaps", "conflicts", "confidence")))
            check("循环内直调：真模型确实产出了要点", bool(data_b.get("points")),
                  f"要点 {len(data_b.get('points') or [])} 条")
    # 桥稳不稳：调用完之后当前循环还得是活的
    await asyncio.sleep(0)
    check("★桥调用后事件循环仍可继续调度（await 能返回）", True)

    # ---------- 4. 报错分支（打桩，不耗 token） ----------
    print("\n===== 4. 工人抛异常时的工具行为（打桩，不耗 token）=====")
    _real = agents.distill_web

    async def _boom(question, materials):
        raise RuntimeError("模拟工人故障")

    agents.distill_web = _boom
    try:
        raw_c = await asyncio.to_thread(
            tools.run_tool, "distill_web", {"question": "x", "materials": "y"}
        )
        check("★工人抛异常时：run_tool 不抛、返回失败话术（主 agent 不会整轮崩）",
              _is_failure_payload(raw_c), raw_c[:80])
    finally:
        agents.distill_web = _real

    print(f"\n结果：{'全部通过 ✅' if not FAIL else f'{FAIL} 项失败 ❌'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

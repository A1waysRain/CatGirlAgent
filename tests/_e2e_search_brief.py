# -*- coding: utf-8 -*-
"""端到端：真实「搜 xxx」链路 = 开浏览器 + 后端读内容 + 模型据内容回答（消耗真实 token）。

打桩原则：**只打桩"开窗口"这一个 I/O 边界**（`_open_and_focus` 换成记录 URL 的空操作，
否则测试会真的弹出浏览器窗口）；事实核验（真联网）与模型调用都是真的。

断言的是**链路一致性**而非具体内容——Bing RSS 通道本身时好时坏：
  · fact_brief 拿到正文 → 回复必须基于正文（不得说"没能读到"）
  · fact_brief 返回空   → 回复必须如实说没读到（不得编内容）
两种都算通过，不一致才算失败。

跑法：cd Cat_Girl && PYTHONIOENCODING=utf-8 PYTHONPATH="$(pwd)" .venv/Scripts/python.exe tests/_e2e_search_brief.py
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
_TMP = tempfile.mkdtemp(prefix="catgirl_e2e_search_")
os.environ["APPDATA"] = _TMP
os.environ["CATGIRL_SKIP_PET"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)          # 让 pydantic 读到 dev .env 的 Key

import backend.routers.chat as chat_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import backend.main as m  # noqa: E402

# ---- 只打桩"开窗口"：记录 URL，不真的弹浏览器 ----
OPENED: list[str] = []
chat_mod._open_and_focus = lambda url: OPENED.append(url)

# ---- 包一层 fact_brief（降级路径）与 distill_web（提炼工人）：保留真实行为，只记录产出 ----
_real_brief = chat_mod.fact_brief
BRIEFS: list[str] = []


def _spy_brief(query, *a, **k):
    out = _real_brief(query, *a, **k)
    BRIEFS.append(out)
    return out


chat_mod.fact_brief = _spy_brief

_real_distill = chat_mod.distill_web
DISTILLS: list[tuple] = []          # [(是否成功, 结果或异常名)]


async def _spy_distill(question, materials):
    try:
        out = await _real_distill(question, materials)
        DISTILLS.append(("ok", out))
        return out
    except Exception as exc:
        DISTILLS.append(("fail", type(exc).__name__))
        raise


chat_mod.distill_web = _spy_distill

client = TestClient(m.app)
QUERY = sys.argv[1] if len(sys.argv) > 1 else "2026 F1 赛历"

print(f"用户消息：搜 {QUERY}")
r = client.post("/api/chat_response", json={"chatmassage": f"搜 {QUERY}"})
assert r.status_code == 200, f"HTTP {r.status_code}"

reply = "".join(
    json.loads(line[5:].strip()).get("text", "")
    for line in r.text.splitlines()
    if line.startswith("data:") and line[5:].strip().startswith("{")
    and json.loads(line[5:].strip()).get("type") == "delta"
)
distill_ok = bool(DISTILLS) and DISTILLS[0][0] == "ok"
distilled = DISTILLS[0][1] if distill_ok else None
fallback_used = bool(BRIEFS)
digest = ""
if distill_ok:
    digest = chat_mod.render_distill(distilled)
elif fallback_used:
    digest = BRIEFS[0]

print("\n---- 浏览器 ----")
print("开的是:", OPENED[0][:90] if OPENED else "★没开（链路断了）")
print(f"\n---- 路径 ----")
print(f"提炼工人：{'成功' if distill_ok else ('失败 → 降级到 fact_brief' if DISTILLS else '未被调用')}")
if DISTILLS and not distill_ok:
    print(f"  失败原因：{DISTILLS[0][1]}")
print(f"fact_brief 降级路径：{'用过' if fallback_used else '没用（提炼成功了）'}")
if distilled:
    print(f"\n---- 工人产出（要点 {len(distilled.get('points', []))} 条 / "
          f"列表 {len(distilled.get('schedule') or [])} 项 / "
          f"缺口 {len(distilled.get('gaps', []))} / 矛盾 {len(distilled.get('conflicts', []))}）----")
    for s in (distilled.get("schedule") or [])[:30]:
        print("  ▸", str(s)[:120])
    for p in distilled.get("points", [])[:12]:
        print("  •", str(p)[:110])
    for g in distilled.get("gaps", []):
        print("  缺口：", str(g)[:110])
    for c in distilled.get("conflicts", []):
        print("  矛盾：", str(c)[:150])
print(f"\n---- 注入主对话的证据段（{len(digest)} 字）----")
print(digest[:900] if digest else "（空 → 本次确实没读到正文，走降级）")
print("\n---- 猫娘回复 ----")
print(reply)

FAIL = 0


def check(label, cond):
    global FAIL
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        FAIL += 1


# 内容只会来自注入的正文（模型自己编不出 2026 赛历的具体站名/时刻）
F1_WORDS = ("澳大利亚", "中国大奖赛", "巴林", "赛历", "上海", "墨尔本", "正赛", "排位赛", "赛程")

print()
check("浏览器确实打开了必应搜索页", bool(OPENED) and "cn.bing.com/search" in OPENED[0])
check("提炼工人被调用了一次", len(DISTILLS) == 1)
check("提炼成功或明确走了降级", distill_ok or fallback_used)
if distill_ok:
    check("工人产出结构化字段齐全",
          all(k in distilled for k in ("points", "sources", "gaps", "conflicts", "confidence")))
    check("工人给出了要点", bool(distilled.get("points")))
if digest:
    hits = [w for w in F1_WORDS if w in reply]
    check("★读到正文 → 回复确实用了正文内容", len(hits) >= 2)
    print(f"      （命中的赛历关键词：{'、'.join(hits) or '无'}）")
    # 整轮"完全没读到"的降级话术不该出现（部分来源没读到是正常且被鼓励的如实说明）
    check("★没走整轮降级话术", "没能读到网页正文" not in reply)
    check("回复不是空话（有实质长度）", len(reply.strip()) > 40)
else:
    check("★没读到 → 回复如实说明", "没读到" in reply or "没能读到" in reply)
    check("没读到时不编内容（不出现赛历关键词）", not any(w in reply for w in F1_WORDS[:4]))

print(f"\n结果：{'全部通过 ✅' if not FAIL else f'{FAIL} 项失败 ❌'}")
sys.exit(1 if FAIL else 0)

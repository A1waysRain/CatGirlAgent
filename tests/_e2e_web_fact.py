# -*- coding: utf-8 -*-
"""端到端：真联网跑 verify_current_fact（Bing RSS 通道，无需搜索 API Key）。

与 `_verify_web_fact.py` 的分工：那份打桩 I/O 测逻辑，这份真发请求测真实数据质量——
证据分级、市场地区、来源时效这些只有在真网上才看得出效果。

跑法（需联网；本机系统代理会被 urllib 自动读取，无需额外配置）：
    PYTHONIOENCODING=utf-8 PYTHONPATH="$(pwd)" .venv/Scripts/python.exe tests/_e2e_web_fact.py
    # 指定问题：... tests/_e2e_web_fact.py "2026 F1 赛历"

判定原则：**联网通道本身不可用不算失败**（打印 SKIP 退出 0），
但**契约违反一律判失败**（如 verified 却没有 2 个独立域名、full 来源是站点首页）。
"""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="catgirl_e2e_web_fact_")
os.environ["APPDATA"] = _TMP
os.environ["CATGIRL_SKIP_PET"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend import tools  # noqa: E402

TOTAL = [0, 0]
QUERIES = sys.argv[1:] or ["2026 F1 赛历", "DeepSeek 最新模型"]


def check(label, condition):
    TOTAL[0] += 1
    if condition:
        print(f"[PASS] {label}")
    else:
        TOTAL[1] += 1
        print(f"[FAIL] {label}")


def show(report):
    ev = report.get("evidence", {})
    print(f"  status={report['status']} confidence={report['confidence']} "
          f"market={report.get('market')} freshness={report.get('freshness')} "
          f"耗时={report.get('elapsed_s')}s")
    print(f"  证据：full={ev.get('full')} snippet={ev.get('snippet')} none={ev.get('none')} "
          f"独立域名={ev.get('independent_domains')}")
    for s in report.get("sources", []):
        age = s.get("age_days")
        print(f"    [{s.get('grade'):>7}] {s.get('domain'):<28} "
              f"{(s.get('published') or '无时间')[:31]:<32} "
              f"{'' if age is None else str(age) + '天前'}")
        for reason in s.get("grade_reasons", []):
            print(f"               └ {reason}")
    for c in report.get("caveats", []):
        print(f"  ⚠ {c}")


try:
    print(f"查询：{QUERIES}\n")
    got_real_data = False
    for query in QUERIES:
        print(f"===== {query} =====")
        report = json.loads(tools.tool_verify_current_fact(query))
        show(report)
        print()

        # --- 契约不变量（与网络结果无关，必须永远成立）---
        check(f"[{query}] status 在合法取值内",
              report["status"] in {"verified", "insufficient", "failed"})
        check(f"[{query}] 无 conflicting（已移除该状态）", "conflicting" not in json.dumps(report))
        check(f"[{query}] 每条来源都有 grade 与原因",
              all(s.get("grade") in {"full", "snippet", "none"} and "grade_reasons" in s
                  for s in report["sources"]))
        check(f"[{query}] full 来源不是站点首页",
              all(not tools._fact_is_root_page(s["url"])
                  for s in report["sources"] if s["grade"] == "full"))
        check(f"[{query}] full 来源正文都够长",
              all(len(s["extract"]) >= tools._FACT_MIN_EXTRACT
                  for s in report["sources"] if s["grade"] == "full"))
        check(f"[{query}] verified 必有 ≥2 个独立域名",
              report["status"] != "verified" or report["evidence"]["independent_domains"] >= 2)
        check(f"[{query}] verified 时 confidence 不为 low",
              report["status"] != "verified" or report["confidence"] in {"high", "medium"})
        check(f"[{query}] non-verified 时 confidence 不为 high",
              report["status"] == "verified" or report["confidence"] != "high")
        check(f"[{query}] 带核验时间与耗时",
              bool(report["checked_at"]) and isinstance(report["elapsed_s"], (int, float)))
        check(f"[{query}] caveats 保留不可信资料声明",
              any("不可信资料" in c for c in report["caveats"]))

        if report["sources"]:
            got_real_data = True

    if not got_real_data:
        print("\n[SKIP] 搜索通道没返回任何结果——本机网络/代理不通，契约判定未生效（退出 0）")
        sys.exit(0)

    print(f"\n真联网事实核验 e2e：通过 {TOTAL[0]} / 失败 {TOTAL[1]}")
    sys.exit(1 if TOTAL[1] else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

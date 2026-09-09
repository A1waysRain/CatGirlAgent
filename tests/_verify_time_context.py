# -*- coding: utf-8 -*-
"""验证聊天时间上下文：绝对时钟、跨日历史标记与插入位置约束。"""
from datetime import datetime

from backend.chat_service import current_time_hint
from backend.routers.chat import _annotate_recent_dates

failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global failed
    if not condition:
        failed += 1
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


# 2026-09-03 是星期三；固定时间避免测试受运行时钟影响。
morning = current_time_hint(datetime(2026, 9, 3, 11, 18))
check("当前时间含日期、星期与上午", "2026年9月3日 星期四 上午 11 点 18 分" in morning, morning)

night = current_time_hint(datetime(2026, 9, 3, 23, 5))
check("深夜时段正确", "深夜 23 点 05 分" in night, night)

recent = [
    {"role": "user", "content": "9月1日的事", "ts": datetime(2026, 9, 1, 22, 30).timestamp()},
    {"role": "assistant", "content": "同一天回复", "ts": datetime(2026, 9, 1, 22, 31).timestamp()},
    {"role": "user", "content": "9月2日设提醒", "ts": datetime(2026, 9, 2, 23, 46).timestamp()},
    {"role": "assistant", "content": "同一天确认", "ts": datetime(2026, 9, 2, 23, 47).timestamp()},
    {"role": "user", "content": "9月3日打开酷狗", "ts": datetime(2026, 9, 3, 11, 18).timestamp()},
]
annotated = _annotate_recent_dates(recent)
check("跨日期首条带日期时间", annotated[2]["content"].startswith("（9月2日 23:46）"), annotated[2]["content"])
check("第二次跨日期也带标记", annotated[4]["content"].startswith("（9月3日 11:18）"), annotated[4]["content"])
check("同一天消息不重复加日期", not annotated[1]["content"].startswith("（") and not annotated[3]["content"].startswith("（"))
check("模型副本不改会话原文", recent[2]["content"] == "9月2日设提醒" and annotated[2] is not recent[2])

print("全部通过" if failed == 0 else f"{failed} 项失败")
raise SystemExit(1 if failed else 0)

"""验证「时间感知问候」：时间差分级 + 提示生成（配合 sessions 消息里的 ts 字段）。

用法：在 Cat_Girl 目录下跑  .venv/Scripts/python.exe _verify_greeting_gap.py
"""
import time

from backend.chat_service import (
    GREET_GAP_BREAK,
    GREET_GAP_LONG,
    _gap_seconds,
    greeting_hint,
)

now = time.time()


def msgs(prev_age: float, cur_age: float = 0.0):
    """构造两条消息：prev_age=上一条距现在的秒数，cur_age=当前这条距现在的秒数（默认现在）。

    注意不能给中间消息加偏移——那样 gap 就不是精确的 prev_age，边界测试会偏差 1 秒。
    """
    return [
        {"role": "user", "content": "我要睡大觉", "ts": now - prev_age},
        {"role": "assistant", "content": "快去睡吧主人晚安喵", "ts": now - prev_age},
        {"role": "user", "content": "晚上好", "ts": now - cur_age},
    ]


fail = 0

# 1) 短间隔：1 小时 → 无提示
h = greeting_hint(msgs(prev_age=3600))
ok = h is None
print(f"[{'PASS' if ok else 'FAIL'}] 短间隔 1h → 无提示: {h}")
fail += not ok

# 2) 跨夜：20 小时 → 关心提示（傲娇嘴硬心软，且明确禁止质问）
h = greeting_hint(msgs(prev_age=20 * 3600))
ok = h is not None and "20小时" in h and "嘴硬心软" in h and "质问" in h
print(f"[{'PASS' if ok else 'FAIL'}] 跨夜 20h → 关心提示: {h!r}")
fail += not ok

# 3) 久别：4 天 → 想念提示
h = greeting_hint(msgs(prev_age=4 * 86400))
ok = h is not None and "4天" in h and "想念" in h
print(f"[{'PASS' if ok else 'FAIL'}] 久别 4天 → 想念提示: {h!r}")
fail += not ok

# 4) 少于两条消息 → None
ok = greeting_hint([{"role": "user", "content": "只有一条", "ts": now}]) is None
print(f"[{'PASS' if ok else 'FAIL'}] 单条消息 → None")
fail += not ok

# 5) 旧消息缺 ts（升级前历史）→ None，不崩
h = greeting_hint(
    [
        {"role": "user", "content": "旧", "id": "a"},
        {"role": "assistant", "content": "旧", "id": "b"},
        {"role": "user", "content": "晚上好", "ts": now},
    ]
)
ok = h is None
print(f"[{'PASS' if ok else 'FAIL'}] 上一条缺 ts → None: {h}")
fail += not ok

# 6) 阈值边界：8h 整 → 有提示；8h 少 1 秒 → 无提示
h1 = greeting_hint(msgs(prev_age=GREET_GAP_BREAK))
h2 = greeting_hint(msgs(prev_age=GREET_GAP_BREAK - 1))
ok = h1 is not None and h2 is None
print(f"[{'PASS' if ok else 'FAIL'}] 边界 8h: 整点有提示={h1 is not None}, 少1s无提示={h2 is None}")
fail += not ok

# 7) 阈值边界：3 天整 → 想念；3 天少 1 秒 → 关心（非想念）
h1 = greeting_hint(msgs(prev_age=GREET_GAP_LONG))
h2 = greeting_hint(msgs(prev_age=GREET_GAP_LONG - 1))
ok = h1 is not None and "想念" in h1 and h2 is not None and "想念" not in h2
print(f"[{'PASS' if ok else 'FAIL'}] 边界 3天: 整点想念={h1 is not None}, 少1s关心={h2 is not None}")
fail += not ok

# 8) _gap_seconds：正常算差 / 单条 None
g = _gap_seconds(msgs(prev_age=7200))
ok = g is not None and abs(g - 7200) < 2
print(f"[{'PASS' if ok else 'FAIL'}] _gap_seconds 7200s ≈ {g}")
fail += not ok
ok = _gap_seconds([{"role": "user", "content": "x"}]) is None
print(f"[{'PASS' if ok else 'FAIL'}] _gap_seconds 单条 → None")
fail += not ok

print("\n" + ("全部通过" if fail == 0 else f"{fail} 项失败"))
raise SystemExit(1 if fail else 0)

"""实测情绪超时自动回待机（_set_mood 挂超时 / 到点 _clear_mood / 新情绪与 happy 取消旧超时）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import desktop.pet as pet_mod
from desktop.pet import Pet, load_config  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  <- {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def make_pet():
    return Pet("http://127.0.0.1:1", load_config(), parent_pid=None)


def main():
    # 1. 设情绪 → 挂超时
    pet = make_pet()
    try:
        pet._set_mood("tsundere")
        check("设傲娇情绪后 _mood=tsundere", pet._mood == "tsundere")
        check("设傲娇情绪后 _in_emotion=True", pet._in_emotion())
        check("设傲娇情绪后挂了超时定时器", pet._mood_job is not None)

        # 2. 手动触发超时回调 → 回到待机
        pet._clear_mood()
        check("超时回调后 _mood=None", pet._mood is None)
        check("超时回调后 _mood_job=None", pet._mood_job is None)
        check("超时回调后 _in_emotion=False", not pet._in_emotion())

        # 3. 新情绪覆盖旧超时（定时器被替换，mood 更新）
        pet._set_mood("angry")
        pet._set_mood("sad")
        check("新情绪覆盖后 _mood=sad", pet._mood == "sad")
        check("新情绪覆盖后仍有超时定时器", pet._mood_job is not None)

        # 4. happy 清情绪 → 取消定时器
        pet._set_mood("happy")
        check("happy 清情绪后 _mood=None", pet._mood is None)
        check("happy 清情绪后 _mood_job=None", pet._mood_job is None)

        # 5. 真超时到点（把超时改短，跑主循环等它触发）
        pet_mod.MOOD_TIMEOUT = 0.5
        pet._set_mood("tease")
        check("超时改短后仍挂定时器", pet._mood_job is not None)
        pet.root.after(1200, pet.root.quit)  # 1.2s 后退出主循环，让 0.5s 定时器触发
        pet.root.mainloop()
        check("0.5s 超时到点自动回待机", pet._mood is None and not pet._in_emotion(),
              f"_mood={pet._mood} _mood_job={pet._mood_job}")
        pet_mod.MOOD_TIMEOUT = 20  # 还原

    finally:
        try:
            pet.quit()
        except SystemExit:
            pass

    print("\n" + ("全部通过 ✅" if not FAILURES else f"失败 {len(FAILURES)} 项: {FAILURES} ❌"))
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()

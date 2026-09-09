"""实测桌宠贴边停靠的鼠标接近滑出逻辑（真实 Tk，模拟鼠标位置）。

不跑 mainloop，直接手动调 _dock/_dock_hover_check/_dock_slide_loop，
验证「停靠 → 鼠标远离 → 鼠标靠近 → 滑出」整条链路。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from desktop.pet import Pet, load_config  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  <- {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


# 鼠标坐标模拟器
class Mouse:
    x = 0
    y = 0


def make_pet():
    cfg = load_config()
    pet = Pet("http://127.0.0.1:1", cfg, parent_pid=None)
    # 覆盖指针读取：改成走我们控制的坐标
    pet.root.winfo_pointerx = lambda: Mouse.x
    pet.root.winfo_pointery = lambda: Mouse.y
    return pet


def main():
    pet = make_pet()
    try:
        sx, sy, sw, sh = 0, 0, pet.root.winfo_screenwidth(), pet.root.winfo_screenheight()
        wx = 0
        # work_area：简化为整屏（任务栏有无不影响右侧 x 计算）
        ww = sw
        peek = pet._dock_cfg()["peek"]
        w = pet.w
        print(f"屏幕 {sw}x{sh}  宠物 {w}x{pet.h}  peek={peek}")
        print(f"out_pos={ww - w}  hide_pos={ww - peek}")

        # 1. 停靠到右侧
        pet._dock("right")
        pet.root.update()  # 处理待定事件，让 WM 真正移动窗口（否则 winfo_x 停在 0）
        x, y = pet.root.winfo_x(), pet.root.winfo_y()
        print(f"停靠后位置 = ({x},{y})")
        check("停靠后 _docked=True", pet._docked)
        check("停靠后 _dock_mouse_far=False（刚停靠不弹）", pet._dock_mouse_far is False)
        check("停靠后 x 贴近右侧只露 peek", abs(x - (ww - peek)) <= 2)

        # 2. 鼠标在屏幕中间（远离）→ 记下离开标记，保持收起
        Mouse.x = ww // 2
        Mouse.y = y + pet.h // 2
        pet._dock_hover_check()
        check("鼠标远离后 _dock_mouse_far=True", pet._dock_mouse_far is True)
        check("鼠标远离后仍收起（target=hide_pos）",
              pet._dock_target == (ww - peek, pet.root.winfo_y()), f"target={pet._dock_target}")

        # 3. 鼠标靠近右侧边缘 → 应滑出
        Mouse.x = ww - 5
        pet._dock_hover_check()
        check("鼠标靠近后 _dock_out=True", pet._dock_out is True)
        check("鼠标靠近后 target=out_pos（完全滑出）",
              pet._dock_target == (ww - w, pet.root.winfo_y()), f"target={pet._dock_target}")

        # 4. 跑几帧滑出动画 → x 应逐渐接近 out_pos
        for _ in range(40):
            pet._dock_slide_loop()
        pet.root.update()
        x = pet.root.winfo_x()
        print(f"滑动后 x = {x}（out_pos={ww - w}）")
        check("滑出动画执行到位（x≈out_pos）", abs(x - (ww - w)) <= 2, f"x={x}")

        # 5. 鼠标再远离 → 应收回（只露 peek）
        Mouse.x = ww // 2
        pet._dock_hover_check()
        check("再次远离后 _dock_out=False", pet._dock_out is False)
        check("再次远离后 target=hide_pos",
              pet._dock_target == (ww - peek, pet.root.winfo_y()), f"target={pet._dock_target}")

        # 6. 鼠标贴近但从未远离过（刚停靠场景）→ 不应滑出
        pet._dock("right")  # 重新停靠，重置 _dock_mouse_far=False
        Mouse.x = ww - 5
        pet._dock_hover_check()
        check("刚停靠鼠标在边缘不弹（_dock_mouse_far=False）", pet._dock_out is False)

        # 7. 【回归】情绪卡住时滑出不能挡：先远离再靠近，情绪在也应滑出
        pet._dock("right")
        Mouse.x = ww // 2            # 先远离 → 记下离开标记
        pet._dock_hover_check()
        pet._set_mood("tsundere")     # 模拟后端推来情绪后情绪卡住
        Mouse.x = ww - 5              # 再靠近边缘 → 应滑出
        pet._dock_hover_check()
        check("情绪卡住时鼠标靠近仍滑出（不被 _in_emotion 挡）",
              pet._dock_out is True and pet._dock_target == (ww - w, pet.root.winfo_y()),
              f"_in_emotion={pet._in_emotion()} _dock_out={pet._dock_out} target={pet._dock_target}")

    finally:
        try:
            pet.quit()
        except SystemExit:
            pass

    print("\n" + ("全部通过 ✅" if not FAILURES else f"失败 {len(FAILURES)} 项: {FAILURES} ❌"))
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()

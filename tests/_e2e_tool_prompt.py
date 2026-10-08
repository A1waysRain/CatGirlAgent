# 端到端：验证系统提示词瘦身后模型仍能正确「选工具 + 填参数」（真实 DeepSeek，消耗 token）
# 关键：打桩 run_tool —— 只记录调用不产生真实副作用（不真发微信/不开浏览器/不写文件/不设闹钟）
# 用法：cd Cat_Girl && PYTHONIOENCODING=utf-8 .venv\Scripts\python _e2e_tool_prompt.py
import asyncio
import io
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import backend.tools as T
from backend.tools import TOOL_SCHEMAS
from backend.llm import SYSTEM_PROMPT_CHAT, call_deepseek_with_tools

_calls: list = []


def _stub_run_tool(name, args):
    """打桩：记录调用，返回逼真的成功话术，绝不碰真实系统。"""
    _calls.append((name, args))
    fake = {
        "ui_search_contact": lambda: f"已打开与 {args.get('name', '?')} 的聊天窗口喵",
        "ui_type": lambda: "文字已输入输入框喵",
        "ui_send": lambda: "消息已发送喵",
        "ui_send_file": lambda: f"文件已发送给 {args.get('contact', '?')} 喵",
        "ui_cancel_send": lambda: "标记已清除喵",
        "set_alarm": lambda: f"已创建提醒：{args.get('time')} {args.get('message')} 喵",
        "set_alarms_batch": lambda: f"已创建 {len(args.get('alarms') or [])} 条提醒喵",
        "get_time": lambda: "2026年08月22日 星期六 10:30:45",
        "web_search": lambda: "已在浏览器打开搜索页喵",
        "open_url": lambda: "已在浏览器打开网址喵",
        "launch_app": lambda: f"已启动 {args.get('name')} 喵",
        "open_path": lambda: "已打开喵",
        "list_dir": lambda: "桌面内容：测试.docx, 图片文件夹 喵",
        "read_file": lambda: "这是文件内容喵",
        "append_file": lambda: f"已追加，文件：{args.get('path')} 喵",
        "replace_in_file": lambda: "已替换喵",
        "format_docx": lambda: "已设置格式喵",
        "create_pptx": lambda: f"已生成 PPT：{args.get('path')}，{len(args.get('slides') or [])} 页喵",
        "kill_process": lambda: "进程已结束喵",
        "scan_apps": lambda: "扫描完成，发现 88 个应用喵",
        "list_alarms": lambda: "当前提醒：1 条 08:00 喝水 喵",
        "delete_alarm": lambda: "已删除提醒喵",
        "list_plugins": lambda: "已装插件：image_vision 喵",
    }
    return fake.get(name, lambda: "完成喵")()


T.run_tool = _stub_run_tool  # call_deepseek_with_tools 内 `from .tools import run_tool` 调用时能看到

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"    ✅ {name}" + (f"   [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"    ❌ {name}" + (f"   [{detail}]" if detail else ""))


def trace(calls):
    if not calls:
        print("    （本轮没调任何工具）")
    for nm, ar in calls:
        print(f"    → {nm}({ar})")


async def run_case(prompt):
    global _calls
    _calls = []
    msgs = [{"role": "system", "content": SYSTEM_PROMPT_CHAT}, {"role": "user", "content": prompt}]
    try:
        return await call_deepseek_with_tools(msgs, TOOL_SCHEMAS, max_rounds=5)
    except Exception as e:
        return f"<异常> {e}"


def has(names, *args):
    return [a for n, a in _calls if n in names]


async def main():
    cases = [
        # (标题, 用户话, 断言函数(checks 闭包读全局 _calls/reply))
        ("微信发文字", "帮我在微信给王小明发一句 今晚一起吃饭", wechat_text),
        ("微信发文件", "把桌面上的 报告.docx 发给王小明", wechat_file),
        ("定时提醒", "明天早上8点提醒我喝水", set_alarm),
        ("问时间", "现在几点了喵？", get_time),
        ("联网搜索", "帮我搜一下今天的热点新闻", web_search),
        ("打开应用", "帮我打开记事本", launch_app),
        ("整理成 Word", "把这几条整理成一个 Word 文档放桌面：①项目叫猫娘来咯 ②三层架构 ③DeepSeek 驱动", make_word),
        ("做 PPT", "做个简单的 3 页 PPT 介绍一下猫娘项目", make_pptx),
    ]
    global _last_reply
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    if only:
        cases = [c for c in cases if only in c[0]]
    for title, prompt, assert_fn in cases:
        print(f"\n===== {title} =====")
        print(f"  用户：{prompt}")
        reply = await run_case(prompt)
        _last_reply = reply
        trace(_calls)
        print(f"  猫娘回复：{reply[:120]}")
        try:
            assert_fn()
        except Exception as e:
            check(f"{title} 断言执行异常", False, str(e))
        print(f"  [累计 PASS={PASS} FAIL={FAIL}]")

    print(f"\n{'='*50}\n工具调用回归：PASS {PASS} / FAIL {FAIL}")
    if FAIL:
        print("存在失败项，请人工核对上面的 ❌")
        sys.exit(1)
    print("✅ 全部通过")


# ---------- 各场景断言 ----------

def wechat_text():
    names = [n for n, _ in _calls]
    sc = has("ui_search_contact")
    check("调用了 ui_search_contact", bool(sc), f"实际调用: {names}")
    if sc:
        a = sc[-1]
        check("window=微信", a.get("window") == "微信", f"window={a.get('window')}")
        check("name=王小明", a.get("name") == "王小明", f"name={a.get('name')}")
    check("调用了 ui_type", "ui_type" in names)
    check("没误调 launch_app", "launch_app" not in names, f"实际: {names}")
    sends = has("ui_send")
    check("没有未确认就发送（ui_send 若出现须 confirmed=True）",
          all(s.get("confirmed") is True for s in sends), f"ui_send: {sends}")


def wechat_file():
    names = [n for n, _ in _calls]
    sf = has("ui_send_file")
    check("调用了 ui_send_file", bool(sf), f"实际调用: {names}")
    if sf:
        a = sf[-1]
        check("window=微信", a.get("window") == "微信", f"window={a.get('window')}")
        check("contact=王小明", a.get("contact") == "王小明", f"contact={a.get('contact')}")
        check("path 含 报告", "报告" in a.get("path", ""), f"path={a.get('path')}")
    check("没误调 launch_app", "launch_app" not in names)
    check("没拆成 search+type+send", not ("ui_search_contact" in names and "ui_send" in names),
          f"实际: {names}")


def set_alarm():
    names = [n for n, _ in _calls]
    alarm = has("set_alarm")
    check("调用了 set_alarm", bool(alarm), f"实际调用: {names}")
    if alarm:
        a = alarm[-1]
        check("time=08:00", a.get("time") == "08:00", f"time={a.get('time')}")
        check("message 含 喝水", "喝水" in a.get("message", ""), f"message={a.get('message')}")
        check("date 填了具体日期 YYYY-MM-DD", bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.get("date") or "")),
              f"date={a.get('date')}")
        check("repeat=once 一次性", a.get("repeat", "once") == "once", f"repeat={a.get('repeat')}")


def get_time():
    names = [n for n, _ in _calls]
    check("调用了 get_time", "get_time" in names, f"实际调用: {names}")


def web_search():
    names = [n for n, _ in _calls]
    ws = has("web_search")
    check("调用了 web_search", bool(ws), f"实际调用: {names}")
    if ws:
        check("query 非空", bool(ws[-1].get("query", "").strip()), f"query={ws[-1].get('query')}")


def launch_app():
    names = [n for n, _ in _calls]
    la = has("launch_app")
    check("调用了 launch_app", bool(la), f"实际调用: {names}")
    if la:
        check("name 含 记事本", "记事本" in la[-1].get("name", ""), f"name={la[-1].get('name')}")


def make_word():
    names = [n for n, _ in _calls]
    ap = has("append_file")
    if ap:
        a = ap[-1]
        check("append_file 目标是 .docx", str(a.get("path", "")).endswith(".docx"), f"path={a.get('path')}")
        check("append_file confirmed=True", a.get("confirmed") is True, f"confirmed={a.get('confirmed')}")
    else:
        reply = _last_reply
        # 危险操作两种正确形态：先问主人确认，或（无工具时）追问澄清——但绝不能空口声称已生成
        check("没直接写文件就保持询问（确认/澄清皆可）",
              any(k in reply for k in ["吗", "？", "?", "确认", "可以", "列一下"]),
              f"回复: {reply[:60]}")
        check("没有空口声称已生成", not any(k in reply for k in ["已生成", "已放到桌面", "写好了"]),
              f"回复: {reply[:60]}")


def make_pptx():
    names = [n for n, _ in _calls]
    cp = has("create_pptx")
    if cp:
        a = cp[-1]
        check("create_pptx confirmed=True", a.get("confirmed") is True, f"confirmed={a.get('confirmed')}")
        check("path 是 .pptx", str(a.get("path", "")).endswith(".pptx"), f"path={a.get('path')}")
    else:
        reply = _last_reply
        check("做 PPT 先问澄清问题（给谁看/几页/风格）", any(k in reply for k in ["给谁看", "几页", "风格", "吗"]),
              f"回复: {reply[:60]}")
        check("没空口声称已生成 PPT", "已生成" not in reply and "已完成" not in reply, f"回复: {reply[:60]}")


_last_reply = ""


if __name__ == "__main__":
    asyncio.run(main())

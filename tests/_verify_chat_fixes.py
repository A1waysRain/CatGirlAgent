# -*- coding: utf-8 -*-
"""验证 2026-08-20 两个修复（隔离 APPDATA，不真开浏览器/不真启动应用/不真调模型）：
1. chat() 工具过滤覆盖 bug：search/launch 分支不再把已摘的提醒工具重新引入（统一 disabled_tools）
2. 「待会 X 点」时间解析对 soon 敏感：晚上 22 点说"待会十一点"= 当天 23:00（不再推明天早上 11:00）
"""
import asyncio
import datetime
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, ".")

import backend.routers.chat as chat_mod
from backend.routers.chat import _extract_alarm_request as F

_tmp_dirs = []


def fresh_appdata():
    d = tempfile.mkdtemp(prefix="catgirl_verify_chatfix_")
    os.environ["APPDATA"] = d
    os.environ["CATGIRL_SKIP_PET"] = "1"
    _tmp_dirs.append(d)


ok = True


def check(label, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))


# 假时钟：patch chat 模块的 datetime（_extract_alarm_request 内部用 datetime.now()）
class FakeDT(datetime.datetime):
    _fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def set_now(dt):
    FakeDT._fixed = dt


chat_mod.datetime = FakeDT

print("===== 1. 「待会 X 点」soon 时间解析 =====")

# 用户报的 bug：晚上 22 点说"待会十一点"→ 应为当天 23:00（不是早上 11 点推明天）
set_now(datetime.datetime(2026, 8, 19, 22, 0, 0))
g = F("待会十一点提醒我做终末地日常")
check("晚22点 待会十一点 → 今天23:00", bool(g) and g["time"] == "23:00" and g["date"] == "2026-08-19", str(g))
g = F("待会11点提醒我喝水")
check("晚22点 待会11点 → 今天23:00", bool(g) and g["time"] == "23:00" and g["date"] == "2026-08-19", str(g))

# 其它 soon 边界
set_now(datetime.datetime(2026, 8, 19, 10, 0, 0))   # 上午 10 点
g = F("待会11点提醒我看比赛")
check("上午10点 待会11点 → 今天11:00", bool(g) and g["time"] == "11:00" and g["date"] == "2026-08-19", str(g))
set_now(datetime.datetime(2026, 8, 19, 14, 0, 0))   # 下午 2 点
g = F("待会11点提醒我开会")
check("下午14点 待会11点 → 今天23:00", bool(g) and g["time"] == "23:00" and g["date"] == "2026-08-19", str(g))
set_now(datetime.datetime(2026, 8, 19, 23, 30, 0))  # 深夜 23:30
g = F("待会11点提醒我睡觉")
check("深夜23:30 待会11点 → 明天11:00", bool(g) and g["time"] == "11:00" and g["date"] == "2026-08-20", str(g))
# 回归：原"待会4点判早晚"用例（下午 3 点）
set_now(datetime.datetime(2026, 8, 19, 15, 0, 0))
g = F("待会4点提醒我看比赛")
check("下午15点 待会4点 → 今天16:00（回归）", bool(g) and g["time"] == "16:00" and g["date"] == "2026-08-19", str(g))
# 回归：裸小时（不带 soon）行为不变
set_now(datetime.datetime(2026, 8, 19, 15, 0, 0))
g = F("4点提醒我看比赛")
check("裸4点下午 → 16:00 今天（回归）", bool(g) and g["time"] == "16:00" and g["date"] == "2026-08-19", str(g))
set_now(datetime.datetime(2026, 8, 19, 2, 0, 0))
g = F("4点提醒我看比赛")
check("凌晨裸4点 → 04:00（回归）", bool(g) and g["time"] == "04:00" and g["date"] == "2026-08-19", str(g))

print("===== 1.5 否定句误创建防护 =====")
set_now(datetime.datetime(2026, 8, 19, 10, 0, 0))
g = F("不要在晚上八点提醒我")
check("『不要在晚上八点提醒我』不建", g is None, str(g))
g = F("别在晚上八点提醒我")
check("『别在晚上八点提醒我』不建", g is None, str(g))
g = F("不用晚上八点提醒我了")
check("『不用晚上八点提醒我了』不建", g is None, str(g))
g = F("晚上八点提醒我")
check("『晚上八点提醒我』照常建", bool(g) and g["time"] == "20:00", str(g))
g = F("别忘了提醒我八点开会")
check("『别忘了提醒我八点开会』仍建（肯定句豁免）", bool(g) and g["time"] == "08:00" and g["message"] == "开会", str(g))
g = F("不要忘了晚上八点提醒我")
check("『不要忘了晚上八点提醒我』仍建（肯定句豁免）", bool(g) and g["time"] == "20:00", str(g))

print("===== 2. chat() 工具过滤覆盖 =====")
fresh_appdata()

# 打桩 stream_answer：记录收到的 tools，不做任何真实调用
captured = {}


async def fake_stream_answer(sid, msgs, user_text, tools=None, user_msg_id=None,
                             write_requested=False, write_state=None, alarm_created=None):
    captured["tools"] = sorted(t["function"]["name"] for t in (tools or []))
    captured["alarm_created"] = alarm_created
    captured["write_state"] = write_state or {}
    captured["msgs"] = msgs
    yield {"type": "done", "message_ids": {"assistant": "fake"}}


chat_mod.stream_answer = fake_stream_answer
_real_time_hint = chat_mod.current_time_hint
chat_mod.current_time_hint = lambda: "【现在时间】固定时间注记"

# 打桩副作用函数：不真开浏览器/不真启动应用/不真建闹钟
_real_open = chat_mod._open_search
chat_mod._open_search = lambda *a, **k: "已为 xx 打开搜索页"
_real_brief = chat_mod.fact_brief
chat_mod.fact_brief = lambda query: "status=verified；example.com：F1 赛历正文要点"
_real_verify_fact = chat_mod.tool_verify_current_fact
chat_mod.tool_verify_current_fact = lambda query, **kwargs: '{"status":"verified","evidence":{"full":1,"independent_domains":1},"sources":[]}'
_real_distill = chat_mod.distill_web
async def _fake_distill(question, materials):
    return {"points": ["F1 赛历正文要点"], "sources": [{"domain": "example.com", "published": "今天", "grade": "full"}], "gaps": [], "conflicts": [], "confidence": "medium"}
chat_mod.distill_web = _fake_distill
_real_auto = chat_mod._auto_launch_apps
_auto = {"launched": ["原神"], "failed": []}          # 可切换：部分成功 / 全部失败
chat_mod._auto_launch_apps = lambda names: (_auto["launched"], _auto["failed"])
_real_add = chat_mod.scheduler.add_alarm
chat_mod.scheduler.add_alarm = lambda **kw: {"time": kw["time"], "message": kw.get("message", ""), "date": kw.get("date")}

ALL = sorted(t["function"]["name"] for t in chat_mod.TOOL_SCHEMAS)
set_now(datetime.datetime(2026, 8, 19, 22, 0, 0))


async def call_chat(text):
    captured.clear()
    resp = await chat_mod.chat(SimpleNamespace(chatmassage=text, session_id=None))
    async for _ in resp.body_iterator:
        pass
    return captured


async def run():
    c = await call_chat("搜一下原神新版本，待会11点提醒我打游戏")   # alarm+search 同时
    check("alarm+search：set_alarm 保持移除（原 bug）", "set_alarm" not in c["tools"], str(c["tools"]))
    check("alarm+search：web_search/open_url 移除", "web_search" not in c["tools"] and "open_url" not in c["tools"])
    check("alarm+search：get_time 一并移除，避免重复确认时间", "get_time" not in c["tools"])
    check("alarm+search：其他无关工具保留", "read_file" in c["tools"])

    c = await call_chat("待会11点提醒我打游戏")                      # 只 alarm
    check("只 alarm：set_alarm 移除", "set_alarm" not in c["tools"])
    check("只 alarm：get_time 移除", "get_time" not in c["tools"])
    check("只 alarm：search/launch 工具不受影响", "web_search" in c["tools"] and "launch_app" in c["tools"])
    alarm_note = " ".join(m.get("content", "") for m in c["msgs"] if m["role"] == "system")
    check("只 alarm：系统要求简短确认", "只用一到两句" in alarm_note and "不要猜测主人是否熬夜" in alarm_note)

    c = await call_chat("搜一下原神新版本")                          # 只 search
    check("只 search：set_alarm 保留（不误摘）", "set_alarm" in c["tools"])
    check("只 search：web_search/open_url 移除", "web_search" not in c["tools"] and "open_url" not in c["tools"])
    search_note = " ".join(m.get("content", "") for m in c["msgs"] if m["role"] == "system")
    check("只 search：注入搜索摘要", "example.com" in search_note and "F1 赛历正文要点" in search_note)
    check("只 search：核验工具已摘除", "verify_current_fact" not in c["tools"])

    chat_mod.fact_brief = lambda query: ""
    async def _failed_distill(question, materials):
        raise RuntimeError("stub failure")
    chat_mod.distill_web = _failed_distill
    c = await call_chat("搜一下无法读取的网页")
    search_note = " ".join(m.get("content", "") for m in c["msgs"] if m["role"] == "system")
    check("搜索读不到正文时明确降级", "没能读到网页正文" in search_note)
    chat_mod.fact_brief = _real_brief
    chat_mod.distill_web = _real_distill

    # ★设置项 search_open_browser=False：不弹浏览器，但照样读内容讲给主人
    #   验证方式是"恢复真实的 _open_search、只打桩真正开窗的 _open_and_focus"，
    #   这样一旦代码仍去开浏览器，opened 就会记到 URL，断言立刻失败。
    opened: list = []
    _real_open_and_focus = chat_mod._open_and_focus
    _real_load_settings = chat_mod.load_settings
    chat_mod._open_search = _real_open
    chat_mod._open_and_focus = lambda url: opened.append(url)
    chat_mod.load_settings = lambda: {"search_open_browser": False}
    chat_mod.fact_brief = lambda query: "status=verified；example.com：F1 赛历正文要点"
    # 提炼工人在上面的用例末尾已被恢复成真函数——这里必须重新打桩，
    # 否则本段会真去联网提炼（既慢又让断言依赖真实内容）。
    chat_mod.distill_web = _fake_distill

    c = await call_chat("搜一下原神新版本")
    search_note = " ".join(m.get("content", "") for m in c["msgs"] if m["role"] == "system")
    check("关弹窗：确实没开浏览器", opened == [], str(opened))
    check("关弹窗：仍然读内容并注入", "example.com" in search_note and "F1 赛历正文要点" in search_note)
    check("关弹窗：禁止说“搜索页已打开”", "没有打开浏览器" in search_note and "绝不许说" in search_note)
    check("关弹窗：搜索/核验工具仍摘除",
          all(t not in c["tools"] for t in ("web_search", "open_url", "verify_current_fact")))
    # distill_web 必须一起摘：材料已由后端取好注入，留着它模型可能自填"材料"传进工人
    # → 把幻觉喂给工人再当证据用（M2 注册成工具后新增的风险）。
    check("关弹窗：提炼工人工具也摘除（防模型自填材料喂幻觉）",
          "distill_web" not in c["tools"], str(c["tools"]))
    check("关弹窗：搜索读取标记为临时工具结论",
          "verify_current_fact" in c["write_state"].get("tool_trace", []),
          str(c["write_state"]))

    # B站搜 + 关弹窗：既没开浏览器也没有正文可读 → 如实说明，不许装作打开了
    c = await call_chat("B站搜 原神")
    search_note = " ".join(m.get("content", "") for m in c["msgs"] if m["role"] == "system")
    check("关弹窗+B站搜：没开浏览器", opened == [], str(opened))
    check("关弹窗+B站搜：如实说没结果可给", "既没打开浏览器" in search_note and "没读到网页正文" in search_note)

    # 开弹窗时行为不变（回归）
    chat_mod.load_settings = lambda: {"search_open_browser": True}
    c = await call_chat("搜一下原神新版本")
    search_note = " ".join(m.get("content", "") for m in c["msgs"] if m["role"] == "system")
    check("开弹窗：恢复开浏览器", opened and "cn.bing.com" in opened[-1], str(opened))
    check("开弹窗：提示主人看屏幕", "搜索页已打开" in search_note)

    # 设置开关也管模型自己调的 web_search（否则模型仍会弹窗）
    import backend.tools as _tools_mod
    _real_tools_load = _tools_mod.load_settings
    _tools_mod.load_settings = lambda: {"search_open_browser": False}
    _opened2: list = []
    _real_af2 = _tools_mod._open_and_focus
    _tools_mod._open_and_focus = lambda url: _opened2.append(url)
    ws = _tools_mod.tool_web_search("原神新版本")
    check("关弹窗：web_search 工具不弹窗而是引导核验",
          _opened2 == [] and "关掉了" in ws and "verify_current_fact" in ws)
    _tools_mod._open_and_focus = _real_af2
    _tools_mod.load_settings = _real_tools_load

    chat_mod._open_and_focus = _real_open_and_focus
    chat_mod.load_settings = _real_load_settings
    chat_mod.fact_brief = _real_brief
    chat_mod.distill_web = _real_distill

    c = await call_chat("打开原神，待会11点提醒我打游戏")            # alarm+launch 同时
    check("alarm+launch：set_alarm 保持移除", "set_alarm" not in c["tools"])
    check("alarm+launch：launch_app 移除", "launch_app" not in c["tools"])
    check("alarm+launch：搜索工具保留", "web_search" in c["tools"])

    c = await call_chat("打开原神")                                  # 只 launch（部分成功）
    check("只 launch：launch_app 移除", "launch_app" not in c["tools"])
    check("只 launch：set_alarm 保留", "set_alarm" in c["tools"])

    # 全部失败：launched 空、launch_failed 非空 → 也必须注入失败说明 + 禁止本轮重试
    _auto["launched"], _auto["failed"] = [], ["微信"]
    c = await call_chat("打开微信")
    join_notes = " ".join(m.get("content", "") for m in c["msgs"] if m["role"] == "system")
    check("全失败：launch_app 仍移除（禁止瞎重试）", "launch_app" not in c["tools"])
    check("全失败：注入失败说明", "启动失败" in join_notes and "微信" in join_notes)
    check("全失败：要求如实承认、别谎报成功", "别谎报成功" in join_notes and "没打开" in join_notes)
    _auto["launched"], _auto["failed"] = ["原神"], []                # 还原

    c = await call_chat("你好呀")                                    # 无命令
    check("无命令：工具全集保留", c["tools"] == ALL, str(c["tools"]))
    time_idx = next((i for i, message in enumerate(c["msgs"]) if "【现在时间】" in message.get("content", "")), -1)
    first_recent = next((i for i, message in enumerate(c["msgs"]) if message.get("role") != "system"), len(c["msgs"]))
    check("普通聊天在 recent 前注入当前时间", 0 <= time_idx < first_recent)


asyncio.run(run())

print("===== 3. regenerate 加固（恢复/精简工具/审计/取消） =====")
import backend.sessions as sess_mod
import backend.routers.sessions as sess_router

cap = {}


async def drain(resp):
    async for _ in resp.body_iterator:
        pass


# ---- 3.1 失败路径：error → 恢复旧答 + 精简工具 + 注入 note ----
fresh_appdata()
sess_mod.create_session()
sid = sess_mod.get_current()
sess_mod.append_message(sid, "user", "帮我解释 asyncio")
old_a = sess_mod.append_message(sid, "assistant", "异步生成器就是...")[-1]
old_id = old_a["id"]


async def fake_stream_error(sid, msgs, user_text, tools=None, user_msg_id=None,
                            write_requested=False, write_state=None, **kw):
    cap.update(tools=[t["function"]["name"] for t in (tools or [])],
               write_requested=write_requested, write_state=write_state, msgs=msgs)
    yield {"type": "error", "detail": "本喵打游戏去了喵"}


sess_router.stream_answer = fake_stream_error
resp = asyncio.run(sess_router.sessions_regenerate(sid))
asyncio.run(drain(resp))
cur = sess_mod.get_session(sid)["messages"]
check("失败后旧回答带原id恢复", bool(cur) and cur[-1].get("role") == "assistant" and cur[-1].get("id") == old_id, str(cur))
check("regenerate 工具精简：无 set_alarm/append_file/ui_send", not {"set_alarm", "append_file", "ui_send"} & set(cap["tools"]), str(cap["tools"]))
check("regenerate 工具精简：保留 get_time/read_file", {"get_time", "read_file"} <= set(cap["tools"]))
join_sys = " ".join(m.get("content", "") for m in cap["msgs"] if m["role"] == "system")
check("regenerate 注入『只重组织文字』note", "只重新组织文字" in join_sys)
regen_time_idx = next((i for i, message in enumerate(cap["msgs"]) if "【现在时间】" in message.get("content", "")), -1)
regen_recent = next((i for i, message in enumerate(cap["msgs"]) if message.get("role") != "system"), len(cap["msgs"]))
check("regenerate 在 recent 前注入当前时间", 0 <= regen_time_idx < regen_recent)
check("普通问题 write_requested=False", cap["write_requested"] is False and cap["write_state"] is None)

# ---- 3.2 提醒重生成：确认已建提醒，绝不再建 ----
fresh_appdata()
sess_mod.create_session()
sid_alarm = sess_mod.get_current()
sess_mod.append_message(sid_alarm, "user", "待会六点提醒我看仙术杯哦")
sess_mod.append_message(sid_alarm, "assistant", "旧回答")
alarm_request = F("待会六点提醒我看仙术杯哦")
real_list_alarms = sess_router.scheduler.list_alarms
real_add_alarm = sess_router.scheduler.add_alarm
real_find_exact_alarm = sess_router.scheduler.find_exact_alarm
add_calls = []
sess_router.scheduler.list_alarms = lambda: [dict(alarm_request)]
sess_router.scheduler.find_exact_alarm = lambda requested: dict(alarm_request)
sess_router.scheduler.add_alarm = lambda **kw: add_calls.append(kw)
resp = asyncio.run(sess_router.sessions_regenerate(sid_alarm))
asyncio.run(drain(resp))
alarm_sys = " ".join(m.get("content", "") for m in cap["msgs"] if m["role"] == "system")
check("提醒重生成：注入已存在确认", "提醒已经存在" in alarm_sys)
check("提醒重生成：包含准确时间和内容", alarm_request["time"] in alarm_sys and alarm_request["message"] in alarm_sys)
check("提醒重生成：不再建提醒", not add_calls)
check("提醒重生成：set_alarm 仍被禁用", "set_alarm" not in cap["tools"])

# 原回答异常/旧版遗漏时，重新生成应补建一次，而不是空口说已有或推给手机闹钟。
sess_router.scheduler.list_alarms = lambda: []
sess_router.scheduler.find_exact_alarm = lambda requested: None
sess_router.scheduler.add_alarm = lambda **kw: dict(kw)
resp = asyncio.run(sess_router.sessions_regenerate(sid_alarm))
asyncio.run(drain(resp))
created_sys = " ".join(m.get("content", "") for m in cap["msgs"] if m["role"] == "system")
check("提醒重生成：不存在时补建", "已由系统成功创建" in created_sys)
check("提醒重生成：补建字段准确", alarm_request["time"] in created_sys and alarm_request["message"] in created_sys)
sess_router.scheduler.list_alarms = real_list_alarms
sess_router.scheduler.add_alarm = real_add_alarm
sess_router.scheduler.find_exact_alarm = real_find_exact_alarm

# ---- 3.3 重生成写文件回复：不误触“本轮未写”审计 ----
fresh_appdata()
sess_mod.create_session()
sid3 = sess_mod.get_current()
sess_mod.append_message(sid3, "user", "帮我生成报告.docx 放桌面")
sess_mod.append_message(sid3, "assistant", "已生成喵")
resp = asyncio.run(sess_router.sessions_regenerate(sid3))
asyncio.run(drain(resp))
check("写文件回复重生成不启用本轮写入审计", cap["write_requested"] is False and cap["write_state"] is None)

# ---- 3.4 成功路径：done → 不触发恢复 ----
fresh_appdata()
sess_mod.create_session()
sid2 = sess_mod.get_current()
sess_mod.append_message(sid2, "user", "问题")
old_b = sess_mod.append_message(sid2, "assistant", "旧回答")[-1]


async def fake_stream_done(sid, msgs, user_text, tools=None, user_msg_id=None, **kw):
    cap.update(tools=[t["function"]["name"] for t in (tools or [])])
    yield {"type": "done", "message_ids": {"assistant": "new-id"}}


sess_router.stream_answer = fake_stream_done
resp = asyncio.run(sess_router.sessions_regenerate(sid2))
asyncio.run(drain(resp))
cur2 = sess_mod.get_session(sid2)["messages"]
check("成功路径不恢复旧答", all(m.get("id") != old_b["id"] for m in cur2), str(cur2))
check("成功路径工具同样精简", not {"set_alarm", "append_file", "ui_send"} & set(cap["tools"]))

# ---- 3.5 取消路径：BaseException → 也恢复旧答（第二十点） ----
fresh_appdata()
sess_mod.create_session()
sid4 = sess_mod.get_current()
sess_mod.append_message(sid4, "user", "问题")
old_c = sess_mod.append_message(sid4, "assistant", "旧回答取消场景")[-1]


async def fake_stream_cancel(sid, msgs, user_text, tools=None, user_msg_id=None, **kw):
    yield {"type": "delta", "text": "开头"}   # 先吐一个字，再被"取消"（模拟客户端断开）
    raise asyncio.CancelledError()


async def drain_cancel(resp):
    try:
        async for _ in resp.body_iterator:
            pass
    except asyncio.CancelledError:
        pass


sess_router.stream_answer = fake_stream_cancel
resp = asyncio.run(sess_router.sessions_regenerate(sid4))
asyncio.run(drain_cancel(resp))
cur4 = sess_mod.get_session(sid4)["messages"]
check("取消后旧回答也恢复", bool(cur4) and cur4[-1].get("role") == "assistant" and cur4[-1].get("id") == old_c["id"], str(cur4))

# ---- 3.6 restore_assistant 幂等 ----
sess_mod.restore_assistant(sid4, {"id": "dup", "role": "assistant", "content": "x"})
sess_mod.restore_assistant(sid4, {"id": "dup", "role": "assistant", "content": "x"})
dup_cnt = sum(1 for m in sess_mod.get_session(sid4)["messages"] if m.get("id") == "dup")
check("restore_assistant 幂等（不重复）", dup_cnt == 1, str(dup_cnt))
sess_router.stream_answer = None  # 清理 mock 引用

# 恢复打桩
chat_mod._open_search = _real_open
chat_mod.fact_brief = _real_brief
chat_mod.tool_verify_current_fact = _real_verify_fact
chat_mod.distill_web = _real_distill
chat_mod._auto_launch_apps = _real_auto
chat_mod.scheduler.add_alarm = _real_add
chat_mod.current_time_hint = _real_time_hint

for d in _tmp_dirs:
    shutil.rmtree(d, ignore_errors=True)
print("ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)

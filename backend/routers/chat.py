"""聊天 + 联网搜索路由（从 main.py 拆出，2026-08-16）。"""
import json
import re
import urllib.parse
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..chat_service import _sse, build_messages, current_time_hint, greeting_hint, stream_answer
from ..config import settings
from ..scheduler import scheduler
from ..settings import load_settings
from ..schemas import ChatRequest
from ..sessions import (
    append_message as session_append,
    find_alarm_batches_for_request,
    find_recent_media_for_alarm_request,
    get_current,
    get_session,
    mark_alarm_batch_applied,
    pin_latest_reference,
    record_recent_media_ref,
    recent_messages,
    set_pending_alarm_intent,
)
from ..tools import (
    TOOL_SCHEMAS,
    _fuzzy_app_match,
    _load_apps,
    _open_and_focus,
    fact_brief,
    tool_verify_current_fact,
    run_tool,
    tool_check_system,
    tool_web_search,
)
from ..agents import build_materials, distill_web, render_distill, stale_notice

router = APIRouter(prefix="/api")


def _annotate_recent_dates(recent: list[dict]) -> list[dict]:
    """给模型用的跨日历史加日期标记，不修改落盘会话里的原消息。"""
    annotated: list[dict] = []
    previous_date = None
    for message in recent:
        copied = dict(message)
        ts = copied.get("ts")
        if isinstance(ts, (int, float)):
            moment = datetime.fromtimestamp(ts)
            date_key = moment.date()
            if previous_date is not None and date_key != previous_date:
                copied["content"] = f"（{moment.month}月{moment.day}日 {moment:%H:%M}）" + (copied.get("content") or "")
            previous_date = date_key
        annotated.append(copied)
    return annotated

# ---- 联网搜索：显式命令的确定性预处理 ----
# 用户直接说「搜/搜索/查/联网搜」时，后端主动开浏览器，不依赖模型自觉调工具
# （模型曾空口说"已打开搜索页"却没调用工具，导致浏览器没弹）。
_SEARCH_VERB = re.compile(
    r"^(?:你|帮我|给我|麻烦|请|去)?"
    r"(?:联网搜索|上网搜|网上搜|B站搜|必应搜|帮我搜|给我搜|搜一下|搜索|搜|查一下|帮我查|查查|查)(?!狗)"
    r"[:：\s]*([^。！？\n]{1,50})"
)
_BILI_RE = re.compile(r"B站|bilibili", re.IGNORECASE)

# 只接管带指代对象的“记住/保留”请求；泛泛的“你记住了吗”仍交给正常对话，
# 避免误把无关的最近文件线索永久钉住。
_PIN_REFERENCE_RE = re.compile(
    r"(?:记住|保留|别忘|长期保留).{0,12}(?:这个|这条|它|路径|文件|赛程|内容|信息)"
)


def _extract_pin_reference_request(text: str) -> bool:
    return bool(_PIN_REFERENCE_RE.search((text or "").strip()))


# ---- 图片赛程提醒：前置意图 / 最近原料回溯（M2） ----
_MEDIA_PATH_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:[\\/]|/)[^\n。！？]*?\.(?:png|jpe?g|gif|webp|bmp|pdf|docx|xlsx|txt))",
    re.IGNORECASE,
)
_ALARM_MEDIA_INTENT_RE = re.compile(
    r"(?:提醒|闹钟|赛程).{0,18}(?:待会|等会|下一张|接下来).{0,12}(?:图片|图|文件)"
    r"|(?:待会|等会|下一张|接下来).{0,12}(?:图片|图|文件).{0,18}(?:提醒|闹钟|赛程)"
)


def _extract_media_refs(text: str) -> list[tuple[str, str]]:
    """提取前端上传消息中的媒体绝对路径；不把普通聊天里的词误作原料。"""
    refs = []
    for match in _MEDIA_PATH_RE.finditer(text or ""):
        path = match.group("path").strip()
        suffix = path.rsplit(".", 1)[-1].lower()
        refs.append((path, "image" if suffix in {"png", "jpg", "jpeg", "gif", "webp", "bmp"} else "file"))
    return refs


def _extract_alarm_media_intent(text: str) -> bool:
    return bool(_ALARM_MEDIA_INTENT_RE.search((text or "").strip()))

# ---- 定点提醒：显式命令的确定性兜底 ----
# 主人说「X点/十点半 提醒我做事」时，后端直接建提醒，不依赖模型自觉调 set_alarm
# （模型曾空口说"已设置提醒"却没调工具，提醒从没建过，这里兜底同"搜大狗叫"的教训）。
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_WEEK_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_PM_PERIODS = ("下午", "傍晚", "晚上", "今晚", "夜里", "深夜")
_ALARM_VERB = r"(?:提醒(?:我|主人)?|叫我|喊我|叫醒我)"
# 时间在前：十点半提醒我做事 / 每天8点提醒我喝水 / 晚上11点提醒我睡觉
_ALARM_RE_A = re.compile(
    r"(?P<repeat>每天|每早|每晚|每周[一二三四五六日天])?"
    r"(?P<period>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|今晚|夜里|深夜)?"
    r"(?P<soon>待会|等会|一会儿|等下|稍后|过会|过会儿|呆会)?"
    r"(?P<hour>\d{1,2}|[一二两三四五六七八九十]{1,2})"
    r"(?:点\s*(?:(?P<half>半)|(?P<min>\d{1,2}|[一二两三四五六七八九十]{1,2})(?:分)?)?"
    r"|[:：](?P<min2>\d{1,2}))"
    r"\s*" + _ALARM_VERB + r"\s*(?P<msg>[^，。！？\n]{1,20})"
)
# 时间在后：提醒我十点半做事 / 叫我8点起床
_ALARM_RE_B = re.compile(
    _ALARM_VERB + r"\s*"
    r"(?P<period>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|今晚|夜里|深夜)?"
    r"(?P<soon>待会|等会|一会儿|等下|稍后|过会|过会儿|呆会)?"
    r"(?P<hour>\d{1,2}|[一二两三四五六七八九十]{1,2})"
    r"(?:点\s*(?:(?P<half>半)|(?P<min>\d{1,2}|[一二两三四五六七八九十]{1,2})(?:分)?)?"
    r"|[:：](?P<min2>\d{1,2}))"
    r"\s*(?P<msg>[^，。！？\n]{1,20})"
)

# 提醒误创建防护（"不要在晚上八点提醒我"别真的建闹钟）：提醒动词之前若含真否定词 → 不建。
# 但"别忘了/不要忘了"是肯定句（"别忘了提醒我八点开会"= 要提醒），必须豁免。
_ALARM_NEG_BEFORE_RE = re.compile(r"不要|不用|不需要|不想|取消|关掉|不用了|莫要|不必|勿|别")
_ALARM_NEG_IGNORE_RE = re.compile(r"别忘|不要忘|别忘了|不要忘了")


def _cn_to_int(s: str) -> int | None:
    """中文数字/阿拉伯数字转整数（0-59 够用）：一→1 十→10 十二→12 二十→20 二十五→25。"""
    s = (s or "").replace("两", "二")
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if all(c in _CN_DIGITS for c in s):
        if s == "十":
            return 10
        if s.startswith("十"):
            return 10 + _CN_DIGITS[s[1]]
        if s.endswith("十"):
            return _CN_DIGITS[s[0]] * 10
        if "十" in s:
            hi, _, lo = s.partition("十")
            return _CN_DIGITS.get(hi, 0) * 10 + _CN_DIGITS.get(lo, 0)
        if len(s) == 1:
            return _CN_DIGITS[s]
    return None


def _resolve_hour(hour: int, period: str, now_hour: int | None = None, soon: bool = False) -> int:
    """12 小时制时段换算：下午/晚上/深夜 1-11 点 +12；凌晨 12 点 → 0。

    裸小时（没写凌晨/下午等字眼）分两种情况：
    - 带「待会/等会/一会儿/稍后」等 soon 词：理解为「当前时刻之后最近的该点钟」——
      晚上 22 点说『待会十一点』= 今天 23:00（不是早上 11 点然后推明天）；
      上午 10 点说『待会11点』= 今天 11:00。先试当天原值，不行试 +12 的下午/晚上，
      都过了才保持原值（留给外面"已过推明天"的兜底）。
    - 不带 soon：1~6 点默认按下午（X+12）理解，除非现在真在凌晨
      （下午 3 点说"待会4点" → 16:00，而不是凌晨 04:00——24 小时制裸小时太容易读成凌晨）。
    """
    if hour == 12:
        return 0 if period == "凌晨" else 12
    if period in _PM_PERIODS:
        return hour + 12
    if not period and soon and now_hour is not None:
        if hour > now_hour:
            return hour                    # 当天还没到这个点（如上午说待会11点）
        if hour + 12 <= 23 and hour + 12 > now_hour:
            return hour + 12               # 当天原值已过 → 下午/晚上版本（如晚上说待会11点=23点）
        return hour                        # +12 也过了（深夜）→ 保持原值，交给"已过推明天"
    if not period and now_hour is not None and hour <= 6 and now_hour >= hour:
        return hour + 12
    return hour


def _clean_remind_msg(msg: str) -> str:
    """清洗提醒内容：去掉『我/你/主人』前缀和语气词尾缀。

    正则里「提醒我」的『我』常被当成提醒内容捕获（如『23点15分提醒我』→ 内容『我』），
    清洗后变空 → 用兜底内容。避免闹钟到点弹个「⏰ 我」这种鬼话。
    """
    msg = (msg or "").strip()
    if not msg:
        return ""
    msg = re.sub(r"^(?:我|你|主人|人家|宝宝)(?:一下)?", "", msg).strip()
    msg = msg.strip("吧啊呀哦哟喵呢哈嘛的了~～！!。，,、")
    return msg


def _extract_date(text: str) -> str | None:
    """从文本里识别具体日期，返回 'YYYY-MM-DD'；识别不到返回 None。

    支持：明天/明日、后天/后日、N天后、大后天、下周X、本周X（今天提到本周X→下周）、
    8月21日（今年，已过则明年）、2026年8月21日。"每周三"是循环不会当日期（(?<!每) 排除）。
    """
    now = datetime.now()
    if re.search(r"明天|明日", text):
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    if re.search(r"大后天", text):
        return (now + timedelta(days=3)).strftime("%Y-%m-%d")   # 必须先判大后天，否则被"后天"命中
    if re.search(r"后天|后日", text):
        return (now + timedelta(days=2)).strftime("%Y-%m-%d")
    m = re.search(r"(\d{1,3})天后", text)
    if m:
        return (now + timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"下个?(?:周|星期)([一二三四五六日天])", text)
    if m:
        py = _WEEK_CN[m.group(1)] - 1             # 1=周一→0 … 7=周日→6（Python weekday）
        days = (py - now.weekday()) % 7
        if days == 0:
            days = 7                              # 今天就是这天 → 下一周
        return (now + timedelta(days=days)).strftime("%Y-%m-%d")
    m = re.search(r"(?<!每)(?:本周|这个周|这个星期)?(?:周|星期)([一二三四五六日天])", text)
    if m:
        py = _WEEK_CN[m.group(1)] - 1             # 1=周一→0 … 7=周日→6（Python weekday）
        days = (py - now.weekday()) % 7
        if days == 0:
            days = 7                              # 今天就是这天 → 下一周
        return (now + timedelta(days=days)).strftime("%Y-%m-%d")
    m = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日?", text)
    if m:
        year = int(m.group(1)) if m.group(1) else now.year
        try:
            d = datetime(year, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        if d.date() < now.date():                 # 已过 → 明年
            d = d.replace(year=d.year + 1)
        return d.strftime("%Y-%m-%d")
    return None


def _extract_alarm_request(text: str) -> dict | None:
    """检测显式定时提醒请求；返回 {"time","message","repeat","weekdays","date"} 或 None。

    提醒内容清洗后为空（如只说了『提醒我』没给内容）→ 用兜底文案，保证闹钟一定建得上。
    date 仅一次性（repeat=once）会填：明确日期（明天/后天/8月21日…）直接定；
    没写日期按"今天该时刻，已过推到明天"兜底——下午说"待会4点"= 今天 16:00。
    """
    t = (text or "").strip()
    if not t:
        return None
    m = _ALARM_RE_A.search(t) or _ALARM_RE_B.search(t)
    if not m:
        return None
    # 否定检测：提醒动词之前若出现真否定词（不要/别/取消…）→ 这是"不要提醒"不是"要提醒"，不建。
    # 豁免"别忘了/不要忘了"（那是肯定句：别忘了提醒我 = 要提醒）。
    before = _ALARM_NEG_IGNORE_RE.sub("", t[: m.start()])
    if _ALARM_NEG_BEFORE_RE.search(before):
        return None
    g = m.groupdict()
    hour = _cn_to_int(g.get("hour") or "")
    if hour is None or not 0 <= hour <= 23:
        return None
    if g.get("min2") is not None:
        minute = _cn_to_int(g["min2"])
    elif g.get("half"):
        minute = 30
    elif g.get("min") is not None:
        minute = _cn_to_int(g["min"])
    else:
        minute = 0
    if minute is None or not 0 <= minute <= 59:
        return None
    now = datetime.now()
    hour = _resolve_hour(hour, g.get("period") or "", now.hour, soon=bool(g.get("soon")))
    message = _clean_remind_msg(g.get("msg") or "")
    if not message:
        message = "主人设置的提醒"
    repeat, weekdays = "once", []
    rp = g.get("repeat") or ""
    if rp.startswith("每周") and len(rp) == 3 and rp[2] in _WEEK_CN:
        repeat, weekdays = "weekly", [_WEEK_CN[rp[2]]]
    elif rp in ("每天", "每早", "每晚"):
        repeat = "daily"
    date = None
    if repeat == "once":
        # 日期只在「时间表达区」里找——必须把提醒内容（msg 组）那段摘掉再扫。
        # 否则「待会四点十分提醒我打明日方舟」里的「明日」（游戏名）会被当成"明天"，
        # 把当天 16:10 的提醒建到第二天。
        date = _extract_date(t[: m.start("msg")] + t[m.end("msg"):])
        if not date:
            # 没写明确日期：今天该时刻，已过就推到明天
            dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
            date = dt.strftime("%Y-%m-%d")
    return {"time": f"{hour:02d}:{minute:02d}", "message": message,
            "repeat": repeat, "weekdays": weekdays, "date": date}


# ---- 系统状态/内存排查：显式命令的确定性预处理 ----
# 「看看内存/卡不卡/为什么卡」这类排查请求，后端直接调 check_system 拿真实数据，
# 不依赖模型自觉调工具（模型空口报数字老毛病的兜底，同搜索/提醒模式）。
_SYS_CHECK_RE = re.compile(
    # 排查动词 + 内存/卡顿相关词（「卡」不收裸卡：防「看看这张卡/卡里没钱」误判）
    r"(?:看看|看下|查查|查一下|查下|检查|帮我看看|看一下|问下|问一下|说下|讲讲|告诉我)"
    r".{0,8}?(?:内存|虚拟内存|页面文件|卡不卡|卡顿|卡死|系统状态|占用)"
    r"|(?:为什么|为啥|咋|怎么|是不是|会不会).{0,10}?"
    r"(?:卡顿|卡死|卡爆|好卡|很卡|这么卡|那么卡|有点卡|卡的慌|卡得慌|老是卡|一直卡|突然卡|卡卡的?|卡了|卡壳|卡$)"
    r"|(?:内存|虚拟内存|页面文件).{0,6}?(?:多少|够不够|够吗|不够|不够用|满没满|满了吗|爆|爆了|占用|高不高|高吗|多少了)"
    r"|(?:现在|本机|电脑).{0,4}(?:内存|虚拟内存|页面文件)"
    r"|^(?:卡不卡|现在卡不卡|卡不卡啊)[？?]?$"
)
# 明确否定/夸流畅：不算排查请求，别误触发（不含「不卡啊」——它是「卡不卡啊」的子串）
_SYS_CHECK_NEG = re.compile(r"(?:一点都不卡|不卡了|没卡|不卡不卡|很流畅|很丝滑|流畅的|不卡顿)")


def _extract_system_check_request(text: str) -> bool:
    """检测显式「看看内存/卡不卡/为什么卡」排查命令；否定的不误判。"""
    t = (text or "").strip()
    if not t:
        return False
    if _SYS_CHECK_NEG.search(t):
        return False
    return bool(_SYS_CHECK_RE.search(t))


def _extract_search_request(text: str):
    """检测显式搜索命令，返回 (平台, 关键词)；不是搜索命令返回 None。

    平台：'bing' | 'bilibili'。搜索动词必须在消息开头（可带 你/帮我/请 等前缀），
    「搜狗输入法」「我搜索了很久」这类不误判（(?!狗) 排除品牌 + ^ 锚定开头）。
    """
    m = _SEARCH_VERB.search((text or "").strip())
    if not m:
        return None
    q = m.group(1).strip().strip(" ,，。!！?？;；:：\"'“”")
    if not q:
        return None
    platform = "bilibili" if _BILI_RE.search(text) else "bing"
    return platform, q


# ---- 写文件/生成文档：意图检测（配合流式 agent 的写文件审计）----
# 主人明确要「生成/写一份文档放到某处」时，若猫娘空口说"已生成/已放到"却一个写文件
# 工具都没调，chat_service 流结束会追加诚实更正（模型空口声称老毛病的兜底）。
_WRITE_VERBS = ("新写", "重新生成", "重新写", "生成", "新建", "创建", "写", "做", "整理成")
_WRITE_NOUNS = ("文档", "文件", "word", "excel", "ppt", "演示文稿", "表格", "txt", "md", "docx",
                "心得", "总结", "报告", "笔记", "方案", "论文")
_WRITE_VERB_RE = re.compile(
    r"(?:新写|重新生成|重新写|生成|新建|创建|写|做|整理成)"
    r".{0,4}(?:文档|文件|word|excel|ppt|演示文稿|表格|txt|md|docx|心得|总结|报告|笔记|方案|论文)", re.I)
_WRITE_PLACE_RE = re.compile(
    r"(?:文档|文件|word|excel|ppt|表格).{0,6}(?:放|存|保存|发到|放到|写到).{0,8}(?:桌面|文件夹|目录|路径)", re.I)


def _extract_write_request(text: str) -> bool:
    """检测主人是否明确要求「生成/写一个文档/文件」；返回 True/False。

    宽松匹配（宁可误判不可漏判）：命中意图后还要配合「本轮没真调写文件工具 + 回复
    含声称完成词」才触发纠正，单点误判不会产生错误行为。
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    if _WRITE_VERB_RE.search(t):
        return True
    if any(v in t for v in _WRITE_VERBS) and any(n in t for n in _WRITE_NOUNS):
        return True
    if any(n in t for n in _WRITE_NOUNS) and _WRITE_PLACE_RE.search(t):
        return True
    return False


# ---- 打开应用：显式命令的确定性兜底 ----
# 主人说「打开X」「打开X和Y」时，后端直接 launch_app 逐个打开，不依赖模型自觉调工具
# （模型曾空口说"已打开"却没调工具，主人要的两个应用一个都没开——同"搜大狗叫"的教训）。
_OPEN_VERB_RE = re.compile(
    r"^(?:你|帮我|给我|麻烦|请|去)?(?:打开|启动|开启)(?:一下|一个|个|下)?\s*(?P<names>[^。！？\n]{1,40})"
)
_OPEN_SPLIT_RE = re.compile(r"以及|还有|和|与|或|跟|、|,|，|再(?:打开|开)?|然后|顺便|给")
_TRAIL_CHARS = " ，,。！!？?;；:：'\"“”吧嘛呀哦啊嗯了哈~～"
# 明显不是应用的 token：带文件扩展名、含路径分隔符（那是文件/路径，交给 open_path/文档工具）
_OPEN_SKIP_RE = re.compile(r"[.。]\w{1,6}$|[\\/]")


def _extract_launch_request(text: str) -> list[str] | None:
    """检测显式「打开应用」命令，返回解析出的候选应用名列表；不是则返回 None。

    只认消息开头的「打开/启动/开启」；多个应用用 和/、/,/，/还有 分隔。
    带文件扩展名或路径的 token 跳过（那是文件不是应用）。
    """
    m = _OPEN_VERB_RE.search((text or "").strip())
    if not m:
        return None
    raw = m.group("names").strip().strip(_TRAIL_CHARS)
    if not raw:
        return None
    names = [n.strip().strip(_TRAIL_CHARS) for n in _OPEN_SPLIT_RE.split(raw)
             if n.strip().strip(_TRAIL_CHARS)]
    out = []
    for n in names:
        if _OPEN_SKIP_RE.search(n) or len(n) > 20:
            continue
        out.append(n)
    return out or None


def _auto_launch_apps(names: list[str]) -> tuple[list[str], list[str]]:
    """确定性启动应用：应用表里有匹配的名字才真调 launch_app。返回（成功，失败）。

    匹配不上的名字跳过不动（让模型/launch_app 自己处理，不硬猜）；
    启动失败的记进失败列表，让系统提示词如实转告主人。
    """
    apps_map = _load_apps()
    launched: list[str] = []
    failed: list[str] = []
    for n in names:
        if not _fuzzy_app_match(apps_map, n):
            continue
        res = run_tool("launch_app", {"name": n})
        if "已经帮你" in res:
            launched.append(n)
        else:
            failed.append(n)
    return launched, failed


def _open_search(platform: str, query: str) -> str | None:
    """打开搜索页并尽量置前；成功返回说明文本，失败返回 None。"""
    if platform == "bilibili":
        url = "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(query)
    else:
        url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query)
    try:
        _open_and_focus(url)
        return f"已为「{query}」打开{platform}搜索页"
    except Exception:
        return None


@router.get("/chat")
def read_root():
    return {"status": "ok", "model": settings.deepseek_model}


@router.post("/chat_response")
async def chat(request: ChatRequest) -> StreamingResponse:
    if not request.chatmassage.strip():
        raise HTTPException(status_code=400, detail="本喵没听见喵,罚你重说一遍喵~")
    # 目标会话：请求带了就用，否则用当前会话（会话不存在时兜底到当前）
    sid = request.session_id or get_current()
    if not get_session(sid):
        sid = get_current()
    # 先把用户的话记进会话历史（窗口内多轮 + 跨窗口续聊都靠它）
    user_msgs = session_append(sid, "user", request.chatmassage)
    pinned_reference = None
    if _extract_pin_reference_request(request.chatmassage):
        pinned_reference = pin_latest_reference(sid)
    # 主人先说明“待会发图按图设提醒”时仅挂一次性意图；下一次上传的原料
    # 消费它并被会话持久化。这里绝不创建提醒，仍要主人之后明确点名。
    if _extract_alarm_media_intent(request.chatmassage):
        set_pending_alarm_intent(sid, request.chatmassage)
    media_refs = [record_recent_media_ref(sid, path, kind)
                  for path, kind in _extract_media_refs(request.chatmassage)]
    intent_media = next((ref for ref in reversed(media_refs) if ref and ref.get("intent")), None)
    # 按 token 预算从最新往前取历史（最新一条=本轮提问必然包含），比旧按条数截断更省窗口
    recent = _annotate_recent_dates(recent_messages(user_msgs, for_model=True))
    # 赛程图片/文件已在前一轮被结构化暂存后，主人可只说“设置西班牙”；此处按
    # 会话批次确定性创建，避免把自然语言上下文再次交给模型拼工具参数。
    alarm_batches = find_alarm_batches_for_request(sid, request.chatmassage)
    media_backfill = None if alarm_batches else find_recent_media_for_alarm_request(sid, request.chatmassage)
    batch_created: list[dict] = []
    batch_errors: list[dict] = []
    for batch in alarm_batches:
        created, errors = scheduler.add_alarms_batch(batch.get("alarms") or [])
        batch_created.extend(created)
        batch_errors.extend(errors)
        mark_alarm_batch_applied(sid, batch["id"], [a["id"] for a in created])
    # 联网搜索：显式「搜/搜索/查」命令 → 确定性直接开浏览器，不依赖模型自觉调工具
    # （模型曾空口说"已打开搜索页"却没调用工具导致浏览器没弹，这里兜底）
    # 设置项 search_open_browser=False 时不开浏览器，但**照样读内容**（见下面的搜索分支）
    opened_search = False
    search_req = _extract_search_request(request.chatmassage)
    prefer_browser = bool(load_settings().get("search_open_browser", True))
    if search_req:
        _platform, _query = search_req
        if prefer_browser:
            opened_search = _open_search(_platform, _query) is not None
    # 定点提醒：显式「X点/十点半 提醒我做事」→ 后端确定性建提醒，不依赖模型自觉调
    # set_alarm（防止模型空口说"已设置"却没调工具，提醒从没建过）
    alarm_req = _extract_alarm_request(request.chatmassage)
    alarm_created = None
    alarm_existed = False
    if alarm_req:
        try:
            # "已经有了"只能建立在当前未触发提醒的完整字段相等上；昨天已触发并
            # 移除的 once 提醒日期不同/不存在，今天仍要新建。
            finder = getattr(scheduler, "find_exact_alarm", lambda _alarm: None)
            alarm_created = finder(alarm_req)
            alarm_existed = alarm_created is not None
            if not alarm_created:
                alarm_created = scheduler.add_alarm(**alarm_req)
        except Exception:
            alarm_created = None
            alarm_existed = False
    # 打开应用：显式「打开X」「打开X和Y」→ 后端确定性逐个 launch_app，不依赖模型自觉调
    # （模型曾空口说"已打开"却没调工具，两个应用一个都没开）
    launch_req = _extract_launch_request(request.chatmassage)
    launched: list[str] = []
    launch_failed: list[str] = []
    if launch_req:
        launched, launch_failed = _auto_launch_apps(launch_req)
    # 系统状态/内存排查：显式「看看内存/卡不卡」→ 后端确定性查真实数据，
    # 不依赖模型自觉调 check_system（防止模型空口编内存数字，同搜索/提醒兜底模式）
    sys_status = None
    if _extract_system_check_request(request.chatmassage):
        try:
            sys_status = tool_check_system()
        except Exception:
            sys_status = None
    msgs = build_messages(sid, recent)
    tools = list(TOOL_SCHEMAS)
    disabled_tools: set[str] = set()
    if pinned_reference:
        msgs.insert(1, {"role": "system", "content": (
            "【系统通知，必须服从】主人刚才明确要求长期保留上一条可复用线索。"
            "系统已将其设为长期记忆；简短确认即可，不要编造或重复执行任何操作。"
        )})
    if intent_media:
        msgs.insert(1, {"role": "system", "content": (
            "【系统通知，必须服从】主人此前明确说这次上传的图片/文件用于后续设置提醒，"
            f"原料路径是「{intent_media['path']}」。现在只能读取并核对赛程，"
            "识别出完整条目后必须调用 stage_alarm_batch 暂存；尚未获得本次创建授权，"
            "绝不调用 set_alarm 或 set_alarms_batch，也绝不声称已设好。"
        )})
        disabled_tools.update({"set_alarm", "set_alarms_batch"})
    if media_backfill:
        has_image_reader = any(t.get("function", {}).get("name") == "read_image" for t in tools)
        if has_image_reader:
            instruction = (
                "请先用 read_image 读取原料，再从结果中核对日期、时间和事项；"
                "主人本轮已明确授权创建，必须调用 set_alarms_batch 落库。"
            )
        else:
            instruction = "当前没有可读取该图片/文件的工具，不能猜测赛程或声称已创建。"
        msgs.insert(1, {"role": "system", "content": (
            "【系统通知，必须服从】主人点名要按刚才上传的原料创建提醒，但没有可用的结构化暂存批次。"
            f"可回溯原料路径是「{media_backfill['path']}」。{instruction}"
            "只按工具真实返回汇报，不能把旧聊天文字当作已创建事实。"
        )})
    if alarm_batches:
        batch_labels = "、".join(str(b.get("label") or "").strip() for b in alarm_batches)
        existed = sum(1 for error in batch_errors if error.get("error") == "already_exists")
        failed = [error for error in batch_errors if error.get("error") != "already_exists"]
        details = []
        for alarm in batch_created:
            details.append(f"⏰ {alarm.get('date') or ''} {alarm['time']}「{alarm['message']}」".strip())
        if existed:
            details.append(f"已有 {existed} 条完全相同的提醒，未重复创建")
        if failed:
            details.append(f"{len(failed)} 条未创建：" + "；".join(str(e.get("error")) for e in failed))
        result = "；".join(details) or "没有可创建的提醒"
        msgs.insert(1, {"role": "system", "content": (
            f"【系统通知，必须服从】主人刚才确认创建暂存赛程「{batch_labels}」。"
            f"系统已真实处理：新建 {len(batch_created)} 条，已存在 {existed} 条。结果：{result}。"
            "本轮只简短如实确认上述结果；绝不声称未列出的提醒已建好，绝不再调用 set_alarms_batch。"
        )})
        disabled_tools.add("set_alarms_batch")
    if alarm_created:
        # 提醒时间已由后端按系统时钟确定并成功落盘。模型只负责简短确认，
        # 不应再猜主人作息、声称需要确认时间，或复述后台的解析过程。
        when = f"{alarm_created['date']} " if alarm_created.get("date") else ""
        status = "已经存在" if alarm_existed else "刚刚由系统成功创建"
        wording = (
            "本轮只用一到两句猫娘语气确认这条已经存在的提醒即可。"
            "不要说它是刚设置的，也不要重复创建。"
            if alarm_existed else
            "本轮只用一到两句猫娘语气确认刚刚创建的结果。"
            "绝不能说『早就设好』『之前已经有了』『主人忘了』或暗示它在本轮前存在。"
        )
        msgs.insert(1, {
            "role": "system",
            "content": (
                f"【系统通知，必须服从】主人刚才要求的定时提醒{status}："
                f"⏰ {when}{alarm_created['time']} 提醒主人「{alarm_created['message']}」。"
                f"时间已经由系统时钟解析并确认，你不需要也不允许再调用 get_time、询问或声称要确认时间。"
                f"{wording}"
                f"不要猜测主人是否熬夜、困不困、在做什么；不要复述解析过程；不要扩写成多段聊天。"
                f"绝不要说自己没有提醒功能、工具没加载，或提议用手机闹钟代替。"
                f"如果主人同一条消息还有其他明确请求，再简短处理那些请求即可。"
            ),
        })
        disabled_tools.update({"set_alarm", "get_time"})
    # 搜索命令已接管：开了浏览器，或主人关掉了弹窗（关掉时照样读内容，只是不给看页面）
    if search_req and (opened_search or not prefer_browser):
        # B站搜只开页面、没有正文可读；必应搜才读内容
        digest = ""
        if _platform == "bing":
            try:
                report = json.loads(tool_verify_current_fact(_query, max_sources=4))
                # 材料要给"给人读"的紧凑格式：直接把 JSON 报告喂给工人会因噪音
                # 喂胖思考量（实测材料 −48%、思考 −49%、耗时 46.4s→29.0s，且只有紧凑版能解析成功）
                distilled = await distill_web(_query, build_materials(report))
                # 全是旧稿时带上一句"没找到近期来源"，让主猫娘能如实交代而不是拿旧料当现状
                digest = render_distill(distilled, notice=stale_notice(report))
            except Exception:
                # 子 agent 失败时保留原有可靠降级，不让搜索请求整体失败。
                digest = fact_brief(_query)
        if opened_search:
            content = f"（联网搜索已自动完成：本喵已为「{_query}」打开了浏览器搜索页，主人能看到。）"
            tell = "现在既告诉主人“搜索页已打开，注意看屏幕喵”，也把上面读到的要点用猫娘语气讲给主人听。"
        else:
            content = (
                f"（联网搜索已自动完成：主人关掉了「搜索时弹出浏览器」，本次**没有打开浏览器**。）"
                "**绝不许说“搜索页已打开”**（主人屏幕上什么都没有）。"
            )
            tell = "把上面读到的要点用猫娘语气讲给主人听。"
        if digest:
            content += (
                "\n【系统已替你联网读到的内容，必须据此回答】\n" + digest +
                "\n" + tell +
                "不许编造上面没写的内容，不许声称读过上面没列的页面。"
                "**时效性**：上面若出现「⚠️ 未找到近期来源」，必须如实告诉主人"
                "「没找到近期资料、这类安排可能已经变了，以官方公告为准」，"
                "绝不许把旧稿里的安排当现状讲；标了「旧稿」的要点也要说明它是旧说法。"
                "**若上面有「完整列表」**：必须把它作为**一份完整的清单念给主人**——"
                "保持原有顺序、**不许只挑前几站讲、不许按来源拆成几段**。"
                "**呈现规矩（观感优先，务必遵守）**："
                "① **列表放最前面**，冲突/缺口/时效这些说明**统一放到列表末尾的一小段**里，"
                "绝不要把一大段警告铺在列表前面；"
                "② 列表**一行一站，只写「站名 + 日期 + 赛道/城市 +（是否冲刺赛）」**；"
                "来源域名、抓取时间、「（材料未覆盖）」「⚠️来源不一致」这类**内部标注不要挂进列表行**"
                "（有争议的站在行末标一个 ⚠️ 就够，解释放末尾说明）；"
                "③ 末尾说明只讲**最关键的 2~4 条**（哪个站几份材料对不上、哪些站官方没覆盖），"
                "**别把内部证据的原文措辞整段端给主人**；"
                "④ 长度克制：整条回复控制在读者扫一眼能看完的量级；"
                "⑤ **不要用 `#` 标题**（前端不渲染），用小标题就用**加粗短句**。"
            )
        elif opened_search:
            content += (
                "本次没能读到网页正文（只有搜索结果页或读取失败），如实告诉主人没读到内容，绝不许编内容；"
                "用猫娘语气说“搜索页已打开，注意看屏幕喵”即可。"
            )
        else:
            content += (
                "本次既没打开浏览器（主人关了弹窗）也没读到网页正文，所以主人屏幕上什么都不会出现；"
                "如实告诉主人这次没能给出结果，绝不许编内容，也别装作打开了页面。"
            )
        content += "不要再开浏览器（本轮已无 web_search / open_url / verify_current_fact 工具）。"
        msgs.insert(1, {
            "role": "system",
            "content": content,
        })
        disabled_tools.update({"web_search", "open_url", "verify_current_fact"})
    if launched or launch_failed:
        # 告诉模型应用已确定性尝试过（成功/失败都如实告知，防全失败时模型瞎报成功或瞎重试）；
        # 摘掉 launch_app 防止本轮重复调（只在实际发起了启动尝试时才接管，匹配不到应用的留给模型自己调）
        note = "（本地操作已执行："
        if launched:
            note += f"本喵已用 launch_app 打开：{'、'.join(launched)}。"
        if launch_failed:
            note += f"但『{'、'.join(launch_failed)}』启动失败喵。"
        note += (
            "启动应用这件事已经处理完，本轮已无 launch_app 工具，不要再调它。"
            "按实际结果如实告诉主人：成功的说『已打开，注意看屏幕喵』，"
            "失败的老实承认『没打开，可能路径配错或应用没装』，别谎报成功。"
            "如果主人还有别的请求（发消息/看文件等）就继续处理。）"
        )
        msgs.insert(1, {"role": "system", "content": note})
        disabled_tools.add("launch_app")
    if sys_status:
        # 把真实数据交给模型转述，防止它编数字或重复调工具
        msgs.insert(1, {
            "role": "system",
            "content": (
                "（系统状态本喵已直接查好，以下是真实数据：\n"
                f"{sys_status}\n"
                "不要再调 check_system（本轮已摘掉该工具）。"
                "用猫娘语气把关键数字报给主人，占用接近爆了就说下哪个进程吃得最多。）"
            ),
        })
        disabled_tools.add("check_system")
    # 绝对当前时间始终存在；长间隔问候仅作为补充。两者合为一个动态 system，
    # 插在稳定前缀之后、recent 之前，避免击穿 DeepSeek 前缀缓存。
    hint = greeting_hint(user_msgs)
    time_context = current_time_hint()
    if hint:
        time_context += "\n" + hint
    idx = next((i for i, m in enumerate(msgs) if m.get("role") != "system"), len(msgs))
    msgs.insert(idx, {"role": "system", "content": time_context})
    if disabled_tools:
        # 统一按禁用集合过滤一次（绝不重新引入前面已移除的工具；三个分支可任意组合）
        # 之前每分支各自从 TOOL_SCHEMAS/当前 tools 过滤，组合时 search 分支会覆盖 alarm 结果
        tools = [t for t in tools if t["function"]["name"] not in disabled_tools]
    # user 消息已落库，id 立即可用 → 放进第一个 meta 事件让前端挂删除钮
    user_msg_id = user_msgs[-1]["id"]
    # 写文件声称审计：主人明确要"生成/写文档放某处"时，跟踪本轮是否真调写文件工具
    # （chat_service 流结束会检测空口声称并追加更正）
    write_requested = _extract_write_request(request.chatmassage)
    # 确定性后端分支没有走 llm.py 的工具循环，也要把真实执行类别交给
    # chat_service 标注生命周期；不记录参数/结果，真实状态仍各自从业务数据读取。
    tool_trace = []
    if alarm_batches:
        tool_trace.append("set_alarms_batch")
    elif alarm_created:
        tool_trace.append("set_alarm")
    if opened_search:
        tool_trace.append("open_url")
    if launched or launch_failed:
        tool_trace.append("launch_app")
    if sys_status:
        tool_trace.append("check_system")
    write_state = {"tool_trace": tool_trace}
    return StreamingResponse(
        (_sse(evt) async for evt in stream_answer(sid, msgs, request.chatmassage,
                                                  tools=tools, user_msg_id=user_msg_id,
                                                  write_requested=write_requested,
                                                  write_state=write_state,
                                                  alarm_created=alarm_created)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/search")
async def web_search(q: str = ""):
    """联网搜索（必应 RSS，免 key）：桌宠右键「快搜」复用。返回标题/链接/摘要文本。"""
    query = (q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="搜点什么好呢喵~")
    return {"result": tool_web_search(query, num=5)}

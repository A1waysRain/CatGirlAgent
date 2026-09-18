# -*- coding: utf-8 -*-
"""隔离验证：严格提醒去重 + 会话暂存赛程确认创建。"""
import asyncio
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, ".")

# 提醒日期必须是未来（add_alarm 会拒绝过去日期）；写死日期会随日子过期，故相对今天算
_DAY_A = (date.today() + timedelta(days=30)).isoformat()
_DAY_B = (date.today() + timedelta(days=31)).isoformat()

root = tempfile.mkdtemp(prefix="catgirl_alarm_batch_")
os.environ["APPDATA"] = root
os.environ["CATGIRL_SKIP_PET"] = "1"

from backend.scheduler import Scheduler
from backend.sessions import (
    PENDING_ALARM_BATCH_TTL,
    PENDING_ALARM_INTENT_TTL,
    RECENT_MEDIA_TTL,
    _prune_alarm_context,
    create_session,
    find_alarm_batches_for_request,
    find_recent_media_for_alarm_request,
    get_current,
    get_session,
    mark_alarm_batch_applied,
    record_recent_media_ref,
    set_pending_alarm_intent,
    stage_alarm_batch,
)
import backend.routers.chat as chat_mod

ok = True


def check(label, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))


# ---- 1. 同一条提醒必须严格全等才视为已存在 ----
scheduler = Scheduler()
base = {"date": _DAY_A, "time": "19:30", "message": "西班牙大奖赛一练",
        "repeat": "once", "weekdays": [], "action": None, "goal": None, "scope": None}
first = scheduler.add_alarm(**base)
check("完整相同命中已有", scheduler.find_exact_alarm(base)["id"] == first["id"])
for field, value in (("date", _DAY_B), ("time", "19:31"),
                     ("message", "西班牙大奖赛二练"), ("repeat", "daily")):
    changed = dict(base)
    changed[field] = value
    check(f"{field} 不同不算已有", scheduler.find_exact_alarm(changed) is None)

created, errors = scheduler.add_alarms_batch([base])
check("批量完全重复不再创建", not created and len(errors) == 1 and errors[0]["error"] == "already_exists")

# ---- 2. 会话暂存赛程，只在主人点名确认时匹配 ----
create_session()
sid = get_current()
spain = [
    {"date": _DAY_A, "time": "19:30", "message": "西班牙大奖赛一练"},
    {"date": _DAY_A, "time": "23:00", "message": "西班牙大奖赛二练"},
]
stage_alarm_batch(sid, "F1西班牙大奖赛", spain)
check("未点名不匹配", find_alarm_batches_for_request(sid, "西班牙什么时候比赛") == [])
matched = find_alarm_batches_for_request(sid, "先设置西班牙的吧")
check("赛事简称点名匹配", len(matched) == 1 and matched[0]["label"] == "F1西班牙大奖赛")
check("赛事否定不匹配", find_alarm_batches_for_request(sid, "西班牙先不设置") == [])

# ---- 2b. M2：前置意图只消费下一份原料；陈年素材/批次不会误命中 ----
set_pending_alarm_intent(sid, "我待会发张图，你按图帮我设置提醒")
media = record_recent_media_ref(sid, r"D:\\catgirl\\spanish.png", "image")
check("前置意图由下一张图消费", media and media.get("intent", {}).get("media_path") == r"D:\\catgirl\\spanish.png")
check("刚才图片请求可回溯原料", find_recent_media_for_alarm_request(sid, "把刚才那张图设置成提醒")
      and find_recent_media_for_alarm_request(sid, "把刚才那张图设置成提醒")["path"] == r"D:\\catgirl\\spanish.png")
batch_with_source = stage_alarm_batch(sid, "F1意大利大奖赛", spain)
check("暂存批次保留原料路径", batch_with_source.get("source_path") == r"D:\\catgirl\\spanish.png")
expired = {"pending_alarm_intent": {"created_at": 1},
           "recent_media_refs": [{"path": "old.png", "created_at": 1}],
           "pending_alarm_batches": [{"label": "旧赛程", "created_at": 1}]}
check("陈年意图/原料/批次会被清理", _prune_alarm_context(
    expired, now=max(PENDING_ALARM_INTENT_TTL, RECENT_MEDIA_TTL, PENDING_ALARM_BATCH_TTL) + 2)
    and not expired["pending_alarm_intent"] and not expired["recent_media_refs"] and not expired["pending_alarm_batches"])

# ---- 2c. 泛指“都设置”/一句点名多个赛事 → 一次全部返回（2026-09-13 修复） ----
# 线上原话是「都设置，巴林的排位赛在10月3号下午4点，正赛在10月4号下午三点」：
# 旧实现只认命中的唯一批次，主人没复述的“阿塞拜疆”批次被永久搁置。
sid_all = create_session()["id"]
az_batch = stage_alarm_batch(sid_all, "F1阿塞拜疆大奖赛",
                             [{"date": _DAY_A, "time": "16:30", "message": "阿塞拜疆大奖赛一练"}])
bh_batch = stage_alarm_batch(sid_all, "F1巴林大奖赛",
                             [{"date": _DAY_B, "time": "12:30", "message": "巴林大奖赛一练"}])
mixed = find_alarm_batches_for_request(sid_all, "都设置，巴林的排位赛在10月3号下午4点")
check("泛指“都设置”带出全部未应用批次", len(mixed) == 2, str([b["label"] for b in mixed]))
check("共有片段“大奖赛”不连坐其它批次",
      [b["label"] for b in find_alarm_batches_for_request(sid_all, "设置巴林大奖赛")] == ["F1巴林大奖赛"])
check("一句点名多个赛事", len(find_alarm_batches_for_request(sid_all, "把阿塞拜疆和巴林都设置上")) == 2)
check("收窄“只设置巴林”仍按具体名匹配",
      [b["label"] for b in find_alarm_batches_for_request(sid_all, "只设置巴林")] == ["F1巴林大奖赛"])
check("排除措辞“除了巴林都设置”不接管", find_alarm_batches_for_request(sid_all, "除了巴林都设置") == [])
check("否定“都先不要设置”不接管", find_alarm_batches_for_request(sid_all, "这些都先不要设置") == [])
mark_alarm_batch_applied(sid_all, bh_batch["id"], ["fake-bh"])
check("已应用批次不被泛指重复带出",
      [b["label"] for b in find_alarm_batches_for_request(sid_all, "都设置")] == ["F1阿塞拜疆大奖赛"])
check("批次 id 齐全（供逐个创建后标记）", bool(az_batch.get("id")) and bool(bh_batch.get("id")))

# ---- 3. chat 路由用已暂存参数确定性批量创建，不依赖模型调用工具 ----
chat_mod.scheduler._alarms.clear()
chat_mod.scheduler._save_alarms()
stage_alarm_batch(sid, "F1西班牙大奖赛", spain)
captured = {}


async def fake_stream_answer(sid, msgs, user_text, tools=None, **kwargs):
    captured["systems"] = "\n".join(m.get("content", "") for m in msgs if m.get("role") == "system")
    captured["tools"] = [item["function"]["name"] for item in (tools or [])]
    yield {"type": "done", "message_ids": {"assistant": "fake"}}


chat_mod.stream_answer = fake_stream_answer


async def invoke():
    response = await chat_mod.chat(SimpleNamespace(chatmassage="先设置西班牙的吧", session_id=sid))
    async for _ in response.body_iterator:
        pass


asyncio.run(invoke())
alarms = chat_mod.scheduler.list_alarms()
check("确认后真实创建两条", len(alarms) == 2, str(alarms))
check("确认结果注入系统消息", "新建 2 条" in captured.get("systems", ""))
check("确认轮禁用批量工具", "set_alarms_batch" not in captured.get("tools", []))

asyncio.run(invoke())
check("重复确认不叠加", len(chat_mod.scheduler.list_alarms()) == 2)

# ---- 3b. 泛指“都设置”：后端一次把两个未应用批次全建出来（本次 bug 的端到端回归） ----
sid_all2 = create_session()["id"]
stage_alarm_batch(sid_all2, "F1阿塞拜疆大奖赛",
                  [{"date": _DAY_A, "time": "16:30", "message": "阿塞拜疆大奖赛一练"}])
stage_alarm_batch(sid_all2, "F1巴林大奖赛",
                  [{"date": _DAY_B, "time": "12:30", "message": "巴林大奖赛一练"}])
chat_mod.scheduler._alarms.clear()
chat_mod.scheduler._save_alarms()
captured.clear()


async def invoke_all():
    response = await chat_mod.chat(SimpleNamespace(chatmassage="都设置", session_id=sid_all2))
    async for _ in response.body_iterator:
        pass


asyncio.run(invoke_all())
check("泛指确认一次建出两个批次的提醒", len(chat_mod.scheduler.list_alarms()) == 2,
      str(chat_mod.scheduler.list_alarms()))
check("多批次结果与批次名都写进系统消息",
      "新建 2 条" in captured.get("systems", "")
      and "F1阿塞拜疆大奖赛" in captured.get("systems", "")
      and "F1巴林大奖赛" in captured.get("systems", ""))

# ---- 4. 缺暂存批次时，点名“刚才图片”会把真实路径注入本轮，而非猜历史 ----
sid_m2 = create_session()["id"]
record_recent_media_ref(sid_m2, r"D:\\catgirl\\fallback.png", "image")
captured.clear()


async def invoke_m2():
    response = await chat_mod.chat(SimpleNamespace(chatmassage="把刚才那张图设置成提醒", session_id=sid_m2))
    async for _ in response.body_iterator:
        pass


asyncio.run(invoke_m2())
check("缺批次时注入最近图片原料", r"D:\\catgirl\\fallback.png" in captured.get("systems", ""))
check("缺批次时不把旧文本当已创建", "不能把旧聊天文字当作已创建事实" in captured.get("systems", ""))

shutil.rmtree(root, ignore_errors=True)
print("ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)

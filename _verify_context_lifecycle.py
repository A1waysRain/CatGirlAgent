"""验证工具结论生命周期：历史可见、模型上下文与小本本可过滤。"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, ".")

tmp = tempfile.mkdtemp(prefix="catgirl_verify_lifecycle_")
os.environ["APPDATA"] = tmp
os.environ["CATGIRL_SKIP_PET"] = "1"
os.makedirs(os.path.join(tmp, "catgirl"), exist_ok=True)

from backend import sessions as S
from backend import summary as summary_mod
from backend.chat_service import _assistant_lifecycle, build_messages
from backend.routers.chat import _extract_pin_reference_request

ok = fail = 0


def check(label, got, want=True):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  PASS {label}")
    else:
        fail += 1
        print(f"  FAIL {label}: got={got!r}, want={want!r}")


sid = S.get_current()
expired = time.time() - 60
S.append_message(sid, "user", "打开原神")
S.append_message(sid, "assistant", "已经帮主人打开原神喵",
                 context_policy="temporary", expires_at=expired, summary_allowed=False)
S.append_message(sid, "user", "赛程文件在哪里")
S.append_message(sid, "assistant", "赛程文件在 D:\\赛事\\赛程.xlsx",
                 context_policy="reference", expires_at=time.time() + 86400,
                 summary_allowed=True)

all_messages = S.get_session(sid)["messages"]
model_messages = S.recent_messages(all_messages, for_model=True)
check("过期临时结论仍留在会话历史", any("打开原神" in m["content"] for m in all_messages))
check("过期临时结论不进入模型上下文", not any("已经帮主人打开原神" in m["content"] for m in model_messages))
check("未过期路径线索进入模型上下文", any("赛程.xlsx" in m["content"] for m in model_messages))

eligible = [m for m in all_messages if S.is_summary_eligible(m)]
check("临时结论不进入摘要输入", not any("已经帮主人打开原神" in m["content"] for m in eligible))
check("路径线索允许摘要", any("赛程.xlsx" in m["content"] for m in eligible))

# 全部不可摘要的一段也必须推进 n，否则后台会反复处理该段。
fake_session = {"messages": [
    {"role": "assistant", "content": "已点击", "summary_allowed": False,
     "expires_at": expired},
] * 10, "summary": []}
old_budget = S.HISTORY_TOKEN_BUDGET
try:
    S.HISTORY_TOKEN_BUDGET = 1
    block = summary_mod._compute_new_block(fake_session)
    check("过期临时段仍可确定摘要进度", block is not None)
finally:
    S.HISTORY_TOKEN_BUDGET = old_budget

S.append_message(sid, "user", "主人偏好黑咖啡")
S.append_message(sid, "assistant", "记住啦，主人偏好黑咖啡喵", context_policy="pinned")
pinned = S.recent_messages(S.get_session(sid)["messages"], for_model=True)
check("长期记忆不因时间过滤", any("黑咖啡" in m["content"] for m in pinned))

reference = S.append_message(sid, "assistant", "文件位于 D:\\资料\\课件.docx",
                             context_policy="reference", expires_at=time.time() + 3600,
                             summary_allowed=False)[-1]
promoted = S.pin_latest_reference(sid)
check("明确保留请求可将最近线索升为 pinned", promoted and promoted["message_id"] == reference["id"])
managed = S.list_context_items(sid)
check("pinned 线索出现在可管理列表", any(i["message_id"] == reference["id"] and i["policy"] == "pinned" for i in managed))
S.set_context_item_policy(sid, reference["id"], "drop")
check("丢弃后线索退出模型上下文",
      not any(m.get("id") == reference["id"] for m in S.recent_messages(S.get_session(sid)["messages"], for_model=True)))
check("带指代对象的记住请求可确定性识别", _extract_pin_reference_request("记住这个路径"))
check("泛泛询问不会误钉住线索", not _extract_pin_reference_request("你还记得吗"))

msgs = build_messages(sid, model_messages)
check("组装消息不重新带回过期结论", not any("已经帮主人打开原神" in m.get("content", "") for m in msgs))

from backend.scheduler import scheduler
alarm = scheduler.add_alarm(
    date=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
    time="08:30", message="看仙术杯",
)
with_alarm = build_messages(sid, model_messages)
check("提醒真实状态不依赖过期聊天结论",
      any("看仙术杯" in m.get("content", "") for m in with_alarm))
check("严格查重仍能找到真实提醒", scheduler.find_exact_alarm(alarm) is not None)
check("提醒混合查询仍按状态结论处理",
      _assistant_lifecycle(["set_alarm", "get_time"]).get("context_policy"), "state")

print(f"\n结果：{ok} PASS, {fail} FAIL")
raise SystemExit(1 if fail else 0)

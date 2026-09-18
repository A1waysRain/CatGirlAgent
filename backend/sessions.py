"""多会话：每个会话独立历史，存 %APPDATA%/catgirl/sessions/<id>.json。

文件布局：
  %APPDATA%/catgirl/sessions/
    <id>.json            # {"id","title","created_at","updated_at","messages":[...]}
  %APPDATA%/catgirl/sessions_index.json   # {"current":"<id>","order":["<id>",...]}

旧版单一 history.json 首次启动时自动迁移为「会话 1」。
之所以不用前端 localStorage：桌面壳每次启动随机端口，localStorage 按端口隔离不可靠。
"""

import json
import os
import threading
import time
import uuid
from pathlib import Path

from .history import MAX_STORED, load_history

# 每次请求最多发给大模型的历史 token 预算（L1 按预算取历史，而不是按条数）
HISTORY_TOKEN_BUDGET = 8000

# 图片赛程“先说意图、再发图”只应消费紧接着的素材；暂存批次和最近素材
# 也不能无限留存，否则“刚才那张图”可能误命中陈年赛程。
PENDING_ALARM_INTENT_TTL = 30 * 60
PENDING_ALARM_BATCH_TTL = 24 * 3600
RECENT_MEDIA_TTL = 24 * 3600

_lock = threading.Lock()


def est_tokens(text: str) -> int:
    """粗略估算文本 token 数：中文（含全角标点）≈1 token/字，英文/数字≈4 字符/token。

    不需要精确，只用来控制历史窗口大小（引 tiktoken 太重了）。
    """
    if not text:
        return 0
    han = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = max(0, len(text) - han)
    return int(han + other / 4) + 1


def is_context_active(message: dict, now: float | None = None) -> bool:
    """消息是否仍可作为模型上下文；过期不影响聊天历史展示。"""
    try:
        expires_at = message.get("expires_at")
        return not isinstance(expires_at, (int, float)) or expires_at > (now or time.time())
    except Exception:
        return True


def is_summary_eligible(message: dict, now: float | None = None) -> bool:
    """消息是否可进入小本本；临时执行结论永远不应被摘要。"""
    return message.get("summary_allowed", True) is not False and is_context_active(message, now)


def recent_messages(messages: list[dict], budget: int = HISTORY_TOKEN_BUDGET,
                    for_model: bool = False) -> list[dict]:
    """从最新往前取消息，token 凑满预算即停；最新一条必然包含（那是本轮提问）。

    比旧「按条数 20 截断」聪明：长消息不再挤掉整个窗口，短消息让窗口能装更多轮。
    ``for_model=True`` 时跳过已过期的工具结论；原始聊天历史仍完整保留。
    """
    if not messages:
        return []
    out: list[dict] = []
    total = 0
    for msg in reversed(messages):
        if for_model and not is_context_active(msg):
            continue
        total += est_tokens(msg.get("content") or "")
        out.append(msg)
        if total >= budget:
            break
    out.reverse()
    return out


def _base_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl"


def _sessions_dir() -> Path:
    return _base_dir() / "sessions"


def _index_file() -> Path:
    return _base_dir() / "sessions_index.json"


def _legacy_file() -> Path:
    return _base_dir() / "history.json"


# ---------- 底层读写 ----------

def _load_index() -> dict:
    try:
        f = _index_file()
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("order"), list):
                return data
    except Exception:
        pass
    return {}


def _save_index(index: dict) -> None:
    _base_dir().mkdir(parents=True, exist_ok=True)
    tmp = _index_file().with_name(_index_file().name + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_index_file())


def _session_file(sid: str) -> Path:
    return _sessions_dir() / f"{sid}.json"


def _load_session(sid: str) -> dict | None:
    try:
        f = _session_file(sid)
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("id") == sid:
                # 旧会话文件的消息可能没有 id，懒补一次并落盘（无损升级）
                _ensure_message_ids(data)
                return data
    except Exception:
        pass
    return None


def _ensure_message_ids(session: dict) -> None:
    """给缺失 id 的旧消息补 id；有补过就落盘（旧会话文件无损升级）。"""
    changed = False
    for msg in session.get("messages", []):
        if isinstance(msg, dict) and not msg.get("id"):
            msg["id"] = uuid.uuid4().hex[:12]
            changed = True
    if changed:
        _save_session(session)


def _save_session(session: dict) -> None:
    _sessions_dir().mkdir(parents=True, exist_ok=True)
    tmp = _session_file(session["id"]).with_name(session["id"] + ".tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_session_file(session["id"]))


def _new_session_record(title: str = "新会话") -> dict:
    now = time.time()
    return {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "created_at": now,
        "updated_at": now,
        "messages": [],
        "summary": [],  # L2 增量摘要记忆：[{n, text}, ...]，n=已摘要到的消息数
        "pending_alarm_batches": [],  # 图片/文件识别出的待确认赛程，不等同于已创建提醒
        "pending_alarm_intent": None,  # 主人发图前说“按待会图片设提醒”的一次性意图
        "recent_media_refs": [],  # 最近上传图片/文件原料，供“刚才那张图”回溯
    }


def _summary(session: dict) -> dict:
    return {
        "id": session.get("id"),
        "title": session.get("title") or "新会话",
        "count": len(session.get("messages", [])),
        "updated_at": session.get("updated_at", 0),
    }


def _migrate_legacy() -> None:
    """旧版单一 history.json → 会话「会话 1」（只在首次无 index 时做一次）。"""
    try:
        if not _legacy_file().exists():
            return
        msgs = load_history()
        if not msgs:
            return
        session = _new_session_record("会话 1")
        session["messages"] = msgs
        _save_session(session)
        _save_index({"current": session["id"], "order": [session["id"]]})
    except Exception:
        pass


def _ensure_index() -> dict:
    """保证 index 存在且 current 有效、order 引用的会话文件都在，返回 index。"""
    index = _load_index()
    if index.get("order"):
        index["order"] = [sid for sid in index["order"] if _session_file(sid).exists()]
        if not index["order"]:
            index = {}
    if not index.get("order"):
        _migrate_legacy()
        index = _load_index()
    if not index.get("order"):
        session = _new_session_record()
        _save_session(session)
        index = {"current": session["id"], "order": [session["id"]]}
        _save_index(index)
    if index.get("current") not in index["order"]:
        index["current"] = index["order"][0]
        _save_index(index)
    return index


# ---------- 对外 API（全部线程安全） ----------

def list_sessions() -> list[dict]:
    """返回会话摘要列表（不含 messages），按最近活跃倒序。"""
    with _lock:
        index = _ensure_index()
        out = []
        for sid in index["order"]:
            session = _load_session(sid)
            if session:
                out.append(_summary(session))
        out.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return out


def get_current() -> str:
    """当前会话 id；没有则自动建一个。"""
    with _lock:
        return _ensure_index()["current"]


def get_session(sid: str) -> dict | None:
    """完整会话（含 messages）；不存在返回 None。"""
    with _lock:
        _ensure_index()
        return _load_session(sid)


def create_session() -> dict:
    """新建「新会话」并设为当前，返回其摘要。"""
    with _lock:
        index = _ensure_index()
        session = _new_session_record()
        _save_session(session)
        index["order"].insert(0, session["id"])
        index["current"] = session["id"]
        _save_index(index)
        return _summary(session)


def set_current(sid: str) -> bool:
    """切换当前会话；不存在返回 False。"""
    with _lock:
        index = _ensure_index()
        if sid in index["order"]:
            index["current"] = sid
            _save_index(index)
            return True
        return False


def delete_session(sid: str) -> dict:
    """删除会话。若删的是当前会话，跳到剩余最近活跃的；全删光则自动新建。返回新 current 摘要。"""
    with _lock:
        index = _ensure_index()
        if sid in index["order"]:
            index["order"].remove(sid)
            try:
                _session_file(sid).unlink()
            except Exception:
                pass
            if index["current"] == sid:
                remain = [s for s in index["order"] if _load_session(s)]
                if remain:
                    remain.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
                    index["current"] = remain[0]["id"]
                else:
                    session = _new_session_record()
                    _save_session(session)
                    index["current"] = session["id"]
                    index["order"] = [session["id"]]
            _save_index(index)
        return _summary(_load_session(index["current"]))


def clear_session(sid: str) -> dict | None:
    """清空会话消息，标题重置为「新会话」，同时清掉它的记忆小本本（消息都没了，摘要留着矛盾）。返回摘要；会话不存在返回 None。"""
    with _lock:
        _ensure_index()
        session = _load_session(sid)
        if not session:
            return None
        session["messages"] = []
        session["title"] = "新会话"
        session["summary"] = []
        session["pending_alarm_batches"] = []
        session["pending_alarm_intent"] = None
        session["recent_media_refs"] = []
        session["updated_at"] = time.time()
        _save_session(session)
        return _summary(session)


def _find_round_indices(messages: list, i: int) -> list[int]:
    """一轮对话 = 一条 user + 紧随其后的 assistant（或该条孤立消息本身）。

    以被删消息 i 为准找整轮下标：
      - 该条是 user → [i, i+1]（若 i+1 是 assistant），否则 [i]
      - 该条是 assistant → [i-1, i]（若 i-1 是 user），否则 [i]
    """
    if not messages or not (0 <= i < len(messages)):
        return []
    role = messages[i].get("role")
    if role == "user":
        if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
            return [i, i + 1]
        return [i]
    if role == "assistant":
        if i - 1 >= 0 and messages[i - 1].get("role") == "user":
            return [i - 1, i]
        return [i]
    return [i]


def pop_last_assistant(sid: str) -> tuple[str, list[dict], dict] | None:
    """弹掉最后一条 assistant 消息，返回 (其前一条 user 的文本, 剩余消息, 被删的 assistant 消息)。

    第三项带原 id，供 regenerate 失败时恢复旧回答用（避免"先删后建，失败即丢"）。
    会话不存在或最后一条不是 assistant → 返回 None（无可重生成）。
    """
    with _lock:
        _ensure_index()
        session = _load_session(sid)
        if not session:
            return None
        messages = session.get("messages", [])
        if not messages or messages[-1].get("role") != "assistant":
            return None
        user_text = ""
        if len(messages) >= 2 and messages[-2].get("role") == "user":
            user_text = messages[-2].get("content", "")
        old = messages[-1]
        del messages[-1]
        session["updated_at"] = time.time()
        _save_session(session)
        return user_text, messages, old


def restore_assistant(sid: str, msg: dict) -> None:
    """把一条 assistant 消息恢复到会话末尾（regenerate 失败时把旧回答带原 id 补回来）。

    幂等：会话里已存在同 id 的消息则跳过（防重复恢复）。
    """
    with _lock:
        _ensure_index()
        session = _load_session(sid)
        if not session:
            return
        messages = session.get("messages", [])
        mid = msg.get("id")
        if mid and any(m.get("id") == mid for m in messages):
            return
        messages.append(msg)
        session["updated_at"] = time.time()
        _save_session(session)


def delete_round(sid: str, message_id: str) -> list[dict] | None:
    """删除包含指定消息的那一轮对话（user+assistant 或孤立消息）。

    消息不存在返回 None；会话由调用方先确认存在。返回剩余消息列表。
    """
    with _lock:
        _ensure_index()
        session = _load_session(sid)
        if not session:
            return None
        messages = session.get("messages", [])
        idx = next((k for k, m in enumerate(messages) if m.get("id") == message_id), None)
        if idx is None:
            return None
        for k in reversed(sorted(_find_round_indices(messages, idx))):
            del messages[k]
        if not messages:
            session["title"] = "新会话"
        session["updated_at"] = time.time()
        _save_session(session)
        return messages


def search_messages(sid: str, query: str, limit: int = 50) -> list[dict]:
    """在当前会话消息里按内容检索（大小写不敏感的子串匹配），按消息顺序返回前 limit 条。

    返回 [{message_id, role, content}, ...]；会话不存在或查询为空返回 []。
    """
    with _lock:
        _ensure_index()
        session = _load_session(sid)
        if not session:
            return []
        q = (query or "").strip().lower()
        if not q:
            return []
        out = []
        for msg in session.get("messages", []):
            content = msg.get("content") or ""
            if q in content.lower():
                out.append({
                    "message_id": msg.get("id"),
                    "role": msg.get("role"),
                    "content": content,
                })
                if len(out) >= limit:
                    break
        return out


def append_message(sid: str, role: str, content: str, *, context_policy: str | None = None,
                   expires_at: float | None = None, summary_allowed: bool | None = None,
                   fact_refs: list[dict] | None = None) -> list[dict]:
    """向会话追加一条消息并落盘（首条 user 消息自动命名），返回该会话最新消息列表（截 MAX_STORED）。"""
    with _lock:
        index = _ensure_index()
        session = _load_session(sid) or _load_session(index["current"])
        content = (content or "").strip()
        if not content:
            return session["messages"]
        # ts 记录消息落库时刻，供「时间感知问候」算主人这次提问离上次互动隔了多久
        message = {
            "role": role,
            "content": content,
            "id": uuid.uuid4().hex[:12],
            "ts": time.time(),
        }
        # 生命周期字段是可选的：旧会话和所有未分类消息继续按原行为处理。
        if context_policy:
            message["context_policy"] = context_policy
        if isinstance(expires_at, (int, float)):
            message["expires_at"] = expires_at
        if summary_allowed is not None:
            message["summary_allowed"] = bool(summary_allowed)
        if fact_refs:
            message["fact_refs"] = list(fact_refs)
        session["messages"].append(message)
        # 首条用户消息自动命名（去换行，截 12 字）
        if role == "user" and session["title"] in ("新会话", ""):
            title = content.replace("\n", " ").strip()
            session["title"] = title[:12] if len(title) > 12 else (title or "新会话")
        session["messages"] = session["messages"][-MAX_STORED:]
        session["updated_at"] = time.time()
        _save_session(session)
        return session["messages"]


# ---------- 上下文保留项（M1） ----------

_MANAGEABLE_CONTEXT_POLICIES = {"reference", "pinned"}


def list_context_items(sid: str) -> list[dict]:
    """返回主人可管理的路径/文件等保留线索，不返回临时操作或真实业务状态。"""
    with _lock:
        _ensure_index()
        session = _load_session(sid)
        if not session:
            return []
        items = []
        for msg in session.get("messages", []):
            if msg.get("context_policy") not in _MANAGEABLE_CONTEXT_POLICIES:
                continue
            if not is_context_active(msg):
                continue
            items.append({
                "message_id": msg.get("id"),
                "content": msg.get("content") or "",
                "policy": msg.get("context_policy"),
                "expires_at": msg.get("expires_at"),
            })
        return items


def set_context_item_policy(sid: str, message_id: str, action: str) -> dict | None:
    """将 reference 升为 pinned，或停止保留一条 reference/pinned 线索。"""
    with _lock:
        _ensure_index()
        session = _load_session(sid)
        if not session:
            return None
        for msg in session.get("messages", []):
            if msg.get("id") != message_id:
                continue
            if msg.get("context_policy") not in _MANAGEABLE_CONTEXT_POLICIES:
                return None
            if action == "pin":
                msg["context_policy"] = "pinned"
                msg.pop("expires_at", None)
                msg["summary_allowed"] = True
            elif action == "drop":
                # 历史仍可查看；只让它立即退出未来上下文和未来摘要。
                msg["context_policy"] = "discarded"
                msg["expires_at"] = time.time()
                msg["summary_allowed"] = False
            else:
                return None
            session["updated_at"] = time.time()
            _save_session(session)
            return {
                "message_id": msg.get("id"), "content": msg.get("content") or "",
                "policy": msg.get("context_policy"), "expires_at": msg.get("expires_at"),
            }
        return None


def pin_latest_reference(sid: str) -> dict | None:
    """主人说“记住这个路径/赛程”时，确定性保留最近一条有效 reference。"""
    with _lock:
        _ensure_index()
        session = _load_session(sid)
        if not session:
            return None
        for msg in reversed(session.get("messages", [])):
            if msg.get("context_policy") != "reference" or not is_context_active(msg):
                continue
            msg["context_policy"] = "pinned"
            msg.pop("expires_at", None)
            msg["summary_allowed"] = True
            session["updated_at"] = time.time()
            _save_session(session)
            return {"message_id": msg.get("id"), "content": msg.get("content") or ""}
        return None


# ---------- 待确认批量提醒（赛程/安排识别结果） ----------

# 主人不复述赛事名、直接说“都设置/全部设上”时，把还没应用过的批次一次全给。
# 必须与授权动词（设置/设上/挂上/创建/建立）同现才生效，见 find_alarm_batches_for_request。
_BATCH_ALL_WORDS = ("都设", "都挂", "都建", "都创", "全都", "全部", "所有", "统统",
                    "一并", "一起设", "两个都", "俩都", "每个都")
# 带排除/否定措辞时整句交回模型，不做确定性创建：“除了巴林都设置”这类范围表达
# 本函数表达不了，硬按具体赛事名建恰恰会建反（注：“只设置巴林”属收窄不属排除，
# 走具体赛事名匹配即可，别加进这里）。
_BATCH_EXCLUDE_WORDS = ("除了", "除开", "别设", "不要设", "不设", "先不设", "取消")


def _two_plus_substrings(label: str) -> set[str]:
    """赛事名本身 + 所有两字以上连续片段（主人只说“西班牙”也能命中“F1西班牙大奖赛”）。"""
    text = (label or "").strip()
    names = {text} if text else set()
    for start in range(len(text)):
        for end in range(start + 2, len(text) + 1):
            names.add(text[start:end])
    return names


def _batch_all_requested(request: str) -> bool:
    """是否用“都设置/全部设上”这类措辞授权（带排除词时不算）。"""
    if any(word in request for word in _BATCH_EXCLUDE_WORDS):
        return False
    return any(word in request for word in _BATCH_ALL_WORDS)


def _batch_all_positions(request: str) -> list[int]:
    """泛指词在句中的结束位置，用来判断“都”是泛指全部还是列举全部。"""
    positions = []
    for word in _BATCH_ALL_WORDS:
        start = request.find(word)
        while start != -1:
            positions.append(start + len(word))
            start = request.find(word, start + 1)
    return positions


def _prune_alarm_context(session: dict, now: float | None = None) -> bool:
    """清理已过期的图片意图、原料和未确认批次；返回是否发生修改。"""
    now = now or time.time()
    changed = False
    intent = session.get("pending_alarm_intent")
    if isinstance(intent, dict) and float(intent.get("created_at") or 0) + PENDING_ALARM_INTENT_TTL <= now:
        session["pending_alarm_intent"] = None
        changed = True
    refs = [ref for ref in session.get("recent_media_refs", [])
            if isinstance(ref, dict) and float(ref.get("created_at") or 0) + RECENT_MEDIA_TTL > now]
    if len(refs) != len(session.get("recent_media_refs", [])):
        session["recent_media_refs"] = refs
        changed = True
    batches = []
    for batch in session.get("pending_alarm_batches", []):
        if not isinstance(batch, dict):
            changed = True
            continue
        applied_at = batch.get("applied_at")
        created_at = float(batch.get("created_at") or 0)
        # 已应用批次只用于紧邻的重复确认查重；未应用批次也不能陈年误命中。
        stamp = float(applied_at or created_at)
        if stamp + PENDING_ALARM_BATCH_TTL <= now:
            changed = True
            continue
        batches.append(batch)
    if len(batches) != len(session.get("pending_alarm_batches", [])):
        session["pending_alarm_batches"] = batches
        changed = True
    return changed


def set_pending_alarm_intent(sid: str, text: str) -> dict:
    """记录“下一张图/文件用于提醒”的一次性前置意图。"""
    with _lock:
        index = _ensure_index()
        session = _load_session(sid) or _load_session(index["current"])
        _prune_alarm_context(session)
        intent = {"text": (text or "").strip(), "created_at": time.time()}
        session["pending_alarm_intent"] = intent
        session["updated_at"] = time.time()
        _save_session(session)
        return dict(intent)


def record_recent_media_ref(sid: str, path: str, kind: str = "image") -> dict | None:
    """记录本会话最近上传的原料；若有未过期前置意图则由这次原料消费。"""
    path = (path or "").strip()
    if not path:
        return None
    with _lock:
        index = _ensure_index()
        session = _load_session(sid) or _load_session(index["current"])
        _prune_alarm_context(session)
        ref = {"path": path, "kind": kind or "image", "created_at": time.time()}
        refs = list(session.get("recent_media_refs", []))
        refs.append(ref)
        session["recent_media_refs"] = refs[-10:]
        intent = session.get("pending_alarm_intent")
        if isinstance(intent, dict):
            intent["media_path"] = path
            intent["media_kind"] = ref["kind"]
            intent["consumed_at"] = time.time()
            session["pending_alarm_intent"] = None
            ref["intent"] = dict(intent)
        session["updated_at"] = time.time()
        _save_session(session)
        return dict(ref)


def find_recent_media_for_alarm_request(sid: str, text: str) -> dict | None:
    """“把刚才那张图设成提醒”没有暂存批次时，返回唯一最新的可回溯原料。"""
    request = (text or "").strip()
    if not request or not any(word in request for word in ("设置", "设上", "挂上", "创建", "建立")):
        return None
    if not any(word in request for word in ("刚才", "这张", "那张", "图片", "图", "文件", "这个")):
        return None
    with _lock:
        index = _ensure_index()
        session = _load_session(sid) or _load_session(index["current"])
        changed = _prune_alarm_context(session)
        refs = list(session.get("recent_media_refs", []))
        if changed:
            session["updated_at"] = time.time()
            _save_session(session)
        return dict(refs[-1]) if refs else None

def stage_alarm_batch(sid: str, label: str, alarms: list[dict]) -> dict:
    """保存一个尚未授权创建的结构化提醒批次；同名批次覆盖旧识别结果。"""
    label = (label or "").strip()
    clean = []
    for item in alarms or []:
        if not isinstance(item, dict):
            continue
        time_text = str(item.get("time") or "").strip()
        message = str(item.get("message") or "").strip()
        if not time_text or not message:
            continue
        clean.append({
            "date": str(item.get("date") or "").strip() or None,
            "time": time_text,
            "message": message,
            "repeat": str(item.get("repeat") or "once"),
            "weekdays": list(item.get("weekdays") or []),
        })
    if not label or not clean:
        raise ValueError("赛程批次需要名称和至少一条完整提醒")
    with _lock:
        index = _ensure_index()
        session = _load_session(sid) or _load_session(index["current"])
        _prune_alarm_context(session)
        batches = [b for b in session.get("pending_alarm_batches", [])
                   if str(b.get("label") or "").casefold() != label.casefold()]
        source = (session.get("recent_media_refs") or [])[-1:]
        batch = {"id": uuid.uuid4().hex[:12], "label": label, "alarms": clean,
                 "created_at": time.time(), "applied_alarm_ids": []}
        if source:
            batch["source_path"] = source[0].get("path")
        batches.append(batch)
        session["pending_alarm_batches"] = batches[-10:]
        session["updated_at"] = time.time()
        _save_session(session)
        return dict(batch)


def find_alarm_batches_for_request(sid: str, text: str) -> list[dict]:
    """从主人明确的“设置某赛事/都设置”请求里，取出所有该创建的已暂存批次。

    返回 0 个（没授权或没命中）、1 个或多个：主人一句里点名多个赛事、或用
    “都设置”泛指时一次全给，交由调用方逐个确定性创建。旧实现只在恰好命中
    一个批次时才返回，主人说“都设置”却没复述赛事名时其余批次会被永久搁置。
    """
    request = (text or "").strip()
    if not request or not any(word in request for word in ("设置", "设上", "挂上", "创建", "建立")):
        return []
    # 否定/范围排除措辞（不设、别设、取消、除了…）一律不接管，交回模型按上下文处理
    if any(word in request for word in _BATCH_EXCLUDE_WORDS):
        return []
    with _lock:
        index = _ensure_index()
        session = _load_session(sid) or _load_session(index["current"])
        changed = _prune_alarm_context(session)
        batches = [b for b in session.get("pending_alarm_batches", []) if isinstance(b, dict)]
        labels = [str(b.get("label") or "").strip() for b in batches]
        # “大奖赛”这类两个批次共有的片段不能当识别依据，否则“设置巴林大奖赛”
        # 会连阿塞拜疆一起命中；先把各批次名称片段的两两交集算出来再排除。
        shared: set[str] = set()
        for i, left in enumerate(labels):
            left_aliases = _two_plus_substrings(left)
            for right in labels[i + 1:]:
                shared |= left_aliases & _two_plus_substrings(right)
        matches: list[dict] = []
        hits_all: set[str] = set()
        for batch, label in zip(batches, labels):
            # 允许主人只说“西班牙”，但至少要命中赛事名中的一个两字以上片段。
            hits = [name for name in (_two_plus_substrings(label) - shared)
                    if name and name in request]
            if not hits:
                continue
            matches.append(batch)
            hits_all.update(hits)
        if _batch_all_requested(request):
            # 看“都”前面有没有列举过赛事名：“把阿塞拜疆和巴林都设置上”的“都”只覆盖
            # 列举的那些；前面没提名字（“都设置，巴林的排位赛在10月3号下午4点”）才是
            # 全部未应用批次——线上正是这句，旧实现只建了命中的巴林、阿塞拜疆被搁置。
            covered = any(name in request[:pos]
                          for pos in _batch_all_positions(request) for name in hits_all)
            if not covered:
                listed = {id(b) for b in matches}
                matches.extend(b for b in batches
                               if not b.get("applied_alarm_ids") and id(b) not in listed)
        if changed:
            session["updated_at"] = time.time()
            _save_session(session)
        return [dict(b) for b in matches]


def mark_alarm_batch_applied(sid: str, batch_id: str, alarm_ids: list[str]) -> None:
    """保留批次供后续查重，并记录本次真实创建出的提醒 id。"""
    with _lock:
        index = _ensure_index()
        session = _load_session(sid) or _load_session(index["current"])
        for batch in session.get("pending_alarm_batches", []):
            if batch.get("id") == batch_id:
                batch["applied_alarm_ids"] = list(alarm_ids)
                batch["applied_at"] = time.time()
                session["updated_at"] = time.time()
                _save_session(session)
                return


# ---------- L2 增量摘要记忆（summary 字段读写） ----------

# 摘要段数上限：满了把最早的 2 段合并成 1 段（set_summary 里保证）
MAX_SUMMARY_SEGMENTS = 3


def get_summary(sid: str) -> list[dict]:
    """返回可展示的摘要段列表；内部的进度标记不暴露给前端。"""
    with _lock:
        _ensure_index()
        session = _load_session(sid)
        if not session:
            return []
        return [dict(seg) for seg in session.get("summary", [])
                if isinstance(seg, dict) and seg.get("text")]


def clear_summary(sid: str) -> None:
    """清空会话摘要（记忆小本本）。"""
    with _lock:
        _ensure_index()
        session = _load_session(sid)
        if session:
            session["summary"] = []
            session["updated_at"] = time.time()
            _save_session(session)


def set_summary(sid: str, segments: list[dict]) -> None:
    """整段替换摘要（线程安全）。n 钳制到消息数内；超过段数上限自动合并最早的 2 段。"""
    with _lock:
        _ensure_index()
        session = _load_session(sid)
        if not session:
            return
        msgs = session.get("messages", [])
        segs = []
        for s in segments or []:
            if not isinstance(s, dict):
                continue
            try:
                n = min(max(int(s.get("n", 0)), 0), len(msgs))
            except Exception:
                n = 0
            text = str(s.get("text") or "").strip()
            if text:
                segs.append({"n": n, "text": text})
            elif s.get("skip"):
                # 只推进摘要进度的内部标记：该段全是明确不应记住的临时结论。
                segs.append({"n": n, "text": "", "skip": True})
        # 新段已覆盖旧 skip 标记时，旧标记不再有意义，避免累积空段。
        compact = []
        for i, seg in enumerate(segs):
            if seg.get("skip") and any(other.get("n", 0) >= seg["n"] for other in segs[i + 1:]):
                continue
            compact.append(seg)
        segs = compact
        # 段数上限：把最早的 2 段合并成 1 段（text 拼接；n 取靠后的，覆盖的消息范围更大）
        visible_positions = [i for i, seg in enumerate(segs) if seg.get("text")]
        while len(visible_positions) > MAX_SUMMARY_SEGMENTS:
            ia, ib = visible_positions[0], visible_positions[1]
            a, b = segs[ia], segs[ib]
            segs[ia:ib + 1] = [{"n": b["n"], "text": a["text"] + "\n" + b["text"]}]
            visible_positions = [i for i, seg in enumerate(segs) if seg.get("text")]
        session["summary"] = segs
        session["updated_at"] = time.time()
        _save_session(session)

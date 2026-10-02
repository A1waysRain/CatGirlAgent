"""验证会话标题：大模型提炼自动命名 + 手动改名。

提炼调用**全程打桩**（不耗 DeepSeek token）；会话/API 走隔离 APPDATA。
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, ".")

# 硬赋值隔离（别用 setdefault：Windows 上 APPDATA 本来就有值，等于没隔离，
# 桩数据会写进主人真实的 actions.log —— 2026-09-27 实际发生过）
_tmp = tempfile.mkdtemp(prefix="catgirl_verify_title_")
os.environ["APPDATA"] = _tmp
os.environ["CATGIRL_SKIP_PET"] = "1"
os.makedirs(os.path.join(_tmp, "catgirl"), exist_ok=True)

from backend import agents, sessions as S, title as T  # noqa: E402

ok = 0
fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  ✓ {label}")
    else:
        fail += 1
        print(f"  ✗ {label}\n      got  {got!r}\n      want {want!r}")


class FakeDistill:
    """可编程假提炼器：value=要返回的标题（None=提炼失败）、delay=模拟耗时、calls=调用次数。

    ★从第 3 节起**必须一直挂这个假实现**：一旦把 T.distill_title 还原成真的，
    `_run_title` 就会真去打 DeepSeek（cwd 在 Cat_Girl 时 .env 里有真 Key），
    本想打桩却真的花了 token —— 本次写测试时就踩过一次，是真模型返回的"整理周报"。
    """

    def __init__(self):
        self.value = None
        self.delay = 0.0
        self.calls = 0

    async def __call__(self, user_text, answer):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.value


FAKE = FakeDistill()
T.distill_title = FAKE


# ================= 1. _clean_title：把模型返回洗成标题 =================
print("\n[1] _clean_title 清洗")
check("去「」包裹", agents._clean_title("「微信给张永富发消息」"), "微信给张永富发消息")
check("去《》包裹", agents._clean_title("《周报整理》"), "周报整理")
check("去引号+尾句号（引号在标点里面）", agents._clean_title("“测试标题”。"), "测试标题")
check("去【】包裹", agents._clean_title("【测试标题】"), "测试标题")
check("去「标题：」前缀", agents._clean_title("标题：微信发消息"), "微信发消息")
check("只取第一行", agents._clean_title("第一行\n第二行是解释"), "第一行")
check("去尾部标点", agents._clean_title("微信发消息。"), "微信发消息")
check("超长截 20 字", len(agents._clean_title("好" * 30)), 20)
check("纯空白 → None", agents._clean_title("   "), None)
check("空串 → None", agents._clean_title(""), None)
check("只有标点 → None", agents._clean_title("。。"), None)
check("None → None", agents._clean_title(None), None)


# ================= 2. distill_title：请求契约 + 失败一律 None =================
# 这一节测的是真函数本体（只把 httpx 换成假的，不走网络）
print("\n[2] distill_title 请求契约与失败兜底")
_original_client = agents.httpx.AsyncClient
_seen = {}


def _client_returning(content):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            _seen["payload"] = kwargs["json"]
            return Response()

    return Client


try:
    agents.httpx.AsyncClient = _client_returning("微信给张永富发消息")
    got = asyncio.run(agents.distill_title("帮我在微信和张永富发一句开会", "好嘞喵"))
    payload = _seen["payload"]
    check("正常返回标题", got, "微信给张永富发消息")
    check("stream 关闭", payload["stream"], False)
    check("temperature 0.3", payload["temperature"], 0.3)
    # ★防回归：思考 token 计入 completion 配额，给少了 content 恒空（见 distill_web docstring）
    check("max_tokens 给足（>=16000）", payload["max_tokens"] >= 16000, True)
    check("只发 system+user 两条", len(payload["messages"]), 2)
    check("不带主对话历史", "messages" not in payload["messages"][1]["content"], True)

    # 模型返回空 content（思考吃光配额时的真实症状）→ 必须 None，不能返回空串
    agents.httpx.AsyncClient = _client_returning("")
    check("content 为空 → None", asyncio.run(agents.distill_title("在吗", "在喵")), None)
    agents.httpx.AsyncClient = _client_returning("   \n  ")
    check("content 只有空白 → None", asyncio.run(agents.distill_title("在吗", "在喵")), None)

    class Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise RuntimeError("网络炸了")

        async def __aexit__(self, *a):
            pass

    agents.httpx.AsyncClient = Boom
    check("网络异常 → None（不抛）", asyncio.run(agents.distill_title("在吗", "在喵")), None)

    agents.httpx.AsyncClient = _client_returning("标题")
    check("空 user_text → None", asyncio.run(agents.distill_title("", "喵")), None)
finally:
    agents.httpx.AsyncClient = _original_client


# ================= 3. title_auto 标记（存量不动的根据） =================
print("\n[3] title_auto 标记")
check("新建会话 title_auto=True", S._new_session_record().get("title_auto"), True)
check("create_session 出来的可认领", S.claim_title(S.create_session()["id"]) is not None, True)

# 老会话文件里根本没有这个字段 → falsy → 永远不会被自动起名（这便是"存量不动"）
legacy_sid = S.create_session()["id"]
_legacy = S._load_session(legacy_sid)
_legacy.pop("title_auto", None)
S._save_session(_legacy)
check("无该字段的老会话 claim 失败（存量不动）", S.claim_title(legacy_sid), None)

old_sid = S.create_session()["id"]
S.clear_session(old_sid)
check("清空会话后回到 True（下一轮可重新起名）", S.claim_title(old_sid) is not None, True)


# ================= 4. _run_title 正常落笔 =================
print("\n[4] _run_title 正常路径")
sid = S.create_session()["id"]
S.append_message(sid, "user", "帮我在微信和张永富发一句明天开会")
S.append_message(sid, "assistant", "好嘞喵")
before = S._load_session(sid)
before_updated = before["updated_at"]
check("首条消息已给占位标题（截 12 字）", before["title"], "帮我在微信和张永富发一句")

FAKE.value = "微信给张永富发消息"
FAKE.calls = 0
prev = S.claim_title(sid)
check("认领成功并返回当时的标题当快照", prev, "帮我在微信和张永富发一句")
T._run_title(sid, "帮我在微信和张永富发一句明天开会", "好嘞喵", prev)

after = S._load_session(sid)
check("标题已换成提炼结果", after["title"], "微信给张永富发消息")
check("title_auto 落为 False", after["title_auto"], False)
# 改标题不算"最近活跃"，不该打乱会话列表排序
check("updated_at 未被 bump", after["updated_at"], before_updated)


# ================= 5. _run_title 该放弃的四种情形 =================
print("\n[5] _run_title 放弃（不覆盖主人 / 不写陈旧标题）")


def _claimed_session():
    """造一个「已认领、正在提炼中」的会话，返回 (sid, 快照标题)。"""
    s = S.create_session()["id"]
    S.append_message(s, "user", "帮我整理一下周报")
    S.append_message(s, "assistant", "好喵")
    return s, S.claim_title(s)


FAKE.value = "周报整理"

# (a) 认领后再认领 → 失败（并发去重的根据）
s1, prev1 = _claimed_session()
check("已认领的会话再次认领失败", S.claim_title(s1), None)
T._run_title(s1, "帮我整理一下周报", "好喵", prev1)
check("(a) 正常写入", S._load_session(s1)["title"], "周报整理")
T._run_title(s1, "帮我整理一下周报", "好喵", prev1)
check("(a) 二次调用不覆盖（title_auto 已 False 且快照对不上）", S._load_session(s1)["title"], "周报整理")

# (b) 提炼中主人手动改名 → 快照对不上，放弃
s2, prev2 = _claimed_session()
S.set_title(s2, "主人自己起的名字")
T._run_title(s2, "帮我整理一下周报", "好喵", prev2)
check("(b) 手动改名不被覆盖", S._load_session(s2)["title"], "主人自己起的名字")

# (c) 提炼中主人清空会话（清空把标题重置成「新会话」，快照对不上）
s3, prev3 = _claimed_session()
S.clear_session(s3)
T._run_title(s3, "帮我整理一下周报", "好喵", prev3)
check("(c) 清空后不糊上陈旧标题", S._load_session(s3)["title"], "新会话")

# (d) 提炼中主人把会话删了
s4, prev4 = _claimed_session()
S.delete_session(s4)
T._run_title(s4, "帮我整理一下周报", "好喵", prev4)
check("(d) 会话已删 → 静默无事", S._load_session(s4), None)

# 提炼失败 → 认领权放回去，下一轮还能重试（不然一次网络抖动就永远拿不到智能标题）
s5, prev5 = _claimed_session()
FAKE.value = None
T._run_title(s5, "帮我整理一下周报", "好喵", prev5)
check("提炼失败时标题保持占位", S._load_session(s5)["title"], "帮我整理一下周报")
check("提炼失败后认领权放回（可重试）", S.claim_title(s5) is not None, True)
FAKE.value = "周报整理"


# ================= 6. maybe_title 不重复触发 =================
print("\n[6] maybe_title 不重复触发")
s6 = S.create_session()["id"]
S.append_message(s6, "user", "看看桌面上有什么")
S.append_message(s6, "assistant", "好喵")
FAKE.value = "看看桌面文件"
FAKE.calls = 0
T.maybe_title(s6, "看看桌面上有什么", "好喵")
time.sleep(0.6)  # 等后台线程跑完
check("第一次触发起了提炼", FAKE.calls, 1)
T.maybe_title(s6, "看看桌面上有什么", "好喵")
time.sleep(0.4)
check("第二次不再触发（title_auto 已 False）", FAKE.calls, 1)
check("标题已落笔", S._load_session(s6)["title"], "看看桌面文件")


# ================= 7. 并发去重（背靠背两次触发只提炼一次） =================
print("\n[7] 并发去重")
s7 = S.create_session()["id"]
S.append_message(s7, "user", "帮我查一下赛程")
S.append_message(s7, "assistant", "好喵")
FAKE.value = "查询赛程"
FAKE.delay = 0.3
FAKE.calls = 0
# 认领是同步发生在起线程之前的，所以背靠背的第二次必然认领不到
T.maybe_title(s7, "帮我查一下赛程", "好喵")
T.maybe_title(s7, "帮我查一下赛程", "好喵")
time.sleep(1.2)
check("两次触发只提炼一次", FAKE.calls, 1)
FAKE.delay = 0.0


# ================= 8. 改名路由 =================
print("\n[8] PUT /api/sessions/{sid}/title")
from fastapi.testclient import TestClient  # noqa: E402
import backend.main as main  # noqa: E402

client = TestClient(main.create_app(allowed_origins=None))

rsid = S.create_session()["id"]
S.append_message(rsid, "user", "帮我打开记事本")
S.append_message(rsid, "assistant", "好喵")

r = client.put(f"/api/sessions/{rsid}/title", json={"title": "  自己起的标题  "})
check("合法请求 200", r.status_code, 200)
check("返回 id 与 title（已 trim）",
      (r.json()["session"]["id"], r.json()["session"]["title"]), (rsid, "自己起的标题"))
check("落库生效", S._load_session(rsid)["title"], "自己起的标题")
check("title_auto 被置 False", S._load_session(rsid)["title_auto"], False)

check("空白标题 400", client.put(f"/api/sessions/{rsid}/title", json={"title": "   "}).status_code, 400)
check("缺字段 400", client.put(f"/api/sessions/{rsid}/title", json={}).status_code, 400)
check("会话不存在 404", client.put("/api/sessions/不存在/title", json={"title": "x"}).status_code, 404)

# 改过名之后，即使还有提炼线程在飞，也不能覆盖主人起的名
FAKE.value = "自动提炼的名字"
T._run_title(rsid, "帮我打开记事本", "好喵", "帮我打开记事本")
check("改过名的会话不被提炼覆盖", S._load_session(rsid)["title"], "自己起的标题")

check("超长标题被截断到 60",
      len(client.put(f"/api/sessions/{rsid}/title", json={"title": "长" * 200}).json()["session"]["title"]), 60)
check("列表接口能读到标题",
      any(s["id"] == rsid for s in client.get("/api/sessions").json()["sessions"]), True)


# ================= 9. 顺带修的 delete_session 崩溃 =================
# 写本测试时撞出来的存量 bug：remain 收集的是 id 字符串不是会话字典 →
# 删「当前会话」时只要还剩别的会话就 AttributeError 500（"剩下的会话"那个分支进不去）。
print("\n[9] delete_session 删当前会话（顺带修的存量崩溃）")
da = S.create_session()["id"]
db = S.create_session()["id"]
S.set_current(da)
newcur = S.delete_session(da)
check("删当前会话不崩且跳到剩下的会话", newcur["id"], db)
check("被删的会话确实没了", S.get_session(da), None)
# 再删一次同样走「还剩别的会话」这条分支（有 bug 的就是它）
last = S.delete_session(db)
check("再删一个当前会话也不崩", S.get_session(last["id"]) is not None, True)
check("current 是有效会话", last["id"] in [s["id"] for s in S.list_sessions()], True)
# 注：「删光后自动新建」走的是 else 分支，本来就没这个 bug，这里不重复测。


print(f"\n结果：{ok} 通过，{fail} 失败")
sys.exit(1 if fail else 0)

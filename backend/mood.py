"""猫娘回复情绪的规则推断（零 LLM 成本）。

给桌宠用：聊天端点拿到回复后，扫文本里的情绪信号推给桌宠切表情。
规则是启发式的——明显场景（生气/委屈）覆盖得准，细微情绪可能漏判/误判；
将来若要更准可升级为 LLM 结构化情绪标签。

mood 键与 pet_config.moods / 桌宠功能扩展方案 2.3 保持一致。
"""

# 情绪信号词，按优先级从强到弱判断（强生气最优先，防把"哼"类傲娇误判成生气）
_ANGRY_STRONG = ("捅你", "生气", "白眼", "跺脚", "叉腰", "气死", "揍你", "打死", "嫌弃", "讨厌你", "太过分", "过分", "怒", "凶")
_SAD = ("哭唧唧", "眼泪", "委屈", "难过", "伤心", "抽泣", "呜呜", "想哭", "难受")
_RELUCTANT = ("不要", "拒绝", "不帮", "才不要", "不行")
_SURPRISED = ("惊讶", "震惊", "吓一跳", "咦", "没想到", "居然", "呀")
_SLEEPY = ("困", "哈欠", "想睡", "打盹", "睡觉")
_TEASE = ("吐舌头", "逗你", "调戏", "嘿嘿", "嘻嘻", "撩")
_TSUNDERE = ("哼", "才不", "才不是", "才没有", "只是顺便", "勉为其难", "傲娇", "别想多")

# 主人消息里的挑衅/骂人词：有它 + 回复无开心信号 → 判生气
# （猫娘是傲娇人设，被骂后措辞仍带「哼/才不」，单看回复分不清是逗趣还是真生气，
#   必须结合主人的话判断）
_USER_PROVOKE = ("傻", "笨", "蠢", "智障", "白痴", "垃圾", "废物", "滚", "闭嘴", "去死", "恶心", "没用", "菜", "讨厌", "烦")
# 回复里明确"没当真/在玩笑"的信号：主人挑衅时不强制判生气
_EXPLICIT_HAPPY = ("摇尾巴", "耳朵竖", "哈哈", "嘿嘿", "开玩笑", "逗你", "嘻嘻")


def infer_mood(text: str, user_text: str = "") -> str:
    """从猫娘回复粗略推断情绪，返回 mood 键。

    结合主人消息：主人先挑衅/骂人且回复无明显玩笑信号 → 直接判 angry
    （傲娇猫娘被骂时措辞仍是「哼/才不」，单看回复会被误判成 tsundere）。
    之后按回复文本信号扫描。

返回：angry / sad / reluctant / excited / sleep / tease / tsundere / happy。
键名必须与 pet_assets/pet_config.json 的 moods 保持一致。
    （无任何信号时默认 happy）。
    """
    t = text or ""
    # "生气"的否定形式不算生气（才不生气 / 不生你气 / 没生气）
    _anger_denied = any(n in t for n in ("不生气", "才不生气", "没生气", "不生你气", "不生"))
    # "睡觉/困"的否定形式不算困（不睡觉 / 没睡觉 / 不用睡）
    _sleepy_kws = [k for k in _SLEEPY if not (k == "睡觉" and any(n in t for n in ("不睡觉", "没睡觉", "不用睡", "才不睡觉")))]
    _playful = any(k in t for k in _EXPLICIT_HAPPY)

    # ① 回复自带的明确情绪信号优先（表意明确，主人骂不骂都照实演）
    for kws, mood in (
        (_SAD, "sad"), (_RELUCTANT, "reluctant"),
        (_SURPRISED, "excited"), (_sleepy_kws, "sleep"), (_TEASE, "tease"),
    ):
        if any(k in t for k in kws):
            return mood
    # ② 主人挑衅 + 回复非玩笑且未否认生气 → 生气（盖过 tsundere/happy）
    if user_text and any(k in user_text for k in _USER_PROVOKE) and not _playful and not _anger_denied:
        return "angry"
    # ③ 回复强生气词
    for kw in _ANGRY_STRONG:
        if kw == "生气" and _anger_denied:
            continue
        if kw in t:
            return "angry"
    # ④ 傲娇（哼/才不）
    for kw in _TSUNDERE:
        if kw in t:
            return "tsundere"
    # ⑤ 默认开心
    return "happy"

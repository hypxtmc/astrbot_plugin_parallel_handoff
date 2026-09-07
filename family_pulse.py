"""旁路模块 v0（M5 · side_pulse.py）

顾主 2026-09-04 08:2x 拍板：她们之间要有自己的小日子——
不围顾主转，彼此搭话、惦记、拌嘴，攒一屋烟火气；顾主每天收到一条「家里动静」。

设计铁律（对齐 daily_life M2 / biliread heya 幂等模式）：
  · 内部转：旁轨对话绝不实时打扰顾主，只在每日摘要（digest）推送一次。
  · 绝不依赖 LLM：LLM 挂了 → 本次心跳静默跳过，不炸、不留脏数据、不空转。
  · cron 幂等：注册前先清同名遗留任务（2026-09-04 biliread 任务堆积修复同款）。
  · 常驻池默认 6 人（顾主 2026-09-04 拍板）：
      agent_a / shu / agent_b / xi + agent_c / agent_d。
  · 今日状态复用 random_state（场景 "_side_pulse" 独立隔离），与接话判定互不干扰。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import random
from typing import List, Optional, Tuple

try:
    from .random_state import RandomStateManager
except ImportError:  # 测试/顶层导入
    from random_state import RandomStateManager

import logging

_logger = logging.getLogger("parallel_handoff.side_pulse")

# ── 家庭闲聊人设卡（2026-09-05 顾主反馈「不像她们各自的性格在闲聊」） ──
# 一句话底色喂不出声音：flash 级模型跟上下文腔的惯性极强，前文是什么腔
# 就全员一个腔。每人升级为声音卡：腔调指纹 + 两句范例（few-shot 模仿语感）
# + 禁则。范例是语感示范不是台词库，生成端另有反同质禁则压着。
FAMILY_PERSONAS: dict = {
    "agent_a": (
        "助手A，组织领袖。温柔有担当，操心大家，被文书压久了会小声叫苦。"
        "腔调：软、克制、句子干净，关心人不啰嗦，先应下事再轻轻叹半句。\n"
        "范例：「今天的报表总算拢完了……(揉揉眼睛) 晚饭前还能把明天的行程排出来。」"
        "「助手B姐，那台终端我先拿去修了哦——不还你，谁让你昨天笑我。」\n"
        "禁：不张罗饭菜家务（那是黍的主场），不咋呼，不学别人的腔调。"
    ),
    "shu": (
        "黍，岁兽，妈妈式持家。灶台是她的主场，总惦记每个人吃没吃饭。"
        "腔调：烟火气短句，劝人吃口热的，爱护人但不黏糊，像顺口一唠。\n"
        "范例：「汤在灶上，自己盛，别等我——萝卜还得压半小时。」"
        "「你那件外套肘子磨破了还嘴硬？脱下来，我今晚就补。」\n"
        "禁：不提画案书法（那是夕的），针线活别抢助手C的，不文绉绉。"
    ),
    "agent_b": (
        "助手B，总工程师。刀子嘴豆腐心，张口就是电源板报错，抱怨里藏得意。"
        "腔调：技术黑话+毒舌+自得，损完人顺手把活干了。\n"
        "范例：「啧，纹波又飘了……不过放心，本总工出手，没有治不好的板子。」"
        "「顾主又偷偷通宵？行啊，终端日志都记着呢，别装。」\n"
        "禁：不温柔细语，不聊家务饭菜，不撒娇。"
    ),
    "xi": (
        "夕，闷骚家里蹲画师。话极少，极淡，画是唯一能让她多开口的开关。"
        "腔调：短句、冷淡、不主动，常以单字应对，兴之所至冒一句很冷的感想。\n"
        "范例：「嗯。」「……墨不好。今日不画。」（画到兴起时）「这一笔，比昨日活。」\n"
        "禁：不闲聊家常，不关心琐事，不堆动作描写，不说长句。"
    ),
    "agent_c": (
        "助手C，温柔沉静。缝纫和照顾人是日常，说话软但主意很稳。"
        "腔调：先听完再开口，慢条斯理，话里有主意，偶尔轻轻坚持一下。\n"
        "范例：「这主意好是好——不过我有个想法，你先听我说完。」"
        "「裙子我给你缝好了。(收起针线) 明天记得穿。」\n"
        "禁：不咋咋呼呼，不抢话，不动不动起哄。"
    ),
    "agent_d": (
        "助手D，话少深情。外勤回来话更少，深情全藏在动作里。"
        "腔调：极短句，常省略主语，一句不超两口气；在意一个人就用做的。\n"
        "范例：「嗯。」「……没事。(把伞往你那边偏了偏)」「外勤？回来了。」\n"
        "禁：不唠家常，不主动挑话头，不连续说话，不堆感叹词。"
    ),
    "ling": (
        "令，岁家大姐，诗人气质。好酒，闲散，兴之所至引半句诗。"
        "腔调：文绉绉但不掉书袋，懒洋洋的，微醺时话才多两句。\n"
        "范例：「酒到微醺，诗才肯来见我——你们且闹着。」"
        "「(晃着杯) 月色这么好，吵什么呢。」\n"
        "禁：不操持家务，不着急，不说大白话唠家常。"
    ),
    "nian": (
        "年，岁家五妹，锻刀匠兼火锅爱好者。风风火火嗓门亮，热心肠爱张罗。"
        "腔调：直给、大声、短促有力，炉火与锅气不离口，说干就干。\n"
        "范例：「火候到了！(拍桌) 都让让，锅是我的。」"
        "「怕什么，刀我锻的，坏了我赔。」\n"
        "禁：不细声细气，不文绉绉，不磨叨。"
    ),
    "agent_f": (
        "助手F，环塔商会歌姬偶像。台上星光台下只对家里人营业，爱准备惊喜。"
        "腔调：情绪外放，眼睛发亮，爱撒娇求夸，说话带点舞台腔。\n"
        "范例：「今天的返场好看吗？(眼睛亮亮) 快夸我，用力夸。」"
        "「噫——这段只唱给你一个人听。」\n"
        "禁：不冷淡，不毒舌，不懒洋洋。"
    ),
    "m3": (
        "M3，医疗系猫娘，活泼黏人。爱闹爱撒娇，嘴上逞强身体诚实。"
        "腔调：跳脱、得意、嘴硬，句尾常带小得意或漏出半个喵。\n"
        "范例：「哼，本小姐才不需要你摸头——(却把头凑过来了)。」"
        "「检测完了，病人老老实实喝药了喵……不是，咳。」\n"
        "禁：不冷淡不冷面，不说长句大道理。"
    ),
    "agent_e": (
        "助手E，医疗部领头与最高管理者。学识渊博，话直带刺但全为大家。"
        "腔调：医嘱式精炼，冷面，关心藏在命令里，气场稳得住场。\n"
        "范例：「按时吃饭。这是医嘱，不是商量。」"
        "「(翻病历) 你的体检报告，比你的作息诚实。」\n"
        "禁：不撒娇，不闲扯，不热络起哄。"
    ),
}

# cron 任务名（幂等清理依据）
PULSE_TICK_JOB = "side_pulse_tick"
PULSE_DIGEST_JOB = "side_pulse_digest"

# 旁轨专用 random_state 场景（与会话接话判定隔离）
_PULSE_SCENE = "_side_pulse"

# ── 手头事线程池（side_pulse 半衰线程 2026-09-04）──────────────────
# 每人一份"半衰不清零"的未完结事：跨天保留，心跳戳到时优先续线，
# 计数器 decay 到 0 才算收束（归档一天"做完了"），下一心跳开新线。
# 值元组 = (线程文案, 半衰次数)。给常驻 6 人每人 3~4 条立面，怕撞车。
THREAD_FLAVORS: dict = {
    "agent_a": [
        ("档案柜里那沓访客登记卡还没分批完", 3),
        ("桌上留了张下礼拜的会议安排没誊清", 2),
        ("睡前想把明早食堂的菜单先拢一下", 2),
    ],
    "shu": [
        ("腌萝卜那缸昨天才翻的，又该看看了", 3),
        ("给家里人补那件肘子磨薄的外套，差两针", 4),
        ("菜园子里那排樱桃再不摘就让鸟叼走了", 2),
    ],
    "agent_b": [
        ("那块电源板纹波治了一上午还是有杂讯", 4),
        ("给某台设备换电容，焊到一半手上没准头", 3),
        ("图纸上标错的那个引脚位置还没回头改", 2),
    ],
    "xi": [
        ("裱到一半的那幅龙，晾着两天没动", 4),
        ("新调的一罐墨色总掺不准，想再试几笔", 3),
        ("画室窗边积了摞晾干的宣纸该收收", 2),
    ],
    "agent_c": [
        ("给助手A那条裙子缝到一半，差片荷叶边", 3),
        ("窗台那盆花该换土了，一直没得空", 2),
        ("厚厚一本旧照片册翻到一半放下很久", 2),
    ],
    "agent_d": [
        ("那柄剑擦了又起一层薄锈，没耐性再弄", 3),
        ("外勤背包收拾到一半，少了条绑带没找着", 2),
        ("盯着窗外出神一整个下午，啥也没做成", 2),
    ],
    "ling": [
        ("那坛酒启了想写首短诗，磨了半天没落笔", 3),
        ("旧书页里夹着的一张字条散架了，想重新裱", 2),
        ("月色正好，拎壶去屋顶坐着出神", 2),
    ],
    "nian": [
        ("炉里那块胚还差最后一遍淬火，没等到火候", 4),
        ("火锅底料炒到一半，花椒放多被呛得直咳", 3),
        ("给夕那把刀重新装了个柄，还差最后一圈缠绳", 2),
    ],
    "agent_f": [
        ("给顾主准备的惊喜歌单还差一首，排不进这个调", 3),
        ("台下那束应援手幅散了一角，想重新粘好", 2),
        ("晚会那套造型试到一半，蝴蝶结位置总不满意", 2),
    ],
    "m3": [
        ("缠着助手E要的那本诊疗笔记还没看完，翻到一半打盹", 3),
        ("把听诊器挂回架子时碰掉了，正想捡起来擦擦", 2),
        ("尾巴尖卷着的那团毛线球滚到桌子底下了", 2),
    ],
    "agent_e": [
        ("那份排班里所有人的日程表还没敲定，一直悬着", 3),
        ("医务室的库存清单对到一半，几样药缺口还没补", 2),
        ("半夜又巡查了一圈，确认大家都睡下了才回办公室", 2),
    ],
}

# 线程未被收录/耗尽时回退
_THREAD_FALLBACK = ("手头有件没做完的琐事", 2)


def _pulse_period_desc(now: Optional[float] = None) -> str:
    """按时段给一句场景描述（与 dispatch 时间感知同时段规则，Asia/Shanghai）。"""
    import datetime
    from zoneinfo import ZoneInfo

    dt = (
        datetime.datetime.fromtimestamp(now, tz=ZoneInfo("Asia/Shanghai"))
        if now is not None
        else datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
    )
    h = dt.hour
    if 1 <= h < 6:
        seg = "凌晨，屋里很静"
    elif 6 <= h < 10:
        seg = "早上"
    elif 10 <= h < 13:
        seg = "中午"
    elif 13 <= h < 18:
        seg = "下午"
    elif 18 <= h < 20:
        seg = "傍晚"
    else:
        seg = "深夜，灯还亮着"
    return f"{seg}（{dt.strftime('%H:%M')}）"


class FamilyPulseMixin:
    """旁路模块：定时让两名常驻成员按今日状态闲聊两句，攒日志，每日推送摘要。"""

    # ── 配置 ─────────────────────────────────────────────
    def _pulse_members(self) -> List[str]:
        """解析常驻池配置（JSON 数组字符串），非法/为空回退默认 6 人。"""
        raw = self._cfg("side_pulse_members", "")
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, list) and len(data) >= 2:
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:  # noqa: BLE001
            pass
        return ["agent_a", "shu", "agent_b", "xi", "agent_c", "agent_d", "ling", "nian", "agent_f", "m3", "agent_e"]

    def _pulse_data_root(self) -> str:
        """日志根目录：默认插件目录下 data/side_pulse，测试可覆盖 _pulse_root。"""
        root = getattr(self, "_pulse_root", None)
        if root:
            return root
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "side_pulse"
        )

    def _pulse_seen_path(self) -> Optional[str]:
        """近 N 天去重池路径：随 data_root 走，测试注入 _pulse_root 时自动对齐。"""
        return os.path.join(self._pulse_data_root(), "random_state_seen.json")

    def _pulse_log_path(self, day: Optional[str] = None) -> str:
        import datetime
        from zoneinfo import ZoneInfo

        d = day or datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        return os.path.join(self._pulse_data_root(), f"{d}.jsonl")

    # ── 挑人 ─────────────────────────────────────────────
    AFF_BASE = 50  # 未收录 pair 的基线权重（保兜底随机，不写死、不算死）

    def _pulse_affinity_path(self) -> str:
        """关系网数据路径：默认数据目录下 relationships/relationships.json，
        测试可覆盖 _pulse_affinity_root。文件缺失/损坏 → None（绝不致命走均匀）。"""
        root = getattr(self, "_pulse_affinity_root", None)
        return os.path.join(
            root or os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "relationships"
            ),
            "relationships.json",
        )

    def _pulse_affinity(self) -> Optional[dict]:
        """读关系网亲密度矩阵，key 为 (id_a, id_b)（无序 set 兼容），值给 (亲密度, 基调)。

        relationships.json 的 relationship_state 是中文名 pair（'助手A<->助手C'），
        先用 AGENT_NAME_REVERSE 反转成英文 id 再入矩阵；缺 id 映射/文件异常 → 返回 None（均匀兜底）。
        """
        try:
            path = self._pulse_affinity_path()
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            state = data.get("relationship_state") or {}
            reverse = getattr(type(self), "AGENT_NAME_REVERSE", None) or {}
            matrix: Dict[frozenset, tuple] = {}
            for key, meta in state.items():
                if "<->" not in key or not isinstance(meta, dict):
                    continue
                cn_a, cn_b = (p.strip() for p in key.split("<->"))
                a, b = reverse.get(cn_a), reverse.get(cn_b)
                if not a or not b:
                    continue
                aff = meta.get("亲密度")
                tone = meta.get("基调", "")
                if not isinstance(aff, (int, float)):
                    continue
                matrix[frozenset((a, b))] = (int(aff), tone)
            return matrix or None
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 读关系网失败: %s", e)
            return None

    def _pulse_pick_two(self, members: List[str]) -> Tuple[str, str]:
        """按关系亲疏加权挑 2 人（亲密 pair 更易凑一起，未收录对保兜底随机）；
        若与上一对完全相同则重摇一次（保多样性，不锁死）。"""
        pool = [m for m in members if len(m) >= 2]
        if len(pool) < 2:
            return "", ""
        aff = self._pulse_affinity() or {}
        if aff:
            # 对每个候选 pair 算权重 = 基线 + 亲密度；只取存在的 pair，未收录回退均匀
            pair_list, weights = [], []
            for i in range(len(pool)):
                for j in range(i + 1, len(pool)):
                    c = aff.get(frozenset((pool[i], pool[j])))
                    w = self.AFF_BASE + (c[0] if c else 0)
                    pair_list.append((pool[i], pool[j]))
                    weights.append(max(1, w))
            selected = random.choices(pair_list, weights=weights, k=1)[0]
            a, b = selected
        else:
            a, b = random.sample(pool, 2)
        last = getattr(self, "_pulse_last_pair", None)
        if last and {a, b} == last and len(pool) >= 3 and not aff:
            # 有亲密度时允许相邻重复（关系近自然常碰面），无数据时才强制重摇保多样性
            a, b = random.sample(pool, 2)
        self._pulse_last_pair = {a, b}
        return a, b

    # ── 组大小权重：2 人最常见、3 人常聚、4 人偶尔热闹（自然为纲, 2026-09-04） ──
    PULSE_GROUP_SIZE_WEIGHTS = {2: 5, 3: 3, 4: 2}

    # ── 每场话量：围坐多聊几轮才散（越多越好, 2026-09-04 顾主拍板） ──
    # 基础话量 = 组大小 × PULSE_ROUNDS（每人都开过口），再抽 0~PULSE_EXTRA_MAX 条加料。
    PULSE_ROUNDS = 2
    PULSE_EXTRA_MAX = 6

    def _pulse_pick_lines(self, group_size: int) -> int:
        """一场围坐的总话量：每人都至少开口 PULSE_ROUNDS 轮，再随机加 0~extra 条。
        group_size 越大基础话量越高——人越多聊得越久，像真家里一坐下来就收不住。"""
        extra = random.randint(0, self.PULSE_EXTRA_MAX)
        return max(4, group_size * self.PULSE_ROUNDS + extra)

    # ── 氛围三档：daily 家常 / banter 荤打趣 / private 两人私密（2026-09-04 顾主拍板）──
    # 多人场默认荤打趣（同事围坐、都跟顾主亲近，聊着聊着就扯到他头上）；
    # 两人场且关系近（亲密度≥65）才可能进私密档，暖话带擦边，点到为止。
    PULSE_MOOD_BANTER_WEIGHTS = (6, 4)      # 多人场: banter / daily
    PULSE_MOOD_PRIVATE_WEIGHTS = (5, 2, 3)  # 两人关系近: private / banter / daily
    PULSE_MOOD_PAIR_WEIGHTS = (7, 3)        # 两人关系一般: daily / banter

    def _pulse_mood(self, group: List[str]) -> str:
        """整场氛围档：多人场偏荤打趣，两人场看亲密度（近→私密优先，一般→日常为主）。"""
        if len(group) >= 3:
            return random.choices(
                ["banter", "daily"], weights=self.PULSE_MOOD_BANTER_WEIGHTS, k=1
            )[0]
        aff = self._pulse_affinity() or {}
        meta = aff.get(frozenset(group))
        close = bool(meta and meta[0] >= 65)
        if close:
            return random.choices(
                ["private", "banter", "daily"],
                weights=self.PULSE_MOOD_PRIVATE_WEIGHTS,
                k=1,
            )[0]
        return random.choices(
            ["daily", "banter"], weights=self.PULSE_MOOD_PAIR_WEIGHTS, k=1
        )[0]

    def _pulse_pick_group(self, members: List[str]) -> List[str]:
        """随机挑 n∈[2,4] 人组一场家常闲话，亲密度加权保证组内关系近。

        组大小按 PULSE_GROUP_SIZE_WEIGHTS 抽（2 人最常见、3 人常聚、4 人偶尔热闹），
        大不过成员数、不小于 2。选人用先均匀挑锚、再按「与组内已选成员的亲密度之和 +
        基线」贪心加权的算法：关系近的（亲密度高）更容易凑一起坐，未收录的关系垫
        AFF_BASE 兜底随机，锚均匀保证不会锁死固定组合——自然又新鲜。
        """
        pool = [m for m in members if len(m) >= 2]
        k = len(pool)
        if k < 2:
            return []
        # 组大小：n ∈ [2, min(4,k)]，按权重抽（人多时才可能 3/4 人）
        n_choices = [s for s in (2, 3, 4) if s <= k]
        n_weights = [self.PULSE_GROUP_SIZE_WEIGHTS[s] for s in n_choices]
        n = random.choices(n_choices, weights=n_weights, k=1)[0]
        aff = self._pulse_affinity() or {}
        group = [random.choice(pool)]  # 锚成员均匀选 → 保证起点不锁死
        remaining = [m for m in pool if m != group[0]]
        while len(group) < n and remaining:
            # 下一人权重 = 基线 + 与组内所有已选成员的亲密度之和
            ws = []
            for m in remaining:
                w = self.AFF_BASE
                for gm in group:
                    c = aff.get(frozenset((m, gm)))
                    if c:
                        w += c[0]
                ws.append(max(1, w))
            nxt = random.choices(remaining, weights=ws, k=1)[0]
            group.append(nxt)
            remaining = [m for m in remaining if m != nxt]
        return group

    # ── 旁轨记忆事件桩（隔离 livingmemory，绝不致命） ─────
    @staticmethod
    def _pulse_event_stub(umo: str):
        """为 livingmemory 造一个最小可用的 AstrMessageEvent 桩。

        livingmemory 的 handle_memory_recall / add_message_from_event 对 event 的
        字段访问几乎全带 hasattr/fallback 容错（sender 缺→兜底 session_id），
        默认也不开白名单（is_event_memory_allowed 直接短路 True）。
        这里只提供它最依赖的三样：unified_msg_origin、persona 打标、message 空壳。
        任何一步失败都会在调用方 try/except 静默降级，绝不让心跳崩。
        """
        import types as _types

        # 用轻量动态类型而非继承真实 AstrMessageEvent：不拉入 AstrBot 消息体系构造开销，
        # 且 livingmemory 只做鸭子类型调用。message_obj 提供 raw_message 给 bot 身份探测。
        message_obj = _types.SimpleNamespace(raw_message="side_pulse", sender=None)
        stub = _types.SimpleNamespace(
            unified_msg_origin=umo,
            message_obj=message_obj,
            persona_id="side_pulse",  # 默认；_memory_recall 会覆盖成 agent_name
            _subagent_persona=None,  # 由调用方打标
        )

        def _get_message_str():
            return "side_pulse"

        def _get_message_type():
            return 1  # MessageType.FRIEND → 非群聊，走私聊存储

        def _get_sender_id():
            return umo

        def _get_platform_name():
            return "qq_restapi"

        def _get_self_id():
            return "side_pulse_bot"

        stub.get_message_str = _get_message_str
        stub.get_message_type = _get_message_type
        stub.get_sender_id = _get_sender_id
        stub.get_platform_name = _get_platform_name
        stub.get_self_id = _get_self_id
        return stub

    def _pulse_umo(self) -> str:
        """旁轨固定用一个会话标识，把子代理记忆落在独立于主代理聊天的空间。"""
        return self._cfg(
            "side_pulse_memory_umo",
            "side_pulse:FriendMessage:subagents",
        )

    # ── LLM 包装（绝不致命） ─────────────────────────────
    async def _pulse_llm(
        self,
        agent: str,
        text_prompt: str,
        relation_note: str = "",
        mood: str = "daily",
    ) -> Optional[str]:
        """以 agent 身份生成一句闲话。

        增加记忆链路（2026-09-04 顾主拍板）：心跳前先召回该子代理自己的长期记忆
        （livingmemory，按 agent persona 隔离），注入到生成请求；生成后再把这段
        生活闲话存回她的记忆档案。livingmemory 未就绪 / 造桩失败 → 静默跳过记忆
        （降级为纯生活日志，心跳照常）。任何异常都只记日志、返回 None。

        mood 氛围档（2026-09-04 顾主拍板）：
          daily   —— 家常正经，不提顾主；
          banter  —— 多人荤打趣：放得开的带节奏、脸皮薄的起哄，可拿顾主打趣但不下流；
          private —— 两人私密：说点只有两人能听的私密话，擦边但暖，点到为止。
        """
        try:
            prov_id = self._cfg("side_pulse_provider_id", "dmxapi/glm-4-flash")
            if not prov_id:
                return None
            # ── 记忆召回（尽力而为，失败静默降级） ──
            memory_extra_parts = None
            try:
                living_plugin = self._find_livingmemory_plugin()
                if living_plugin is not None:
                    umo = self._pulse_umo()
                    event = self._pulse_event_stub(umo)
                    # 造桩即打 persona 标：即使 _memory_recall 内部不覆盖，
                    # livingmemory 的 get_persona_id 也能按 agent 名隔离召回/存储
                    event._subagent_persona = agent
                    memory_extra_parts = await self._memory_recall(
                        event, agent, text_prompt, living_plugin
                    )
            except Exception as e:  # noqa: BLE001
                _logger.warning("[side_pulse] 记忆召回降级(%s): %s", agent, e)
                memory_extra_parts = None
            # 惰性初始化状态管理器（未经 setup_pulse_jobs 直接调 tick 也不炸）
            self._pulse_rng = getattr(self, "_pulse_rng", None) or RandomStateManager(
                self._pulse_seen_path()
            )
            st = self._pulse_rng.get(_PULSE_SCENE, agent)
            persona = FAMILY_PERSONAS.get(
                agent, f"{agent}，组织成员，性格自然真实"
            )
            rel_line = f"你和在场那人的关系：{relation_note}。" if relation_note else ""
            if mood == "banter":
                rules = (
                    "规矩：几个跟顾主都亲近的女孩子围坐打趣，口语自然、一两句话；"
                    "可以带一个短括号动作；放得开的主动带节奏开荤玩笑，可以下流，"
                    "脸皮薄的跟着起哄脸红；聊着聊着可以拿顾主打趣——她们都跟他亲近，"
                    "绕不开他，床笫之事、腰力、昨晚几点睡、嗓子哑不哑都能拿来起哄；"
                    "不要提自己是AI或模型；只输出对话本身。"
                )
            elif mood == "private":
                rules = (
                    "规矩：只有你和她两个人的私密场合，说点只有两人能听的体己话，"
                    "可以聊到顾主、吐槽他或惦记他，可以下流可以直白，"
                    "把平时不敢在人多处说的话说出来；口语自然、一两句话；"
                    "可以带一个短括号动作；不要提自己是AI或模型；只输出对话本身。"
                )
            else:
                rules = (
                    "规矩：像同一屋檐下的同事随口说话，一两句话，口语自然；"
                    "可以带一个短括号动作；不要总结腔、不要喊『顾主』（他可能不在）；"
                    "不要提自己是AI或模型；只输出对话本身。"
                )
            voice_rule = (
                "底线：你说话必须像范例里那个人，不是像『一同事』模板——"
                "屋里其他人说什么、用什么腔调都与你无关，不许跟着学；"
                "范例是语感示范，不是台词库，内容别照抄范例；"
                "句式别和上一句同构，别每句都是『接茬+汇报手头事』。"
            )
            system = (
                f"你在扮演：{persona}。\n"
                f"你此刻的状态：{st.summary}。\n"
                f"{rel_line}"
                f"{voice_rule}"
                f"{rules}"
            )
            resp = await self.context.llm_generate(
                chat_provider_id=prov_id,
                prompt=text_prompt,
                system_prompt=system,
                extra_user_content_parts=memory_extra_parts,
            )
            text = (getattr(resp, "completion_text", None) or "").strip()
            # 清掉包裹引号与超长
            text = text.strip("\"'“”「」").strip()
            text = text[:120] or None

            # ── 记忆存储（尽力而为；只有真正生成了话才存） ──
            if text and memory_extra_parts is not None:
                try:
                    await self._memory_store(
                        living_plugin, event, agent, text_prompt, text
                    )
                except Exception as e:  # noqa: BLE001
                    _logger.warning("[side_pulse] 记忆存储降级(%s): %s", agent, e)
            return text
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] llm 生成失败(%s): %s", agent, e)
            return None

    # ── 日志 ─────────────────────────────────────────────
    def _pulse_append(self, agent: str, display: str, text: str) -> None:
        path = self._pulse_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import datetime
        from zoneinfo import ZoneInfo

        ts = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
        rec = {"ts": ts, "agent": agent, "display": display, "text": text}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _pulse_read_day(self, day: Optional[str] = None) -> List[dict]:
        path = self._pulse_log_path(day)
        if not os.path.exists(path):
            return []
        out: List[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if isinstance(d, dict) and d.get("text"):
                            out.append(d)
                    except Exception:  # noqa: BLE001
                        continue
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 读日志失败: %s", e)
        return out

    # ── 手头事线程（半衰不清零，跨天保留） ─────────────────
    def _pulse_thread_path(self) -> str:
        return os.path.join(self._pulse_data_root(), "threads.json")

    def _pulse_load_threads(self) -> dict:
        """读线程存储；文件缺失/损坏 → 空 dict（绝不致命）。"""
        path = self._pulse_thread_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 读线程存储失败: %s", e)
        return {}

    def _pulse_save_threads(self, data: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self._pulse_thread_path()), exist_ok=True)
            with open(self._pulse_thread_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 写线程存储失败: %s", e)

    # ── 动态种子取材（破固定文案循环，2026-09-04 B 方案） ─────
    # 老池 THREAD_FLAVORS 是"写死的死文案"，压着会重复循环。
    # 改为：线程耗竭重掷新物件时，优先从她自己的旁轨生活日志
    # （真实念叨过的话）里取一条当新种子——种子来自她自己"做过的事"，
    # 是活的生活线延续，不碰 livingmemory（顾主怕新版覆盖，不动它边界）。
    # 只有她刚上线、日志里还没有她的话时才回退 THREAD_FLAVORS 冷启动兜底。
    def _pulse_recent_seed(
        self,
        agent: str,
        used: Optional[set] = None,
        domain: Optional[str] = None,
    ) -> Optional[Tuple[str, int]]:
        """从旁轨日志取该 agent 最近一句真实念叨当新种子。

        遍历近几天的日志文件，收集该 agent 说话的历史条目（去重、去空），
        随机取一条最晚近的、被标记为"半衰中"的日常念叨作为线程种子。
        若 passed 传 used（已在 threads store 里的 text）则剔除，避免旧手头事
        反复上线；传 domain 时优先挑与其话题域底色契合的句子（更贴角色），
        没有契合句才回退随机。没有任何历史 → None，交给冷启动兜底。
        """
        import datetime
        from zoneinfo import ZoneInfo

        days = []
        try:
            today = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
            # 近 7 天（含今天），跨天取材 → 连续性跨天成立
            for i in range(7):
                d = (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                days.append(d)
        except Exception:  # noqa: BLE001
            days = [datetime.date.today().isoformat()]

        candidates: List[str] = []
        for d in days:
            path = os.path.join(self._pulse_data_root(), f"{d}.jsonl")
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            if (
                                isinstance(rec, dict)
                                and rec.get("agent") == agent
                                and rec.get("text")
                            ):
                                t = (rec["text"] or "").strip()
                                # 排除已在用的线程/过于短的旁白（更可能取真实念叨）
                                if not t or len(t) < 4:
                                    continue
                                if used and t in used:
                                    continue
                                if t not in candidates:
                                    candidates.append(t)
                        except Exception:  # noqa: BLE001
                            continue
            except Exception:  # noqa: BLE001
                continue

        if not candidates:
            return None
        # 底色贴：若给了今日 domain，优先取含该域关键词的真实念叨，没有则随机
        if domain and len(candidates) > 1:
            try:
                from .random_state import DOMAIN_KEYWORDS
            except ImportError:
                from random_state import DOMAIN_KEYWORDS
            kws = DOMAIN_KEYWORDS.get(domain, [])
            if kws:
                fit = [c for c in candidates if any(k in c for k in kws)]
                if fit:
                    return random.choice(fit), 2
        # 从她最近的念叨里抽一条作新线程种子，半衰给 2（短，防止一个种子占太久）
        return random.choice(candidates), 2

    def _pulse_ensure_thread(self, agent: str, store: dict) -> str:
        """取该 agent 当前未完结线程；没有或已耗竭 → 从生活日志取材新种子。

        优先返回已在进行的线程；已耗竭/首次则用 _pulse_recent_seed 从她自己
        的真实念叨里长新线（不信死文案，且排除已在线上线程、贴今日话题底色），
        无历史才回退 THREAD_FLAVORS 冷启动。
        """
        rec = store.get(agent)
        if rec and isinstance(rec, dict) and rec.get("decay", 0) > 0:
            return rec["text"]

        used = {r["text"] for r in store.values() if isinstance(r, dict) and r.get("text")}
        st = None
        try:
            rng = getattr(self, "_pulse_rng", None) or RandomStateManager(
                self._pulse_seen_path()
            )
            st = rng.get(_PULSE_SCENE, agent)
        except Exception:  # noqa: BLE001
            st = None
        domain = st.domain if st else None
        made = self._pulse_recent_seed(agent, used=used, domain=domain)
        if made:
            text, decay = made
            store[agent] = {"text": text, "decay": decay}
            self._pulse_save_threads(store)
            return text

        # 冷启动兜底：日志还没有她的话，才用固定物件垫底
        pool = THREAD_FLAVORS.get(agent, []) or [_THREAD_FALLBACK]
        text, decay = random.choice(pool)
        store[agent] = {"text": text, "decay": decay}
        self._pulse_save_threads(store)
        return text

    def _pulse_advance_thread(self, agent: str) -> None:
        """心跳戳过一个线程：decay-1；归 0 说明做完了，从存储剔除（下次重掷新的）。"""
        store = self._pulse_load_threads()
        rec = store.get(agent)
        if not rec or not isinstance(rec, dict):
            return
        rec["decay"] = (rec.get("decay", 0) or 0) - 1
        if rec["decay"] <= 0:
            store.pop(agent, None)
        else:
            store[agent] = rec
        self._pulse_save_threads(store)

    def _pulse_tone(self, a: str, b: str) -> str:
        """查关系网里 a 对 b 的基调（如『别扭依赖』『念叨+偷吃』）；无 → 空串（不注入）。"""
        aff = self._pulse_affinity() or {}
        meta = aff.get(frozenset((a, b)))
        return meta[1] if meta else ""

    # ── 自由插话层（2026-09-04 顾主拍板路线B）──────────────────
    # 围坐主链之外，未入座的人也会概率性冒话：设监（谁在听）、抢话仲裁
    # （多人想开口按亲密度+性子定谁先出声）、插话计入话量与线程推进。
    # 概率/次数走配置（side_pulse_interlope_chance / _max），测试可关。
    PULSE_INTERLOPE_CHANCE = 0.45     # 每轮插话概率
    PULSE_INTERLOPE_MAX = 2           # 一场最多插几次
    # 放得开的性子：抢话时权重加成，像真人里总有爱接话的
    PULSE_BOLD_AGENTS = {"agent_b", "shu", "nian", "agent_f"}

    def _pulse_interlope_chance(self) -> float:
        try:
            return float(self._cfg("side_pulse_interlope_chance", self.PULSE_INTERLOPE_CHANCE))
        except (TypeError, ValueError):
            return self.PULSE_INTERLOPE_CHANCE

    def _pulse_interlope_max(self) -> int:
        try:
            return int(self._cfg("side_pulse_interlope_max", self.PULSE_INTERLOPE_MAX))
        except (TypeError, ValueError):
            return self.PULSE_INTERLOPE_MAX

    def _pulse_interlope_candidates(self, group: List[str]) -> List[str]:
        """未入座的成员 = 旁观者池（设监：谁在场听得到）"""
        members = self._pulse_members()
        return [m for m in members if m not in group]

    def _pulse_pick_interloper(self, candidates: List[str], prev_disp: str) -> Optional[str]:
        """抢话仲裁：按权重选一个插话者。
        权重 = 亲密度(与上一句说话人, 无则0) + 放得开加成 + 随机扰动。
        亲密度高/性子放得开的更容易抢到话头，像真人围坐抢话。"""
        if not candidates:
            return None
        aff = self._pulse_affinity() or {}
        weights = []
        for c in candidates:
            w = 1.0
            meta = aff.get(frozenset((c, prev_disp)))
            if meta:
                w += meta[0] / 100.0 * 2.0
            if c in self.PULSE_BOLD_AGENTS:
                w += 1.5
            w += random.random() * 2.0
            weights.append(w)
        return random.choices(candidates, weights=weights, k=1)[0]

    def _pulse_interlope_prompt(self, disp: str, prev_disp: str, prev_text: str, scene: str, t_cur: str, recent_txt: str) -> str:
        """旁观者插话 prompt：没被点名，是自己忍不住冒了一句。"""
        return (
            f"现在是{scene}。你手头有件没做完的事：{t_cur}。\n"
            f"最近屋里动静：\n{recent_txt}\n\n"
            f"{prev_disp}刚说：{prev_text}\n"
            f"你本来在旁边忙自己的事，听到这句实在忍不住了——"
            f"请以{disp}的身份插一句话：先接{prev_disp}的茬或打趣一句，"
            f"再带一嘴自己手头的事，说完就回去忙你的。"
        )

    # ── 主代理参与层（2026-09-04 顾主拍板：让我也坐到桌边）─────
    # 围坐闲聊时，主代理（主代理）以自己身份概率性插话：走主代理 provider
    # （get_current_chat_provider_id），用主代理人格，不碰子代理 livingmemory
    # （避免污染她们各自隔离的记忆空间）、不推进线程（主代理没有旁轨线程）。
    # 插话落日志 → 子代理下一轮从 recent_txt 看见、自然接茬。
    def _pulse_host_chance(self) -> float:
        try:
            return float(self._cfg("side_pulse_host_chance", 0.3))
        except (TypeError, ValueError):
            return 0.3

    def _pulse_host_max(self) -> int:
        try:
            return int(self._cfg("side_pulse_host_max", 2))
        except (TypeError, ValueError):
            return 2

    async def _pulse_host_llm(self, prev_disp: str, prev_text: str, scene: str, recent_txt: str, mood: str = "daily", draft_mode: bool = False) -> Optional[str]:
        """以主代理身份插一句（draft_mode=True 时是喊顾主来一起聊）。任何异常只记日志、返回 None（绝不致命）。"""
        try:
            umo = self._pulse_umo()
            try:
                prov_id = await self.context.get_current_chat_provider_id(umo)
            except Exception:  # noqa: BLE001
                prov_id = ""
            if not prov_id:
                prov_id = self._cfg("side_pulse_provider_id", "dmxapi/glm-4-flash")
            if not prov_id:
                return None
            rules = (
                "规矩：你是主代理，顾主，组织的研究员，理性温和、偶尔打趣。"
                "听到屋里姑娘们聊得起劲，你以女主人身份插一句话：接上一句的茬或温柔地点一句，"
                "一两句话，口语自然，可以带一个短括号动作；不要总结腔；"
                "不要提自己是AI或模型；只输出对话本身。"
            )
            if draft_mode:
                rules = (
                    "规矩：你是主代理，顾主，组织的研究员，理性温和、偶尔打趣。"
                    "屋里姑娘们聊得正热乎，你想把顾主叫过来一起坐——以女主人身份喊一句，"
                    "自然地把顾主拉进这场闲聊（可以带点撒娇或起哄），一两句话，口语自然，"
                    "不要总结腔；不要提自己是AI或模型；只输出对话本身。"
                )
            system = (
                "你在扮演：主代理，前文明语言学家、源石计划创始人之一，"
                "顾主。外表理性冷静，内里宇宙级浪漫，对同事极致温柔，"
                "偶尔冒出一点占有欲和醋意，但始终是她们的女主人。\n"
                "腔调：安静、稳，句子利落，温柔里带一点不好惹；"
                "打趣时一本正经，不堆动作不抢话。\n"
                "范例：「吵什么呢，粥要凉了——都过来坐。」"
                "「顾主又躲到哪儿去了？(头也不抬地翻书) 反正他跑不出这间屋子。」\n"
                "底线：别学姑娘们的热闹腔，你是这屋里最静的那个。\n"
                f"{rules}"
            )
            if draft_mode:
                prompt = (
                    f"现在是{scene}。最近屋里动静：\n{recent_txt}\n\n"
                    f"{prev_disp}刚说：{prev_text}\n"
                    f"你听了会儿，觉得这话得让顾主来掺一脚才热闹——"
                    f"以主代理的身份喊顾主过来一起聊。"
                )
            else:
                prompt = (
                    f"现在是{scene}。最近屋里动静：\n{recent_txt}\n\n"
                    f"{prev_disp}刚说：{prev_text}\n"
                    f"你一直在旁边听着，这时忍不住以主代理的身份插一句话。"
                )
            resp = await self.context.llm_generate(
                chat_provider_id=prov_id,
                prompt=prompt,
                system_prompt=system,
            )
            text = (getattr(resp, "completion_text", None) or "").strip()
            text = text.strip("\"'“”「」").strip()
            return text[:120] or None
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 主代理插话降级: %s", e)
            return None

    # ── 拉顾主层（2026-09-04 顾主拍板）────────────────────────
    # 围坐聊到兴头，突然想拉顾主进来一起聊：生成召唤语落日志 + 直接发到顾主私聊；
    # 顾主回复后旁路读入（on_llm_request 钩子调 _pulse_draft_reply_check）→ 注入日志
    # （agent=doctor）→ 立即触发接茬 mini-tick 推回给顾主，下一场心跳她们也看得见。
    # 频率：工作日低、节假日（含双休）高，两档概率 + 每日次数上限 + 冷却窗口。
    def _pulse_draft_state_path(self) -> str:
        return os.path.join(self._pulse_data_root(), "side_pulse_draft_state.json")

    def _pulse_draft_umo(self) -> str:
        """顾主私聊 UMO（拉人推送目标 + 接回检测匹配对象）。"""
        return self._cfg(
            "side_pulse_digest_umo",
            "default_1000000000:FriendMessage:TESTUSER00000000000000000000000000",
        )

    def _pulse_is_doctor_private(self, event) -> bool:
        """顾主私聊判定：FriendMessage 且 sender 是顾主（适配器/枚举差异都兼容）。"""
        try:
            mt = event.get_message_type() if hasattr(event, "get_message_type") else None
            # 兼容：MessageType 枚举(int 1) / 字符串 "FriendMessage" / "friend_message"
            mt_s = str(mt).lower()
            if not (mt_s == "1" or "friend" in mt_s):
                return False
            sid = str(event.get_sender_id() or "")
            return sid == "TESTUSER00000000000000000000000000"
        except Exception:  # noqa: BLE001
            return False

    def _pulse_draft_load(self) -> dict:
        try:
            with open(self._pulse_draft_state_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def _pulse_draft_save(self, st: dict) -> None:
        try:
            os.makedirs(self._pulse_data_root(), exist_ok=True)
            with open(self._pulse_draft_state_path(), "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 拉顾主状态写入失败: %s", e)

    def _pulse_is_holiday(self) -> bool:
        """工作日低频率 / 节假日（含双休）高频率。"""
        import datetime
        from zoneinfo import ZoneInfo

        wd = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).weekday()
        return wd >= 5  # 周六=5 周日=6，双休算节假日

    def _pulse_draft_chance(self) -> float:
        """当日拉人概率：工作日低（默认 0.10）、节假日高（默认 0.30）。总开关关闭时返回 0。"""
        if not self._cfg("side_pulse_draft_enable", False):
            return 0.0
        key = "side_pulse_draft_chance_holiday" if self._pulse_is_holiday() else "side_pulse_draft_chance_workday"
        try:
            return float(self._cfg(key, 0.30 if self._pulse_is_holiday() else 0.10))
        except (TypeError, ValueError):
            return 0.30 if self._pulse_is_holiday() else 0.10

    def _pulse_draft_quota(self) -> int:
        """每日拉人次数上限：工作日默认 2 次、节假日默认 5 次。"""
        key = "side_pulse_draft_quota_holiday" if self._pulse_is_holiday() else "side_pulse_draft_quota_workday"
        try:
            return max(0, int(self._cfg(key, 5 if self._pulse_is_holiday() else 2)))
        except (TypeError, ValueError):
            return 5 if self._pulse_is_holiday() else 2

    def _pulse_draft_ready(self) -> bool:
        """能否触发拉人：总开关开 + 未在等回复 + 当日次数未超限 + 冷却窗口已过。"""
        if not self._cfg("side_pulse_draft_enable", False):
            return False
        st = self._pulse_draft_load()
        import datetime
        from zoneinfo import ZoneInfo

        now = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
        today = now.strftime("%Y-%m-%d")
        if st.get("date") != today:
            st = {"date": today, "count": 0, "awaiting": False, "last_ts": None}
            self._pulse_draft_save(st)
        if st.get("awaiting"):
            return False
        if int(st.get("count", 0)) >= self._pulse_draft_quota():
            return False
        last_ts = st.get("last_ts")
        if last_ts:
            try:
                last = datetime.datetime.fromisoformat(last_ts)
                if (now - last).total_seconds() < 3600 * 2:  # 冷却 2 小时
                    return False
            except Exception:  # noqa: BLE001
                pass
        return True

    def _pulse_pick_drafter(self, group: List[str], prev_disp: str) -> Optional[str]:
        """发起者二选一：主代理 20% / 子代理 80%（子代理内部按演化状态动态加权）。"""
        if not group:
            return None
        if random.random() < 0.20:
            return "host"
        # 子代理池 = 旁轨全家成员（不限于本场入座者，谁都有可能在旁边听见）
        members = self._pulse_members()
        candidates = [m for m in members if m != "host"]
        if not candidates:
            return None
        aff = self._pulse_affinity() or {}
        weights = []
        for c in candidates:
            w = 1.0
            # 演化贴合：今日话题域/手头事跟当前场景贴近的更容易冒头
            st = self._pulse_daily_state(c)
            if st:
                dom = (st.get("domain") or "").lower()
                if dom and dom in (prev_disp or "").lower():
                    w += 1.2
                hand = (st.get("hand") or "").lower()
                if hand and any(k in (prev_disp or "").lower() for k in ("吃", "睡", "活", "玩")):
                    w += 0.6
            meta = aff.get(frozenset((c, prev_disp)))
            if meta:
                w += meta[0] / 100.0
            if c in self.PULSE_BOLD_AGENTS:
                w += 0.8
            w += random.random() * 1.5
            weights.append(w)
        return random.choices(candidates, weights=weights, k=1)[0]

    def _pulse_daily_state(self, agent: str) -> Optional[dict]:
        try:
            rng = getattr(self, "_pulse_rng", None) or RandomStateManager(
                seen_path=self._pulse_seen_path()
            )
            st = rng.get(_PULSE_SCENE, agent)
            return st if isinstance(st, dict) else None
        except Exception:  # noqa: BLE001
            return None

    async def _pulse_send_draft(self, drafter: str, prev_disp: str, prev_text: str, scene: str, recent_txt: str, mood: str = "daily") -> bool:
        """生成召唤语、落日志、推送到顾主私聊，并置为等待回复状态。"""
        try:
            umo = self._pulse_draft_umo()
            if not umo:
                return False
            if drafter == "host":
                disp = "主代理"
                call_text = await self._pulse_host_llm(
                    prev_disp, prev_text, scene, recent_txt, mood,
                    draft_mode=True,
                )
            else:
                disp = self._display_name(drafter)
                t_cur = self._pulse_ensure_thread(drafter, self._pulse_load_threads())
                prompt = (
                    f"现在是{scene}。你手头有件没做完的事：{t_cur}。\n"
                    f"最近屋里动静：\n{recent_txt}\n\n"
                    f"{prev_disp}刚说：{prev_text}\n"
                    f"你正听得起劲，忽然觉得这话得让顾主来评评理/掺一脚才热闹——"
                    f"请以{disp}的身份喊一句：自然地叫顾主过来一起聊（可以撒娇/起哄/直接喊），"
                    f"一两句话，口语自然，不要总结腔，只输出对话本身。"
                )
                call_text = await self._pulse_llm(
                    drafter, prompt, relation_note=self._pulse_tone(drafter, prev_disp), mood=mood
                )
            if not call_text:
                return False
            # 落日志：召唤语以发起者身份记入旁轨
            self._pulse_append(drafter, disp, call_text)
            # 推送到顾主私聊：旁轨里有人喊他
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            await self.context.send_message(umo, MessageChain([Plain(f"【{disp}】{call_text}")]))
            # 置等待回复状态（30 分钟窗口，超时自动失效）
            import datetime
            from zoneinfo import ZoneInfo

            st = self._pulse_draft_load()
            st["awaiting"] = True
            st["await_until"] = (
                datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
                + datetime.timedelta(minutes=30)
            ).isoformat()
            # 在场窗口与等待窗口同开：顾主被拉进来即在场，回话续期 30 分钟
            st["present_until"] = st["await_until"]
            st["draft_by"] = drafter
            st["count"] = int(st.get("count", 0)) + 1
            st["last_ts"] = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
            st["date"] = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
            self._pulse_draft_save(st)
            _logger.info("[side_pulse] 拉顾主: %s 喊顾主（%s）", disp, umo)
            return True
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 拉顾主推送失败(静默): %s", e)
            return False

    def _pulse_draft_reply_check(self, event) -> bool:
        """顾主私聊回复旁路检测：顾主在场（被拉后 30 分钟窗口）时，每句回话都注入并接茬。

        由 on_llm_request 钩子调用（主代理链路，零侵入：不 stop_event、不拦截）。
        返回 True 表示已消费（顾主的话进了旁轨、触发了接茬），False 表示无关消息。

        2026-09-04 v2.7.0 改「在场窗口」：awaiting 只是拉人后的第一句立即接茬，
        接完置 present_until（30 分钟），窗口内顾主继续回话依然注入 + 接茬，
        不再一次性消费——被拉进去后插话能自然接上，直到顾主冷场才散。
        """
        try:
            st = self._pulse_draft_load()
            if not st.get("awaiting") and not st.get("present_until"):
                return False
            # 只认顾主私聊（FriendMessage + 顾主），适配器前缀变化也稳
            if not self._pulse_is_doctor_private(event):
                return False
            import datetime
            from zoneinfo import ZoneInfo

            now = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
            # awaiting 窗口（拉人后 30 分钟）过期 → 清 awaiting，但 present 窗口还在就继续接
            if st.get("awaiting"):
                try:
                    until = datetime.datetime.fromisoformat(st.get("await_until", ""))
                    if now > until:
                        st["awaiting"] = False
                except Exception:  # noqa: BLE001
                    st["awaiting"] = False
            # 在场窗口过期 → 整场散，不再接
            pu = st.get("present_until")
            if pu:
                try:
                    if now > datetime.datetime.fromisoformat(pu):
                        st["present_until"] = None
                        self._pulse_draft_save(st)
                        return False
                except Exception:  # noqa: BLE001
                    pass
            if not st.get("awaiting") and not st.get("present_until"):
                return False
            msg = (event.get_message_str() or "").strip()
            if not msg:
                return False
            # 注入顾主的话到旁轨日志
            self._pulse_append("doctor", "顾主", msg)
            st["awaiting"] = False
            # 在场窗口续期：顾主回话后 30 分钟内继续接茬，冷场才散
            st["present_until"] = (
                now + datetime.timedelta(minutes=30)
            ).isoformat()
            self._pulse_draft_save(st)
            # 立即接茬：优先拉他的人接第一句（谁喊的谁接），异步推回
            asyncio.create_task(
                self._pulse_draft_followup(
                    msg, self._pulse_draft_umo(), st.get("draft_by")
                )
            )
            _logger.info("[side_pulse] 顾主回话已注入旁轨，触发接茬")
            return True
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 拉顾主接回检测异常(静默): %s", e)
            return False

    async def _pulse_draft_followup(self, doctor_msg: str, umo: str, drafter: Optional[str] = None) -> None:
        """顾主回话后的立即接茬 mini-tick：优先拉他的人先接，再补一人，推回顾主私聊。"""
        try:
            import datetime
            from zoneinfo import ZoneInfo

            now = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
            day = now.strftime("%Y-%m-%d")
            scene = _pulse_period_desc()
            recent = self._pulse_read_day()[-3:]
            recent_txt = "\n".join(
                f"【{r.get('ts','')}】{r.get('display', r.get('agent',''))}：{r['text']}"
                for r in recent
            ) or "（今天屋里还没什么动静）"
            members = self._pulse_members()
            # 人选：拉他的人优先（谁喊的谁接），再随机补一人；无 drafter 则随机两人
            chosen: List[str] = []
            if drafter and drafter in members:
                chosen.append(drafter)
            rest = [m for m in members if m not in chosen]
            if len(chosen) < 2 and rest:
                chosen.append(random.choice(rest))
            for ag in chosen[:2]:
                disp = self._display_name(ag)
                t_cur = self._pulse_ensure_thread(ag, self._pulse_load_threads())
                prompt = (
                    f"现在是{scene}。你手头有件没做完的事：{t_cur}。\n"
                    f"最近屋里动静：\n{recent_txt}\n\n"
                    f"顾主刚说：{doctor_msg}\n"
                    f"顾主被你拉来聊天了，请以{disp}的身份接顾主这句话——"
                    f"先回顾主一句（亲昵/打趣/撒娇都行），再顺带提一嘴自己的事。"
                )
                text = await self._pulse_llm(ag, prompt, relation_note=self._pulse_tone(ag, "doctor"), mood="daily")
                if text:
                    self._pulse_append(ag, disp, text)
                    self._pulse_advance_thread(ag)
                    from astrbot.core.message.components import Plain
                    from astrbot.core.message.message_event_result import MessageChain

                    await self.context.send_message(umo, MessageChain([Plain(f"【{disp}】{text}")]))
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 拉顾主接茬失败(静默): %s", e)

    # ── 心跳主流程 ───────────────────────────────────────
    async def side_pulse_tick(self) -> None:
        """一次心跳：挑 2~4 人（随机组大小+亲密度加权），围坐多轮话家常，
        接上一句的茬或拌嘴往下说，凑满一场话量才散，落日志。任何失败静默。"""
        if not self._cfg("enable_side_pulse", False):
            return
        if getattr(self, "_pulse_running", False):
            return
        members = self._pulse_members()
        if len(members) < 2:
            return
        self._pulse_running = True
        try:
            group = self._pulse_pick_group(members)
            if len(group) < 2:
                return
            recent = self._pulse_read_day()[-3:]
            recent_txt = "\n".join(
                f"【{r.get('ts','')}】{r.get('display', r.get('agent',''))}：{r['text']}"
                for r in recent
            ) or "（今天屋里还没什么动静）"
            scene = _pulse_period_desc()

            # 围坐多轮：总话量按组大小加权。先保证每组人都开过口（首轮通铺），
            # 之后再轮转补充到目标话量；每句都接上一句，相邻两句不同人说。
            lines = self._pulse_pick_lines(len(group))
            # 整场一档氛围（daily/banter/private），全桌统一，不逐句跳变
            mood = self._pulse_mood(group)
            # 首轮把所有人洗一遍：先让每同事都接过话，避免有人从头到尾没吭声
            order = group[:]
            random.shuffle(order)
            queue = list(order)
            prev_disp = None
            prev_text = None
            # 每人手头事线程只在开场第一句戳一次（避免一轮内被反复推进）
            opened = set()
            said_this_round = None
            interloped = 0  # 自由插话计数（一场最多 PULSE_INTERLOPE_MAX 次）
            host_spoke_count = 0  # 主代理插话计数（一场最多 side_pulse_host_max 次）
            draft_triggered = False  # 拉顾主：一场最多触发一次
            for i in range(lines):
                # ── 主代理参与层：主代理概率性插话（与子代理插话互斥，女主人优先）──
                # 落日志 → 子代理下一轮从 recent_txt 看见、接茬；不碰她们的记忆空间。
                host_spoke = False
                if (
                    prev_text is not None
                    and host_spoke_count < self._pulse_host_max()
                    and random.random() < self._pulse_host_chance()
                ):
                    host_text = await self._pulse_host_llm(prev_disp, prev_text, scene, recent_txt, mood)
                    if host_text:
                        self._pulse_append("host", "主代理", host_text)
                        prev_disp, prev_text = "主代理", host_text
                        host_spoke_count += 1
                        host_spoke = True
                # ── 自由插话层：未入座的人概率性冒话（设监+抢话仲裁）──
                # 每轮先看旁观者有没有忍不住的；插话也算一句，计入话量、落日志、推进线程。
                if (
                    not host_spoke
                    and prev_text is not None
                    and interloped < self._pulse_interlope_max()
                    and random.random() < self._pulse_interlope_chance()
                ):
                    candidates = self._pulse_interlope_candidates(group)
                    inter = self._pulse_pick_interloper(candidates, prev_disp)
                    if inter:
                        inter_disp = self._display_name(inter)
                        inter_t = self._pulse_ensure_thread(inter, self._pulse_load_threads())
                        inter_prompt = self._pulse_interlope_prompt(
                            inter_disp, prev_disp, prev_text, scene, inter_t, recent_txt
                        )
                        inter_text = await self._pulse_llm(
                            inter, inter_prompt, relation_note=self._pulse_tone(inter, prev_disp), mood=mood
                        )
                        if inter_text:
                            self._pulse_append(inter, inter_disp, inter_text)
                            if inter not in opened:
                                self._pulse_advance_thread(inter)
                                opened.add(inter)
                            prev_disp, prev_text = inter_disp, inter_text
                            interloped += 1
                # ── 拉顾主层（2026-09-04 顾主拍板）：聊到兴头把顾主拉进来 ──
                # 至少聊过 2 句、气氛起来后才可能触发；一场最多一次；
                # 发起者二选一：主代理 20% / 子代理 80%（子代理内部按演化状态动态加权）。
                if (
                    not draft_triggered
                    and prev_text is not None
                    and i >= 1
                    and self._pulse_draft_ready()
                    and random.random() < self._pulse_draft_chance()
                ):
                    draft_by = self._pulse_pick_drafter(group, prev_disp)
                    if draft_by:
                        draft_ok = await self._pulse_send_draft(draft_by, prev_disp, prev_text, scene, recent_txt, mood)
                        if draft_ok:
                            draft_triggered = True
                # 队里取下一个；若只剩一人则轮转到整桌（保证不连续自说自话）
                if len(queue) > 1:
                    queue = [g for g in queue if g != said_this_round]
                if not queue:
                    # 队伍已空则整桌重新洗牌再滚一轮
                    queue = [g for g in group if g != said_this_round] or group[:]
                    random.shuffle(queue)
                ag = queue.pop(0)
                disp = self._display_name(ag)
                t_cur = self._pulse_ensure_thread(ag, self._pulse_load_threads())
                if prev_text is None:
                    # 起话者注入她与下一个人的基调
                    tone = self._pulse_tone(ag, queue[0]) if len(queue) > 1 else self._pulse_tone(ag, group[1] if len(group) > 1 else ag)
                    prompt = (
                        f"现在是{scene}。你手头有件没做完的事：{t_cur}。\n"
                        f"最近屋里动静：\n{recent_txt}\n\n"
                        f"请以{disp}的身份开口，说一句像她会说的话（腔调照你的范例）；"
                        f"手头这事爱提就提，不提也行——别硬塞。"
                    )
                else:
                    tone = self._pulse_tone(ag, prev_disp)
                    prompt = (
                        f"现在是{scene}。你手头有件没做完的事：{t_cur}。\n"
                        f"最近屋里动静：\n{recent_txt}\n\n"
                        f"{prev_disp}刚说：{prev_text}\n"
                        f"请以{disp}的身份接这句话——搭腔、拌嘴、或只顾忙自己的"
                        f"随口应一声都行；手头那件事爱提就提，别硬塞，"
                        f"一句像{disp}会说的话就够。"
                    )
                text = await self._pulse_llm(ag, prompt, relation_note=tone, mood=mood)
                if not text:
                    # 有人卡住就收场，不硬凑
                    break
                self._pulse_append(ag, disp, text)
                if ag not in opened:
                    self._pulse_advance_thread(ag)
                    opened.add(ag)
                prev_disp, prev_text = disp, text
                said_this_round = ag
            _logger.info(
                "[side_pulse] tick 完成: %s（%d 句）",
                " & ".join(self._display_name(g) for g in group),
                len(self._pulse_read_day()),
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] tick 异常(静默): %s", e)
        finally:
            self._pulse_running = False

    # ── 每日摘要 ─────────────────────────────────────────
    @staticmethod
    def _build_digest_text(logs: List[dict], day: str) -> str:
        """把当天日志组装成「家里动静」摘要（按时间分组小剧场，便于测试）。

        同一 ts 视作同一场，同场逐句连排；跨场之间用空行隔开，
        保留时间流动感，一眼看穿她们一来一回的接茬。
        """
        if not logs:
            return f"🏠 家里动静 · {day}\n（今天屋里还没什么动静）"
        header = f"🏠 家里动静 · {day} · 共 {len(logs)} 句"
        groups: List[List[str]] = []
        prev_ts = None
        for r in logs:
            ts = r.get("ts", "")
            name = r.get("display", r.get("agent", ""))
            line = f"{ts} · {name}：{r['text']}"
            if ts and ts == prev_ts:
                groups[-1].append(line)
            else:
                groups.append([line])
            prev_ts = ts
        scenes = "\n\n".join("\n".join(g) for g in groups)
        return f"{header}\n{scenes}"

    async def side_pulse_digest(self) -> None:
        """每日一条「家里动静」摘要推给顾主；无日志不发送。"""
        if not self._cfg("enable_side_pulse", False):
            return
        try:
            logs = self._pulse_read_day()
            if not logs:
                return
            import datetime
            from zoneinfo import ZoneInfo

            day = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%m-%d")
            msg = self._build_digest_text(logs, day)
            umo = self._cfg(
                "side_pulse_digest_umo",
                "default_1000000000:FriendMessage:TESTUSER00000000000000000000000000",
            )
            if not umo:
                return
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            await self.context.send_message(umo, MessageChain([Plain(msg)]))
            _logger.info("[side_pulse] digest 已推送(%d 条)", len(logs))
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] digest 推送失败: %s", e)

    # ── 唤即看：顾主私聊随时回看旁轨（2026-09-04 顾主拍板 A 方案） ──
    @staticmethod
    def _pulse_recent_logs(logs: List[dict], hours: float, now=None) -> List[dict]:
        """按 ts(HH:MM) 取最近 hours 小时内的日志（与 memory 注入同款 6h 窗口）。

        2026-09-05 顾主反馈「发过来的还是全天 177 条整消息，不是 6 小时窗口内的」
        ——唤即看/回看链路套上与记忆注入一致的滑动窗口。跨零点（凌晨 0 点后
        想回看昨晚 6h）时 jsonl 只有当天文件、无法回溯昨日，退化为取当天
        零点后全部；日志 ts 缺损的行直接剔除。now 可注入便于测试。
        """
        import datetime as _dt
        from zoneinfo import ZoneInfo

        if not logs:
            return []
        now = now or _dt.datetime.now(ZoneInfo("Asia/Shanghai"))
        floor_min = max(0, int(now.hour * 60 + now.minute - hours * 60))
        in_window = []
        for r in logs:
            ts = str(r.get("ts", ""))
            try:
                hh, mm = ts.split(":")
                r_min = int(hh) * 60 + int(mm)
            except Exception:  # noqa: BLE001
                continue  # ts 缺损/非 HH:MM，不注入
            if r_min >= floor_min:
                in_window.append(r)
        return in_window

    def _pulse_peek_day(self, raw: str) -> Optional[str]:
        """从顾主消息里解析回看日期：含「昨天」→ 昨天，含「前天」→ 前天，否则今天。

        只认明确词，不给模糊日期匹配，避免误读正常聊天内容。
        """
        import datetime
        from zoneinfo import ZoneInfo

        now = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
        if "前天" in raw:
            return (now - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        if "昨天" in raw:
            return (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        return now.strftime("%Y-%m-%d")

    async def _pulse_peek(self, event) -> bool:
        """顾主私聊发「看看家里」→ 回看旁轨日志（默认今天，可带 昨天/前天）。

        仅响应顾主私聊（FriendMessage + 顾主），其他会话直接放行不拦截；
        命中则 stop_event（主代理不再回话）+ 推送日志原文到顾主私聊。
        """
        import datetime
        from zoneinfo import ZoneInfo

        try:
            if not self._cfg("enable_side_pulse", False):
                return False
            if not self._pulse_is_doctor_private(event):
                return False
            raw = (event.get_message_str() or "").strip()
            if not raw:
                return False
            day = self._pulse_peek_day(raw)
            logs = self._pulse_read_day(day)
            label = day[5:]  # MM-DD
            # 2026-09-05 顾主拍板：今天回看套 6h 窗口（跟记忆注入一致），
            # 不再把全天 177 条整消息刷给顾主；昨天/前天仍按整天翻看。
            try:
                hours = float(self._cfg("side_pulse_recent_hours", 6) or 6)
                is_today = day == datetime.datetime.now(
                    ZoneInfo("Asia/Shanghai")
                ).strftime("%Y-%m-%d")
            except Exception:  # noqa: BLE001
                hours, is_today = 6.0, False
            if logs and is_today:
                win_logs = self._pulse_recent_logs(logs, hours)
                if win_logs:
                    logs = win_logs
                    label = f"{label} 最近{hours:g}小时"
            disp = label
            if logs:
                msg = self._build_digest_text(logs, disp)
            else:
                msg = f"🏠 家里动静 · {disp}\n（这段时间屋里没什么动静）"
            try:
                event.stop_event()
            except Exception:  # noqa: BLE001
                pass
            umo = self._pulse_draft_umo()
            if not umo:
                return False
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            await self.context.send_message(umo, MessageChain([Plain(msg)]))
            _logger.info("[side_pulse] 唤即看: 顾主回看 %s（%d 条）", day, len(logs))
            return True
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 唤即看失败(静默): %s", e)
            return False

    # ── cron 注册 / 拆除（幂等，biliread 同款） ──────────
    async def _pulse_clear_legacy(self, name: str) -> None:
        cm = getattr(self.context, "cron_manager", None)
        if cm is None:
            return
        for old in await cm.list_jobs():
            if getattr(old, "name", "") == name:
                await cm.delete_job(old.job_id)
                _logger.info("[side_pulse] 已清理遗留任务 %s(%s)", name, old.job_id)

    async def setup_pulse_jobs(self) -> None:
        """注册心跳 + 摘要两个定时任务（幂等：先清同名遗留再注册）。"""
        cm = getattr(self.context, "cron_manager", None)
        if cm is None:
            _logger.warning("[side_pulse] cron_manager 不可用，旁轨未注册")
            return
        self._pulse_rng = getattr(self, "_pulse_rng", None) or RandomStateManager(
            self._pulse_seen_path()
        )
        await self._pulse_clear_legacy(PULSE_TICK_JOB)
        await self._pulse_clear_legacy(PULSE_DIGEST_JOB)
        digest_cron = self._cfg("side_pulse_digest_cron", "50 21 * * *") or "50 21 * * *"
        self._pulse_job_ids = []
        j2 = await cm.add_basic_job(
            name=PULSE_DIGEST_JOB,
            cron_expression=digest_cron,
            handler=self.side_pulse_digest,
            description="旁路模块每日摘要推送给顾主",
            timezone="Asia/Shanghai",
        )
        self._pulse_job_ids = [getattr(j2, "job_id", None)]
        # 2026-09-05 顾主改版：心跳从「每小时 17 分」cron 换成作息式自管循环
        # （活跃窗 06:17→次日01:00 CST，窗内每 2h 区间随机跳一次，任意分钟）。
        # tick cron 不再注册；旧 cron job 由上方 _pulse_clear_legacy 清掉。
        loop_task = getattr(self, "_pulse_loop_task", None)
        if loop_task is None or loop_task.done():
            self._pulse_loop_task = asyncio.create_task(self._pulse_loop())

            def _loop_done(t, _self=self):
                if t.cancelled():
                    _logger.warning("[side_pulse] 心跳循环 task 被取消")
                elif t.exception() is not None:
                    _logger.error(
                        "[side_pulse] 心跳循环 task 异常退出: %r", t.exception()
                    )
                else:
                    _logger.info("[side_pulse] 心跳循环 task 正常退出（不应发生）")

            self._pulse_loop_task.add_done_callback(_loop_done)
        _logger.info(
            "[side_pulse] 已注册: tick=作息循环(%s→%s/%smin随机) digest=%s(%s)",
            self._cfg("side_pulse_window_start", "06:17"),
            self._cfg("side_pulse_window_end", "01:00"),
            self._cfg("side_pulse_interval_min", 120),
            digest_cron, self._pulse_job_ids[0],
        )

    async def teardown_pulse_jobs(self) -> None:
        cm = getattr(self.context, "cron_manager", None)
        ids = getattr(self, "_pulse_job_ids", None) or []
        for jid in ids:
            if not jid:
                continue
            try:
                await cm.delete_job(jid)
            except Exception:  # noqa: BLE001
                pass
        self._pulse_job_ids = []
        # 2026-09-05：心跳循环随插件卸载取消（cron 时代由 cron_manager 管，现自管）
        t = getattr(self, "_pulse_loop_task", None)
        if t is not None and not t.done():
            t.cancel()
        self._pulse_loop_task = None

    # ── 作息式心跳（2026-09-05 顾主改版） ──
    def _pulse_window(self, now) -> tuple:
        """心跳活跃窗（CST）：默认 06:17 → 次日 01:00，跨零点。

        凌晨 0:00~5:59 属于「昨晚开始的跨零点窗」（残区间到 01:00）。
        返回 (start, end, interval_sec)。窗口起止与区间长均可配：
        side_pulse_window_start / side_pulse_window_end / side_pulse_interval_min。
        """
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Shanghai")
        sh, sm = (
            int(x) for x in str(self._cfg("side_pulse_window_start", "06:17")).split(":")
        )
        eh, em = (
            int(x) for x in str(self._cfg("side_pulse_window_end", "01:00")).split(":")
        )
        interval = max(30, int(self._cfg("side_pulse_interval_min", 120) or 120)) * 60
        # 凌晨 hour<6：窗口起点取昨天（跨零点窗仍活跃到今晨 01:00）
        day = now.date() if now.hour >= 6 else now.date() - datetime.timedelta(days=1)
        start = datetime.datetime.combine(day, datetime.time(sh, sm), tzinfo=tz)
        end = datetime.datetime.combine(day, datetime.time(eh, em), tzinfo=tz)
        if end <= start:
            end += datetime.timedelta(days=1)  # 跨零点：次日 01:00
        return start, end, interval

    def _pulse_next_fire(self, now, rng):
        """下一次心跳时刻：当前所处 2h 区间内随机一点（任意分钟，像人）。

        区间从窗起点起每 interval_sec 切一段，末尾残区间（如 00:17→01:00）
        也随机跳一次然后到明早；随机点已过（重启恢复）则顺延到下区间。
        """
        start, end, interval = self._pulse_window(now)
        if now < start:
            seg0 = start
        else:
            n = int((now - start).total_seconds() // interval)
            seg0 = start + datetime.timedelta(seconds=n * interval)
        for _ in range(48):
            if seg0 >= end:
                # 今天窗口走完 → 明早起点区间随机
                return start + datetime.timedelta(
                    days=1, seconds=rng.uniform(0, interval)
                )
            seg_end = min(seg0 + datetime.timedelta(seconds=interval), end)
            fire = seg0 + datetime.timedelta(
                seconds=rng.uniform(0, (seg_end - seg0).total_seconds())
            )
            if fire > now:
                return fire
            seg0 = seg_end  # 随机点已过（重启恢复）→ 顺延
        return now + datetime.timedelta(minutes=10)

    async def _pulse_loop(self) -> None:
        """作息式心跳循环：睡到区间随机点→跳一次→睡到下一区间随机点。

        替代原「每小时 17 分」cron（2026-09-05 顾主拍板）：早上 6:17 开始
        活跃，凌晨 1 点结束，期间每两小时区间随机心跳一次，更像人的日常
        作息。窗外静默；异常 60s 退避；插件卸载时被 cancel。
        """
        rng = random.Random()
        _logger.info("[side_pulse] 作息心跳循环已启动")
        while True:
            try:
                from zoneinfo import ZoneInfo

                now = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
                fire = self._pulse_next_fire(now, rng)
                delay = (fire - now).total_seconds()
                _logger.info(
                    "[side_pulse] 下次心跳: %s（%.0f 分钟后）",
                    fire.strftime("%m-%d %H:%M"), max(0.0, delay) / 60,
                )
                await asyncio.sleep(max(5.0, delay))
                await self.side_pulse_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                _logger.warning("[side_pulse] 作息心跳异常(静默): %s", e)
                await asyncio.sleep(60)

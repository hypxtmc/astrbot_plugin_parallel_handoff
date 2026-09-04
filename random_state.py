"""三期 · 个体状态种子（random_state.py）

顾主 2026-09-03 拍板三期重版：接话人不能被算死、每人日常话题随机演化。
本模块是 M1 —— 状态机层（纯规则，不含 LLM）：
  · DailyState：每人每日红：心情基调 / 手头事 / 今日话题域 + 今日种子。
  · RandomStateManager：按 unified_msg_origin 场景隔离；每天随机重掷一次。
  · 供 M2 注入器（GLM-4-Flash 离线写心情）与 M3 接话权重叠加消费。
  · 绝不依赖 LLM：无注入器时也走纯规则随机兜底，绝不掉链子。

边界：只做「今日随机状态」这一层，不推翻读空气四段 R1/R2/R4 主判据。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_DEFAULT_TIMEZONE = "Asia/Shanghai"

# ── 生活域池（每人今日话题从这里随机演化，天天不一样） ──────────
LIFE_DOMAINS: List[str] = [
    "工作",     # 手上正干的活
    "生活",     # 吃/睡/日常琐事
    "兴趣",     # 爱好、最近沉迷的东西
    "吐槽",     # 今日烦心事、小牢骚
    "深夜随笔", # 感性时刻、胡思乱想
    "出差/外勤",# 出门的事、所见所闻
    "八卦/事",  # 身边人/组织大小事
    "私房",     # 想他的私密时刻（2026-09-04 顾主拍板 A：真随机也能下流一点）
]

# 心情基调池
MOOD_POOL: List[str] = [
    "专注", "亢奋", "平静", "慵懒", "急躁", "愉悦", "低落", "好奇",
]

# 生活域→领域关键词：供 M3 今日状态契合度（_daily_affinity）做纯关键词匹配。
# 命中即给该 agent 今日状态加分——谁今日话题契合当前消息，谁更自然接上。
DOMAIN_KEYWORDS: Dict[str, List[str]] = {
    "工作":     ["项目", "任务", "开会", "报告", "代码", "问题", "怎么弄", "怎么改", "搞定", "进度", "交活"],
    "生活":     ["吃", "饭", "睡", "累", "饿", "收拾", "回家", "出门", "买菜", "天气"],
    "兴趣":     ["歌", "画", "音乐", "电影", "书", "游戏", "乐器", "展览", "好看", "听了"],
    "吐槽":     ["烦", "受不了", "气死", "糟心", "无语", "唉", "坑", "讨厌", "累死", "烦人"],
    "深夜随笔": ["想", "旧", "回忆", "过去", "以前", "如果", "发呆", "梦", "夜", "感"],
    "出差/外勤":["出门", "外勤", "跑", "路上", "回来", "去趟", "出差", "赶车", "返程", "见过"],
    "八卦/事":  ["听说", "听说没", "你知道吗", "传言", "吃瓜", "新鲜事", "消息", "啥事", "谁", "咋"],
    "私房":     ["想他", "昨晚", "腰", "床", "被窝", "抱", "亲", "睡不着", "澡", "热"],
}

# 手头事池（配合生活域，给出今日在忙的具体事）
HAND_FLAVOR: Dict[str, List[str]] = {
    "工作":     ["在拆一个难缠的报错", "在赶一份清单", "刚开完会", "在查资料"],
    "生活":     ["刚吃完饭", "在收拾房间", "泡了杯东西", "想出去走走"],
    "兴趣":     ["在鼓捣喜欢的东西", "刚看了点有意思的", "在听歌", "在写点什么"],
    "吐槽":     ["被一件事烦到了", "今天有点不顺", "刚才被噎了一句", "腰有点累"],
    "深夜随笔": ["在想些有的没的", "翻到旧东西发呆", "有点想说话", "在放空"],
    "出差/外勤":["刚跑完一单", "在外头晃", "路上遇到点事", "在等返程"],
    "八卦/事":  ["听说件新鲜事", "在打听点什么", "刚听人聊了件事", "在琢磨个消息"],
    "私房":     ["刚洗完澡躺床上", "在被窝里翻来覆去", "想起昨晚的事有点走神", "一个人待着有点想他"],
}

# ── 角色底色锚（persona_domain_weights）──────────────────────────
# 顾主 2026-09-03 拍板：随机生活必须"贴角色性格的小邻域里演化"，不被算死也不脱底色。
# 每个子代理给 8 个生活域一组相对权重（0 则永不出现在该域的底色抽取池），
# 掷 domain 时按权重倾斜；未收录的 agent 回退旧纯随机，行为不变。
# 5% 概率完全跳脱（WILD_CHANCE）：无视底色、从全域均匀抽，保留生活趣味。
# 数值全是"相对倾向"，不是硬比例；调某域权重即可微调某角色的生活底色。
PERSONA_DOMAIN_WEIGHTS: Dict[str, Dict[str, float]] = {
    # 助手A：领袖副手兼照顾者——工作操心 + 生活日常交替（私房给极低：害羞款）
    "agent_a":     {"工作": 3, "生活": 3, "八卦/事": 2, "兴趣": 2, "深夜随笔": 1, "私房": 0.5},
    # 助手B：总工程师——焊板子赶工是常态，深夜随笔是补觉前的胡话（放得开，私房权重高）
    "agent_b":   {"工作": 5, "吐槽": 3, "深夜随笔": 3, "兴趣": 2, "生活": 1, "私房": 2},
    # 助手C：隐退的温柔魔王，缝纫幼师——日常烟火气+偶尔旧事（温柔款私房）
    "agent_c":  {"生活": 4, "深夜随笔": 3, "兴趣": 2, "八卦/事": 2, "工作": 1, "私房": 1},
    # 助手D：话少深情的前深海猎人——外勤奔波 + 沉默的思绪（沉默的想他款）
    "agent_d":     {"工作": 3, "出差/外勤": 3, "深夜随笔": 2, "生活": 2, "私房": 1},
    # 夕：闷骚家里蹲画师——画室宅 + 兴趣驱动 + 深夜放空（脸皮薄，极低）
    "xi":        {"兴趣": 5, "生活": 3, "深夜随笔": 2, "吐槽": 1, "私房": 0.5},
    # 令：饮酒吟诗的岁兽——深夜随笔/诗酒是灵魂底色（微醺时想他款）
    "ling":      {"深夜随笔": 5, "兴趣": 3, "八卦/事": 2, "生活": 2, "私房": 1},
    # 年：火锅电影锻造的烟火气热闹匠人（放得开，私房权重高）
    "nian":      {"兴趣": 4, "生活": 3, "工作": 2, "吐槽": 2, "八卦/事": 1, "私房": 2},
    # 黍：种田持家的妈妈式岁兽——生活烟火占绝对主导（放得开，私房权重高）
    "shu":       {"生活": 5, "工作": 2, "兴趣": 2, "八卦/事": 1, "私房": 2},
    # 助手F：歌姬偶像——舞台工作 + 音乐兴趣是主旋律（放得开，私房权重高）
    "agent_f":     {"工作": 4, "兴趣": 4, "生活": 2, "深夜随笔": 1, "私房": 2},
    # M3：医疗顾问新战士——门诊工作 + 求知兴趣 + 吐槽战场见闻（黏人款私房）
    "m3":        {"工作": 4, "兴趣": 3, "吐槽": 2, "生活": 2, "私房": 1},
    # 助手E：组织操盘手——政务工作几乎就是她的全部日常（极低，工作占满）
    "agent_e":   {"工作": 6, "吐槽": 2, "深夜随笔": 1, "生活": 1, "私房": 0.5},
}

# 5% 完全随机事件：跳出角色底色邻域，掷一个"今天有点不一样"的日子
WILD_CHANCE: float = 0.05
# B（2026-09-04 顾主拍板）：跳脱里再 roll 一次"今天格外想他"——
# 5% 跳脱 × 30% = 1.5% 总概率强制进私房域，素日子为主、偶尔来一下
WILD_PRIVATE_CHANCE: float = 0.30

# 心情底色锚：mood 也可按角色倾向倾斜（默认全池均匀；收录的按权重）
PERSONA_MOOD_WEIGHTS: Dict[str, Dict[str, float]] = {
    "agent_a":    {"平静": 3, "专注": 2, "愉悦": 2, "亢奋": 1},
    "agent_b":  {"专注": 3, "慵懒": 3, "亢奋": 2, "愉悦": 1},
    "agent_c": {"平静": 4, "愉悦": 3, "专注": 1, "好奇": 1},
    "agent_d":    {"平静": 4, "专注": 2, "低落": 1, "愉悦": 1},
    "xi":       {"平静": 3, "慵懒": 3, "专注": 2, "好奇": 1},
    "ling":     {"慵懒": 3, "愉悦": 3, "平静": 2, "亢奋": 1},
    "nian":     {"亢奋": 3, "愉悦": 3, "好奇": 2, "专注": 1},
    "shu":      {"平静": 3, "愉悦": 3, "专注": 2, "亢奋": 1},
    "agent_f":    {"亢奋": 3, "愉悦": 3, "好奇": 2, "专注": 1},
    "m3":       {"专注": 3, "好奇": 3, "愉悦": 2, "亢奋": 1},
    "agent_e":  {"专注": 4, "平静": 3, "慵懒": 1},
}

# 心情池（全量，供 5% 跳脱与未收录 agent 用）
MOOD_POOL_ALL: List[str] = [
    "专注", "亢奋", "平静", "慵懒", "急躁", "愉悦", "低落", "好奇",
]

def _weighted_pick(
    rng: random.Random,
    weights: Dict[str, float],
    fallback_pool: List[str],
) -> str:
    """带权抽取：只从权重表里挑域内合法项；权重表空/全非法则回退均匀 choice。

    保底逻辑保证：即便权重表配错（key 不在池里），也不会抛异常或返回池外值。
    """
    if not weights:
        return rng.choice(fallback_pool)
    pool: List[str] = []
    w: List[float] = []
    for k, v in weights.items():
        if k in fallback_pool and v > 0:
            pool.append(k)
            w.append(v)
    if not pool:
        return rng.choice(fallback_pool)
    return rng.choices(pool, weights=w, k=1)[0]


@dataclass
class DailyState:
    """每人在某场景一天的随机状态。"""
    agent: str
    day: str = ""                       # 状态所属日期（YYYY-MM-DD），跨天自动重掷
    mood: str = "平静"
    domain: str = "生活"
    hand: str = ""                      # 今日手头事的具体描述
    seed: int = 0                       # 当日确定性种子（驱动话题/情绪演化）
    llm_updated: bool = False           # 标记是否为 LLM 注入（M2）

    @property
    def summary(self) -> str:
        """发给接话判定的简洁状态串。"""
        return f"{self.mood}/{self.domain}" + (f"/{self.hand}" if self.hand else "")

    def live(self) -> bool:
        """状态是否仍属于今天（跨天即失效，进入重掷）。"""
        return self.day == _today()


def _today(now: Optional[float] = None) -> str:
    """按 Asia/Shanghai 求今天的 YYYY-MM-DD。传 now（unix 秒）可求该时点日期（测试/跨日模拟用）。"""
    import datetime

    t = time.localtime(now) if now else time.localtime()
    try:
        import zoneinfo

        z = zoneinfo.ZoneInfo(_DEFAULT_TIMEZONE)
        if now is not None:
            # 用传入 now 映射到目标时区的日期，保证跨日/测试可正确推进
            dt = datetime.datetime.fromtimestamp(now, tz=z)
        else:
            dt = datetime.datetime.now(z)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d", t)


def _deterministic_seed(agent: str, day: str) -> int:
    """以 agent+day 求确定性种子：同一天内对同一人的抽取可复现（去抖动）。"""
    return int(hashlib.md5(f"{agent}:{day}".encode("utf-8")).hexdigest()[:8], 16)


# ── 近 N 天手头事去重池（修"固定池撞车"，2026-09-04）──────────
# 顾主 16:45 实证：黍的"刚开完会（一个人）"8-26 与 8-30 撞车、
# "在收拾房间，刚歇口气"8-29 与 9-01 撞车——根因是 HAND_FLAVOR 每域
# 只有 4 条，确定性 seed 跨天独立，隔几天必撞。加一个去重池记录每
# agent 每天最终掷出的 hand（按 domain 记），供跨天偏置：过去 N 天用过的
# 同 domain hand 优先不重复。纯函数 roll_daily_state 不被污染（avoid 为空
# 行为完全不变，测试确定性成立）；写盘失败绝不致命（降级旧行为）。
# 生产由 RandomStateManager.seen_path 注入真实路径；测试默认 None 不写盘。
_HAND_SUFFIXES = ("（一个人）", "，刚歇口气")
_ROLL_AVOID_DAYS = 7

_DEFAULT_SEEN_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "random_state_seen.json"
)


def _base_hand(hand: str) -> str:
    """剥离手头事尾部个性后缀，得到池内原始手头事（供去重比对）。"""
    for s in _HAND_SUFFIXES:
        if hand.endswith(s):
            return hand[: -len(s)]
    return hand


def _seen_load(path: Optional[str] = None) -> dict:
    """读去重池；path 为空/缺失/损坏 → 空 dict（绝不致命）。"""
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _seen_save(data: dict, path: Optional[str] = None) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _recent_avoid_hands(
    agent: str, domain: str, day: str, path: Optional[str] = None
) -> list:
    """过去 _ROLL_AVOID_DAYS 天内该 agent 同 domain 用过的 base hand（不含今天）。"""
    import datetime

    data = _seen_load(path)
    if not data:
        return []
    try:
        today = datetime.date.fromisoformat(day)
        avoid = []
        for i in range(1, _ROLL_AVOID_DAYS + 1):
            d = (today - datetime.timedelta(days=i)).isoformat()
            rec = (data.get(d) or {}).get(agent)
            if (
                isinstance(rec, dict)
                and rec.get("domain") == domain
                and rec.get("hand")
            ):
                avoid.append(rec["hand"])
        return avoid
    except Exception:
        return []


def _record_seen(
    agent: str, domain: str, hand: str, day: str, path: Optional[str] = None
) -> None:
    """记录该 agent 今天掷出的 base hand（供未来跨天去重）。"""
    if not path:
        return
    try:
        data = _seen_load(path)
        data.setdefault(day, {})[agent] = {"domain": domain, "hand": _base_hand(hand)}
        _seen_save(data, path)
    except Exception:
        pass


def roll_daily_state(
    agent: str,
    now: Optional[float] = None,
    avoid_hands: Optional[List[str]] = None,
) -> DailyState:
    """为 agent 掷一个今日随机状态（确定性随机：同日同人结果稳定）。

    2026-09-03 顾主拍板加角色底色锚：
      · 默认按 PERSONA_DOMAIN_WEIGHTS / PERSONA_MOOD_WEIGHTS 带权抽取，
        让状态贴角色性格（助手B多工作吐槽、令多深夜随笔、黍多生活烟火）
      · WILD_CHANCE=5% 概率完全跳脱：无视底色从全域均匀抽，
        保留"今天有点不一样"的生活趣味（不跳脱也不会被算死——权重只倾斜非锁死）
      · 未收录 agent：完全回退旧行为（均匀 choice），零影响
    """
    day = _today(now)
    seed = _deterministic_seed(agent, day)
    rng = random.Random(seed)

    # 5% 跳脱判定用独立派生 seed，避免与 domain/mood 抽取共享序列时
    # 因抽取顺序/池大小造成系统性偏移（否则确定性 seed 下 wild 率会偏）。
    wild = random.Random(f"{seed}:wild").random() < WILD_CHANCE

    if wild:
        # 跳脱：完全无视底色，全域均匀
        mood = rng.choice(MOOD_POOL_ALL)
        # B：跳脱里 30% 概率强制私房域（今天格外想他），独立派生 seed 保确定性
        wild_private = (
            random.Random(f"{seed}:wild_private").random() < WILD_PRIVATE_CHANCE
        )
        domain = "私房" if wild_private else rng.choice(LIFE_DOMAINS)
    else:
        mood = _weighted_pick(
            rng,
            PERSONA_MOOD_WEIGHTS.get(agent, {}),
            MOOD_POOL_ALL,
        )
        domain = _weighted_pick(
            rng,
            PERSONA_DOMAIN_WEIGHTS.get(agent, {}),
            LIFE_DOMAINS,
        )
    flavors = HAND_FLAVOR.get(domain, ["在忙点事"])
    # 跨天去重：avoid_hands 传近 N 天用过的同 domain base hand，剔除后再抽，
    # 撞车不再；剔除后没剩候选则回退全池（保证有结果）。avoid 为空行为不变。
    pool = flavors
    if avoid_hands:
        pool = [f for f in flavors if f not in avoid_hands] or flavors
    hand = rng.choice(pool)
    # 手头事带个性后缀，让同领域的表达也不重样
    suffix = rng.choice(["", "（一个人）", "，刚歇口气"])
    return DailyState(
        agent=agent,
        day=day,
        mood=mood,
        domain=domain,
        hand=hand + suffix,
        seed=seed,
    )


class RandomStateManager:
    """按 unified_msg_origin 场景隔离管理每人每日状态。

    模式与读空气 _presences 对齐：{unified_msg_origin -> {agent -> DailyState}}。
    惰性初始化：第一次取某场景/某人状态时才生成，避免为无谓场景堆内存。
    """

    def __init__(self, seen_path: Optional[str] = None) -> None:
        self._states: Dict[str, Dict[str, DailyState]] = {}
        # 近 N 天去重池文件；None=不写盘（测试/默认纯内存，行为等同旧版）
        self.seen_path: Optional[str] = seen_path

    # ── 核心存取 ─────────────────────────────
    def get(
        self,
        scene: str,
        agent: str,
        now: Optional[float] = None,
        auto_roll: bool = True,
    ) -> DailyState:
        """取某场景某 agent 的今日状态；越界/跨天时自动重掷（auto_roll）。"""
        scene_map = self._states.setdefault(scene, {})
        st = scene_map.get(agent)
        if st is None or not st.live():
            if not auto_roll:
                if st is None:
                    return roll_daily_state(agent, now)
                return st
            st = self._roll_with_avoid(agent, now)
            _record_seen(agent, st.domain, st.hand, st.day, self.seen_path)
            scene_map[agent] = st
        return st

    def _roll_with_avoid(self, agent: str, now: Optional[float] = None) -> DailyState:
        """掷今日状态；若撞近 N 天用过的 base hand，剔除后重掷一次（有限重试）。

        去重偏置只在此生产入口生效；失败绝不致命（降级为普通掷取）。
        """
        st = roll_daily_state(agent, now)
        try:
            avoid = _recent_avoid_hands(agent, st.domain, st.day, self.seen_path)
            if avoid and _base_hand(st.hand) in avoid:
                # 撞车 → 用 avoid 剔池重掷（保留原 mood/domain），Swap 手头事
                st.hand = roll_daily_state(agent, now, avoid_hands=avoid).hand
        except Exception:
            pass
        return st

    def set_llm(self, scene: str, agent: str, mood: str, domain: str, hand: str = "") -> DailyState:
        """M2 注入器写入口：用 LLM 读对话得出的状态覆盖今日状态。"""
        st = roll_daily_state(agent)
        st.mood = mood or st.mood
        st.domain = domain if domain in LIFE_DOMAINS else st.domain
        st.hand = hand or st.hand
        st.llm_updated = True
        self._states.setdefault(scene, {})[agent] = st
        return st

    def update_scene(self, scene: str, agent: str, st: DailyState) -> None:
        """外部覆写（测试/注入用）。"""
        self._states.setdefault(scene, {})[agent] = st

    # ── 查询 ────────────────────────────────
    def all(self, scene: str) -> Dict[str, DailyState]:
        return dict(self._states.get(scene, {}))

    # ── M3 · 今日状态契合度（纯关键词，零 LLM） ──────────
    def daily_affinity(
        self,
        scene: str,
        agent: str,
        message: str,
        now: Optional[float] = None,
    ) -> int:
        """计算 agent 今日状态对当前消息的契合得分。

        思路：命中的今日话题域关键词越多，越说明「他今天正处在这个话题上」，
        越自然接这话。同一个人不会天天稳坐接话位——明天换 domain 契合度就变了。

        返回 0..len(命中词) 的计数；错误归零（绝不因状态缺失炸调用方）。
        """
        try:
            st = self.get(scene, agent, now=now)
        except Exception:
            return 0
        if not st:
            return 0
        kws = DOMAIN_KEYWORDS.get(st.domain, [])
        if not kws:
            return 0
        low = (message or "").lower()
        return sum(1 for k in kws if k.lower() in low)

    def best_affinity(
        self,
        scene: str,
        candidates: List[str],
        message: str,
        now: Optional[float] = None,
    ) -> str | None:
        """在候选接话人里挑今日状态最契合消息的一个。

        平手时返回最先出现的；都 0 分返回 None（今天谁都不特别契合，交主代理自然接）。
        """
        best = None
        best_score = 0
        for a in candidates:
            s = self.daily_affinity(scene, a, message, now)
            if s > best_score:
                best = a
                best_score = s
        return best

    def agents(self, scene: str) -> List[str]:
        return list(self._states.get(scene, {}).keys())

    def summary(self, scene: str) -> Dict[str, str]:
        """供接话判定/日志的紧凑视图：agent -> "mood/domain/hand"。"""
        return {
            a: s.summary for a, s in self._states.get(scene, {}).items()
        }

    def clear(self, scene: Optional[str] = None) -> None:
        if scene is None:
            self._states.clear()
        else:
            self._states.pop(scene, None)
"""三期 · 个体状态种子（random_state.py）

用户 2026-09-03 拍板三期重版：接话人不能被算死、每人日常话题随机演化。
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
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("parallel_handoff.random_state")

_DEFAULT_TIMEZONE = "Asia/Shanghai"

# ── 四小时粒度（2026-09-26 用户拍板）────────────────────────────
# 原设计"每天一掷"：同一天里早八与晚十一的心情/手头事完全相同，用户实测把它
# 描述为"停滞时间"——时间对角色是"跳"的，不是"流"的。现在一天切 SLOTS_PER_DAY
# 段（每 SLOT_HOURS 小时一段），每段独立重掷：停滞感消失，且一天内有起伏。
# 兼容：day 字段语义不变（仍 YYYY-MM-DD），供按天落盘的去重池与既有测试用；
# 新增 slot 字段（YYYY-MM-DD:NN）承载"段"。live() 优先比 slot，slot 为空回落比 day。
SLOT_HOURS: int = 4
SLOTS_PER_DAY: int = 24 // SLOT_HOURS

# 跨段承接用的内存滚动窗口：记住该 agent 最近 N 段用过的手头事
# （配合 prev 字段让"在拆报错"能演化成下一段的事，而不是每段从零开始）
_RECENT_SLOT_MEMORY: int = 3



def _load_random_state_data() -> dict:
    """从插件 data/ 目录加载随机生活数据表（不进仓库；缺文件时用空默认）。

    life_domains 兜底 ["工作", "生活"] 防 rng.choice 空池崩溃，其余空即休眠。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "random_state_data.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            logger.info("[random_state] 已加载 random_state_data.json（%d 组数据）", len(data))
            return data
        return {}
    except FileNotFoundError:
        logger.info("[random_state] 数据文件 random_state_data.json 不存在，使用空默认")
        return {}
    except Exception as exc:
        logger.warning("[random_state] 数据文件 random_state_data.json 加载失败（用空默认继续）: %s", exc)
        return {}


_RS_DATA = _load_random_state_data()

# ── 生活域池（每人今日话题从这里随机演化，天天不一样） ──────────
LIFE_DOMAINS: List[str] = _RS_DATA.get("life_domains") or ["工作", "生活"]

# 心情基调池
MOOD_POOL: List[str] = [
    "专注", "亢奋", "平静", "慵懒", "急躁", "愉悦", "低落", "好奇",
]

# 生活域→领域关键词：供 M3 今日状态契合度（_daily_affinity）做纯关键词匹配。
# 命中即给该 agent 今日状态加分——谁今日话题契合当前消息，谁更自然接上。
DOMAIN_KEYWORDS: Dict[str, List[str]] = _RS_DATA.get("domain_keywords", {})

# 手头事池（配合生活域，给出今日在忙的具体事）
HAND_FLAVOR: Dict[str, List[str]] = _RS_DATA.get("hand_flavor", {})

# ── 角色底色锚（persona_domain_weights）──────────────────────────
# 用户 2026-09-03 拍板：随机生活必须"贴角色性格的小邻域里演化"，不被算死也不脱底色。
# 每个子代理给 8 个生活域一组相对权重（0 则永不出现在该域的底色抽取池），
# 掷 domain 时按权重倾斜；未收录的 agent 回退旧纯随机，行为不变。
# 5% 概率完全跳脱（WILD_CHANCE）：无视底色、从全域均匀抽，保留生活趣味。
# 数值全是"相对倾向"，不是硬比例；调某域权重即可微调某角色的生活底色。
PERSONA_DOMAIN_WEIGHTS: Dict[str, Dict[str, float]] = _RS_DATA.get("persona_domain_weights", {})

# 5% 完全随机事件：跳出角色底色邻域，掷一个"今天有点不一样"的日子
WILD_CHANCE: float = 0.05
# B（2026-09-04 用户拍板）：跳脱里再 roll 一次"今天格外感性"——
# 5% 跳脱 × 30% = 1.5% 总概率强制进私房域，素日子为主、偶尔来一下
WILD_PRIVATE_CHANCE: float = 0.30

# 心情底色锚：mood 也可按角色倾向倾斜（默认全池均匀；收录的按权重）
PERSONA_MOOD_WEIGHTS: Dict[str, Dict[str, float]] = _RS_DATA.get("persona_mood_weights", {})

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
    slot: str = ""                      # 状态所属时段（YYYY-MM-DD:NN，四小时一段）
    mood: str = "平静"
    domain: str = "生活"
    hand: str = ""                      # 本段手头事的具体描述
    prev: str = ""                      # 上一段的手头事（承接用，让生活有"线"）
    seed: int = 0                       # 本段确定性种子（驱动话题/情绪演化）
    llm_updated: bool = False           # 标记是否为 LLM 注入（M2）

    @property
    def summary(self) -> str:
        """发给接话判定的简洁状态串。"""
        return f"{self.mood}/{self.domain}" + (f"/{self.hand}" if self.hand else "")

    def live(self) -> bool:
        """状态是否仍属于当前时段（跨段即失效，进入重掷）。

        slot 为空时回落按 day 判定——兼容不传 slot 的旧调用（手造 DailyState / 老测试）。
        """
        if self.slot:
            return self.slot == _slot()
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


def _slot(now: Optional[float] = None) -> str:
    """按 Asia/Shanghai 求当前时段 key：YYYY-MM-DD:NN（NN = hour // SLOT_HOURS，0..5）。

    传 now（unix 秒）可求该时点所属时段（测试/跨段模拟用）。取时区失败则退本地时间，
    与 _today() 的容错口径一致——绝不因时区问题抛异常。
    """
    import datetime

    try:
        import zoneinfo

        z = zoneinfo.ZoneInfo(_DEFAULT_TIMEZONE)
        dt = (
            datetime.datetime.fromtimestamp(now, tz=z)
            if now is not None
            else datetime.datetime.now(z)
        )
    except Exception:
        dt = (
            datetime.datetime.fromtimestamp(now)
            if now is not None
            else datetime.datetime.now()
        )
    return f"{dt.strftime('%Y-%m-%d')}:{dt.hour // SLOT_HOURS}"


def _deterministic_seed(agent: str, day: str) -> int:
    """以 agent+day 求确定性种子：同一天内对同一人的抽取可复现（去抖动）。"""
    return int(
        hashlib.md5(f"{agent}:{day}".encode("utf-8"), usedforsecurity=False).hexdigest()[:8], 16
    )


# ── 近 N 天手头事去重池（修"固定池撞车"，2026-09-04）──────────
# 用户 16:45 实证：固定手头事"刚开完会（一个人）"8-26 与 8-30 撞车、
# "在收拾房间，刚歇口气"8-29 与 9-01 撞车——根因是 HAND_FLAVOR 每域
# 只有 4 条，确定性 seed 跨天独立，隔几天必撞。加一个去重池记录每
# agent 每天最终掷出的 hand（按 domain 记），供跨天偏置：过去 N 天用过的
# 同 domain hand 优先不重复。纯函数 roll_daily_state 不被污染（avoid 为空
# 行为完全不变，测试确定性成立）；写盘失败绝不致命（降级旧行为）。
# 生产由 RandomStateManager.seen_path 注入真实路径；测试默认 None 不写盘。
_HAND_SUFFIXES = ("（一个人）", "，刚歇口气")
_ROLL_AVOID_DAYS = 7

def _resolve_seen_file() -> str:
    """去重池落盘位置 —— 跟随 AstrBot 实例根，不是插件自己的目录。

    插件目录在试验场里是软链，写它等于把假卡灌进生产（2026-09-26 光种暴露）。
    优先框架 data 路径（内部认 ASTRBOT_ROOT / cwd），退环境变量，再退插件目录。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return os.path.join(get_astrbot_data_path(), "random_state_seen.json")
    except Exception:
        root = os.environ.get("ASTRBOT_ROOT")
        base = (
            os.path.join(root, "data")
            if root
            else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        )
        return os.path.join(base, "random_state_seen.json")


_DEFAULT_SEEN_FILE = _resolve_seen_file()


def sync_seen_path(rng, target: Optional[str]) -> bool:
    """把实例手里的去重池路径同步到 target；返回是否真的换了。

    热重载不重建实例对象：路径口径变更（2026-09-26 插件目录 → 实例 data）
    也得就地换，否则新路径只在新进程里成立，热重载完还在往旧位置写（假绿）。
    """
    if not target or getattr(rng, "seen_path", None) == target:
        return False
    rng.seen_path = target
    return True


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
    prev_hand: str = "",
) -> DailyState:
    """为 agent 掷一个当前时段的随机状态（确定性随机：同段同人结果稳定）。

    2026-09-03 用户拍板加角色底色锚：
      · 默认按 PERSONA_DOMAIN_WEIGHTS / PERSONA_MOOD_WEIGHTS 带权抽取，
        让状态贴合各角色性格（按类型倾斜）
      · WILD_CHANCE=5% 概率完全跳脱：无视底色从全域均匀抽，
        保留"今天有点不一样"的生活趣味（不跳脱也不会被算死——权重只倾斜非锁死）
      · 未收录 agent：完全回退旧行为（均匀 choice），零影响
    """
    day = _today(now)
    slot = _slot(now)
    seed = _deterministic_seed(agent, slot)
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
        slot=slot,
        mood=mood,
        domain=domain,
        hand=hand + suffix,
        prev=prev_hand,
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
        # 跨段承接（2026-09-26）：该 agent 上一段的手头事 + 近 _RECENT_SLOT_MEMORY 段
        # 用过的手头事。纯内存滚动，重启即空（按天去重仍由 seen_path 落盘保底）。
        self._last_hand: Dict[str, str] = {}
        self._recent_hands: Dict[str, List[str]] = {}

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
        """掷当前时段状态；跨段承接 + 双层去重（近段内存 / 近 N 天落盘）。

        2026-09-26 用户拍板改：
          · 承接：把上一段的 base hand 写进 st.prev，让生活有"线"而不是每段从零开始。
          · 近段去重：同 agent 近 _RECENT_SLOT_MEMORY 段用过的不再抽——四小时一段
            之后撞车窗口从"隔几天"缩短到"隔几段"，只靠按天落盘的池子不够。
          · 跨天去重：原逻辑保留（按 day 落盘，跨重启仍在）。
        任何一步失败都降级为普通掷取，绝不致命。
        """
        st = roll_daily_state(agent, now)
        try:
            # 承接：上一段的手头事（同场景内存，跨段自动滚动）
            _prev = self._last_hand.get(agent, "")
            if _prev:
                st.prev = _prev
            # 近段去重：内存窗口优先剔池
            _recent = list(self._recent_hands.get(agent, []))
            if _recent and _base_hand(st.hand) in _recent:
                st.hand = roll_daily_state(agent, now, avoid_hands=_recent).hand
            # 跨天去重：原逻辑（剔除后没剩候选则内部回退全池，保证有结果）
            avoid = _recent_avoid_hands(agent, st.domain, st.day, self.seen_path)
            if avoid and _base_hand(st.hand) in avoid:
                st.hand = roll_daily_state(agent, now, avoid_hands=avoid).hand
        except Exception:
            pass
        # 记账：本段成为下一段的 prev；窗口只留最近 N 段
        try:
            _base = _base_hand(st.hand)
            self._last_hand[agent] = _base
            _buf = self._recent_hands.setdefault(agent, [])
            _buf.append(_base)
            del _buf[:-_RECENT_SLOT_MEMORY]
        except Exception:
            pass
        return st

    def set_llm(self, scene: str, agent: str, mood: str, domain: str, hand: str = "") -> DailyState:
        """M2 注入器写入口：用 LLM 读对话得出的状态覆盖今日状态。

        2026-09-26 用户实证「同一角色连续两天重复同一动作」后补记账：
        原实现只覆盖 st、不写任何历史，于是 recent_hands()（近段窗口）与跨天落盘池
        对 LLM 卡永远是空的——注入侧「禁复清单」与 random_state「跨天去重」两层
        对生产主力路径（卡由 GLM 写）全部空转，模型每天从零重编同一套先验。
        现在与 _roll_with_avoid 同口径记账：prev 承接 + 近 N 段滚动窗口 + 跨天落盘池。
        """
        st = roll_daily_state(agent)
        st.mood = mood or st.mood
        st.domain = domain if domain in LIFE_DOMAINS else st.domain
        st.hand = hand or st.hand
        st.llm_updated = True
        # 承接：上一段的手头事当本段起点（与规则路径同口径，让日子有"线"）
        try:
            _prev = self._last_hand.get(agent, "")
            if _prev:
                st.prev = _prev
        except Exception:
            pass
        self._states.setdefault(scene, {})[agent] = st
        # 记账：LLM 卡也必须进近段窗口与跨天池，否则明天的 avoid 永远是空的
        try:
            _base = _base_hand(st.hand or "")
            if _base:
                self._last_hand[agent] = _base
                _buf = self._recent_hands.setdefault(agent, [])
                _buf.append(_base)
                del _buf[:-_RECENT_SLOT_MEMORY]
                _record_seen(agent, st.domain, st.hand, st.day, self.seen_path)
        except Exception:
            pass
        return st

    def update_scene(self, scene: str, agent: str, st: DailyState) -> None:
        """外部覆写（测试/注入用）。"""
        self._states.setdefault(scene, {})[agent] = st

    # ── 查询 ────────────────────────────────
    def all(self, scene: str) -> Dict[str, DailyState]:
        return dict(self._states.get(scene, {}))

    def recent_hands(self, agent: str) -> List[str]:
        """该 agent 近 _RECENT_SLOT_MEMORY 段用过的手头事（去后缀）。

        供注入层拼"这几样你已经说过了"的硬性禁复清单——只提一句"别重复"模型不会听，
        把具体条目摊在它面前才会收敛。内存滚动，重启即空（返回空列表，调用方自然降级）。
        """
        try:
            return list(self._recent_hands.get(agent, []))
        except Exception:
            return []

    def avoid_hands_for(self, agent: str, days: int = _ROLL_AVOID_DAYS) -> List[str]:
        """该 agent 近 days 天用过的全部 base hand（不分 domain），供 LLM 写卡时当禁复清单。

        2026-09-26 加：LLM 写卡那一刻还不知道 domain（domain 是它自己的输出），所以这里
        不按 domain 过滤——宁可给严一点，也别让它把昨天刚端上来的原样再端一次。
        去重池为空/未落盘（seen_path=None）时返回 []，调用方自然降级、不阻塞注入。
        """
        try:
            import datetime

            data = _seen_load(self.seen_path)
            if not data:
                return []
            today = datetime.date.fromisoformat(_today())
            out: List[str] = []
            seen: set = set()
            for i in range(1, days + 1):
                d = (today - datetime.timedelta(days=i)).isoformat()
                rec = (data.get(d) or {}).get(agent)
                if isinstance(rec, dict) and rec.get("hand"):
                    h = rec["hand"]
                    if h not in seen:
                        seen.add(h)
                        out.append(h)
            return out
        except Exception:
            return []

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
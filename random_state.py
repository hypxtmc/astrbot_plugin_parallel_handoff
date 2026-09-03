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
}


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
    """按 Asia/Shanghai 求今天的 YYYY-MM-DD。"""
    import datetime

    t = time.localtime(now) if now else time.localtime()
    # 粗略本地时区取日的兜底：直接用系统本地时间
    try:
        import zoneinfo

        z = zoneinfo.ZoneInfo(_DEFAULT_TIMEZONE)
        dt = datetime.datetime.now(z)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d", t)


def _deterministic_seed(agent: str, day: str) -> int:
    """以 agent+day 求确定性种子：同一天内对同一人的抽取可复现（去抖动）。"""
    return int(hashlib.md5(f"{agent}:{day}".encode("utf-8")).hexdigest()[:8], 16)


def roll_daily_state(agent: str, now: Optional[float] = None) -> DailyState:
    """为 agent 掷一个今日随机状态（确定性随机：同日同人结果稳定）。"""
    day = _today(now)
    seed = _deterministic_seed(agent, day)
    rng = random.Random(seed)

    mood = rng.choice(MOOD_POOL)
    domain = rng.choice(LIFE_DOMAINS)
    flavors = HAND_FLAVOR.get(domain, ["在忙点事"])
    hand = rng.choice(flavors)
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

    def __init__(self) -> None:
        self._states: Dict[str, Dict[str, DailyState]] = {}

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
            st = roll_daily_state(agent, now)
            scene_map[agent] = st
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
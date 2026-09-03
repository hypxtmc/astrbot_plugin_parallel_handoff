"""arbitrate.py — parallel_handoff 读空气仲裁（P0 拆模块·段一状态机骨架）

对应 V2 计划书 docs/read_air_arbitrate_plan.md：
- 段一：ConversationPresence 在场状态模块 + ArbitrationMixin 状态记录方法
- 本文件当前只做【状态记录】，绝不碰任何路由行为（质量/安全优先的最低侵入原则）
- 仲裁判定方法 _arbitrate_directive / _arbitrate_tool 先留空壳，段二再填逻辑

设计核心（博士 2026-09-03 定性）：
① 随机性是三期任务，一期只做「宁静权」克制、不引入子代理日常随机演化；
② 旧怨只体现为贫嘴（刀子嘴豆腐心），不针锋相对，绝不作对抗性仲裁判据；
③ 读空气仲裁 = 在自动路由「该不该派子代理」的犹豫点追加宁静权克制，
   绝不比 _mode_shortcut_decision 更激进、绝不拦博士明确点名的人。
"""

import logging
from collections import deque

from astrbot.api.event import AstrMessageEvent

# random_state 双分支导入：astrbot 包加载→相对；顶层/测试→绝对（对齐 main.py try/except 哲学）
try:
    from .random_state import RandomStateManager
except ImportError:
    from random_state import RandomStateManager

_logger = logging.getLogger("parallel_handoff.arbitrate")

# 发言窗口默认宽度（记录最近 N 条发言，供下轮「读空气」参考）
DEFAULT_PRESENCE_WINDOW = 6
# 主代理虚拟发言人标记（会话 last_speaker 用 "__main__" 表示主代理回复）
MAIN_SPEAKER = "__main__"


class ConversationPresence:
    """单会话在场状态（按 unified_msg_origin 隔离）。

    原型：
        recent       最近 N 条发言 {agent, action(forward/main), text摘要, ts}
        last_speaker 最近发言者（agent_name 或 MAIN_SPEAKER）
        active_chain 是否处于连续承接/对话链中
        pending_batch 本批次待转发的子代理候选（parallel_handoff 多人候选）

    纯内存不持久化；随会话语境自然滚动（deque maxlen 窗口截断）。
    """

    def __init__(self, window: int = DEFAULT_PRESENCE_WINDOW):
        self.window = max(1, int(window))
        self.recent: deque = deque(maxlen=self.window)
        self.last_speaker: str | None = None
        self.active_chain: bool = False
        self.pending_batch: list[str] = []

    def record(self, agent: str, action: str, text: str = "", ts: float | None = None) -> None:
        """记录一条发言并更新 last_speaker / active_chain。

        agent   发言人（agent_name 或 MAIN_SPEAKER）
        action  动作类型：'forward'(子代理转发) / 'main'(主代理回复)
        text    文本摘要（仅记录前 40 字，够「读空气」判断说到哪即可，不存全文）
        """
        import time as _time

        ts = ts if ts is not None else _time.time()
        self.recent.append(
            {
                "agent": agent,
                "action": action,
                "text": (text or "")[:40],
                "ts": ts,
            }
        )
        self.last_speaker = agent
        # 主代理回复视为「断开接管」，后续承接链重置为 False
        self.active_chain = action != "main"

    def clear(self) -> None:
        """清空在场状态（会话切换/超时等场景兜底）"""
        self.recent.clear()
        self.last_speaker = None
        self.active_chain = False
        self.pending_batch = []


class ArbitrationMixin:
    """读空气仲裁 mixin：在场状态管理 + 仲裁判定（混入主壳类 MRO）。

    段一只实现状态管理（_presence_get / _presence_update）；
    _arbitrate_directive / _arbitrate_tool 为段二逻辑留空壳，默认不介入（返回 None/原 calls）。
    """

    # 每个会话的在场状态缓存：{unified_msg_origin -> ConversationPresence}
    _presences: dict = {}

    # 三期·每会话的今日随机状态管理（M3 接话权重叠加的数据源）
    # 单例复用（同一 mixin 实例内所有会话共享 RandomStateManager，按 scene 隔离）
    _daily_states: "RandomStateManager | None" = None

    # ── 开关路由 ─────────────────────────────────────────────
    def _read_air_enabled(self) -> bool:
        """读空气仲裁总开关（默认 False）。开启前零行为变化。"""
        return bool(self._cfg("enable_read_air_arbitrate", False))

    def _presence_window(self) -> int:
        """发言窗口宽度，可配，默认 6。"""
        try:
            return max(1, int(self._cfg("read_air_presence_window", DEFAULT_PRESENCE_WINDOW)))
        except (TypeError, ValueError):
            return DEFAULT_PRESENCE_WINDOW

    # ── 状态读取 ─────────────────────────────────────────────
    def _presence_get(self, event: AstrMessageEvent) -> ConversationPresence:
        """按 unified_msg_origin 惰性初始化并返回会话在场状态。"""
        key = getattr(event, "unified_msg_origin", None) or "default"
        p = self._presences.get(key)
        if p is None:
            p = ConversationPresence(window=self._presence_window())
            self._presences[key] = p
        return p

    # ── 三期·今日随机状态（M3 接话权重叠加） ────────────
    def _daily_states_get(self) -> "RandomStateManager":
        """惰性初始化今日状态管理器（复用单例）。"""
        if self._daily_states is None:
            self._daily_states = RandomStateManager()
        return self._daily_states

    def _daily_affinity_for(self, event: AstrMessageEvent, agent: str, message: str) -> int:
        """M3 今日状态契合度封装：agent 今日话题域对当前消息的命中数。

        纯关键词、零 LLM；状态/异常一律归零，绝不因数据缺失炸调用方。
        供观察日志 + 未来接话权重叠加使用（真实拦截仍待博士验收后开启）。
        """
        try:
            scene = getattr(event, "unified_msg_origin", None) or "default"
            return self._daily_states_get().daily_affinity(scene, agent, message)
        except Exception as e:
            _logger.warning("[read_air][daily] affinity failed (non-fatal): %s", e)
            return 0

    # ── 状态更新 ─────────────────────────────────────────────
    def _presence_update(
        self, event: AstrMessageEvent, agent: str, action: str = "forward", text: str = ""
    ) -> None:
        """在子代理转发后 / 主代理回复后记录发言。

        agent   实际发言人（agent_name 或 MAIN_SPEAKER）
        action  'forward' 或 'main'
        text    文本摘要（可按需传入）
        """
        try:
            p = self._presence_get(event)
            p.record(agent, action, text)
            _logger.debug(
                "[read_air] presence update: session=%s agent=%s action=%s last=%s chain=%s",
                getattr(event, "unified_msg_origin", "default"),
                agent,
                action,
                p.last_speaker,
                p.active_chain,
            )
        except Exception as e:  # 状态记录失败绝不影响主流程（安全优先）
            _logger.warning("[read_air] presence update failed (non-fatal): %s", e)

    # ── 路径 A · 自动路由宁静权仲裁（段二：低侵入日志提示） ──
    def _arbitrate_directive(
        self,
        event: AstrMessageEvent,
        message: str,
        route: str | None,
        mode_decision: bool | None,
    ) -> str | None:
        """自动路由路径的宁静权仲裁。

        入参：
            route           T1/T2 已判出的候选 agent_name（None=未命中，兜底主代理）
            mode_decision   _mode_shortcut_decision 的结果（是否放行主代理的裁决）

        返回：
            None   不介入（保持现有路由行为）
            'main' 建议克制落主代理（不短路、不抢派）

        段二节奏（博士 2026-09-03 12:31 拍板）：
            - 当前只做【低侵入日志提示】：在自动路由要短路抢派子代理之前，输出
              Read-Air 判断日志（它想不想克制），但【绝不实际拦截】路由结果。
            - 等【三期】的随机性日常演化补齐后，再实验性开启真实克制干预。
            总开关 enable_read_air_arbitrate 只在实验开启时置 True，平时默认 False 零影响。
        """
        if not self._read_air_enabled():
            return None

        # 沉默权/最后切底线（无论开不开都该守住，但段二也只做日志）：
        # 博士明确点名的人，读空气绝不建议抢它的派（点名神圣不可侵原则）。
        # 仍在读空气自己动手的路径上不拦——这里仅在自动路由犹豫点给提示。
        if not route or not mode_decision:
            # 无候选或 mode 已放行主代理 → 读空气无克制建议（本来就不短路）
            return None

        # ── 段二/三期低侵入：仅日志观察「读空气想不想克制」+「今日状态契合度」，不实际拦截 ──
        try:
            p = self._presence_get(event)
            quiet = self._read_air_wants_quiet(message, p)
            # 三期 M3：叠加今日状态契合度观察（谁今日话题最契合当前消息）
            affinity = self._daily_affinity_for(event, route, message)
            if quiet:
                _logger.info(
                    "[read_air][observe] 自动路由将短路 %s，但读空气倾向克制落主代理（宁静权）。"
                    "段二观察模式：不实际拦截，待三期后实验性开启。daily_affinity=%d",
                    route,
                    affinity,
                )
            else:
                _logger.info(
                    "[read_air][observe] 自动路由短路 %s，读空气判断无需克制。"
                    "今日状态契合度 daily_affinity=%d。",
                    route,
                    affinity,
                )
        except Exception as e:
            _logger.warning("[read_air] arbitrate observe failed (non-fatal): %s", e)
        # 段二不返 'main'：绝不实际拦截路由，只留日志供观察
        return None

    # ── 读空气是否倾向克制（纯规则，零 LLM，可被 _arbitrate_directive 调用） ──
    def _read_air_wants_quiet(self, message: str, presence: "ConversationPresence") -> bool:
        """判断此刻是否倾向「主代理自然接住」而非抢派子代理。

        零成本规则（不调 LLM），供观察日志用：
        R1 主代理刚回过话（last_speaker 是主代理）→ 顺气口让主代理继续，倾向克制
        R2 群聊性接句话（短、无点名、无技术特征）已在承接链中 → 倾向让主代理收
        R3 消息在 subagent 连续承接链深处 → 倾向克制（不插话打断）
        R4 关系网旧怨子代理 → 特意偏向让主代理兜，避免两旧怨组针锋相对

        返回 True=倾向克制落主代理，False=可让子代理接。
        段二仅作日志信号，不实际干预。
        """
        # R1 主代理刚回过话 → 倾向让主代理继续，别抢
        if presence.last_speaker == MAIN_SPEAKER:
            return True
        # R2 连续承接链已确立，主代理疑似被晾着 → 若消息短且无技术特征倾向克制
        # step1: 承接链很深（最近都在子代理之间互抛）→ 倾向主代理收尾
        if presence.active_chain and len(presence.recent) >= 2:
            # R4 旧怨组：普瑞赛斯/凯尔希近条密集互抛 → 强烈倾向主代理兜，避免针锋相对
            grp = {
                "presis": ["presis", "普瑞赛斯"],
                "kaltsit": ["kaltsit", "凯尔希"],
            }
            recent_agents = [r["agent"] for r in presence.recent]
            for g1, g2 in [("presis", "kaltsit")]:
                c1 = sum(1 for a in recent_agents if a in grp[g1])
                c2 = sum(1 for a in recent_agents if a in grp[g2])
                if c1 >= 2 and c2 >= 2:
                    return True  # 旧怨组互抛 → 主代理兜住，避免针锋相对
            return True
        # 其余默认让子代理按现有规则走
        return False

    # ── 路径 B · LLM 工具侧收敛（段三：温和收敛建议，绝不砍 calls） ──
    def _arbitrate_tool(self, event: AstrMessageEvent, calls: list) -> list:
        """LLM 工具（parallel_handoff / call_subagent）调用前的收敛建议。

        返回：原 calls（绝不过滤；「不砍 calls」是 V2 关键约束，多人并行是主代理显式意图。
        段三只做两件事，均不改变路由结果：
            1. 更新 pending_batch，记录本批 candidate，供路径 A 下轮「读空气」参考。
            2. 温和收敛建议：当博士 UI 只点名一人、但本批 calls 却误带多人时，
               打日志提示主代理「本批是否只需某人」——仅建议，绝不强制。
        """
        if not self._read_air_enabled():
            return calls
        canonical_calls = [c for c in calls if isinstance(c, dict) and c.get("agent_name")]
        # 1) 更新 pending_batch（本批候选名单，供路径 A 读空气参考）
        try:
            p = self._presence_get(event)
            p.pending_batch = [c["agent_name"] for c in canonical_calls]
        except Exception as e:
            _logger.warning("[read_air] pending_batch update failed (non-fatal): %s", e)

        # 2) 温和收敛建议：博士只点名一人、但 calls 误带多人 → 日志提示是否只需某人
        # （复用 RouterMixin._t1_mentions，零重复造轮子；仅提示不砍 calls）
        try:
            if len(canonical_calls) > 1 and hasattr(self, "_t1_mentions"):
                raw = event.get_message_str() if hasattr(event, "get_message_str") else ""
                mentions = set(self._t1_mentions(raw or ""))
                if len(mentions) == 1:
                    named = next(iter(mentions))
                    _logger.info(
                        "[read_air][converge] 博士本轮只点名 %s，但 parallel_handoff 本批 calls 带了 %d 人"
                        "（%s）。是否只需 %s ？建议核对，不强制过滤。",
                        named,
                        len(canonical_calls),
                        "、".join(c["agent_name"] for c in canonical_calls),
                        named,
                    )
                elif len(mentions) == 0:
                    _logger.debug(
                        "[read_air][converge] 本批 calls=%d 人，消息未见点名，多人并行属主代理显式意图，不提示。",
                        len(canonical_calls),
                    )
        except Exception as e:
            _logger.warning("[read_air] converge observe failed (non-fatal): %s", e)
        return calls
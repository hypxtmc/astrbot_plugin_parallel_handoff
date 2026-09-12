"""三期 · GLM-4-Flash 离线心情注入器（daily_life.py）

M2 —— 数据源注入层。用 GLM-4-Flash（走 astrbot provider）离线读近期对话，
给每人 random_state 注入「今日心情 / 手头事 / 话题域」的语义温度。

设计铁律（用户 2026-09-03 拍板）：
  · LLM 只当眼睛，不当手：本注入器只在离线/闲时跑，绝不实时拦截路由、绝不砍 calls。
  · 绝对不依赖 LLM：任何异常 → 降级为纯规则随机状态，注入器本身绝不抛致命错误。
  · 复用 dispatch 的成熟 llm_generate 调用通道，零新增依赖。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Optional

# random_state 双分支导入：astrbot 包加载→相对；顶层/测试→绝对
try:
    from .random_state import LIFE_DOMAINS, RandomStateManager
except ImportError:
    from random_state import LIFE_DOMAINS, RandomStateManager

if TYPE_CHECKING:
    pass

_logger = logging.getLogger("parallel_handoff.daily_life")

# 注入 prompt：让 GLM-4-Flash 读对话、给每人输出结构化今日状态
_INJECT_SYSTEM_PROMPT = (
    "你是组织的气氛观察员。给你一段子代理之间的近期对话记录，"
    "请为每个出现的角色，推断其【今日心情】与【此刻可能手头在忙的事】，"
    "并给其中一个【今日话题倾向】。只输出 JSON，不要任何解释。"
)

_INJECT_USER_TEMPLATE = (
    "角色清单：{agents}\n"
    "今日可选话题倾向：{domains}\n"
    "对话记录：\n{logs}\n\n"
    "请输出 JSON 数组，每个元素格式：\n"
    '{{"agent": "角色名", "mood": "心情", "hand": "手头事", "domain": "话题倾向"}}\n'
    "如信息不足，mood 用『平静』，hand 用空字符串，domain 用 '生活'。"
)


def build_inject_prompt(
    agents: List[str],
    logs: str,
    domains: Optional[List[str]] = None,
) -> Dict[str, str]:
    """组装注入请求的系统/用户 prompt（供调用与测试复用）。"""
    d = domains or LIFE_DOMAINS
    return {
        "system": _INJECT_SYSTEM_PROMPT,
        "user": _INJECT_USER_TEMPLATE.format(
            agents="、".join(agents),
            domains="、".join(d),
            logs=logs or "（暂无）",
        ),
    }


def _coerce_domain(raw: str) -> str:
    """把 LLM 输出的话题倾向归一到 LIFE_DOMAINS（不是白名单的回落 '生活'）。"""
    raw = (raw or "").strip()
    for d in LIFE_DOMAINS:
        if d in raw:
            return d
    return "生活"


class DailyLifeInjector:
    """GLM-4-Flash 离线状态注入器。

    依赖注入：构造时传入 llm_generate 可调用 + 可选 provider id 解析器，
    便于测试不打真模型也能验降级与 JSON 解析逻辑。
    """

    def __init__(
        self,
        rng: RandomStateManager,
        llm_generate: object,
        resolve_provider_id: Optional[object] = None,
        provider_id: str = "",
    ) -> None:
        self._rng = rng
        self._llm_generate = llm_generate  # async (chat_provider_id, prompt, system_prompt) -> resp
        self._resolve_provider_id = resolve_provider_id  # async (umo) -> str | None
        self._provider_id = provider_id  # 固定 GLM-4-Flash provider id（若配置）

    async def inject(
        self,
        scene: str,
        agents: List[str],
        logs: str,
        umo: str = "",
    ) -> Dict[str, str]:
        """离线为 scene 注入今日状态。

        成功：调用 LLM 读最近对话，用结果覆盖各 agent 的 random_state。
        任何异常：打日志后降级为纯规则随机（roll_daily_state 已存在，无需重掷）。

        返回注入摘要 {agent: summary}（成功或降级都返回当前状态）。
        """
        astate: Dict[str, str] = {}
        try:
            # 解析 provider id：显式指定 > 会话 provider > 放弃（走降级）
            prov_id = self._provider_id
            if not prov_id and self._resolve_provider_id:
                prov_id = await self._resolve_provider_id(umo)
            if not prov_id:
                _logger.warning(
                    "[daily_life] no provider id resolvable, degrade to random-state for %s", scene
                )
                return self._fallback(scene, agents)

            prompts = build_inject_prompt(agents, logs)
            resp = await self._llm_generate(
                chat_provider_id=prov_id,
                prompt=prompts["user"],
                system_prompt=prompts["system"],
            )
            text = getattr(resp, "completion_text", None) or ""
            entries = self._parse_json(text)
            for e in entries:
                agent = (e.get("agent") or "").strip()
                if not agent:
                    continue
                self._rng.set_llm(
                    scene,
                    agent,
                    (e.get("mood") or "").strip()[:16],
                    _coerce_domain(e.get("domain") or ""),
                    (e.get("hand") or "").strip()[:32],
                )
            _logger.info("[daily_life] inject %d states for scene=%s", len(entries), scene)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[daily_life] inject failed, degrade to random: %s", e)
            return self._fallback(scene, agents)
        return self._rng.summary(scene)

    def _fallback(self, scene: str, agents: List[str]) -> Dict[str, str]:
        """降级：确保每 agent 至少有一个规则随机状态（绝不缺位）。"""
        for a in agents:
            self._rng.get(scene, a)
        return self._rng.summary(scene)

    @staticmethod
    def _parse_json(text: str) -> List[dict]:
        """解析 LLM 返回的 JSON（容忍 ```json 围栏、前后杂质）。"""
        import json
        import re

        if not text:
            return []
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        seg = m.group(1) if m else text
        start = seg.find("[")
        end = seg.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            data = json.loads(seg[start : end + 1])
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]
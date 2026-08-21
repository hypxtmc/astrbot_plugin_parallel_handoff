"""directive.py — parallel_handoff 路由强制指令构建 + 黑名单映射（P0 拆模块）

对应原 main.py 的 261-274 行（_get_handoff_blacklist 黑名单映射收进这里）
+ 375-463 行区域（_route_directive_inject / _build_route_directive）。
装饰器 @filter.on_llm_request() 保留在 main.py 壳方法上（保证 handler_module_path
匹配插件主模块），本模块提供纯实现。
"""
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart


class DirectiveMixin:
    """路由强制指令构建 + 强制直连黑名单映射"""

    def _get_handoff_blacklist(self) -> set:
        """读取强制直连黑名单（handoff_blacklist_agents 配置，默认空 = 无黑名单）。

        黑名单子代理永远不走 parallel_handoff 插件的 relay 中转：
        - parallel_handoff / call_subagent 调用它们会被拦截，提示改用 transfer_to_xxx 直连
        - 路由强制指令（_build_route_directive）明确要求主代理用 transfer_to_xxx 直连

        返回值：规范化后的 agent id 集合（英文 id + 中文名均按配置原样收录，
        拦截时由 _resolve_agent_name 归一后比对，兼容两种写法）。
        """
        raw = str(self._cfg("handoff_blacklist_agents", "") or "").strip()
        return {name.strip() for name in raw.split(",") if name.strip()}

    # ── 路由强制指令注入（OnLLMRequestEvent 钩子实现）────────
    async def _route_directive_inject(self, event: AstrMessageEvent, req: ProviderRequest):
        """按配置向主代理 LLM 请求注入路由强制指令（软注入，不拦截）。

        触发点：仅主代理请求经过 OnLLMRequestEvent（子代理走 llm_generate 不触发）。
        作用：把 route_mode / call_mode / direct_delivery_agents 算出的路由路径规范
        追加到 req.extra_user_content_parts（请求尾部，livingmemory 同款），主代理必须照走；
        不修改 req.func_tool（工具全保留）。系统提示与历史消息为请求前缀，完全不动，
        故不破坏 DeepSeek 前缀缓存命中率；mark_as_temp 置 _no_save，不写入对话历史。
        已含标记则跳过，避免 agent 循环多轮重复注入。
        """
        try:
            if not bool(self._cfg("enable_route_directive", True)):
                return False
            directive = self._build_route_directive()
            if not directive:
                return False
            marker = "【路由强制指令·parallel_handoff】"
            parts = getattr(req, "extra_user_content_parts", None) or []
            for part in parts:
                text = getattr(part, "text", "")
                if text and marker in text:
                    return False
            req.extra_user_content_parts.append(TextPart(text=directive).mark_as_temp())
            logger.info(
                f"[parallel_handoff] 路由强制指令已注入 extra_user_content_parts "
                f"(route_mode={self._cfg('route_mode', 'direct')}, "
                f"call_mode={self._cfg('call_mode', 'parallel')})"
            )
        except Exception as e:
            logger.warning(f"[parallel_handoff] 路由指令注入失败: {e}")
        return False

    def _build_route_directive(self) -> str:
        """按配置生成路由路径规范文本；配置缺失时不注入"""
        route_mode = str(self._cfg("route_mode", "direct")).strip().lower()
        call_mode = str(self._cfg("call_mode", "parallel")).strip().lower()
        raw_agents = str(self._cfg("direct_delivery_agents", "")).strip()
        agent_ids = [a.strip() for a in raw_agents.split(",") if a.strip()]
        if not agent_ids:
            return ""
        display_map = self._get_name_display_map()
        names = []
        for aid in agent_ids:
            if aid in display_map:
                names.append(str(display_map[aid]))
            elif aid in self.AGENT_DISPLAY_NAME:
                names.append(self.AGENT_DISPLAY_NAME[aid])
            else:
                names.append(aid)
        name_str = "、".join(names)

        lines = [
            "【路由强制指令·parallel_handoff】（由插件注入，主代理必须遵守；不涉及路由的日常对话不受影响）",
            "当判定需要将消息路由给子代理时，一律使用 parallel_handoff 工具，并按以下路径规范执行：",
        ]
        blacklist_ids = self._get_handoff_blacklist()
        blacklist_names = []
        for bid in sorted(blacklist_ids):
            if bid in display_map:
                name = str(display_map[bid])
            elif bid in self.AGENT_DISPLAY_NAME:
                name = self.AGENT_DISPLAY_NAME[bid]
            else:
                name = bid
            if name not in blacklist_names:
                blacklist_names.append(name)
        blacklist_name_str = "、".join(blacklist_names)

        if route_mode == "relay":
            lines.append("- 路由模式 relay：非黑名单子代理回复返回主代理，由主代理把关后转述，不直接发用户端")
        else:
            lines.append(f"- 路由模式 direct：以下子代理回复直接分段转发用户端，不经主代理转述：{name_str}")
            lines.append("- 其余非黑名单子代理走 relay：回复返回主代理，由主代理把关后转述")
        if blacklist_name_str:
            lines.append(
                f"- 强制直连黑名单（{blacklist_name_str}）：必须直接调用 "
                f"transfer_to_xxx 工具直连，禁止用 parallel_handoff / call_subagent 调用，"
                f"其回复直接发用户端，不经过主代理或插件中转"
            )
        if call_mode == "chained":
            lines.append("- 调用模式 chained：多子代理按接龙顺序串行调用，前一个的回复作为后一个的上下文，禁止并行双发")
        else:
            lines.append("- 调用模式 parallel：多子代理并行调用，各说各话，回复统一汇总返回")
        # ── 直发前禁止主代理抢先发言（顾主设定 2026-08-20） ──
        # 调子代理路由前，主代理不得先生成引导/转述/解释文本（如“我这就叫她”“她应你了”），
        # 应直接发起 parallel_handoff 工具调用，让子代理亲口说话。
        # 与 forward.py 的 allow_mainagent_after_direct（管直发后）互补，管住直发前。
        if self._cfg("forbid_pre_tool_mainagent_talk", True):
            lines.append(
                "- 子代理直发纪律：调用 parallel_handoff 路由子代理时，"
                "必须在工具调用前留空、不输出任何主代理导语/转述/解释（如“我这就叫她”“她应你了”）"
                "，直接发起工具调用，由子代理亲口对用户说话；切忌在子代理发言前让主代理插话。"
            )
            lines.append(
                "- 直发后禁言：parallel_handoff 完成子代理直发后，本轮主代理不得再输出任何"
                "总结/转述/收尾文字（如“已传给她”“她在回你了”）——工具调用即本轮回复结束，"
                "无需画蛇添足；等你（顾主）需要时再开口。"
            )
        lines.append("- 工具列表保持完整，禁止摘除或绕过任何工具")
        return "\n".join(lines)

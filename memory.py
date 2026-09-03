"""memory.py — parallel_handoff livingmemory 集成 + 召回 + 记忆工具过滤（P0 拆模块）

对应原 main.py 的 42-64 行（_strip_chain_injection 模块级函数）+
843-854 行（livingmemory 插件查找）+ 911-961 行区域（记忆召回/工具过滤）+
1009-1026 行区域（记忆存储）。
排除逻辑收敛为单一 exclude_agents 集合：由配置直接控制（默认空 = 全部子代理可召回）。
"""
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.tool import ToolSet


def _strip_chain_injection(text: str) -> str:
    """剥离接龙注入的前文块（临时上下文），避免污染子代理长期记忆。

    接龙模式（call_mode=chained）会把上一个子代理的输出注入下一个的
    input，这部分是临时上下文，不应进入本子代理的记忆召回/存储链路。
    """
    marker = "（接龙·上一位）"
    if marker not in text:
        return text
    idx = text.find(marker)
    end_marker = "请接续上文，现在轮到你回应："
    end = text.find(end_marker, idx)
    if end == -1:
        return text[:idx]
    tail = text[end + len(end_marker):]
    return text[:idx] + tail.lstrip("\n")


def _strip_ctx_injection(text: str) -> str:
    """剥离 ContextEngine 注入的跨轮历史块，只保留本轮新输入。

    ctx_engine.inject 会把历史拼成
    "--- 对话历史 ---\\n{历史}\\n--- 新的输入 ---\\n{本轮输入}"
    注入 final_input。若原样存入 livingmemory，历史块会随轮次越滚越大
    （召回时被 inject_with_recent_context 拼进查询文本，导致 token 膨胀）。
    本函数只保留 "--- 新的输入 ---" 之后的本轮输入。
    """
    marker = "--- 新的输入 ---"
    if marker not in text:
        return text
    idx = text.find(marker)
    return text[idx + len(marker):].lstrip("\n")


class MemoryMixin:
    """长期记忆集成：接龙注入剥离 / livingmemory 查找 / 召回 / 存储 / 工具过滤"""

    def _strip_chain_injection(self, text: str) -> str:
        """实例方法包装：接龙注入前文块剥离（供 dispatch 通过 self 调用）"""
        return _strip_chain_injection(text)

    def _strip_ctx_injection(self, text: str) -> str:
        """实例方法包装：跨轮历史块剥离（供 dispatch 通过 self 调用）"""
        return _strip_ctx_injection(text)

    # ── 长期记忆插件查找 ─────────────────────────────────
    def _find_livingmemory_plugin(self):
        """在已加载插件中查找 livingmemory 插件类，找不到返回 None"""
        livingmemory_plugin = None
        try:
            all_stars = self.context.get_all_stars()
            for star in all_stars:
                name = getattr(star, "name", "")
                if "livingmemory" in name.lower():
                    livingmemory_plugin = star.star_cls
                    break
        except Exception:
            pass
        return livingmemory_plugin

    # ── 记忆召回：注入长期记忆 ──
    async def _memory_recall(
        self,
        event: AstrMessageEvent,
        agent_name: str,
        clean_input: str,
        livingmemory_plugin,
    ) -> list:
        """召回 livingmemory 长期记忆，返回记忆注入内容列表（供透传子代理 provider）。

        接龙注入的前文是临时上下文：记忆链路（召回/存储）统一剥离，
        避免上一个子代理的输出污染本子代理的长期记忆（clean_input 由调用方剥离）。
        """
        if livingmemory_plugin:
            try:
                # livingmemory 未就绪时其 handle_memory_recall 会静默短路，
                # 这里先快查初始化状态，区分「插件未就绪」与「确实无记忆」，
                # 且不等其内部最长 30s 的初始化轮询，避免拖慢子代理调用
                initializer = getattr(livingmemory_plugin, "initializer", None)
                if initializer is not None:
                    if getattr(initializer, "is_failed", False):
                        logger.warning(
                            f"[parallel_handoff] livingmemory 初始化失败，跳过 "
                            f"{agent_name} 记忆召回: "
                            f"{getattr(initializer, 'error_message', 'unknown')}"
                        )
                        return []
                    if not getattr(initializer, "is_initialized", False):
                        logger.warning(
                            f"[parallel_handoff] livingmemory 未就绪（初始化中），"
                            f"跳过 {agent_name} 记忆召回"
                        )
                        return []

                event.persona_id = agent_name
                # 2026-09-04 子代理独立记忆库：livingmemory 的 get_persona_id()
                # 只认 _subagent_persona 标记（它不读 persona_id 属性，历史赋值保留防其他链路依赖）
                event._subagent_persona = agent_name
                req = ProviderRequest(
                    prompt=clean_input,
                    extra_user_content_parts=[],
                )
                await livingmemory_plugin.handle_memory_recall(event, req)

                # 保留记忆注入内容，透传给子代理的 provider
                parts = list(req.extra_user_content_parts or [])
                if not parts:
                    logger.debug(
                        f"[parallel_handoff] {agent_name} 记忆召回为空"
                        f"（插件就绪，无匹配记忆）"
                    )
                return parts
            except Exception as e:
                logger.warning(
                    f"[parallel_handoff] Memory recall failed for "
                    f"{agent_name}: {e}"
                )
                return []
        return []

    # ── 构建子代理工具集（记忆工具过滤） ──
    def _build_memory_tools(self, agent_name: str):
        """为子代理构建记忆工具集（仅含 recall/memorize 两个工具）。

        排除逻辑收敛为单一 exclude_agents 集合：由配置直接控制（默认空 = 全部子代理可召回），
        配置（subagent_memory.exclude_agents 或扁平 exclude_agents）决定集合内容。
        """
        subagent_tools = None
        try:
            # 子代理记忆配置：扁平字段优先，兼容旧的 subagent_memory 嵌套对象
            memory_cfg = self._cfg("subagent_memory", {})
            if isinstance(memory_cfg, dict) and memory_cfg:
                memory_enabled = self._cfg("recall_enabled", memory_cfg.get("enabled", True))
                exclude_raw = self._cfg("exclude_agents", memory_cfg.get("exclude_agents", ""))
            else:
                memory_enabled = self._cfg("recall_enabled", True)
                exclude_raw = self._cfg("exclude_agents", "")
            exclude_agents = {name.strip() for name in exclude_raw.split(",") if name.strip()}
            if memory_enabled and agent_name not in exclude_agents:
                global_tools = getattr(
                    self.context.provider_manager, "llm_tools", None
                )
                if global_tools and not global_tools.empty():
                    memory_tools = []
                    for tool in global_tools.func_list:
                        if tool.name in (
                            "recall_long_term_memory",
                            "memorize_long_term_memory",
                        ):
                            memory_tools.append(tool)
                    if memory_tools:
                        subagent_tools = ToolSet(tools=memory_tools)
        except Exception as e:
            logger.warning(
                f"[parallel_handoff] Failed to build agent tools for "
                f"{agent_name}: {e}"
            )
        return subagent_tools

    # ── 记忆存储：存入长期记忆 ──
    async def _memory_store(
        self,
        livingmemory_plugin,
        event: AstrMessageEvent,
        agent_name: str,
        final_input: str,
        raw_response: str,
    ):
        """将本轮 user/assistant 消息写入 livingmemory 对话管理器并做消息数限制"""
        if livingmemory_plugin:
            try:
                # 2026-09-04 子代理独立记忆库：存储链路同样打标，
                # 提炼出的记忆挂到子代理 persona 维度，不与主代理/其他子代理串库
                event._subagent_persona = agent_name
                conv_mgr = (
                    livingmemory_plugin
                    .event_handler._memory_recall.conversation_manager
                )
                # 2026-08-31 修复：存储前剥离 ctx_engine 跨轮历史块，只存本轮干净输入
                # （此前注入的 "--- 对话历史 ---..." 整块随轮次越滚越大，
                #  召回时被 inject_with_recent_context 拼进查询文本导致 token 膨胀）
                clean_input = self._strip_chain_injection(
                    self._strip_ctx_injection(final_input)
                )
                await conv_mgr.add_message_from_event(
                    event, role="user", content=clean_input
                )
                await conv_mgr.add_message_from_event(
                    event, role="assistant", content=raw_response
                )
                session_id = event.unified_msg_origin
                await (
                    livingmemory_plugin
                    .event_handler._memory_recall.message_utils
                    .enforce_message_limit(session_id)
                )
            except Exception as e:
                logger.warning(
                    f"[parallel_handoff] Memory storage failed for "
                    f"{agent_name}: {e}"
                )

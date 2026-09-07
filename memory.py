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

                # 2026-09-05 记忆串库修复：召回改走子代理专属会话桩。
                # livingmemory 按 session+persona 双条件过滤，存储侧拆专属会话后
                # 召回必须用同一 umo 才能查到新库记忆；且不再污染原始 event
                #（此前在原始 event 上打 _subagent_persona，残留会让主代理链路挂错 persona）
                stub = self._subagent_event_stub(event, agent_name)
                req = ProviderRequest(
                    prompt=clean_input,
                    extra_user_content_parts=[],
                )
                await livingmemory_plugin.handle_memory_recall(stub, req)

                # 保留记忆注入内容，透传给子代理的 provider
                parts = list(req.extra_user_content_parts or [])
                if not parts:
                    logger.debug(
                        f"[parallel_handoff] {agent_name} 记忆召回为空"
                        f"（插件就绪，无匹配记忆）"
                    )
                # 2026-09-04 方案 A（博士拍板）：博士私聊直问子代理时并入旁轨会话记忆。
                # 旁轨记忆落在 family_pulse:FriendMessage:subagents 会话 + agent persona
                # 维度，博士私聊会话召回查不到（livingmemory 按 session+persona 双条件
                # 过滤），导致「问阿米娅今天家里聊了什么」她说不记得。此处用旁轨桩
                # 再造一次召回，把家里的记忆也带给子代理；旁轨心跳链路本身已是旁轨
                # 会话，自动跳过不重复。
                parts = await self._merge_pulse_memory_recall(
                    parts, event, agent_name, clean_input, livingmemory_plugin
                )
                return parts
            except Exception as e:
                logger.warning(
                    f"[parallel_handoff] Memory recall failed for "
                    f"{agent_name}: {e}"
                )
                return []
        return []

    # ── 旁轨家常并入（2026-09-04 方案 A → 2026-09-05 博士改版：只抓最近 6 小时） ──
    async def _merge_pulse_memory_recall(
        self,
        parts: list,
        event: AstrMessageEvent,
        agent_name: str,
        clean_input: str,
        livingmemory_plugin,
    ) -> list:
        """博士私聊直问子代理时，并入「最近 N 小时家里动静」（jsonl 滑动窗口）。

        2026-09-05 博士改版：方案 A 原实现并入 livingmemory 旁轨会话的长期
        记忆，从上线起几天累积下来，每次问都把几天历史全部堆进上下文，
        非常难看。博士拍板改成只抓最近 6 小时的闲聊：家里动静的权威来源
        是旁轨 jsonl（心跳对话原文），按滑动时间窗过滤后注入；更早的生活
        线由子代理自己的 recall_long_term_memory 工具按需查询（记忆工具
        仍在手上），不再自动堆叠历史。

        旁轨心跳链路（_pulse_llm）传进来的 event 本身就是旁轨桩，此处直接
        跳过，不重复注入。窗口小时数可配 family_pulse_recent_hours（默认 6）。
        任何失败静默降级，不影响主召回。
        """
        try:
            pulse_umo = self._cfg(
                "family_pulse_memory_umo", "family_pulse:FriendMessage:subagents"
            )
            cur_umo = getattr(event, "unified_msg_origin", "")
            if not pulse_umo or cur_umo == pulse_umo:
                return parts  # 已在旁轨会话，或未配置旁轨会话，跳过
            pulse_parts = self._pulse_log_fallback(agent_name)
            if pulse_parts:
                logger.info(
                    f"[parallel_handoff] 旁轨近窗家常注入 OK [{agent_name}]: "
                    f"{len(pulse_parts)} 条"
                )
                return parts + pulse_parts
            return parts
        except Exception as e:
            logger.warning(
                f"[parallel_handoff] 旁轨家常注入失败(静默) "
                f"[{agent_name}]: {e}"
            )
            return parts

    # ── 旁轨近窗家常注入（2026-09-05 博士改版：滑动时间窗，默认 6 小时） ──
    def _pulse_log_fallback(self, agent_name: str, max_lines: int = 15) -> list:
        """「家里动静」查看链路的注入源：旁轨 jsonl 按最近 N 小时窗口过滤。

        2026-09-05 博士改版：原来取「当天全部最近 max_lines 句」，一天下来
        从早到晚全堆进上下文；现在只取最近 family_pulse_recent_hours
        （默认 6）小时内的家常。ts 为当天 "HH:MM"，窗口跨零点时 jsonl
        只有当天文件、无法回溯昨日，退化为取当天零点后全部再截尾。
        更早的生活线由子代理 recall_long_term_memory 工具按需查询。
        任何失败静默降级，不阻塞主召回。
        """
        try:
            read_day = getattr(self, "_pulse_read_day", None)
            if not callable(read_day):
                return []
            logs = read_day()
            if not logs:
                return []
            import datetime
            from zoneinfo import ZoneInfo

            now = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
            hours = 6
            try:
                hours = float(self._cfg("family_pulse_recent_hours", 6) or 6)
            except Exception:
                pass
            floor_min = max(0, int(now.hour * 60 + now.minute - hours * 60))

            in_window = []
            for r in logs:
                ts = str(r.get("ts", ""))
                try:
                    hh, mm = ts.split(":")
                    r_min = int(hh) * 60 + int(mm)
                except Exception:
                    continue  # ts 缺损/非当天格式，不注入
                if r_min >= floor_min:
                    in_window.append(r)
            logs = in_window[-max_lines:]
            lines = []
            for r in logs:
                name = r.get("display", r.get("agent", ""))
                text = (r.get("text") or "").strip()
                if not text:
                    continue
                # 2026-09-05 过滤：博士贴来的日志块（如带 [Core]/[INFO] 的系统日志）
                # 会被 _pulse_append 原样写进旁轨日志，不适合当「家里聊了什么」注入
                if text.startswith("[20") and (
                    "[Core]" in text or "[INFO]" in text or "core.event_bus" in text
                ):
                    continue
                if len(text) > 500 and (
                    "[INFO]" in text or "[WARN" in text or "[ERROR]" in text
                ):
                    continue
                lines.append(f"{r.get('ts', '')} · {name}：{text}")
            if not lines:
                return []
            today = now.strftime("%Y-%m-%d")
            body = "\n".join(lines)
            # 2026-09-05 修复：extra_user_content_parts 元素必须是 TextPart 对象
            # （裸字符串会触发 provider 端「不支持的额外内容块类型: <class 'str'>」），
            # 与 livingmemory 注入方式对齐：TextPart(text=...).mark_as_temp()
            from astrbot.core.agent.message import TextPart

            return [
                TextPart(
                    text=(
                        f"【家里最近 {hours:g} 小时】{today} 的旁轨家常"
                        f"（{len(lines)} 句）：\n{body}"
                    )
                ).mark_as_temp()
            ]
        except Exception as e:
            logger.warning(
                f"[parallel_handoff] 旁轨家常注入失败(静默): {e}"
            )
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

    # ── 子代理专属记忆会话（2026-09-05 记忆串库修复） ─────────────

    @staticmethod
    def _subagent_event_stub(event, agent_name: str):
        """造子代理专属记忆会话桩（仿 family_pulse._pulse_event_stub）。

        umo = 「{原会话}:subagent:{agent_name}」——存储/召回/提炼三条链路
        共用同一专属会话，与主代理会话彻底隔离；鸭子类型兼容 livingmemory
        对 event 的全部字段访问。失败由调用方 try/except 静默降级。
        """
        import types as _types

        umo = f"{event.unified_msg_origin}:subagent:{agent_name}"
        message_obj = _types.SimpleNamespace(
            raw_message="subagent_memory", sender=None
        )
        stub = _types.SimpleNamespace(
            unified_msg_origin=umo,
            message_obj=message_obj,
            persona_id=agent_name,
            _subagent_persona=agent_name,  # get_persona_id 优先级 0，按子代理隔离
        )
        stub.get_message_str = lambda: "subagent_memory"
        stub.get_message_type = lambda: 1  # 非群聊，走私聊存储
        stub.get_sender_id = lambda: umo
        try:
            _platform = event.get_platform_name()
        except Exception:
            _platform = "qq_restapi"
        stub.get_platform_name = lambda: _platform
        stub.get_self_id = lambda: "subagent_memory_bot"
        return stub

    # ── 子代理英文 id → AstrBot 人格真名（提炼提示词专用） ──
    def _persona_name(self, agent_name: str) -> str:
        """把子代理英文 id 映射成 AstrBot personas 表里的人格真名。

        2026-09-05 修：_maybe_reflect_subagent 的 process_conversation
        (persona_id=...) 只用于取提炼 prompt 的人格底色，原样传英文 id
        （shu/skadi…）在 personas 表（中文名：黍/斯卡蒂…）查不到，每小时
        心跳提炼都 WARN 一条并退化 base_prompt。存储维度（classify_atoms
        的 persona_id）保持英文 id 与召回侧 _subagent_persona 一致，
        不经过本函数，防止记忆库 persona 维度分裂。
        特蕾西娅的人格在库中登记为「特蕾西娅-子代理」，特例映射。
        查不到映射时原样返回（行为与旧版一致，仅可能仍有 WARN）。
        """
        try:
            disp = (self._get_name_display_map() or {}).get(agent_name) or agent_name
        except Exception:
            disp = agent_name
        if disp == "特蕾西娅":
            disp = "特蕾西娅-子代理"
        return disp

    async def _maybe_reflect_subagent(
        self, livingmemory_plugin, stub, agent_name: str
    ):
        """子代理专属会话的主动记忆提炼（达到轮数阈值时触发）。

        livingmemory 的 Reflection 只在主代理 LLM 响应链上触发，子代理
        专属会话永远不会被自动总结 → documents 长期记忆恒为 0 条。
        此处复用 /lmem summarize 同款提炼链路（process_conversation →
        classify_atoms → add_memory），persona 挂子代理名下，记忆落
        子代理自己的独立长期记忆库。任何失败只记日志不抛出。
        """
        try:
            ch = getattr(livingmemory_plugin, "command_handler", None)
            if ch is None:
                return
            cm = getattr(ch, "conversation_manager", None)
            mp = getattr(ch, "_memory_processor", None)
            me = getattr(ch, "memory_engine", None)
            cfg = getattr(ch, "config_manager", None)
            if not (cm and mp and me and cfg):
                return

            session_id = stub.unified_msg_origin
            count = await cm.store.get_message_count(session_id)
            last = await cm.get_session_metadata(
                session_id, "last_summarized_index", 0
            )
            try:
                last = int(last)
            except (TypeError, ValueError):
                last = 0
            if last > count:  # 消息被清理后索引越界，对齐到当前总数
                last = count

            threshold = cfg.get("reflection_engine.summary_trigger_rounds", 10)
            unsummarized = count - last
            if unsummarized < 2 or (unsummarized // 2) < threshold:
                return

            history = await cm.get_messages_range(
                session_id=session_id, start_index=last, end_index=count
            )
            if not history:
                return

            persona_id = agent_name  # 存储维度，须与召回侧 _subagent_persona 一致，勿改
            # 2026-09-05 修：提炼提示词用 personas 表人格真名（中文），
            # 否则每小时心跳提炼都 WARN「人格 'shu' 不存在」
            persona_prompt_id = self._persona_name(agent_name)
            memory_scope = session_id
            try:
                from astrbot_plugin_livingmemory.core.memory_scope import (
                    resolve_memory_scope,
                )

                memory_scope = resolve_memory_scope(cfg, stub) or session_id
            except Exception:
                pass  # import 失败时退化为 session 作用域，不致命

            content, metadata, importance = await mp.process_conversation(
                messages=history,
                is_group_chat=False,
                persona_id=persona_prompt_id,
            )
            atoms = mp.classify_atoms_from_metadata(
                metadata=metadata,
                parent_importance=importance,
                session_id=memory_scope,
                persona_id=persona_id,
            )
            metadata["source_window"] = {
                "session_id": session_id,
                "start_index": last,
                "end_index": count,
                "message_count": unsummarized,
                "triggered_by": "subagent_auto",
            }
            metadata["source_session_id"] = session_id

            source_messages = None
            try:
                from astrbot_plugin_livingmemory.core.utils import (
                    serialize_source_messages,
                )

                thr = float(
                    cfg.get(
                        "reflection_engine.source_retention_importance_threshold",
                        0.8,
                    )
                )
                if importance >= thr:
                    source_messages = serialize_source_messages(history)
            except Exception:
                source_messages = None

            await me.add_memory(
                content=content,
                session_id=memory_scope,
                persona_id=persona_id,
                importance=importance,
                metadata=metadata,
                atoms=atoms,
                source_messages=source_messages,
            )
            await cm.update_session_metadata(
                session_id, "last_summarized_index", count
            )
            await cm.update_session_metadata(
                session_id, "pending_summary", None
            )
            logger.info(
                f"[parallel_handoff] 子代理记忆提炼 OK [{agent_name}]: "
                f"{unsummarized} 条消息 → persona={persona_id} "
                f"（importance={importance:.2f}，scope={memory_scope}）"
            )
        except Exception as e:
            logger.warning(
                f"[parallel_handoff] 子代理记忆提炼失败 [{agent_name}]: {e}"
            )

    # ── 记忆存储：存入长期记忆 ──
    async def _memory_store(
        self,
        livingmemory_plugin,
        event: AstrMessageEvent,
        agent_name: str,
        final_input: str,
        raw_response: str,
    ):
        """将本轮 user/assistant 消息写入 livingmemory 对话管理器并做消息数限制。

        2026-09-05 记忆串库修复：此前直接用博士私聊 event 写历史——
        子代理消息混进主代理会话，livingmemory Reflection 总结主会话时
        把她们的话一并提炼进普瑞赛斯记忆库（博士观察到「记忆都落给了
        普瑞赛斯」的根因）；且子代理专属会话无人触发提炼，长期记忆恒空。
        现改用「{原会话}:subagent:{agent}」专属会话桩存储，写完即检查
        阈值、按子代理 persona 主动提炼长期记忆。
        """
        if livingmemory_plugin:
            try:
                stub = self._subagent_event_stub(event, agent_name)
                conv_mgr = (
                    livingmemory_plugin
                    .event_handler._memory_recall.conversation_manager
                )
                # 2026-08-31 修复：存储前剥离 ctx_engine 跨轮历史块，只存本轮干净输入
                clean_input = self._strip_chain_injection(
                    self._strip_ctx_injection(final_input)
                )
                await conv_mgr.add_message_from_event(
                    stub, role="user", content=clean_input
                )
                await conv_mgr.add_message_from_event(
                    stub, role="assistant", content=raw_response
                )
                await (
                    livingmemory_plugin
                    .event_handler._memory_recall.message_utils
                    .enforce_message_limit(stub.unified_msg_origin)
                )
                # 达到总结阈值时按子代理 persona 提炼长期记忆
                await self._maybe_reflect_subagent(
                    livingmemory_plugin, stub, agent_name
                )
            except Exception as e:
                logger.warning(
                    f"[parallel_handoff] Memory storage failed for "
                    f"{agent_name}: {e}"
                )

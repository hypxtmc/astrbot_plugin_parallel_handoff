"""dispatch.py — parallel_handoff 核心调度（P0 拆模块）

对应原 main.py 的 684-1307 行区域（去重守卫 + parallel_handoff 主流程）
+ 1309-1402 行（跨轮上下文辅助方法）+ 1405-1424 行（call_subagent 实现）。

改造点（纯搬移，行为零变更）：
- 场景注入/剧情基线 → scene.py 的 _build_scene_prefix
- livingmemory 查找/召回/存储/工具过滤 → memory.py 的对应方法
- 分段转发/失败通知/姓名前缀 → forward.py 的对应方法
装饰器 @llm_tool 保留在 main.py 壳方法上（保证 handler_module_path 匹配插件主模块）。
"""
import asyncio
import hashlib
import json
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class DispatchMixin:
    """去重守卫 / 单子代理调用 / 并行调度 / 跨轮上下文 / 统一单代理路由"""

    def _tool_call_dedup_key(
        self,
        event: AstrMessageEvent,
        agents: list = None,
        calls: list = None,
        message: str = None,
    ) -> str:
        """生成工具调用防重 key：消息 ID + 调用内容签名。

        只有完全重复的路由才命中同一 key：同一批子代理 + 相同 input。
        同一条消息内串行调不同子代理、或对同一子代理追问不同问题，各自放行。
        签名优先级：calls(agent+input 对) > message(消歧模式) > agents 名单。
        """
        mid = getattr(getattr(event, "message_obj", None), "message_id", None)
        base = f"mid:{mid}" if mid else f"evt:{id(event)}"
        if calls:
            parts = []
            for c in calls:
                if not isinstance(c, dict):
                    continue
                a = str(c.get("agent_name", "")).strip()
                i = str(c.get("input", "")).strip()
                if a or i:
                    parts.append(f"{a}::{i}")
            if parts:
                sig = hashlib.md5(",".join(sorted(parts)).encode("utf-8")).hexdigest()[:16]
                return f"{base}|sig:{sig}"
        elif message:
            m = str(message).strip()
            if m:
                sig = hashlib.md5(m.encode("utf-8")).hexdigest()[:16]
                return f"{base}|msg:{sig}"
        elif agents:
            sig = ",".join(sorted({str(a).strip() for a in agents if str(a).strip()}))
            if sig:
                return f"{base}|agents:{sig}"
        return base

    def _dedup_guard(
        self,
        event: AstrMessageEvent,
        agents: list = None,
        calls: list = None,
        message: str = None,
    ):
        """LLM 同回合重复调用防重：只有完全重复的路由（同批子代理 + 相同 input）才短路。

        同消息内追问不同问题、调不同子代理，各自放行。
        返回 None 表示放行；返回 str 表示命中重复，直接作为工具结果返回。
        窗口 60s，覆盖一次完整 LLM 生成回合；过期自动清理防内存膨胀。
        """
        dedup_key = self._tool_call_dedup_key(event, agents, calls, message)
        seen = self._tool_call_seen.get(dedup_key)
        if seen and time.time() - seen[0] < 60:
            logger.info(f"[parallel_handoff] 同消息重复调用已短路: key={dedup_key}")
            return json.dumps(
                {
                    "results": [],
                    "note": "该消息已由 parallel_handoff 处理过，本次为 LLM 同回合重复触发，已短路",
                    "dedup": True,
                },
                ensure_ascii=False,
            )
        # 标记本消息已进入路由（60s 窗口内不再重复执行）
        now_ts = time.time()
        stale = [k for k, v in self._tool_call_seen.items() if now_ts - v[0] >= 60]
        for k in stale:
            self._tool_call_seen.pop(k, None)
        self._tool_call_seen[dedup_key] = (now_ts, None)
        return None

    def _maybe_prefix(self, agent_name: str, text: str, enable_name_prefix: bool) -> str:
        """子代理回复姓名前缀（render 块方法化）：先查覆盖表，再走全局开关"""
        overrides = self._get_name_prefix_overrides()
        if isinstance(overrides, dict) and agent_name in overrides:
            should_prefix = overrides[agent_name]
        else:
            should_prefix = enable_name_prefix
        if should_prefix:
            display_name = self._display_name(agent_name)
            prefix_str = f"【{display_name}】\n"
            if not text.startswith(prefix_str):
                text = prefix_str + text
        return text

    async def _call_one(
        self,
        call: dict,
        *,
        event,
        handoff_map: dict,
        scene_prefix: str,
        livingmemory_plugin,
        enable_name_prefix: bool,
        timeout: int,
    ) -> dict:
        """调用单个子代理，带超时和错误隔离。

        原为 parallel_handoff 内嵌闭包，捕获 6 个外层变量——
        提升为显式方法，捕获变量转显式参数（同一引用，行为等价），
        使消费点/分支可被单测直接调用定位。
        """
        agent_name = (call.get("agent_name") or "").strip()
        # 中文名/大小写兼容：助手A -> agent_a，助手 -> agent_a（写回 call，后续统一用英文 id）
        agent_name = self._resolve_agent_name(agent_name)
        call["agent_name"] = agent_name
        input_text = (call.get("input") or "").strip()
        order = call.get("order")

        if not agent_name:
            return {
                "agent_name": "(missing)",
                "success": False,
                "response": "Missing agent_name field",
                "order": order,
            }
        if not input_text:
            return {
                "agent_name": agent_name,
                "success": False,
                "response": "Missing input field",
                "order": order,
            }

        handoff = handoff_map.get(agent_name)
        if not handoff:
            return {
                "agent_name": agent_name,
                "success": False,
                "response": (
                    f"Subagent '{agent_name}' not found. "
                    f"Available: {sorted(handoff_map.keys())}"
                ),
                "order": order,
            }

        # 强制直连黑名单拦截：黑名单子代理不走并行插件中转，
        # 必须由主代理直接调用 transfer_to_xxx 直连（顾主 2026-08-08 硬性指令）
        if agent_name in self._get_handoff_blacklist():
            return {
                "agent_name": agent_name,
                "success": False,
                "response": (
                    f"子代理 '{agent_name}' 在强制直连黑名单中：不允许通过 "
                    f"parallel_handoff / call_subagent 调用，请改用 "
                    f"transfer_to_{agent_name} 工具直连调用。"
                ),
                "order": order,
            }

        # 场景/基线前缀消费点：由 _apply_scene_prefix 决定（基线独立于场景开关）
        final_input = self._apply_scene_prefix(input_text, scene_prefix)

        # ── 记忆召回：注入长期记忆（memory.py） ──
        # 接龙注入的前文是临时上下文：记忆链路（召回/存储）统一剥离，
        # 避免上一个子代理的输出污染本子代理的长期记忆。
        clean_input = self._strip_chain_injection(final_input)
        memory_extra_parts = await self._memory_recall(
            event, agent_name, clean_input, livingmemory_plugin
        )

        # ── 构建子代理工具集（memory.py 记忆工具过滤） ──
        subagent_tools = self._build_memory_tools(agent_name)

        t0 = time.perf_counter()
        try:
            umo = event.unified_msg_origin
            prov_id = (
                handoff.provider_id
                or await self.context.get_current_chat_provider_id(umo)
            )

            # ── 上下文注入：跨轮对话历史 ──
            if self._ctx_enabled:
                ctx_session_id = event.unified_msg_origin
                ctx_key = f"{agent_name}:{ctx_session_id}"
                ctx_history = self._subagent_contexts.get(ctx_key, [])
                if ctx_history:
                    ctx_turns = len(ctx_history) // 2
                    if ctx_turns <= self._ctx_max_turns:
                        ctx_text = self._format_context_history(ctx_history)
                        final_input = f"--- 对话历史 ---\n{ctx_text}\n--- 新的输入 ---\n{final_input}"
                    else:
                        try:
                            ctx_text = await self._compress_context_history(
                                agent_name, ctx_session_id, ctx_history, prov_id, handoff, timeout
                            )
                        except Exception:
                            max_msgs = self._ctx_max_turns * 2
                            ctx_text = self._format_context_history(ctx_history[-max_msgs:])
                        final_input = f"--- 对话历史 ---\n{ctx_text}\n--- 新的输入 ---\n{final_input}"

            llm_resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=prov_id,
                    prompt=final_input,
                    system_prompt=handoff.agent.instructions or "",
                    tools=subagent_tools,
                    extra_user_content_parts=memory_extra_parts,
                ),
                timeout=timeout,
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)
            raw_response = llm_resp.completion_text

            # ── 记忆存储：存入长期记忆（memory.py） ──
            await self._memory_store(
                livingmemory_plugin, event, agent_name, final_input, raw_response
            )

            # 清理 persona_id（由上方的记忆召回阶段设置），确保下轮调用不残留
            if hasattr(event, "persona_id"):
                delattr(event, "persona_id")

            # ── 上下文存储：追加到跨轮对话历史 ──
            if self._ctx_enabled:
                self._append_context(agent_name, event.unified_msg_origin, input_text, raw_response)

            # 自动转发已由 parallel_handoff 的分段转发负责,此处不再重复推送
            raw_response = self._maybe_prefix(agent_name, raw_response, enable_name_prefix)

            return {
                "agent_name": agent_name,
                "success": True,
                "response": raw_response,
                "latency_ms": latency_ms,
                "order": order,
            }
        except asyncio.TimeoutError:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                f"[parallel_handoff] Subagent '{agent_name}' timed out after {timeout}s"
            )
            if hasattr(event, "persona_id"):
                delattr(event, "persona_id")
            err_text = f"Timeout after {timeout}s"
            err_text = self._maybe_prefix(agent_name, err_text, enable_name_prefix)
            return {
                "agent_name": agent_name,
                "success": False,
                "response": err_text,
                "latency_ms": latency_ms,
                "order": order,
            }
        except Exception as e:
            if hasattr(event, "persona_id"):
                delattr(event, "persona_id")
            latency_ms = int((time.perf_counter() - t0) * 1000)
            logger.error(
                f"[parallel_handoff] Subagent '{agent_name}' failed: {e}"
            )
            err_text = f"Error: {e}"
            err_text = self._maybe_prefix(agent_name, err_text, enable_name_prefix)
            return {
                "agent_name": agent_name,
                "success": False,
                "response": err_text,
                "latency_ms": latency_ms,
                "order": order,
            }

    # ── 核心 tool 实现（装饰器 @llm_tool 在 main.py 壳方法上） ──
    async def parallel_handoff(
        self,
        event: AstrMessageEvent,
        calls: list[dict] = None,
        timeout: int = 30,
        message: str = None,
    ) -> str:
        """并行调用多个子代理（如助手A、助手B、助手C、夕、令等）,
同时获取它们的回复并汇总。

使用场景：当需要多个子代理从不同角度回答同一个问题时使用此工具。
例如同时询问助手A和助手C对某件事的看法。

Args:
    calls(array[object]): 子代理调用列表。每个元素必须包含：
        - agent_name(string): 子代理名称,可选值: 助手A, 助手B 等（需在 name_display_map 中配置）
        - input(string): 传给该子代理的问题/指令
        - order(integer, 可选): 输出时的排序序号,越小越靠前
    timeout(number): 单个子代理的超时秒数,默认15秒。超过此时间未返回则跳过该子代理。
    message(string): 当开启消息消歧且不传calls时,传入原始消息文本,工具会自动路由到最近对话的子代理。
"""
        # ── LLM 同回合重复调用防重：同一消息对同一批子代理的重复路由短路 ──
        # 防重 key 含本次路由目标子代理名单，串行调不同子代理可各自放行
        target_agents = [c.get("agent_name") for c in (calls or []) if isinstance(c, dict)]
        dedup_result = self._dedup_guard(
            event, agents=target_agents, calls=calls, message=message
        )
        if dedup_result is not None:
            return dedup_result

        orchestrator = self.context.subagent_orchestrator
        if not orchestrator or not orchestrator.handoffs:
            return json.dumps(
                {"error": "No subagents configured in subagent_orchestrator"},
                ensure_ascii=False,
            )

        # 构建 agent_name -> HandoffTool 映射
        handoff_map: dict = {}
        for h in orchestrator.handoffs:
            handoff_map[h.agent.name] = h

        # ── 读取配置开关 ─────────────────────────────────────
        enable_disambiguation = self._cfg("enable_disambiguation", True)
        enable_scene_inject = self.config.get("enable_scene_inject", True)
        # 读取子代理前缀配置
        enable_name_prefix = self._cfg("enable_subagent_name_prefix", True)
        enable_segmented_forward = self.config.get("enable_segmented_forward", True)

        # ── 消息消歧：无指名消息自动路由 ─────────────────────
        if enable_disambiguation and message and (not calls or len(calls) == 0):
            session_id = event.unified_msg_origin
            last_agent = self._last_agent.get(session_id)
            if last_agent and last_agent in handoff_map:
                calls = [{"agent_name": last_agent, "input": message}]
                logger.info(
                    f"[parallel_handoff] 消歧路由: session={session_id} -> {last_agent}"
                )
            else:
                return json.dumps(
                    {
                        "results": [],
                        "note": (
                            "消歧路由失败：没有最近的对话对象。"
                            f"可用子代理: {sorted(handoff_map.keys())}"
                        ),
                    },
                    ensure_ascii=False,
                )

        if calls is None:
            calls = []

        # ── 场景注入前缀 + 共用剧情基线（scene.py） ──────────
        scene_prefix = self._build_scene_prefix(event, enable_scene_inject)

        # ── 长期记忆插件查找（memory.py） ────────────────────
        livingmemory_plugin = self._find_livingmemory_plugin()

        # ── 单子代理调用 ─────────────────────────────────────
        # 单子代理调用已迁为 self._call_one 方法（捕获变量显式参数化）

        # ── 并行调度 ─────────────────────────────────────────
        if not calls:
            return json.dumps(
                {"results": [], "note": "calls array is empty"},
                ensure_ascii=False,
            )

        call_mode = self._cfg("call_mode", "parallel")

        # ── 读取直接发送名单（提前定义，供接龙流式转发使用） ────
        direct_agents_str = self._cfg("direct_delivery_agents", "agent_a,agent_b,agent_c")
        direct_agents = {
            name.strip().lower()
            for name in direct_agents_str.split(",")
            if name.strip()
        }
        # 非直接发送代理的完整回复收集
        return_agent_results = []

        logger.info(
            f"[parallel_handoff] Dispatching {len(calls)} subagent calls (mode={call_mode})"
        )
        t_total = time.perf_counter()
        if call_mode == "chained":
            # ── 接龙模式：串行调用，前一个子代理的回复注入下一个的输入 ──
            # 注：direct/relay 均为发送方式，与接龙的调用方式正交，互不冲突。
            # 流式转发：每条完成后立刻发送（direct 直接发/失败立刻通知），
            # 无需等整条链跑完；失败的子代理在链上标注，下一个能看到谁掉队。
            results = []
            for i, c in enumerate(calls):
                if i > 0 and results:
                    prev = results[-1]
                    c = dict(c)
                    prev_display = self._display_name(prev.get("agent_name", ""))
                    if prev.get("success"):
                        prev_text = prev.get('response', '')
                        chain_note = (
                            f"（接龙·上一位）【{prev_display}】的回复：\n"
                            f"{prev_text}\n\n"
                            f"请接续上文，现在轮到你回应："
                        )
                        # ── 接龙长回复精简：开关开启且超阈值 → 摘要+首尾 ──
                        summary_enabled = bool(self._cfg("chain_summary_enabled", True))
                        threshold = int(self._cfg("chain_summary_threshold", 600))
                        if summary_enabled and len(prev_text) > threshold:
                            try:
                                summary_prov = str(self._cfg("chain_summary_model", "") or "").strip()
                                if not summary_prov:
                                    summary_prov = await self.context.get_current_chat_provider_id(
                                        event.unified_msg_origin
                                    )
                                summarized = await self._summarize_chain_reply(
                                    prev_display, prev_text, summary_prov, timeout
                                )
                                if summarized:
                                    chain_note = summarized
                            except Exception as e:
                                logger.warning(
                                    f"[parallel_handoff] 接龙精简准备失败，降级原样注入: {e}"
                                )
                    else:
                        # 失败留痕：链上标注谁掉队了，下一个子代理能看到
                        chain_note = (
                            f"（接龙·上一位）【{prev_display}】超时未接：\n"
                            f"（无回复）\n\n"
                            f"请接续上文，现在轮到你回应："
                        )
                    c["input"] = chain_note + (c.get("input") or "")
                r = await self._call_one(
                    c,
                    event=event,
                    handoff_map=handoff_map,
                    scene_prefix=scene_prefix,
                    livingmemory_plugin=livingmemory_plugin,
                    enable_name_prefix=enable_name_prefix,
                    timeout=timeout,
                )
                results.append(r)
                # ── 流式转发：本条立刻发出，不等整条链 ──
                if enable_segmented_forward:
                    if r.get("success") and r.get("agent_name", "").lower() in direct_agents:
                        # direct 代理：已流式转发，统一转发阶段跳过
                        r["_sent"] = True
                        await self._forward_segmented(r.get("response", ""), event)
                    elif not r.get("success"):
                        # 失败：已通知，统一转发阶段跳过
                        r["_sent"] = True
                        await self._send_failure_notify(r, event)
                    # 非 direct 且成功：不打 _sent，
                    # 交由统一转发阶段收集进 return_agent_results 返回完整回复
            # 接龙模式保持调用顺序发送，不按 order 重排（order 仅对并行模式生效）
        else:
            # ── 并行模式（默认）──
            tasks = [
                self._call_one(
                    c,
                    event=event,
                    handoff_map=handoff_map,
                    scene_prefix=scene_prefix,
                    livingmemory_plugin=livingmemory_plugin,
                    enable_name_prefix=enable_name_prefix,
                    timeout=timeout,
                )
                for c in calls
            ]
            results = await asyncio.gather(*tasks)
            # 按 order 排序（如果有的话）
            has_order = any(r.get("order") is not None for r in results)
            if has_order:
                results.sort(
                    key=lambda r: (
                        r.get("order") if r.get("order") is not None else 999999
                    )
                )
        total_latency_ms = int((time.perf_counter() - t_total) * 1000)

        # ── 跟踪最近调用的子代理（用于消歧） ─────────────────
        if enable_disambiguation:
            session_id = event.unified_msg_origin
            for r in results:
                if r.get("success") and r.get("agent_name"):
                    self._last_agent[session_id] = r["agent_name"]

        success_count = sum(1 for r in results if r.get("success"))
        fail_count = len(results) - success_count
        logger.info(
            f"[parallel_handoff] Completed: {success_count}/{len(results)} succeeded, "
            f"{fail_count} failed, total {total_latency_ms}ms"
        )

        # （direct_agents / return_agent_results 已在调度前定义，此处复用）

        # ── 分段转发：按中文括号拆分逐条发送 ─────────────────
        if enable_segmented_forward:
            self._suppress_mainagent_prefix = True
            pending_text = ""
            for r in results:
                agent_name = r.get("agent_name", "")
                is_direct = agent_name.lower() in direct_agents

                # 接龙模式下该条已流式发送/通知，跳过避免重复
                if r.get("_sent"):
                    continue

                if r.get("success") and is_direct:
                    await self._forward_segmented(r.get("response", ""), event)
                elif r.get("success"):
                    # 非直接发送代理 — 收集完整回复返回给主代理
                    if agent_name not in [ra.get("agent_name") for ra in return_agent_results]:
                        return_agent_results.append(r)
                else:
                    await self._send_failure_notify(r, event)

            # ── 构建返回摘要 ─────────────────────────────────
            summary = {
                "segmented_forward": True,
                "note": "各子代理回复已分条直接发送给用户,以下为摘要",
                "results": [
                    {
                        "agent_name": r.get("agent_name"),
                        "success": r.get("success"),
                        "latency_ms": r.get("latency_ms"),
                        "response_preview": (r.get("response", "") or "")[:120],
                    }
                    for r in results
                ],
                "summary": {
                    "total": len(results),
                    "success": success_count,
                    "failed": fail_count,
                    "total_latency_ms": total_latency_ms,
                },
            }
            # 如有非直接发送代理的完整回复，附加到摘要中
            if return_agent_results:
                summary["note"] = (
                    "以下子代理的回复已直接发送给用户。"
                    "以下子代理的完整回复返回给主代理处理。"
                )
                summary["returned_agents"] = [
                    {
                        "agent_name": ra.get("agent_name"),
                        "success": True,
                        "latency_ms": ra.get("latency_ms"),
                        "full_response": ra.get("response", ""),
                    }
                    for ra in return_agent_results
                ]
            # 注释掉: 此flag由钩子清空chain后自行复位, 不在工具内复位以拦截后续回显
            # self._suppress_mainagent_prefix = False
            # 返回子代理回复文本供主代理转发。
            # 修复：非 direct 代理的完整回复收集在 return_agent_results 中，
            # 只返回 pending_text 会把回复吞成 "✓"——有完整回复时必须返回摘要 JSON。
            if pending_text:
                return pending_text
            if return_agent_results:
                return json.dumps(summary, ensure_ascii=False)
            return "✓"

        # ── 默认：合并返回 ───────────────────────────────────
        return json.dumps(
            {
                "results": results,
                "summary": {
                    "total": len(results),
                    "success": success_count,
                    "failed": fail_count,
                    "total_latency_ms": total_latency_ms,
                },
            },
            ensure_ascii=False,
            indent=2,
        )

    # ── 跨轮上下文辅助方法 ─────────────────────────────────

    def _format_context_history(self, history: list[dict]) -> str:
        """格式化对话历史列表为文本"""
        lines = []
        for msg in history:
            role_label = "user" if msg["role"] == "user" else "assistant"
            lines.append(f"{role_label}: {msg['content']}")
        return "\n".join(lines)

    async def _compress_context_history(
        self, agent_name: str, session_id: str, history: list[dict],
        prov_id: str, handoff, timeout: int
    ) -> str:
        """压缩对话历史：保留最近 N 轮完整对话 + LLM 摘要旧对话"""
        keep_count = self._ctx_keep_recent * 2
        if len(history) <= keep_count:
            return self._format_context_history(history)

        recent = history[-keep_count:]
        old = history[:-keep_count]

        old_text = self._format_context_history(old)
        compress_prompt = (
            f"请将以下对话历史压缩为一段简洁的摘要，保留关键信息、上下文和决策。"
            f"压缩后长度约为原文的{self._ctx_compress_ratio}%。"
            f"只输出摘要文本，不要加任何前缀或解释。\n\n{old_text}"
        )

        llm_resp = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=prov_id,
                prompt=compress_prompt,
                system_prompt="你是一个对话摘要助手。请简洁地总结对话内容。",
            ),
            timeout=min(timeout, 30),
        )
        summary = llm_resp.completion_text.strip()

        recent_text = self._format_context_history(recent)
        return f"[历史摘要]\n{summary}\n\n[最近对话]\n{recent_text}"

    async def _summarize_chain_reply(
        self, prev_display: str, text: str, prov_id, timeout: int
    ) -> str | None:
        """接龙长回复精简：压缩为 摘要+原文首尾 格式。

        - 超阈值才触发，保留首尾各 N 字符，中间用摘要衔接
        - 保留「（接龙·上一位）」与「请接续上文，现在轮到你回应：」标记，
          使 _strip_chain_injection 的记忆剥离链路不受影响
        - 精简失败返回 None，由调用方降级为原样注入
        """
        keep = max(20, int(self._cfg("chain_summary_keep_head_tail", 120)))
        head = text[:keep]
        tail = text[-keep:] if len(text) > keep * 2 else ""
        prompt = (
            f"请将下面这段角色回复压缩为一段简洁的摘要，保留关键剧情、情绪、伏笔和重要原话。"
            f"摘要控制在 {max(keep, 150)} 字以内，只输出摘要内容，不要加任何前缀或解释。\n\n{text}"
        )
        try:
            llm_resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=prov_id,
                    prompt=prompt,
                    system_prompt="你是一个剧情摘要助手，负责把接龙中上一位角色的长回复压缩为简练摘要，保留剧情关键信息。",
                ),
                timeout=min(timeout, 30),
            )
            summary = (llm_resp.completion_text or "").strip()
            if not summary:
                return None
            return (
                f"（接龙·上一位）【{prev_display}】的回复（已精简·原文{len(text)}字）：\n"
                f"【摘要】{summary}\n"
                f"【原文开头】{head}\n"
                f"【原文结尾】{tail}\n\n"
                f"请接续上文，现在轮到你回应："
            )
        except Exception as e:
            logger.warning(f"[parallel_handoff] 接龙精简失败，降级原样注入: {e}")
            return None

    def _append_context(self, agent_name: str, session_id: str, user_input: str, assistant_response: str):
        """追加一轮对话到上下文历史，上限 200 条消息"""
        ctx_key = f"{agent_name}:{session_id}"
        if ctx_key not in self._subagent_contexts:
            self._subagent_contexts[ctx_key] = []

        history = self._subagent_contexts[ctx_key]
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": assistant_response})

        if len(history) > 200:
            self._subagent_contexts[ctx_key] = history[-200:]

    # ── 统一单代理路由实现（装饰器 @llm_tool 在 main.py 壳方法上） ──
    async def call_subagent(
        self,
        event: AstrMessageEvent,
        agent_name: str,
        input: str,
    ) -> str:
        """替代 transfer_to_* 工具的统一入口。调用单个子代理并将回复直接分段转发到用户。

使用场景：
- 用户明确要求与某子代理对话（如「助手B，改掌机的事交给你了」）
- 用户提到子代理名字后说正事
- 相比 transfer_to_* 工具，本工具确保回复直接发到用户而不用主代理转述

Args:
    agent_name (string): 子代理名称。支持英文 id（agent_a, agent_b, agent_c, xi 等）和中文名（助手A, 助手B, 助手C, 夕 等），大小写不敏感
    input (string): 传给子代理的完整问题或指令
"""
        calls = [{"agent_name": agent_name, "input": input}]
        return await self.parallel_handoff(event, calls=calls)

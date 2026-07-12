"""
并行子代理调用插件 (Parallel Handoff)

允许主代理通过 parallel_handoff tool 同时调用多个子代理（如助手A、助手C等）,
并行获取所有回复后统一返回结果。

机制：
- 注册一个 LLM tool: parallel_handoff
- 工具接收 calls 列表,每个元素指定 agent_name 和 input
- 使用 asyncio.gather 并行调用多个子代理
- 单个子代理超时 15 秒自动跳过（可配置）
- 单个子代理失败不影响其他子代理
- 不破坏现有的串行 transfer_to_xxx handoff 机制

配置开关（WebUI 插件配置页）：
- enable_scene_inject: 场景注入,调用子代理时自动在 input 里加上当前场景描述
- enable_subagent_name_prefix: 子代理姓名前缀,并行回复时每个结果前加【名字】前缀
- enable_mainagent_name_prefix: 主代理姓名前缀,通过 format_mainagent_message() 给主代理消息加前缀
- enable_disambiguation: 消息消歧,无指名消息自动按最近对话对象路由
- enable_segmented_forward: 分段转发,子代理回应用分条发不合并
"""

import asyncio
import json
import os
import time
import re

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event.filter import llm_tool
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain


@register(
    "astrbot_plugin_parallel_handoff",
    "hypxtmc",
    "并行子代理调用 — parallel_handoff tool,支持同时调用多个子代理",
    "1.1.0",
)
class ParallelHandoffPlugin(Star):
    """并行子代理调用插件"""

    # agent_name -> 中文显示名 映射表
    AGENT_DISPLAY_NAME = {
        "agent_a": "助手A",
        "agent_c": "助手C",
        "tech": "技术Agent",
        "memory": "记忆管家",
        "search": "搜索Agent",
    }
    # 中文显示名 -> agent_name 反向映射
    AGENT_NAME_REVERSE = {v: k for k, v in AGENT_DISPLAY_NAME.items()}

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config else {}
        # 跟踪每个 session 最近成功调用的子代理,用于消息消歧
        self._last_agent: dict[str, str] = {}
        # 迁移旧配置键（必须在 _migrate_config 之后再读一次 config）
        self._migrate_config()

    def _get_name_display_map(self) -> dict:
        """读取 name_display_map,兼容 JSON 字符串格式"""
        raw = self._cfg("name_display_map", "{}")
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        if isinstance(raw, dict):
            return raw
        return {}

    def _display_name(self, agent_name: str) -> str:
        """返回 agent_name 对应的中文显示名,找不到则返回原名

        优先级：name_display_map 配置 > 硬编码 AGENT_DISPLAY_NAME > agent_name 本身
        """
        # 先查配置中的 name_display_map
        display_map = self._get_name_display_map()
        if agent_name in display_map:
            return display_map[agent_name]
        # 回退到硬编码映射
        return self.AGENT_DISPLAY_NAME.get(agent_name, agent_name)

    # ── 主代理前缀（公共方法，供外部调用） ─────────────────
    def format_mainagent_message(self, message: str, agent_name: str) -> str:
        """给主代理消息加【名字】前缀（由 enable_mainagent_name_prefix 配置控制）。

        不调用 LLM,不消耗 token,纯字符串处理。
        主代理（调用 parallel_handoff 的一方）可调用此方法给回复加前缀。

        Args:
            message: 主代理的回复消息文本
            agent_name: 主代理的名称（如 "助手A"、"助手B"）

        Returns:
            加了前缀的文本（配置开启时）,或原文（配置关闭时）
        """
        if self._cfg("enable_mainagent_name_prefix", False):
            display_name = self._display_name(agent_name)
            return f"【{display_name}】\n{message}"
        return message

    # ── 配置读取 helper ──────────────────────────────────────
    def _cfg(self, key: str, default=None):
        """统一读取配置项,兼容 dict 和 AstrBotConfig 对象"""
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default) if hasattr(self.config, key) else default

    def _get_name_prefix_overrides(self) -> dict:
        """读取 name_prefix_overrides,兼容 JSON 字符串和 dict 两种格式"""
        raw = self._cfg("name_prefix_overrides", {})
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return {}
            try:
                import json
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        if isinstance(raw, dict):
            return raw
        return {}

    def _migrate_config(self):
        """迁移旧配置键 enable_name_prefix -> enable_subagent_name_prefix

        AstrBot 热重载时会根据 _conf_schema.json 剔除不在 schema 中的键,
        导致旧键 enable_name_prefix 在 in-memory config 中丢失。
        这里直接读写原始 JSON 文件,绕过 schema 过滤。
        """
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "config", "astrbot_plugin_parallel_handoff_config.json",
        )
        config_path = os.path.normpath(config_path)

        if not os.path.exists(config_path):
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return

        # 仅当旧键存在且新键不存在时才迁移
        if "enable_name_prefix" not in raw:
            return
        if "enable_subagent_name_prefix" in raw:
            # 新旧键同时存在 -> 不覆盖新值,仅删除旧键避免下次重复判断
            raw.pop("enable_name_prefix", None)
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(raw, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            return

        # 迁移：旧键值 -> 新键
        old_value = raw.pop("enable_name_prefix")
        raw["enable_subagent_name_prefix"] = old_value
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            logger.info(
                f"[parallel_handoff] 已迁移旧配置键 enable_name_prefix -> "
                f"enable_subagent_name_prefix = {old_value}"
            )
        except Exception as e:
            logger.warning(f"[parallel_handoff] 配置迁移写入失败: {e}")
            return

        # 同步更新当前内存中的 config,让本次加载立即生效
        if isinstance(self.config, dict):
            self.config["enable_subagent_name_prefix"] = old_value
        else:
            try:
                setattr(self.config, "enable_subagent_name_prefix", old_value)
            except Exception:
                pass

    # ── 配置持久化 ─────────────────────────────────────────
    def _save_config(self, overrides: dict):
        """将 name_prefix_overrides 写入配置文件并同步内存"""
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "config", "astrbot_plugin_parallel_handoff_config.json",
        )
        config_path = os.path.normpath(config_path)

        # 读取现有配置（保留其他键）
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        else:
            raw = {}

        raw["name_prefix_overrides"] = overrides

        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

        # 同步更新内存 config
        if isinstance(self.config, dict):
            self.config["name_prefix_overrides"] = overrides
        else:
            try:
                setattr(self.config, "name_prefix_overrides", overrides)
            except Exception:
                pass

        logger.info(f"[parallel_handoff] name_prefix_overrides 已更新: {overrides}")

    # ── 场景注入 ─────────────────────────────────────────────
    def _build_scene_context(self, event: AstrMessageEvent) -> str:
        """根据 event 构建当前场景上下文文本"""
        parts = []

        sender = event.get_sender_name()
        if sender:
            parts.append(f"当前对话对象：{sender}")

        msg_type = event.get_message_type()
        type_name = msg_type.value if hasattr(msg_type, "value") else str(msg_type)
        if "group" in type_name.lower():
            group_id = event.get_group_id()
            if group_id:
                parts.append(f"当前场景：群聊（群号 {group_id}）")
            else:
                parts.append("当前场景：群聊")
        elif "friend" in type_name.lower() or "private" in type_name.lower():
            parts.append("当前场景：私聊")

        if parts:
            parts.insert(0, "[场景信息]")
        return "\n".join(parts)

    # ── 热重载 ────────────────────────────────────────────────
    @filter.regex(r"^(热重载一下并行子代理调用插件|热重载并行插件|重载插件|reload_parallel)$")
    async def reload_plugin(self, event: AstrMessageEvent):
        """热重载本插件：在QQ发送"热重载并行插件"等短语即可重新加载,无需wake_prefix"""
        star_manager = getattr(self.context, "_star_manager", None)
        if star_manager is None:
            yield event.plain_result("❌ 重载失败：无法获取插件管理器")
            return

        yield event.plain_result("🔄 正在热重载 parallel_handoff 插件...")

        async def _delayed_reload():
            await asyncio.sleep(0.5)
            try:
                success, err = await star_manager.reload("astrbot_plugin_parallel_handoff")
                if success:
                    logger.info("[parallel_handoff] 插件热重载成功")
                    from astrbot.core.message.components import Plain
                    from astrbot.core.message.message_event_result import MessageChain
                    await event.send(MessageChain([Plain("✅ parallel_handoff 插件热重载成功")]))
                else:
                    logger.error(f"[parallel_handoff] 热重载失败: {err}")
                    from astrbot.core.message.components import Plain
                    from astrbot.core.message.message_event_result import MessageChain
                    await event.send(MessageChain([Plain(f"❌ 热重载失败: {err}")]))
            except Exception as e:
                logger.error(f"[parallel_handoff] 热重载异常: {e}")

        asyncio.create_task(_delayed_reload())

    # ── 动态前缀切换 ───────────────────────────────────────
    @filter.regex(r"(?:关掉|打开)（(.+?)）的前缀|（(.+?)）的前缀(?:关|开)了")
    async def toggle_prefix(self, event: AstrMessageEvent):
        """动态切换子代理名前缀开关

        匹配模式：
        - "关掉（子代理名）的前缀" -> 设为 false
        - "打开（子代理名）的前缀" -> 设为 true
        - "（子代理名）的前缀关了" -> 设为 false
        - "（子代理名）的前缀开了" -> 设为 true
        """
        message = event.get_message_str()
        match = re.search(r"(?:关掉|打开)（(.+?)）的前缀|（(.+?)）的前缀(?:关|开)了", message)
        if not match:
            return

        # 提取子代理中文名（group 1 或 group 2）
        chinese_name = match.group(1) or match.group(2)
        if not chinese_name:
            return

        # 反向查找 agent_name
        agent_name = self.AGENT_NAME_REVERSE.get(chinese_name)
        if not agent_name:
            yield event.plain_result(f"❌ 未知子代理「{chinese_name}」,可用：{list(self.AGENT_NAME_REVERSE.keys())}")
            return

        # 判断开关方向
        raw_msg = match.group(0)
        if raw_msg.startswith("关掉") or raw_msg.endswith("关了"):
            new_value = False
            verb = "已关闭"
        elif raw_msg.startswith("打开") or raw_msg.endswith("开了"):
            new_value = True
            verb = "已开启"
        else:
            return

        # 读取当前覆盖表
        overrides = self._get_name_prefix_overrides()
        if not isinstance(overrides, dict):
            overrides = {}
        overrides[agent_name] = new_value

        # 持久化
        self._save_config(overrides)

        yield event.plain_result(f"✅ {verb}「{chinese_name}」的名字前缀")

    # ── 核心 tool ────────────────────────────────────────────
    @llm_tool(name="parallel_handoff")
    async def parallel_handoff(
        self,
        event: AstrMessageEvent,
        calls: list[dict] = None,
        timeout: int = 15,
        message: str = None,
    ) -> str:
        """并行调用多个子代理（如助手A、助手C、tech、memory、search）,
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
        # 读取子代理前缀配置（旧键 enable_name_prefix 已在 _migrate_config 中迁移）
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

        # ── 场景注入前缀 ─────────────────────────────────────
        scene_prefix = ""
        if enable_scene_inject:
            scene_prefix = self._build_scene_context(event)

        # ── 单子代理调用 ─────────────────────────────────────
        async def _call_one(call: dict) -> dict:
            """调用单个子代理,带超时和错误隔离"""
            agent_name = (call.get("agent_name") or "").strip()
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

            # 场景注入：在 input 前拼接场景上下文
            final_input = input_text
            if enable_scene_inject and scene_prefix:
                final_input = f"{scene_prefix}\n\n{input_text}"

            t0 = time.perf_counter()
            try:
                umo = event.unified_msg_origin
                prov_id = (
                    handoff.provider_id
                    or await self.context.get_current_chat_provider_id(umo)
                )

                llm_resp = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=prov_id,
                        prompt=final_input,
                        system_prompt=handoff.agent.instructions or "",
                    ),
                    timeout=timeout,
                )
                latency_ms = int((time.perf_counter() - t0) * 1000)
                raw_response = llm_resp.completion_text

                # 姓名前缀（先查覆盖表,再走全局开关）
                overrides = self._get_name_prefix_overrides()
                if isinstance(overrides, dict) and agent_name in overrides:
                    should_prefix = overrides[agent_name]
                else:
                    should_prefix = enable_name_prefix

                if should_prefix:
                    display_name = self._display_name(agent_name)
                    raw_response = f"【{display_name}】\n{raw_response}"

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
                err_text = f"Timeout after {timeout}s"
                overrides = self._get_name_prefix_overrides()
                if isinstance(overrides, dict) and agent_name in overrides:
                    should_prefix = overrides[agent_name]
                else:
                    should_prefix = enable_name_prefix
                if should_prefix:
                    display_name = self._display_name(agent_name)
                    err_text = f"【{display_name}】\n{err_text}"
                return {
                    "agent_name": agent_name,
                    "success": False,
                    "response": err_text,
                    "latency_ms": latency_ms,
                    "order": order,
                }
            except Exception as e:
                latency_ms = int((time.perf_counter() - t0) * 1000)
                logger.error(
                    f"[parallel_handoff] Subagent '{agent_name}' failed: {e}"
                )
                err_text = f"Error: {e}"
                overrides = self._get_name_prefix_overrides()
                if isinstance(overrides, dict) and agent_name in overrides:
                    should_prefix = overrides[agent_name]
                else:
                    should_prefix = enable_name_prefix
                if should_prefix:
                    display_name = self._display_name(agent_name)
                    err_text = f"【{display_name}】\n{err_text}"
                return {
                    "agent_name": agent_name,
                    "success": False,
                    "response": err_text,
                    "latency_ms": latency_ms,
                    "order": order,
                }

        # ── 并行调度 ─────────────────────────────────────────
        if not calls:
            return json.dumps(
                {"results": [], "note": "calls array is empty"},
                ensure_ascii=False,
            )

        logger.info(
            f"[parallel_handoff] Dispatching {len(calls)} parallel subagent calls"
        )
        t_total = time.perf_counter()
        tasks = [_call_one(c) for c in calls]
        results = await asyncio.gather(*tasks)
        total_latency_ms = int((time.perf_counter() - t_total) * 1000)

        # 按 order 排序（如果有的话）
        has_order = any(r.get("order") is not None for r in results)
        if has_order:
            results.sort(
                key=lambda r: (
                    r.get("order") if r.get("order") is not None else 999999
                )
            )

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

        # ── 分段转发：按中文括号拆分逐条发送 ─────────────────
        if enable_segmented_forward:
            for r in results:
                if r.get("success"):
                    text = r.get("response", "")
                    # 1. 如果文本中含有中文括号,按括号拆分
                    if "（" in text:
                        raw_segments = re.split(r"(（[^）]*）)", text)
                        raw_segments = [s.strip() for s in raw_segments if s.strip()]
                        min_len = self.config.get("min_fragment_length", 5)
                        segments = []
                        i = 0
                        while i < len(raw_segments):
                            seg = raw_segments[i]
                            # 括号内动作描写太短则合并到下一段对话
                            if (
                                seg.startswith("（")
                                and seg.endswith("）")
                                and len(seg) - 2 < min_len
                                and i + 1 < len(raw_segments)
                            ):
                                segments.append(seg + raw_segments[i + 1])
                                i += 2
                            else:
                                segments.append(seg)
                                i += 1
                    else:
                        # 2. 回退到换行拆分：先双换行,再单换行
                        segments = text.split("\n\n")
                        if len(segments) == 1:
                            segments = text.split("\n")
                        segments = [s.strip() for s in segments if s.strip()]
                    for seg in segments:
                        if not seg:
                            continue
                        try:
                            await event.send(MessageChain([Plain(seg)]))
                            await asyncio.sleep(self.config.get("fragment_interval", 0.3))
                        except Exception as e:
                            logger.error(
                                f"[parallel_handoff] 分段发送失败 [{r.get('agent_name')}]: {e}"
                            )
            # 返回摘要给 LLM,避免重复输出完整内容
            return json.dumps(
                {
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
                },
                ensure_ascii=False,
                indent=2,
            )

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

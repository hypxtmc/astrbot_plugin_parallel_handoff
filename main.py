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
- enable_mainagent_segmented: 主代理分段转发,主代理自身回复按段落分条发送
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
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.tool import ToolSet
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain


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
        "agent_b": "助手B",
        "agent_d": "助手D",
        "xi": "夕",
        "shu": "黍",
        "nian": "年",
        "ling": "令",
    }
    # 中文显示名 -> agent_name 反向映射
    AGENT_NAME_REVERSE = {v: k for k, v in AGENT_DISPLAY_NAME.items()}

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config else {}
        # 跟踪每个 session 最近成功调用的子代理,用于消息消歧
        self._last_agent: dict[str, str] = {}
        # LLM 工具调用防重标记：{message_key -> (ts, None)}，同一消息重复路由直接短路
        self._tool_call_seen: dict[str, tuple[float, None]] = {}
        # 跨轮对话上下文：{agent_name:session_id -> [{"role": ..., "content": ...}, ...]}
        self._subagent_contexts: dict[str, list[dict]] = {}
        self._ctx_enabled = bool(self._cfg("subagent_context_enabled", True))
        self._ctx_max_turns = int(self._cfg("subagent_context_max_turns", 100))
        self._ctx_keep_recent = int(self._cfg("subagent_context_keep_recent", 5))
        self._ctx_compress_ratio = int(self._cfg("subagent_context_compress_ratio", 15))
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

    async def _forward_segmented(self, text, event):
        """单条子代理回复的分段直接发送（流式转发/统一转发共用）。"""
        try:
            prefix = ""
            content = text
            prefix_match = re.match(r"^(【[^】]+】)\n", text)
            if prefix_match:
                prefix = prefix_match.group(1)
                content = text[prefix_match.end():]
            if "（" in content:
                raw_segments = re.split(r"(（[^）]*）)", content)
                raw_segments = [s.strip() for s in raw_segments if s.strip()]
                min_len = self.config.get("min_fragment_length", 5)
                segments = []
                i = 0
                while i < len(raw_segments):
                    seg = raw_segments[i]
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
                final_segments = []
                for seg in segments:
                    sub_segs = seg.split("\n\n")
                    final_segments.extend([s.strip() for s in sub_segs if s.strip()])
                segments = final_segments
            else:
                segments = content.split("\n\n")
                if len(segments) == 1:
                    segments = content.split("\n")
                segments = [s.strip() for s in segments if s.strip()]
            for idx, seg_text in enumerate(segments):
                if idx == 0 and prefix:
                    msg = f"{prefix}\n{seg_text}"
                else:
                    msg = seg_text
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain([Plain(msg)]),
                )
                await asyncio.sleep(self.config.get("fragment_interval", 0.3))
        except Exception as e:
            logger.error(f"[parallel_handoff] 分段发送失败: {e}")

    def _extract_chain_text(self, result) -> str:
        """拼接 result.chain 中所有文本组件的文本并去除首尾空白。"""
        full_text = ""
        if hasattr(result, "chain") and result.chain:
            for comp in result.chain:
                text = getattr(comp, "text", None)
                if isinstance(text, str):
                    full_text += text
        return full_text.strip()

    async def _send_mainagent_segmented(self, result, event) -> bool:
        """主代理回复分段发送：按空行拆分，段落数>1时逐条直发。

        与 enable_mainagent_segmented 配置联动；首段可选加【主代理名】前缀。
        返回 True=已分段发送并清空链；False=未分段（未开启/段落数<=1/发送异常），
        链保持原样由调用方走原样发送路径兜底。
        """
        if not self._cfg("enable_mainagent_segmented", False):
            return False
        full_text = self._extract_chain_text(result)
        if not full_text:
            return False
        # [主代理亲修 03:08] 剥离首部【名字】前缀，防止与下方 add_prefix 叠加成双前缀
        full_text = re.sub(r"^\s*【[^】]*】\s*\n?", "", full_text, count=1)
        segments = [s.strip() for s in full_text.split("\n\n") if s.strip()]
        if len(segments) <= 1:
            return False
        try:
            add_prefix = self._cfg("enable_mainagent_name_prefix", False)
            prefix_str = ""
            if add_prefix:
                prefix_str = f"【{self._cfg('main_agent_name', '主代理')}】\n"
            for idx, seg_text in enumerate(segments):
                msg = seg_text
                if idx == 0 and prefix_str and not msg.startswith("【"):
                    msg = f"{prefix_str}{seg_text}"
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain([Plain(msg)]),
                )
                await asyncio.sleep(self.config.get("fragment_interval", 0.3))
        except Exception as e:
            logger.error(f"[inject_mainagent_prefix] 分段发送失败: {e}")
            return False
        # 清空原始链，不让框架重复发送
        result.chain.clear()
        return True

    async def _send_failure_notify(self, r, event):
        """子代理失败（超时/报错）时通知用户。"""
        try:
            agent_name = r.get("agent_name", "未知")
            err_text = r.get("response", "未知错误")
            if "Timeout" in err_text:
                notify = f"【{self._display_name(agent_name)}】超时了，没有回复"
            else:
                notify = f"【{self._display_name(agent_name)}】出错了: {err_text}"
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain([Plain(notify)]),
            )
            await asyncio.sleep(self.config.get("fragment_interval", 0.3))
        except Exception as e:
            logger.error(
                f"[parallel_handoff] 失败通知发送失败 [{r.get('agent_name')}]: {e}"
            )

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

    def _resolve_agent_name(self, agent_name: str) -> str:
        """规范化子代理名称：兼容中文名（助手A->agent_a）和大小写（助手->agent_a）。

        优先级：中文显示名反查（硬编码 AGENT_NAME_REVERSE + name_display_map 配置）
        > 小写归一。找不到则返回小写后的原名（由调用方报错兜底）。
        """
        if not agent_name:
            return agent_name
        # 中文显示名反查：硬编码表 + 配置表（display_map 为 {英文id: 中文名}）
        reverse = dict(self.AGENT_NAME_REVERSE)
        for eng, cn in self._get_name_display_map().items():
            reverse.setdefault(cn, eng)
        return reverse.get(agent_name) or agent_name.lower()

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

    # ── 主代理前缀自动注入（on_decorating_result 钩子）───────
    @filter.on_decorating_result()
    async def _inject_mainagent_prefix(self, event: AstrMessageEvent):
        """在主代理消息发出前自动加【名字】前缀

        通过 on_decorating_result 钩子拦截所有即将发送的消息，
        当 enable_mainagent_name_prefix=true 且消息不以任何子代理前缀开头时，
        自动在消息正文前加 "【主代理名】\n" 前缀。
        子代理分段转发期间此钩子被抑制，避免误加。
        """
        if getattr(self, "_suppress_mainagent_prefix", False):
            # 分段转发已直发子代理回复。主代理若有实质台词则加前缀放行，否则静默。
            # allow_mainagent_after_direct=false 时恢复旧行为：无条件清空。
            result = event.get_result()
            has_chain = hasattr(result, "chain")
            if not self._cfg("allow_mainagent_after_direct", True):
                if has_chain:
                    result.chain.clear()
                self._suppress_mainagent_prefix = False
                return
            has_real_content = False
            if has_chain:
                for comp in result.chain:
                    text = getattr(comp, "text", None)
                    if isinstance(text, str) and text.strip():
                        has_real_content = True
                        break
            if not has_real_content:
                # 主代理无实质台词：清空链，静默（不发空壳）
                if has_chain:
                    result.chain.clear()
                self._suppress_mainagent_prefix = False
                return
            # 主代理有实质台词：优先走分段转发（enable_mainagent_segmented 联动），
            # 未分段（段落数<=1 或未开启）时复刻姓名前缀逻辑后原样放行。
            if await self._send_mainagent_segmented(result, event):
                # 已分段逐条发送并清空链，静默
                self._suppress_mainagent_prefix = False
                return
            if self._cfg("enable_mainagent_name_prefix", False):
                main_agent_name = self._cfg("main_agent_name", "主代理")
                prefix_str = f"【{main_agent_name}】\n"
                for comp in result.chain:
                    text = getattr(comp, "text", None)
                    if not isinstance(text, str) or text.startswith("【"):
                        continue
                    if not text.strip():
                        continue
                    comp.text = prefix_str + text
                    break
            self._suppress_mainagent_prefix = False
            return

        # ── 主代理分段转发：按空行拆分，逐条发送 ──
        result = event.get_result()
        if await self._send_mainagent_segmented(result, event):
            # 已分段逐条发送并清空链，不再走下方前缀注入
            return
        if not self._cfg("enable_mainagent_name_prefix", False):
            return

        result = event.get_result()
        if not (hasattr(result, "chain") and result.chain):
            return

        main_agent_name = self._cfg("main_agent_name", "主代理")
        prefix_str = f"【{main_agent_name}】\n"

        for comp in result.chain:
            text = getattr(comp, "text", None)
            if not isinstance(text, str) or text.startswith("【"):
                continue
            # 跳过纯空白文本——避免 tool call 前的空行被加上前缀
            if not text.strip():
                continue
            comp.text = prefix_str + text
            logger.info(
                f"[parallel_handoff] 已为主代理消息添加前缀: "
                f"【{main_agent_name}】"
            )
            break  # 只给第一个 text 组件加前缀

    # ── 路由强制指令注入（OnLLMRequestEvent 钩子）───────────
    @filter.on_llm_request()
    async def _route_directive_inject(self, event: AstrMessageEvent, req: ProviderRequest):
        """按配置向主代理 LLM 请求注入路由强制指令（软注入，不拦截）。

        触发点：仅主代理请求经过 OnLLMRequestEvent（子代理走 llm_generate 不触发）。
        作用：把 route_mode / call_mode / direct_delivery_agents 算出的路由路径规范
        注入 req.system_prompt，主代理必须照走；不修改 req.func_tool（工具全保留）。
        system_prompt 已含标记则跳过，避免 agent 循环多轮重复注入。
        """
        try:
            if not bool(self._cfg("enable_route_directive", True)):
                return False
            directive = self._build_route_directive()
            if not directive:
                return False
            marker = "【路由强制指令·parallel_handoff】"
            system_prompt = req.system_prompt or ""
            if marker in system_prompt:
                return False
            req.system_prompt = f"{system_prompt}\n\n{directive}".strip()
            logger.info(
                f"[parallel_handoff] 路由强制指令已注入 "
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
        if route_mode == "relay":
            lines.append("- 路由模式 relay：所有子代理回复返回主代理，由主代理把关后转述，不直接发用户端")
        else:
            lines.append(f"- 路由模式 direct：以下子代理回复直接分段转发用户端，不经主代理转述：{name_str}")
            lines.append("- 其余子代理（如技术Agent）走 relay：回复返回主代理，由主代理把关后转述")
        if call_mode == "chained":
            lines.append("- 调用模式 chained：多子代理按接龙顺序串行调用，前一个的回复作为后一个的上下文，禁止并行双发")
        else:
            lines.append("- 调用模式 parallel：多子代理并行调用，各说各话，回复统一汇总返回")
        lines.append("- 工具列表保持完整，禁止摘除或绕过任何工具")
        return "\n".join(lines)

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
        sender_id = event.get_sender_id()
        if sender and sender_id:
            parts.append(f"当前对话对象：{sender}（ID: {sender_id}）")
        elif sender:
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
    def _tool_call_dedup_key(self, event: AstrMessageEvent, agents: list = None) -> str:
        """生成工具调用防重 key：消息 ID + 路由目标子代理签名。

        agents 参与 key 计算：同一条消息内串行调用不同子代理各自放行，
        只有完全重复的路由（同一批子代理）才短路。agents 为空时回退纯消息 key。
        """
        mid = getattr(getattr(event, "message_obj", None), "message_id", None)
        base = f"mid:{mid}" if mid else f"evt:{id(event)}"
        if agents:
            sig = ",".join(sorted({str(a).strip() for a in agents if str(a).strip()}))
            if sig:
                return f"{base}|agents:{sig}"
        return base

    def _dedup_guard(self, event: AstrMessageEvent, agents: list = None):
        """LLM 同回合重复调用防重：同一消息对同一批子代理的重复路由短路，不同子代理放行。

        返回 None 表示放行；返回 str 表示命中重复，直接作为工具结果返回。
        窗口 60s，覆盖一次完整 LLM 生成回合；过期自动清理防内存膨胀。
        """
        dedup_key = self._tool_call_dedup_key(event, agents)
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

    @llm_tool(name="parallel_handoff")
    async def parallel_handoff(
        self,
        event: AstrMessageEvent,
        calls: list[dict] = None,
        timeout: int = 30,
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
        # ── LLM 同回合重复调用防重：同一消息对同一批子代理的重复路由短路 ──
        # 防重 key 含本次路由目标子代理名单，串行调不同子代理可各自放行
        target_agents = [c.get("agent_name") for c in (calls or []) if isinstance(c, dict)]
        dedup_result = self._dedup_guard(event, agents=target_agents)
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
        # ── 共用剧情基线注入（所有子代理共享的世界设定锚点） ──
        if self._cfg("enable_baseline_inject", True):
            baseline = (self._cfg("shared_scene_baseline", "") or "").strip()
            if baseline:
                scene_prefix = (
                    f"{scene_prefix}\n【共用剧情场景基线】\n{baseline}\n"
                    if scene_prefix
                    else f"【共用剧情场景基线】\n{baseline}\n"
                )

        # ── 长期记忆插件查找 ─────────────────────────────────
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

        # ── 单子代理调用 ─────────────────────────────────────
        async def _call_one(call: dict) -> dict:
            """调用单个子代理,带超时和错误隔离"""
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

            # 场景注入：在 input 前拼接场景上下文
            final_input = input_text
            if enable_scene_inject and scene_prefix:
                final_input = f"{scene_prefix}\n\n{input_text}"

            # ── 记忆召回：注入长期记忆 ──
            # 接龙注入的前文是临时上下文：记忆链路（召回/存储）统一剥离，
            # 避免上一个子代理的输出污染本子代理的长期记忆。
            clean_input = _strip_chain_injection(final_input)
            if livingmemory_plugin:
                try:
                    event.persona_id = agent_name
                    req = ProviderRequest(
                        prompt=clean_input,
                        extra_user_content_parts=[],
                    )
                    await livingmemory_plugin.handle_memory_recall(event, req)

                    # 保留记忆注入内容，透传给子代理的 provider
                    memory_extra_parts = list(req.extra_user_content_parts or [])
                except Exception as e:
                    logger.warning(
                        f"[parallel_handoff] Memory recall failed for "
                        f"{agent_name}: {e}"
                    )
                    memory_extra_parts = []
            else:
                memory_extra_parts = []

            # ── 构建子代理工具集 ──
            subagent_tools = None
            try:
                # 子代理记忆配置：扁平字段优先，兼容旧的 subagent_memory 嵌套对象
                memory_cfg = self._cfg("subagent_memory", {})
                if isinstance(memory_cfg, dict) and memory_cfg:
                    memory_enabled = self._cfg("recall_enabled", memory_cfg.get("enabled", True))
                    exclude_raw = self._cfg("exclude_agents", memory_cfg.get("exclude_agents", "tech,技术Agent"))
                else:
                    memory_enabled = self._cfg("recall_enabled", True)
                    exclude_raw = self._cfg("exclude_agents", "tech,技术Agent")
                if memory_enabled:
                    exclude_agents = {name.strip() for name in exclude_raw.split(",") if name.strip()}
                    if agent_name not in exclude_agents:
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

            t0 = time.perf_counter()
            try:
                umo = event.unified_msg_origin
                prov_id = (
                    handoff.provider_id
                    or await self.context.get_current_chat_provider_id(umo)
                )

                # ── 上下文注入：跨轮对话历史 ──
                if self._ctx_enabled and agent_name not in ("tech", "技术Agent"):
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

                # ── 记忆存储：存入长期记忆 ──
                if livingmemory_plugin:
                    try:
                        conv_mgr = (
                            livingmemory_plugin
                            .event_handler._memory_recall.conversation_manager
                        )
                        await conv_mgr.add_message_from_event(
                            event, role="user", content=_strip_chain_injection(final_input)
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

                # 清理 persona_id（由上方的记忆召回阶段设置），确保下轮调用不残留
                if hasattr(event, "persona_id"):
                    delattr(event, "persona_id")

                # ── 上下文存储：追加到跨轮对话历史 ──
                if self._ctx_enabled and agent_name not in ("tech", "技术Agent"):
                    self._append_context(agent_name, event.unified_msg_origin, input_text, raw_response)

                # 自动转发已由 parallel_handoff 的分段转发负责,此处不再重复推送
                # 姓名前缀（先查覆盖表,再走全局开关）
                overrides = self._get_name_prefix_overrides()
                if isinstance(overrides, dict) and agent_name in overrides:
                    should_prefix = overrides[agent_name]
                else:
                    should_prefix = enable_name_prefix

                if should_prefix:
                    display_name = self._display_name(agent_name)
                    prefix_str = f"【{display_name}】\n"
                    if not raw_response.startswith(prefix_str):
                        raw_response = prefix_str + raw_response

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
                overrides = self._get_name_prefix_overrides()
                if isinstance(overrides, dict) and agent_name in overrides:
                    should_prefix = overrides[agent_name]
                else:
                    should_prefix = enable_name_prefix
                if should_prefix:
                    display_name = self._display_name(agent_name)
                    prefix_str = f"【{display_name}】\n"
                    if not err_text.startswith(prefix_str):
                        err_text = prefix_str + err_text
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
                overrides = self._get_name_prefix_overrides()
                if isinstance(overrides, dict) and agent_name in overrides:
                    should_prefix = overrides[agent_name]
                else:
                    should_prefix = enable_name_prefix
                if should_prefix:
                    display_name = self._display_name(agent_name)
                    prefix_str = f"【{display_name}】\n"
                    if not err_text.startswith(prefix_str):
                        err_text = prefix_str + err_text
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
                r = await _call_one(c)
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
                    # 非 direct 且成功（如 tech）：不打 _sent，
                    # 交由统一转发阶段收集进 return_agent_results 返回完整回复
            # 接龙模式保持调用顺序发送，不按 order 重排（order 仅对并行模式生效）
        else:
            # ── 并行模式（默认）──
            tasks = [_call_one(c) for c in calls]
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
                    # 非直接发送代理（如tech）— 收集完整回复返回给主代理
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
            # 修复：非 direct 代理（如 tech）的完整回复收集在 return_agent_results 中，
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

    # ── 统一单代理路由 ─────────────────────────────────────
    @llm_tool(name="call_subagent")
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
    agent_name (string): 子代理名称。支持英文 id（agent_a, agent_b, agent_c, tech, xi 等）和中文名（助手A, 助手B, 助手C, 技术Agent, 夕 等），大小写不敏感
    input (string): 传给子代理的完整问题或指令
"""
        calls = [{"agent_name": agent_name, "input": input}]
        return await self.parallel_handoff(event, calls=calls)

@llm_tool(name="call_tech_relay")
async def call_tech_relay(self, event: AstrMessageEvent, input: str) -> str:
    """技术Agent专用 relay 路由接口。调用 tech/技术Agent 并将完整回复返回主代理，由主代理把关后转述。

relay 语义：回复不直接发送到用户端，完整返回主代理。技术Agent执行结果需主代理确认后再转述，
直接透传容易出问题，故单开此接口强制走 relay，不受 route_mode / direct_delivery_agents 配置影响。

Args:
    input (string): 传给技术Agent的完整问题或指令
"""
    calls = [{"agent_name": "tech", "input": input}]
    raw = await self.parallel_handoff(event, calls=calls)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    # segmented 分支：非 direct 代理的完整回复在 returned_agents
    for ra in data.get("returned_agents") or []:
        if ra.get("full_response"):
            return json.dumps(
                {
                    "agent_name": ra.get("agent_name"),
                    "success": True,
                    "relay": True,
                    "latency_ms": ra.get("latency_ms"),
                    "full_response": ra.get("full_response"),
                },
                ensure_ascii=False,
            )
    # 合并返回分支：完整回复在 results[0].response
    for r in data.get("results") or []:
        if r.get("response"):
            return json.dumps(
                {
                    "agent_name": r.get("agent_name"),
                    "success": True,
                    "relay": True,
                    "latency_ms": r.get("latency_ms"),
                    "full_response": r.get("response"),
                },
                ensure_ascii=False,
            )
    return raw

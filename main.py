"""并行子代理调用插件 (Parallel Handoff) — 入口 + 事件注册（P0 拆模块）

允许主代理通过 parallel_handoff tool 同时调用多个子代理（如助手A、助手C等）,
并行获取所有回复后统一返回结果。

P0 重构说明：本文件只保留插件入口与事件注册（装饰器方法）。被
@filter.on_decorating_result / on_llm_request / regex 与 @llm_tool 装饰的方法必须
定义在本模块，保证 handler_module_path 与插件主模块一致（AstrBot star_manager
按 module_path 绑定插件实例），具体实现通过 super() 转发到各 mixin 模块：
- config.py    ConfigMixin    配置读取/迁移/保存/前缀开关
- directive.py DirectiveMixin 路由强制指令 + 强制直连黑名单映射
- scene.py     SceneMixin     场景上下文 + 剧情基线
- memory.py    MemoryMixin    livingmemory 集成/召回/存储/记忆工具过滤
- dispatch.py  DispatchMixin  去重守卫/单子代理调用/并行调度/跨轮上下文
- forward.py   ForwardMixin   分段转发/主代理前缀注入

导入兼容：真实 AstrBot 环境插件以 data.plugins.xxx 包方式导入（相对导入）；
单测 mock 环境用 spec_from_file_location 直接加载本文件（无 package，走绝对导入）。
"""
import asyncio

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event.filter import llm_tool
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

try:
    from . import config as _config_mod
    from . import directive as _directive_mod
    from . import scene as _scene_mod
    from . import memory as _memory_mod
    from . import dispatch as _dispatch_mod
    from . import forward as _forward_mod
    from . import ctx_engine as _ctx_engine_mod
    from . import router as _router_mod
except ImportError:
    import config as _config_mod
    import directive as _directive_mod
    import scene as _scene_mod
    import memory as _memory_mod
    import dispatch as _dispatch_mod
    import forward as _forward_mod
    import ctx_engine as _ctx_engine_mod
    import router as _router_mod


@register(
    "astrbot_plugin_parallel_handoff",
    "hypxtmc",
    "并行子代理调用 — parallel_handoff tool,支持同时调用多个子代理",
    "2.0.0",
)
class ParallelHandoffPlugin(
    _forward_mod.ForwardMixin,
    _dispatch_mod.DispatchMixin,
    _memory_mod.MemoryMixin,
    _scene_mod.SceneMixin,
    _directive_mod.DirectiveMixin,
    _config_mod.ConfigMixin,
    _router_mod.RouterMixin,
    Star,
):
    """并行子代理调用插件"""

    # agent_name -> 中文显示名 映射表
    AGENT_DISPLAY_NAME = {
        "agent_a": "助手A",
        "agent_c": "助手C",
        "memory": "记忆管家",
        "search": "搜索Agent",
        "agent_b": "助手B",
        "agent_d": "助手D",
        "xi": "夕",
        "shu": "黍",
        "nian": "年",
        "ling": "令",
        "agent_f": "助手F",
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
        # 跨轮对话上下文：独立引擎 ContextEngine（压缩参数构造注入，可独立调参）
        self._ctx_engine = _ctx_engine_mod.ContextEngine(
            enabled=self._cfg("subagent_context_enabled", True),
            max_turns=self._cfg("subagent_context_max_turns", 100),
            keep_recent=self._cfg("subagent_context_keep_recent", 5),
            compress_ratio=self._cfg("subagent_context_compress_ratio", 15),
            llm_generate=lambda *a, **k: (
                self.context.llm_generate(*a, **k)
                if hasattr(self, "context") and self.context else None
            ),
        )

    # ── 事件注册：主代理前缀自动注入（实现见 forward.py ForwardMixin） ──
    @filter.on_decorating_result()
    async def _inject_mainagent_prefix(self, event: AstrMessageEvent):
        """在主代理消息发出前自动加【名字】前缀

        通过 on_decorating_result 钩子拦截所有即将发送的消息，
        当 enable_mainagent_name_prefix=true 且消息不以任何子代理前缀开头时，
        自动在消息正文前加 "【主代理名】\n" 前缀。
        子代理分段转发期间此钩子被抑制，避免误加。
        """
        return await super()._inject_mainagent_prefix(event)

    # ── 事件注册：路由强制指令注入（实现见 directive.py DirectiveMixin） ──
    @filter.on_llm_request()
    async def _route_directive_inject(self, event: AstrMessageEvent, req: ProviderRequest):
        """按配置向主代理 LLM 请求注入路由强制指令（软注入，不拦截）。

        触发点：仅主代理请求经过 OnLLMRequestEvent（子代理走 llm_generate 不触发）。
        作用：把 route_mode / call_mode / direct_delivery_agents 算出的路由路径规范
        追加到 req.extra_user_content_parts（请求尾部，livingmemory 同款），主代理必须照走；
        不修改 req.func_tool（工具全保留）。系统提示与历史消息为请求前缀，完全不动，
        故不破坏 DeepSeek 前缀缓存命中率；mark_as_temp 置 _no_save，不写入对话历史。
        已含标记则跳过，避免 agent 循环多轮重复注入。
        """
        return await super()._route_directive_inject(event, req)

    # ── 事件注册：小模型路由层（T1规则/T2小模型/T3兜底，实现见 router.py RouterMixin） ──
    @filter.on_waiting_llm_request()
    async def _smart_router_check(self, event: AstrMessageEvent):
        """小模型路由层：主代理 LLM 调用前最早停点判向。

        命中（点名/领域词或小模型高置信）→ 子代理直发 + event.stop_event()，
        主代理流程整体短路，省掉主模型推理与记忆召回；未命中原样放行主代理。
        总开关 enable_smart_router 默认关，显式开启才生效。
        """
        return await super()._smart_router_check(event)


    # ── 热重载（插件入口命令，完整实现保留本模块） ────────────
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

    # ── 事件注册：动态前缀切换（实现见 config.py ConfigMixin） ──
    @filter.regex(r"(?:关掉|打开)（(.+?)）的前缀|（(.+?)）的前缀(?:关|开)了")
    async def toggle_prefix(self, event: AstrMessageEvent):
        """动态切换子代理名前缀开关

        匹配模式：
        - "关掉（子代理名）的前缀" -> 设为 false
        - "打开（子代理名）的前缀" -> 设为 true
        - "（子代理名）的前缀关了" -> 设为 false
        - "（子代理名）的前缀开了" -> 设为 true
        """
        return await super().toggle_prefix(event)

    # ── LLM 工具注册：parallel_handoff（实现见 dispatch.py DispatchMixin） ──
    @llm_tool(name="parallel_handoff")
    async def parallel_handoff(
        self,
        event: AstrMessageEvent,
        calls: list[dict] = None,
        timeout: int = 120,
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
    timeout(number): 单个子代理的超时秒数,默认120秒（顾主设定，永久生效）。超过此时间未返回则跳过该子代理。
    message(string): 当开启消息消歧且不传calls时,传入原始消息文本,工具会自动路由到最近对话的子代理。
"""
        return await super().parallel_handoff(event, calls, timeout, message)

    # ── LLM 工具注册：call_subagent（实现见 dispatch.py DispatchMixin） ──
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
    agent_name (string): 子代理名称。支持英文 id（agent_a, agent_b, agent_c, xi 等）和中文名（助手A, 助手B, 助手C, 夕 等），大小写不敏感
    input (string): 传给子代理的完整问题或指令
"""
        return await super().call_subagent(event, agent_name, input)

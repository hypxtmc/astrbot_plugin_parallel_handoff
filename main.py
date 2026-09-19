"""并行子代理调用插件 (Parallel Handoff) — 入口 + 事件注册（P0 拆模块）

允许主代理通过 parallel_handoff tool 同时调用多个子代理（如 agent_a、agent_b 等）,
并行获取所有回复后统一返回结果。

P0 重构说明：本文件只保留插件入口与事件注册（装饰器方法）。被
@filter.on_decorating_result / on_llm_request / regex 与 @llm_tool 装饰的方法必须
定义在本模块，保证 handler_module_path 与插件主模块一致（AstrBot star_manager
按 module_path 绑定插件实例），具体实现通过 super() 转发到各 mixin 模块：
- config.py    ConfigMixin    配置读取/迁移/保存/前缀开关
- directive.py DirectiveMixin 路由强制指令 + 强制直连黑名单映射
- scene.py     SceneMixin     场景上下文
- memory.py    MemoryMixin    livingmemory 集成/召回/存储/记忆工具过滤
- dispatch.py  DispatchMixin  去重守卫/单子代理调用/并行调度/跨轮上下文
- forward.py   ForwardMixin   分段转发/主代理前缀注入

导入兼容：真实 AstrBot 环境插件以 data.plugins.xxx 包方式导入（相对导入）；
单测 mock 环境用 spec_from_file_location 直接加载本文件（无 package，走绝对导入）。
"""
import asyncio
import json
import os

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
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
    from . import session_store as _session_store_mod
    from . import task_runner as _task_runner_mod
    from . import router as _router_mod
    from . import arbitrate as _arbitrate_mod
    from . import metrics as _metrics_mod
except ImportError:
    import config as _config_mod
    import directive as _directive_mod
    import scene as _scene_mod
    import memory as _memory_mod
    import dispatch as _dispatch_mod
    import forward as _forward_mod
    import ctx_engine as _ctx_engine_mod
    import session_store as _session_store_mod
    import task_runner as _task_runner_mod
    import router as _router_mod
    import arbitrate as _arbitrate_mod
    import metrics as _metrics_mod


def _load_display_names() -> dict:
    """从插件 data/ 目录加载子代理显示名映射（不进仓库；缺文件时返回空表）。

    空表时 name_display_map 配置与英文 id 仍可用，中文显示名派生的功能自然降级。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "display_names.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            logger.info("[main] 已加载 display_names.json（%d 项）", len(data))
            return data
        return {}
    except FileNotFoundError:
        logger.info("[main] 数据文件 display_names.json 不存在，使用空表")
        return {}
    except Exception as exc:
        logger.warning("[main] 数据文件 display_names.json 加载失败（用空表继续）: %s", exc)
        return {}



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
    _arbitrate_mod.ArbitrationMixin,
    _metrics_mod.MetricsMixin,
    Star,
):
    """让子代理同时开口、接龙干活、互相来往——主代理执棒，多声部并行。"""

    @filter.on_llm_response()
    async def metrics_on_llm_response(self, event, response):
        """主代理侧的 token 计量。子代理走 tool_loop_agent，不触发这里。"""
        await self.on_llm_response_metrics(event, response)

    # agent_name -> 中文显示名 映射表
    AGENT_DISPLAY_NAME = _load_display_names()
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
        # 2026-09-12 一期：会话落盘。磁盘为真源、内存为热窗口，重启不再失忆。
        # 默认开、可一键关；初始化失败一律回落纯内存，不阻断插件加载。
        _store = None
        if self._cfg("subagent_session_persist", True):
            try:
                _data_dir = str(
                    StarTools.get_data_dir("astrbot_plugin_parallel_handoff")
                )
                _store = _session_store_mod.SessionStore(
                    os.path.join(_data_dir, "subagent_sessions"),
                    retention_days=self._cfg("subagent_session_retention_days", 30),
                )
            except Exception as _store_e:  # noqa: BLE001
                logger.warning(
                    f"[parallel_handoff] 会话落盘初始化失败，回落纯内存: {_store_e}"
                )
                _store = None
        self._ctx_engine = _ctx_engine_mod.ContextEngine(
            enabled=self._cfg("subagent_context_enabled", True),
            max_turns=self._cfg("subagent_context_max_turns", 100),
            store=_store,
        )
        # 二期：后台任务运行器（派单不阻塞主代理；仅在 background=true 时启用）
        try:
            self._task_runner = _task_runner_mod.TaskRunner(
                max_concurrent=self._cfg("subagent_task_max_concurrent", 20),
                max_per_session=self._cfg("subagent_task_max_per_session", 5),
                turn_timeout=self._cfg("subagent_task_turn_timeout", 900),
                on_done=self._on_task_done,
            )
            # 进程刚起来，内存里本不该有残留任务；此调用为防御性清理，
            # 将来若任务也落盘，它就是必需的一步。
            self._task_runner.interrupt_orphans()
        except Exception as _tr_e:  # noqa: BLE001
            logger.warning(f"[parallel_handoff] 后台任务运行器初始化失败: {_tr_e}")
            self._task_runner = None

    async def _on_task_done(self, rec) -> None:
        """后台任务跑完，主动往会话推一条，不用主代理去取。

        - 默认开启，`subagent_task_notify=false` 可关（多任务时怕刷屏）
        - 结果截断 300 字，全文仍用 `task_result` 取
        - 异常一律吞掉：通知失败不该影响任务终态
        """
        try:
            if not self._cfg("subagent_task_notify", True):
                return
            umo = getattr(rec, "session_key", "") or ""
            if not umo or umo == "default":
                return
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            status = getattr(rec, "status", "")
            icon = {
                "done": "✅",
                "failed": "❌",
                "interrupted": "⏱️",
                "stopped": "🚫",
            }.get(status, "•")
            try:
                name = self._display_name(rec.agent)
            except Exception:
                name = rec.agent
            elapsed = round((rec.finished_at or 0) - rec.created_at, 1)

            if status == "done":
                body = (rec.result or "").strip()
                if len(body) > 300:
                    body = body[:300] + f"\n…（共 {len(rec.result)} 字，用 task_result 取全文）"
                text = f"{icon} 【{name}】后台任务完成（{elapsed}s）\n{body}" if body else f"{icon} 【{name}】后台任务完成（{elapsed}s）"
            else:
                text = f"{icon} 【{name}】后台任务未完成（{elapsed}s）：{rec.error or status}"

            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"[parallel_handoff] 后台任务完成通知失败（非致命）: {_e}")

    # ── 事件注册：主代理前缀自动注入（实现见 forward.py ForwardMixin） ──
    @filter.on_decorating_result()
    async def _inject_mainagent_prefix(self, event: AstrMessageEvent):
        """主代理消息发出前自动加【名字】前缀。

        仅当 enable_mainagent_name_prefix=true 且消息不以任何子代理前缀开头时加。
        子代理分段转发期间抑制，避免误加。
        """
        return await super()._inject_mainagent_prefix(event)

    # ── 事件注册：路由强制指令注入（实现见 directive.py DirectiveMixin） ──
    @filter.on_llm_request()
    async def _route_directive_inject(self, event: AstrMessageEvent, req: ProviderRequest):
        """往主代理请求尾部注入路由强制指令（软注入，不拦截）。

        把 route_mode / call_mode / direct_delivery_agents 算出的路径规范追加到
        extra_user_content_parts，主代理照走。系统提示与历史不动，不破坏前缀缓存；
        不碰 func_tool，工具全保留；已含标记则跳过，避免循环重复注入。
        子代理走 llm_generate，不触发本事件。
        """
        return await super()._route_directive_inject(event, req)

    # ── 事件注册：小模型路由层（T1规则/T2小模型/T3兜底，实现见 router.py RouterMixin） ──
    @filter.on_waiting_llm_request()
    async def _smart_router_check(self, event: AstrMessageEvent):
        """小模型路由层：主代理 LLM 调用前的最早停点。

        命中（点名/领域词或小模型高置信）→ 子代理直发并短路主代理，省掉主模型
        推理与记忆召回；未命中原样放行。总开关 enable_smart_router 默认关。
        """
        return await super()._smart_router_check(event)

    # ── 事件注册：忙碌旁路（主代理干活时 T1 点名直达，实现见 router.py RouterMixin） ──
    @filter.custom_filter(_router_mod.BusyRunnerFilter)
    async def _busy_direct_bypass(self, event: AstrMessageEvent):
        """主代理干活时的消息旁路：点名子代理的消息直发，绕过 follow-up 捕获。

        filter 只在该会话有活跃 runner 时通过，且在 follow-up 捕获之前执行；
        命中则直发 + stop_event，未命中放行。
        """
        return await super()._busy_bypass_check(event)

    # ── 热重载（插件入口命令，完整实现保留本模块） ────────────
    @filter.regex(r"^(热重载一下并行子代理调用插件|热重载并行插件|重载插件|reload_parallel)$")
    async def reload_plugin(self, event: AstrMessageEvent):
        """发「热重载并行插件」等短语即可重载本插件，无需 wake_prefix。"""
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
                    await event.send(MessageChain([Plain("✅ parallel_handoff 插件热重载成功")]))
                else:
                    logger.error(f"[parallel_handoff] 热重载失败: {err}")
                    await event.send(MessageChain([Plain(f"❌ 热重载失败: {err}")]))
            except Exception as e:
                logger.error(f"[parallel_handoff] 热重载异常: {e}")

        # [审查修复 2026-09-12] create_task 弱引用语义：不留强引用会被 GC
        # （set + done_callback）。
        if not hasattr(self, "_reload_tasks"):
            self._reload_tasks = set()
        _rt = asyncio.create_task(_delayed_reload())
        self._reload_tasks.add(_rt)
        _rt.add_done_callback(self._reload_tasks.discard)

    # ── 事件注册：动态前缀切换（实现见 config.py ConfigMixin） ──
    @filter.regex(r"(?:关掉|打开)（(.+?)）的前缀|（(.+?)）的前缀(?:关|开)了")
    async def toggle_prefix(self, event: AstrMessageEvent):
        """开关某个子代理的名字前缀。例：「关掉（助手A）的前缀」。"""
        return await super().toggle_prefix(event)

    # ── LLM 工具注册：parallel_handoff（实现见 dispatch.py DispatchMixin） ──
    @llm_tool(name="parallel_handoff")
    async def parallel_handoff(
        self,
        event: AstrMessageEvent,
        calls: list[dict] = None,
        timeout: int = 120,
        message: str = None,
        route_mode: str = None,
        call_mode: str = None,
        mode: str = None,
        background: bool = False,
        speaker: str = None,
    ) -> str:
        """派活给子代理，把它们的回复收回来汇总。

一次可以派多个——要几个角度看同一件事，或者任务能拆开并行，就一次派出去。

Args:
    calls(array[object]): 派单列表，每项含 agent_name（要在 name_display_map 里配过）、
        input（交给它的任务）、order（可选，输出排序）。
        任务按块写：【任务】要解决什么 / 【靶点】定位线索 / 【判断】你当前的判断
        ——待验证的命题，不是事实 / 【依据】判断凭什么 / 【产出】【边界】【完成标准】。
        【判断】一定要写，把假设明说出来她才有东西可反驳。
    timeout(number): 单个子代理的超时秒数，默认 120，超了跳过。
    message(string): 开了消息消歧又不想传 calls 时，直接给原始消息。
    mode(string): 'tech' 技术干活 / 'affection' 日常贴贴，不传按全局配置。
    route_mode(string): 'relay' 先回到我这儿汇总再发 / 'direct' 直发。
    call_mode(string): 'parallel' 并行 / 'chained' 接龙。
    background(boolean): true 时不阻塞，立刻返回 task_id，做完用 task_result 取。
    speaker(string): 这话谁说的，不传默认"主代理"。

派单纪律：带判断，优先派校验而不是实现，同批交同一人，产出默认可疑。
        """
        return await super().parallel_handoff(event, calls, timeout, message, route_mode, call_mode, mode, background, speaker=speaker or "主代理")

    # ── LLM 工具注册：task_status / task_result / task_stop（二期后台任务） ──
    @llm_tool(name="task_status")
    async def task_status(self, event: AstrMessageEvent, task_id: str = None) -> str:
        """查后台任务跑到哪了。不阻塞。

不传 task_id 就列本会话的活跃任务加最近 10 条（含已结束的）。

Args:
    task_id (string): 任务 ID，可不传
        """
        return await super().task_status(event, task_id)

    @llm_tool(name="task_result")
    async def task_result(
        self, event: AstrMessageEvent, task_id: str, timeout: int = 60
    ) -> str:
        """取后台任务的结果。跑完了直接返回，没跑完就等。

Args:
    task_id (string): parallel_handoff 用 background=true 时返回的那个
    timeout (number): 最多等多少秒，默认 60。超时返回当前进度，不报错
        """
        return await super().task_result(event, task_id, timeout)

    @llm_tool(name="task_stop")
    async def task_stop(self, event: AstrMessageEvent, task_id: str) -> str:
        """叫停一个后台任务。

派出去发现没必要了，或者哪个子代理跑偏了。

Args:
    task_id (string): 要停的任务 ID
        """
        return await super().task_stop(event, task_id)

    # ── LLM 工具注册：call_subagent（实现见 dispatch.py DispatchMixin） ──
    @llm_tool(name="call_subagent")
    async def call_subagent(
        self,
        event: AstrMessageEvent,
        agent_name: str,
        input: str,
        speaker: str = None,
    ) -> str:
        """调一个子代理，回复直接分段发到用户——不走我转述。

用户点名要找谁、或者提到某个名字后说正事，用这个。

Args:
    agent_name (string): 子代理名，英文 id 或中文名都认（以 name_display_map 为准）
    input (string): 传给它的完整问题或指令
        """
        return await super().call_subagent(event, agent_name, input, speaker=speaker or "主代理")

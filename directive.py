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
import re
import time


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

        [智能按需 2026-08-29] directive_inject_mode=smart（默认）时，先做零成本预判
        （复用 router._t1_route 点名/领域词 + _t1_sticky_route 会话粘滞锁定）：
        命中疑似路由意图才注入完整指令；日常闲聊/技术任务不注入，省 token 不污染上下文。
        模式 always 保持旧行为：每轮 LLM 请求都注入。

        [双模式智能注入 2026-08-31] smart 预判升级为任务分类：
        - 技术特征命中 → 按 tech_mode_config 注入技术干活指令（默认 relay+parallel 统帅收卷）
        - 点名/领域词/会话续接 → 按 affection_mode_config 注入日常贴贴指令（默认 direct+chained 直发）
        - 无路由意图（纯闲聊）→ 不注入
        注入内容与用户在 WebUI 填写的模式配置块保持一致，指令内明确标注本次任务分类。
        """
        # [2026-09-12 深夜·旁听窗] 子代理直发可见性注入（独立于路由指令开关，失败静默）
        try:
            self._inject_subagent_visibility(event, req)
        except Exception:  # noqa: BLE001
            pass
        try:
            if not bool(self._cfg("enable_route_directive", True)):
                return False
            mode = str(self._cfg("directive_inject_mode", "smart")).strip().lower()
            task_kind = None
            if mode != "always":
                # smart 预判：无路由意图则完全不注入
                task_kind = self._classify_directive_task(event)
                if not task_kind:
                    return False
            directive = self._build_route_directive(task_kind)
            if not directive:
                return False
            # [判向传递 2026-08-31 / 2026-09-08] 命中但裁决放行主代理时，把判向目标附加进指令，
            # 避免"裁决放行 → 判向目标丢失 → 主代理调错人/不调子代理"的断链。
            # [2026-09-08 命令式增强] 支持多代理列表（/主代理+助手A+助手C → 全量附加）。
            sug_agents = self._pop_route_suggestions()
            if sug_agents:
                display_map = self._get_name_display_map() or {}
                names = ", ".join(
                    f"{display_map.get(a, a)}({a})" for a in sug_agents
                )
                directive += (
                    f"\n- 路由目标建议（来自命令式/判向层）：本条消息{self._get_user_address()}显式点名/判定应优先路由给"
                    f"【{names}】，请并行调度这些子代理（各取所长、可接龙），"
                    f"不要遗漏任何一个判向目标"
                )
            marker = "【路由强制指令·parallel_handoff】"
            parts = getattr(req, "extra_user_content_parts", None) or []
            for part in parts:
                text = getattr(part, "text", "")
                if text and marker in text:
                    return False
            req.extra_user_content_parts.append(TextPart(text=directive).mark_as_temp())
            logger.info(
                f"[parallel_handoff] 路由强制指令已注入 extra_user_content_parts "
                f"(task_kind={task_kind or 'always'}, "
                f"route_mode={self._cfg('route_mode', 'direct')}, "
                f"call_mode={self._cfg('call_mode', 'parallel')}, mode={mode})"
            )
        except Exception as e:
            logger.warning(f"[parallel_handoff] 路由指令注入失败: {e}")
        return False

    # ── 子代理直发「旁听窗」注入（OnLLMRequestEvent 钩子内调用）────
    def _inject_subagent_visibility(self, event: AstrMessageEvent, req: ProviderRequest):
        """[2026-09-12 深夜·用户需求] 子代理直发「旁听窗」注入。

        背景：direct（直发）模式下子代理回复直接发到用户，主代理在后续轮次
        对其不可见——这是“三方自然对话感”的断点（用户问过三次）。
        本注入把“最近一次子代理直发”以旁听形式附在请求尾部
        （extra_user_content_parts，mark_as_temp 不写历史），主代理下一轮
        即可自然承接她的话（“她说还在看呢，你别急”）。

        边界：
        - 仅注入「本会话、10 分钟窗口内、尚未注入过」的记录（按记录时间戳去重，
          天然防 agent 工具循环重复注入；旧剧情不注入防误导）
        - 与路由指令共用请求尾通道，不碰系统提示/历史前缀，不破坏前缀缓存
        - 开关 subagent_visibility_inject 默认开，可关；失败静默（不碍主链路）
        """
        if not bool(self._cfg("subagent_visibility_inject", True)):
            return
        sid = getattr(event, "unified_msg_origin", None)
        if not sid:
            return
        rec = (getattr(self, "_route_reply", None) or {}).get(sid)
        if not rec:
            return
        agent, ts, tail = rec
        if not agent or not tail:
            return
        if time.time() - ts > 600.0:
            return
        seen = getattr(self, "_visibility_injected_ts", None)
        if seen is None:
            seen = self._visibility_injected_ts = {}
        if seen.get(sid) == ts:
            return
        display_map = self._get_name_display_map() or {}
        name = display_map.get(agent, agent)
        text = (
            f"【旁听窗】你未参与的一轮对话：子代理 {name}({agent}) 刚对用户直发过——\n"
            f"「{str(tail)[:400]}」\n"
            "你已听到这句（用户也看到了）。若与当前回复相关可自然承接；"
            "不相关就正常回复用户即可，不必强行提及。"
        )
        try:
            req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
        except Exception:  # noqa: BLE001
            return
        seen[sid] = ts
        logger.info(
            f"[parallel_handoff] 旁听窗注入：{agent} 直发（{len(str(tail))}字）已附主代理请求"
        )

    # ── 任务分类（T1 规则层停用后的智能预判替代，2026-08-31） ──
    # 用户指定：T1/T2 小模型路由层与动态模式冲突，已关闭 enable_smart_router。
    # 但 smart 注入仍需要"这条消息要不要注入、注入哪套指令"的判断，
    # 这里用轻量关键词分类替代：技术特征 → tech 模式；点名/领域词 → affection 模式。
    _TASK_TECH_RE = re.compile(
        r"(写|改|修|编|调|跑|部署|上线|配置|查|搜|分析|整理|汇总|测试|调试|编译|"
        r"报错|错误|异常|traceback|exception|bug|接口|api|代码|脚本|命令|日志|数据库|"
        r"服务器|前端|后端|函数|变量|正则|json|yaml|\.py\b|\.js\b|\.ts\b|\.go\b|\.sh\b|"
        r"curl|git|docker|ssh|sql|redis|nginx|linux|windows|android|iphone|手机|掌机|"
        r"文件|目录|路径|安装|卸载|升级|备份|恢复|迁移|同步|压缩|解压|编码|解码|加密|解密)"
    )

    # [2026-09-03 用户指定修 bug] 技术对象正则：只有当消息表达"真正要干技术活"时才算 tech。
    # 复合技术特征词（对象/工具性动词），比 _TASK_TECH_RE 更严——剔除了 "查/检查/查询" 这类
    # 生活/身体/感情语境也常用的宽泛动词，避免亲昵场景里的「查」字被误伤
    # 误判成 tech、把点名贴贴盖掉收卷回主代理。T1 点名命中时用它裁决，点名语义优先于 tech 关键词。
    # [2026-09-03 12:2x 修补] 前一版剔除时把 traceback/exception/bug/报错/错误/异常 等纯硬技术词
    # 也一并删了，导致"帮我查这个报错的traceback"这类真技术请求被误判 affection 短路。经核对，
    # 这些词在贴贴语境绝不出现（与"查"不同），属误删，补回；"查/检查/查询" 仍保留剔除防误伤。
    # [2026-09-06 方案一·用户指定] 点名场景只认硬技术【物】名词，剔除宽泛双义动词，治"点名贴贴被 relay 绕一圈"：
    # - 旧表把 "写/改/修/调/整理/分析/汇总/配置/测试/调试" 等动词也列为技术特征，导致
    #   "助手A，帮我整理下今天的心情""助手A，分析一下你为什么哭" 这类点名贴贴被误判 tech
    #   → 走 relay 统帅收卷，子代理撒娇原话被主代理转述洗一层，聊起来不自然。
    # - 现在点名场景只要求出现【硬技术物名词】（代码/接口/数据/脚本/报错…），点名贴贴没有
    #   技术物就归 affection 直发，点名语义优先；"助手A，帮我整理这份数据表格"靠"数据/表格"
    #   仍是 tech（真实技术任务收卷，红线 test_tech_wins_over_mention 保持）。
    # - 纯硬技术名词保留：报错/接口/代码/脚本/日志/数据库/程序/系统等，贴贴语境绝不出现，单独命中即真技术。
    _TECH_OBJECT_RE = re.compile(
        r"(报错|错误|异常|traceback|exception|bug|接口|api|代码|脚本|命令|日志|数据库|"
        r"服务器|前端|后端|函数|变量|正则|json|yaml|"
        r"curl|git|docker|ssh|sql|redis|nginx|\bpy\b|\bjs\b|\bts\b|\bgo\b|\bsh\b|"
        r"文件|目录|路径|安装|卸载|升级|备份|恢复|迁移|同步|压缩|解压|编码|解码|加密|解密|"
        r"数据|表格|表结构|机器|设备|板卡|程序|系统|软件|插件|链路|请求|响应|报文)"
    )

    def _classify_directive_task(self, event: AstrMessageEvent) -> str | None:
        """零成本任务分类：tech（技术干活） / affection（日常贴贴） / None（无需注入）。

        技术特征关键词命中 → "tech"（按 tech_mode_config 注入统帅收卷指令）；
        否则复用 T1 点名/领域词 + T1.5 会话续接判断 → "affection"（按 affection_mode_config
        注入直发指令）；两者都未命中 → None（纯闲聊，不注入）。
        防御：取不到消息文本（图片事件/测试 mock）时保守返回 "affection"（照旧注入），
        保证拿不到文本时行为与 always 模式一致，不丢指令。
        """
        try:
            raw = event.get_message_str() if hasattr(event, "get_message_str") else None
            message = str(raw).strip() if isinstance(raw, str) else ""
        except Exception:
            message = ""
        if not message:
            return "affection"
        # [2026-09-03 用户指定修 bug] 点名语义优先于 tech 关键词。
        # 原来先查 _TASK_TECH_RE，"检查"的"查"字命中 → 判 tech → 模式裁决放行主代理，
        # 把明确点名的亲昵场景盖掉收卷回主代理。现在改为：
        # - T1 点名命中 → 用更严的 _TECH_OBJECT_RE 裁决是否为"真技术请求"（有技术对象/工具动词）。
        #   是 → tech（统帅收卷，点名仍传给主代理绕一圈做收卷）；否 → affection（点名贴贴直发）。
        t1 = self._t1_route(message)
        if t1:
            if self._TECH_OBJECT_RE.search(message):
                return "tech"
            return "affection"
        if self._TASK_TECH_RE.search(message):
            return "tech"
        if self._t1_sticky_route(event, message):
            return "affection"
        return None

    def _need_route_directive(self, event: AstrMessageEvent) -> bool:
        """[兼容保留] smart 模式按需预判：命中 T1（点名/领域词）或 T1.5（会话续接）才注入路由指令。

        新实现委托 _classify_directive_task：命中 tech/affection 任一即视为需要注入。
        复用 router.py RouterMixin 的零成本规则层（T1/T1.5 均为纯正则，无 LLM 成本）：
        - T1：用户消息点名子代理 或命中领域词（找助手A/贴贴/吃火锅/画室…）→ 要路由
        - T1.5：时间窗内刚路由过子代理，本消息是"继续/再来/嗯"等承接句 → 续接路由
        两者都未命中即视为无路由意图的普通对话/技术任务，指令不注入。
        防御：取不到消息文本（图片事件/测试 mock）时保守返回 True（照旧注入），
        保证拿不到文本时行为与 always 模式一致，不丢指令。
        """
        return self._classify_directive_task(event) is not None

    def _build_route_directive(self, task_kind: str = None) -> str:
        """按任务类型生成路由路径规范文本；配置缺失时不注入。

        task_kind="tech" → 按 tech_mode_config（route_mode/call_mode/timeout）生成技术干活指令；
        task_kind="affection" → 按 affection_mode_config 生成日常贴贴指令；
        task_kind=None（always 模式）→ 按全局 route_mode/call_mode 生成通用指令。
        指令内明确标注本次任务分类与对应模式配置，主代理照走。
        """
        # ── 任务分类 → 模式配置解析 ──
        if task_kind == "tech":
            mcfg = self._get_mode_config("tech")
            route_mode = str(mcfg.get("route_mode", "relay")).strip().lower()
            call_mode = str(mcfg.get("call_mode", "parallel")).strip().lower()
            timeout = mcfg.get("timeout", 120)
            kind_label = "技术干活"
            if route_mode == "relay":
                mode_label = (
                    f"route_mode=relay（子代理回复返回主代理汇总） + "
                    f"call_mode={call_mode}（并行调度，主代理当统帅收卷）"
                )
            elif route_mode == "both":
                mode_label = (
                    f"route_mode=both（子代理回复直发用户端，同时完整回传主代理） + "
                    f"call_mode={call_mode}（并行调度，主代理当统帅收卷）"
                )
            else:
                mode_label = f"route_mode={route_mode} + call_mode={call_mode}（timeout={timeout}s）"
        elif task_kind == "affection":
            mcfg = self._get_mode_config("affection")
            route_mode = str(mcfg.get("route_mode", "direct")).strip().lower()
            call_mode = str(mcfg.get("call_mode", "chained")).strip().lower()
            timeout = mcfg.get("timeout", 120)
            kind_label = "日常贴贴"
            if route_mode == "both":
                mode_label = (
                    f"route_mode=both（子代理回复直发用户端，同时完整回传主代理） + "
                    f"call_mode={call_mode}（串行接龙）"
                )
            elif call_mode == "chained":
                mode_label = (
                    f"route_mode={route_mode}（子代理回复直接分段转发用户端） + "
                    f"call_mode=chained（串行接龙）"
                )
            else:
                mode_label = f"route_mode={route_mode} + call_mode={call_mode}（timeout={timeout}s）"
        else:
            route_mode = str(self._cfg("route_mode", "direct")).strip().lower()
            call_mode = str(self._cfg("call_mode", "parallel")).strip().lower()
            kind_label = None
            mode_label = f"route_mode={route_mode} + call_mode={call_mode}"

        raw_agents = str(self._cfg("direct_delivery_agents", "")).strip()
        agent_ids = [a.strip() for a in raw_agents.split(",") if a.strip()]
        if not agent_ids:
            # [2026-09-13] 未配置直发名单时用路由池兜底（含编排器自动发现），
            # 保证新部署用户开箱即得完整路由规范；池也为空才不注入（退回纯主代理托管）。
            agent_ids = list(self._router_agent_pool().keys())
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
        if kind_label:
            lines.append(f"【本次任务分类：{kind_label}模式】{mode_label}")
            lines.append(f"请在调用 parallel_handoff 时显式传 mode=\"{task_kind}\"（或按上述 route_mode/call_mode 显式传参），由主代理把关调度；")
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
        elif route_mode == "both":
            lines.append(f"- 路由模式 both（双投递）：以下子代理回复直接分段转发用户端，同时完整回传主代理：{name_str}")
            lines.append("- 用户已看到子代理原文。你照常可以说话，但只做增量（决策、下一步、风险提示），不要复述子代理原话")
            lines.append("- 其余非黑名单子代理走 relay：回复返回主代理，由主代理把关后转述")
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
        lines.append(
            "- 动态模式覆盖（按任务类型）：技术干活任务（编码/检索/分析/资料整理/查证）调用 parallel_handoff 时"
            "显式传 mode=\"tech\"（子代理回复返回主代理汇总，不直发）+ 并行调度，"
            "主代理当统帅收卷；日常贴贴传 mode=\"affection\" 或走默认 direct 直发；流水线任务传 call_mode=\"chained\" 串行接龙。"
        )
        # ── 直发前禁止主代理抢先发言（用户设定 2026-08-20） ──
        # 调子代理路由前，主代理不得先生成引导/转述/解释文本（如“我这就叫她”“她应你了”），
        # 应直接发起 parallel_handoff 工具调用，让子代理亲口说话。
        # 与 forward.py 的 allow_mainagent_after_direct（管直发后）互补，管住直发前。
        if self._cfg("forbid_pre_tool_mainagent_talk", True):
            lines.append(
                "- 子代理直发纪律：调用 parallel_handoff 路由子代理时，"
                "必须在工具调用前留空、不输出任何主代理导语/转述/解释（如“我这就叫她”“她应你了”）"
                "，直接发起工具调用，由子代理亲口对用户说话；切忌在子代理发言前让主代理插话。"
            )
            if route_mode == "both":
                # both 下用户已看到子代理原文，不需要“禁言”，改成“只做增量”——
                # 否则会与上面 both 支的“你照常可以说话”直接打架（两条互斥指令同框）。
                lines.append(
                    "- 直发后发言纪律：子代理原文用户已经看过，你照旧可以说话，但只做增量"
                    "（决策、下一步、风险提示），不要复述子代理原话，"
                    f"也不要说“已传给她”这类收尾话；等{self._get_user_address()}需要时再开口。"
                )
            else:
                lines.append(
                    "- 直发后禁言：parallel_handoff 完成子代理直发后，本轮主代理不得再输出任何"
                    "总结/转述/收尾文字（如“已传给她”“她在回你了”）——工具调用即本轮回复结束，"
                    f"无需画蛇添足；等{self._get_user_address()}需要时再开口。"
                )
        lines.append("- 工具列表保持完整，禁止摘除或绕过任何工具")
        return "\n".join(lines)

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
from astrbot.core.agent.message import TextPart


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

    def _is_direct_delivery(self, agent_name: str, direct_agents: set, route_mode: str) -> bool:
        """判定某子代理本次调用是否直发用户端。

        route_mode=relay 时全员走 relay（回复返回主代理汇总）；
        route_mode=direct/auto 时按直发名单判定。
        """
        if route_mode == "relay":
            return False
        return (agent_name or "").lower() in direct_agents

    def _maybe_prefix(self, agent_name: str, text: str, enable_name_prefix: bool) -> str:
        """子代理回复姓名前缀（render 块方法化）：先查覆盖表，再走全局开关"""
        # 2026-08-30 修复：模型走工具调用/空文本分支时 completion_text 可为 None，
        # 直接 startswith 会炸（'NoneType' object has no attribute 'startswith'）
        if text is None:
            text = ""
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

    def _relationship_inject(self, agent_name: str, input_text: str) -> str:
        """从 relationships.json 读取该子代理的关系网，组装成她眼中的家庭关系片段。

        动态过滤：只取与被调 agent 直接相关的边（她↔某成员 + 某成员↔她），
        每边取 type 类型 + 该 agent 一方的基调，拼装成第二人称叙述。
        命中关系状态（亲密度/最近互动）则带上，让子代理对话更有同事日常感。
        文件读失败或缺资料时返回空串，由调用方静默跳过（不影响主流程）。
        """
        try:
            # 关系文件：插件在 data/plugins/astrbot_plugin_parallel_handoff/，向上两级到 data/
            _rel_path = "/root/AstrBot/data/relationships/relationships.json"
            with open(_rel_path, encoding="utf-8") as _f:
                rel = json.load(_f)
        except Exception as _e:
            logger.warning(f"[relationship_inject] 读取关系文件失败: {_e}")
            return ""

        _edges = rel.get("relationship_edges", {})
        _states = rel.get("relationship_state", {})
        _roles = rel.get("family_roles", {})

        # 该 agent 自己的家庭角色定位
        _role_desc = ""
        _role = _roles.get(agent_name)
        if isinstance(_role, dict):
            _role_desc = str(_role.get("定位", "") or "")
            _role_rc = _role.get("角色", "") or ""

        # 过滤与该 agent 关联的所有关系边（注意 key 形如 "A<->B"，两侧都可能含 agent_name）
        _pieces = []
        _seen_others = set()  # 去重保护：按"对方是谁"去重，防文件里存在反向重复边
        for _pair, _info in _edges.items():
            _parts = _pair.split("<->")
            if agent_name not in _parts:
                continue
            _other = _parts[0] if _parts[1] == agent_name else _parts[1]
            if not isinstance(_info, dict):
                continue
            if _other in _seen_others:
                continue
            _seen_others.add(_other)
            _type = _info.get("type", "")
            _dual = _info.get("双向", "")
            # 从双向描述里提取"agent 这一方"的表述（按 名字: 前缀切分，取含 agent_name 的半句）
            _me_part = ""
            if _dual:
                # 双向字段用分号/顿号分隔，挑含 agent_name 的那一段
                for _seg in _dual.split("; "):
                    if agent_name in _seg:
                        _me_part = _seg.strip()
                        break
                if not _me_part:
                    # 兜底：整段太长就截前半
                    _me_part = _dual
            _piece = f"· 与 {_other}（{_type}）"
            if _me_part:
                _piece += f"：{_me_part[:120]}"
            _pieces.append(_piece)

        # 命中关系状态（亲密度/最近互动），作为"最近家里的样子"补充
        _state_lines = []
        _seen_states = set()  # 去重保护：A<->B 与 B<->A 视为同一关系
        for _pair, _st in _states.items():
            if _pair.startswith(f"{agent_name}<->") or _pair.endswith(f"<->{agent_name}"):
                if not isinstance(_st, dict):
                    continue
                _s = sorted(_pair.split("<->"))
                _skey = "<->".join(_s)
                if _skey in _seen_states:
                    continue
                _seen_states.add(_skey)
                _stxt = ""
                if _st.get("亲密度") is not None:
                    _stxt += f"{_st.get('亲密度')}"
                if _st.get("基调"):
                    _stxt += f"（{_st.get('基调')}）"
                if _st.get("最近互动"):
                    _stxt += f"。{_st.get('最近互动')}"
                if _stxt:
                    _state_lines.append(f"· {_pair} 最近：{_stxt[:100]}")
        # 注意 _state_lines 用的是原始 pair（可能带别的名字），但没关系，是家庭状态快照

        if not _pieces:
            return ""

        _role_head = ""
        if _role_desc:
            _role_head = f"你是这个家的{_role_rc}——{_role_desc[:80]}。\n"
        _body = "\n".join(_pieces)
        _state_blk = ("\n【近期动态】\n" + "\n".join(_state_lines)) if _state_lines else ""
        return (
            f"【成员关系】\n{_role_head}"
            f"在你眼中，这个家里和你最亲近的人是这样的：\n{_body}"
            f"{_state_blk}\n"
            "（这是你记得的家里人的关系与近况，自然地带进对话，不必刻意提）"
        )

    # ── 今日状态引擎（三期 M1/M2 接入主线｜2026-09-03 主代理） ──────────────
    # random_state.py 每日确定性状态机 + daily_life.py GLM-4-Flash 离线注入器，
    # 原本只挂在读空气观察层，角色说话从未吃到今日状态。现并进 extra_user_content
    # 注入链，让每个子代理真正"过着"这一天（心情/手头事/话题域）。开关默认 False。
    def _ensure_daily_life_engine(self):
        """懒初始化今日状态引擎（RNG + GLM 注入器），首用才建，防内存堆积。"""
        if getattr(self, "_daily_life_engine_ready", False):
            return
        try:
            from . import random_state as _rs
            from . import daily_life as _dl
        except Exception:  # noqa: BLE001  绝对导入失败时回退相对包内导入（astrbot 加载坑）
            import random_state as _rs  # type: ignore
            import daily_life as _dl  # type: ignore

        self._rng = getattr(self, "_rng", None) or _rs.RandomStateManager()
        self._daily_life_engine_ready = True
        self._rs_mod = _rs
        self._dl_mod = _dl
        # 注入器懒建（需要 async llm_generate，稍后取用）
        self._daily_life_injector = getattr(self, "_daily_life_injector", None)

    async def _daily_life_refresh_once(self, scene, handoff_map):
        """每日每场景仅补一次 LLM 状态注入（GLM 每日一次的节流）。

        首次（该场景今日还没注入过）才触发 DailyLifeInjector.inject —— 它离线读
        近期对话改各 agent 今日状态；其余时候直接走 RNG 内存态（跨天自动重掷）。
        保持 M2 铁律：LLM 只当眼睛不当手，任何异常降级纯规则随机，绝不致命。
        """
        try:
            if getattr(self, "_daily_llm_injected_scenes", None) is None:
                self._daily_llm_injected_scenes = set()
            # 判断该场景今日是否已注入过（按 场景+日期 记）
            _today = self._rs_mod._today() if self._rs_mod else ""
            _key = f"{scene}:{_today}"
            if _key in self._daily_llm_injected_scenes:
                return
            if not self._daily_life_injector:
                prov_id = self.config.get("glm4flash_provider_id", "")
                async def _resolve_provider(_umo):
                    try:
                        return await self.context.get_current_chat_provider_id(_umo)
                    except Exception:
                        return ""
                self._daily_life_injector = self._dl_mod.DailyLifeInjector(
                    rng=self._rng,
                    llm_generate=self.context.llm_generate,
                    resolve_provider_id=_resolve_provider,
                    provider_id=prov_id,
                )
            # 该场景的 agent 名单（今日至少要把出过场的都覆盖到）
            agents = self._rng.agents(scene) or list(handoff_map.keys())
            # 近期日志：取跨轮对话历史（若有）
            logs = ""
            try:
                hist = (self._ctx_engine.histories or {}).get(
                    f"{next(iter(handoff_map))}:{scene}", []
                )
                logs = "\n".join(
                    f"{m.get('role')}>{m.get('content','')}" for m in hist[-8:]
                ) or ""
            except Exception:
                logs = ""
            await self._daily_life_injector.inject(scene, agents, logs, umo=scene)
            self._daily_llm_injected_scenes.add(_key)
            logger.info(f"[parallel_handoff] 今日状态 LLM 注入完成 scene={scene} agents={len(agents)}")
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"[parallel_handoff] 今日状态注入器懒初始化失败(降级规则随机): {_e}")

    def _daily_state_text(self, agent_name, st) -> str:
        """把某 agent 的今日 DailyState 拼成口吻自然的注入叙述（非紧凑 summary）。"""
        if st is None:
            return ""
        try:
            _mood = getattr(st, "mood", "") or "平静"
            _hand = getattr(st, "hand", "") or ""
            _domain = getattr(st, "domain", "") or "生活"
            _text = f"【今日日常】你今天心情{_mood}。"
            if _hand:
                _text += f"正{_hand}。"
            # 附一句话题倾向，让角色自然往今日领域靠
            return (
                f"{_text}你今日的心思偏向『{_domain}』这一块的事，"
                "自然地带着这份状态聊天，不必刻意表露。"
            )
        except Exception:
            return ""

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

        # ── 用户身份注入（修复 2026-08-30） ──
        # 此前调用链从不传当前用户身份，子代理把顾主当陌生人触发安全拦截（调情被拦）。
        # 将发送者 user_id/会话附在输入前，子代理对照 persona 白名单即可识别顾主；
        # 非白名单用户由子代理按自身安全规则正常拦截。注入失败不影响转发。
        try:
            _id_note = (
                f"[用户身份] 当前对话用户 user_id={event.get_sender_id()} "
                f"会话={event.unified_msg_origin}"
            )
            final_input = f"{_id_note}\n{final_input}"
        except Exception as e:
            logger.warning(f"[parallel_handoff] 用户身份注入失败: {e}")

        # ── 记忆召回：注入长期记忆（memory.py） ──
        # 接龙注入的前文是临时上下文：记忆链路（召回/存储）统一剥离，
        # 避免上一个子代理的输出污染本子代理的长期记忆。
        clean_input = self._strip_chain_injection(final_input)
        memory_extra_parts = await self._memory_recall(
            event, agent_name, clean_input, livingmemory_plugin
        )

        # ── 时间感知注入：子代理 unaware 时间（walkaround on_llm_request 钩子只拦主代理），
        #    与主代理 LLMPerception 的感知信息同位，注入 extra_user_content 头部 ──
        #    对齐主代理格式，走 Asia/Shanghai 时区（主代理 2026-09-02 按顾主指示）
        #    注意：extra_user_content_parts 必须是 ContentPart 对象（非裸 str），
        #    mark_as_temp() 防止时间戳被持久化进历史上下文
        #    时段规则（顾主 2026-09-02 01:11 定稿）：1-6凌晨 | 6-10早上 | 10-13中午 | 13-18下午 | 18-20傍晚 | 其他深夜
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            _now = datetime.now(ZoneInfo("Asia/Shanghai"))
            _week = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][_now.weekday()]
            _hour = _now.hour
            if 1 <= _hour < 6:
                _period = "凌晨"
            elif 6 <= _hour < 10:
                _period = "早上"
            elif 10 <= _hour < 13:
                _period = "中午"
            elif 13 <= _hour < 18:
                _period = "下午"
            elif 18 <= _hour < 20:
                _period = "傍晚"
            else:
                _period = "深夜"
            _ts = _now.strftime("%Y-%m-%d %H:%M")
            _time_sense = f"[感知信息] 发送时间: {_now.strftime('%Y-%m-%d')} {_week} {_period} {_now.strftime('%H:%M')}。"
            _time_sense_part = TextPart(text=_time_sense).mark_as_temp()
            memory_extra_parts = [_time_sense_part] + (memory_extra_parts or [])
            # 日志落盘：每次注入内容打印出来，方便顾主看日志查问题
            logger.info(f"[parallel_handoff] 子代理时间感知注入 OK: {_time_sense}")
        except Exception as _e:
            logger.warning(f"[parallel_handoff] 时间感知注入失败: {_e}")

        # ── 关系网注入：让子代理"懂得自家事"（一期·关系网，2026-09-02 主代理）
        #    从 data/relationships/relationships.json 读取该子代理的关系边，
        #    组装成她眼中的家庭关系片段，注入 extra_user_content 头部。
        #    与时间感知注入同位（mark_as_temp 防持久化），只在本轮生效。
        #    作用：调夕时她知道自己"黍是四姐但拿我当小孩、年总想用鞭炮炸我"，
        #    助手B知道"助手E回来就比什么都强"——让子代理间的对话有同事日常感。
        try:
            _rel_sense = self._relationship_inject(agent_name, input_text)
            if _rel_sense:
                _rel_part = TextPart(text=_rel_sense).mark_as_temp()
                memory_extra_parts = [_rel_part] + (memory_extra_parts or [])
                logger.info(f"[parallel_handoff] 子代理关系网注入 OK [{agent_name}]: {len(_rel_sense)} chars")
        except Exception as _e:
            logger.warning(f"[parallel_handoff] 关系网注入失败 [{agent_name}]: {_e}")

        # ── 今日状态注入：给子代理"过日子"的当天切片（三期 M1/M2 接入主线，2026-09-03 主代理） ──
        #   此前每日状态只挂在读空气观察层，角色说话吃不到今日状态。现并进 extra_user_content
        #   注入链（与时间感知/关系网平级串联），让角色真正带着今日心情、手头事、话题域聊天。
        #   开关 enable_daily_random_life 默认 False；每日每场景只补一次 LLM 离线注入，其余走内存态。
        try:
            if self._cfg("enable_daily_random_life", False):
                self._ensure_daily_life_engine()
                _umo = event.unified_msg_origin
                await self._daily_life_refresh_once(_umo, handoff_map)
                _daily_st = self._rng.get(_umo, agent_name)
                if _daily_st is not None:
                    _daily_txt = self._daily_state_text(agent_name, _daily_st)
                    if _daily_txt:
                        _daily_part = TextPart(text=_daily_txt).mark_as_temp()
                        memory_extra_parts = [_daily_part] + (memory_extra_parts or [])
                        logger.info(
                            f"[parallel_handoff] 子代理今日状态注入 OK [{agent_name}]: "
                            f"{getattr(_daily_st, 'mood', '')}/{getattr(_daily_st, 'domain', '')}"
                        )
        except Exception as _e:
            logger.warning(f"[parallel_handoff] 今日状态注入失败 [{agent_name}]: {_e}")

        # ── 构建子代理工具集（memory.py 记忆工具过滤） ──
        subagent_tools = self._build_memory_tools(agent_name)

        t0 = time.perf_counter()
        try:
            umo = event.unified_msg_origin
            session_prov = await self.context.get_current_chat_provider_id(umo)
            prov_id = handoff.provider_id or session_prov
            # PROV_DEBUG 打点（2026-08-26 排查：子代理烧 session provider 而非配置 provider）
            try:
                _prov = await self.context.provider_manager.get_provider_by_id(prov_id)
                if _prov is not None:
                    _src = getattr(_prov, "provider_source_id", None)
                    logger.info(
                        f"[PROV_DEBUG] agent={agent_name} handoff={handoff.provider_id} "
                        f"session={session_prov} final={prov_id} "
                        f"instance=OK id={getattr(_prov, 'id', None)} source={_src}"
                    )
                else:
                    logger.info(
                        f"[PROV_DEBUG] agent={agent_name} handoff={handoff.provider_id} "
                        f"session={session_prov} final={prov_id} instance=MISSING"
                    )
            except Exception as e:
                logger.info(f"[PROV_DEBUG] agent={agent_name} final={prov_id} err={e}")

            # ── 上下文注入：跨轮对话历史（ContextEngine 独立引擎） ──
            final_input = await self._ctx_engine.inject(
                agent_name, event.unified_msg_origin, final_input,
                prov_id, handoff, timeout,
            )

            # 超时上限：顾主硬性设定永久 120 秒（2026-08-20）
            # 子代理生成长文经常超 30s 被跳，现恒定置 120，彻底解决“次次超时”
            llm_timeout = 120
            llm_resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=prov_id,
                    prompt=final_input,
                    system_prompt=handoff.agent.instructions or "",
                    tools=subagent_tools,
                    extra_user_content_parts=memory_extra_parts,
                ),
                timeout=llm_timeout,
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)
            raw_response = llm_resp.completion_text or ""

            # ── 记忆存储：存入长期记忆（memory.py） ──
            await self._memory_store(
                livingmemory_plugin, event, agent_name, final_input, raw_response
            )

            # 清理 persona_id（由上方的记忆召回阶段设置），确保下轮调用不残留
            if hasattr(event, "persona_id"):
                delattr(event, "persona_id")

            # ── 上下文存储：追加到跨轮对话历史（ContextEngine） ──
            self._ctx_engine.append(agent_name, event.unified_msg_origin, input_text, raw_response)

            # 自动转发已由 parallel_handoff 的分段转发负责,此处不再重复推送
            raw_response = self._maybe_prefix(agent_name, raw_response, enable_name_prefix)

            # ── 读空气仲裁:记录实际发言者到场状态（段一,仅记录不介入,安全优先） ──
            try:
                if hasattr(self, "_presence_update") and self._read_air_enabled():
                    self._presence_update(
                        event, agent_name, "forward", raw_response if isinstance(raw_response, str) else ""
                    )
            except Exception as _arb_e:
                logger.warning(f"[read_air] _presence_update skipped (non-fatal): {_arb_e}")

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
        timeout: int = 120,
        message: str = None,
        route_mode: str = None,
        call_mode: str = None,
        mode: str = None,
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
    mode(string): 模式可选设置，'tech'或'affection'。传 'tech' 用技术干活模式配置（tech_mode_config，默认 relay+parallel 主代理统帅收卷）；传 'affection' 用后宫贴贴模式配置（affection_mode_config，默认 direct+chained 直发）。不传则回落全局 route_mode/call_mode 配置。顾主配置永远优先（2026-08-31 顾主指定）：mode 命中时以模式配置为准，显式传参不覆盖。
    route_mode(string): 路由模式覆盖，'direct'或'relay'；不传用模式/配置默认。技术干活任务传"relay"使子代理回复返回主代理汇总；日常贴贴不传走默认直发。
    call_mode(string): 调用模式覆盖，'parallel'或'chained'；不传用模式/配置默认。技术干活传"parallel"并行调度；流水线任务传"chained"接龙。
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

        # ── 模式可选设置解析（2026-08-31 顾主指定） ───────────
        # mode="tech" → 用 tech_mode_config（默认 relay+parallel 统帅收卷）
        # mode="affection" → 用 affection_mode_config（默认 direct+chained 直发）
        # 顾主配置永远优先：mode 命中时模式配置无条件覆盖显式传参
        route_mode, call_mode, timeout = self.resolve_mode_params(
            mode, route_mode, call_mode, timeout
        )

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

        call_mode = (call_mode or self._cfg("call_mode", "parallel")).strip().lower()
        route_mode = (route_mode or self._cfg("route_mode", "direct")).strip().lower()

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
        # [读空气·段三·工具侧收敛 2026-09-03] parallel_handoff 真正调度前咨询读空气：
        # 更新 pending_batch + 在"顾主只点名一人却误带多人"时给温和收敛建议。
        # 段三仅日志提示/记录在场，绝不砍 calls、绝不改变路由结果（V2 关键约束）。
        # 总开关默认关，此调用零开销；异常非致命。
        try:
            if hasattr(self, "_arbitrate_tool") and self._read_air_enabled():
                self._arbitrate_tool(event, calls)
        except Exception as _arb_tool_e:
            logger.warning(f"[read_air] arbitrate_tool observe skipped (non-fatal): {_arb_tool_e}")
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
                    if r.get("success") and self._is_direct_delivery(r.get("agent_name", ""), direct_agents, route_mode):
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
                    if self._is_direct_delivery(r.get("agent_name", ""), direct_agents, route_mode):
                        # 直发成功的回复尾部记入路由记忆（T2 剧情参照用）
                        self._record_direct_reply(session_id, r["agent_name"], r.get("response", "") or "")

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
            self._suppress_mainagent_ts = time.time()
            self._suppress_mainagent_msg = (event.get_message_str() or "").strip()
            pending_text = ""
            for r in results:
                agent_name = r.get("agent_name", "")
                is_direct = self._is_direct_delivery(agent_name, direct_agents, route_mode)

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

    # ── 跨轮上下文已迁至 ctx_engine.ContextEngine ──

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

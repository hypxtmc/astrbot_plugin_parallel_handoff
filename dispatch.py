"""dispatch.py — parallel_handoff 核心调度（P0 拆模块）

对应原 main.py 的 684-1307 行区域（去重守卫 + parallel_handoff 主流程）
+ 1309-1402 行（跨轮上下文辅助方法）+ 1405-1424 行（call_subagent 实现）。

改造点（纯搬移，行为零变更）：
- 场景注入 → scene.py 的 _build_scene_prefix
- livingmemory 查找/召回/存储/工具过滤 → memory.py 的对应方法
- 分段转发/失败通知/姓名前缀 → forward.py 的对应方法
装饰器 @llm_tool 保留在 main.py 壳方法上（保证 handler_module_path 匹配插件主模块）。
"""
import asyncio
import hashlib
import json
import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.message import TextPart

# 后台任务的终态集合（与 task_runner.TERMINAL 保持一致）。
# 本地定义而非 import，避免与 task_runner 产生 import 耦合——
# 两边真跑不到一起时，这里也不会在加载阶段就报错。
_TERMINAL_STATES = {"done", "failed", "stopped", "interrupted"}


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
            # [审查修复 2026-09-12] 原为硬编码部署根绝对路径（非本机部署必失效）；
            # 改为相对解析：插件目录向上三级到 AstrBot 根，再进 data/relationships
            # （与 side_pulse._pulse_affinity_path 同款，支持 _relationship_root 覆盖）。
            _root = getattr(self, "_relationship_root", None)
            _rel_path = os.path.join(
                _root
                or os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "..", "..", "..", "data", "relationships",
                ),
                "relationships.json",
            )
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

        # [2026-09-13 修复] 关系文件 key 是中文显示名（如 "成员甲<->成员乙"），
        # 而真实调用方传入的是英文 id——原实现拿 id 直接匹配中文 key，
        # 永远匹配不上 → _pieces 恒空 → 静默返回空串（9-02 上线以来从未真正注入成功）。
        # 修复：先把 agent_name 归一成中文显示名（英文 id → 中文；已是中文则原样），再匹配。
        _display_map = getattr(type(self), "AGENT_DISPLAY_NAME", None) or {}
        _cn_me = _display_map.get(agent_name) or agent_name
        # 过滤与该 agent 关联的所有关系边（注意 key 形如 "A<->B"，两侧都可能含 _cn_me）
        _pieces = []
        _seen_others = set()  # 去重保护：按"对方是谁"去重，防文件里存在反向重复边
        for _pair, _info in _edges.items():
            _parts = _pair.split("<->")
            if _cn_me not in _parts:
                continue
            _other = _parts[0] if _parts[1] == _cn_me else _parts[1]
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
                # 双向字段用分号分隔（"某人: …; 另一人: …"），取"我这一方"的表述：
                # 优先精确匹配「我的名字:」前缀；找不到再退回"含我名"的弱匹配。
                for _seg in _dual.split("; "):
                    _s = _seg.strip()
                    if _s.startswith(f"{_cn_me}:") or _s.startswith(f"{_cn_me}："):
                        _me_part = _s
                        break
                if not _me_part:
                    for _seg in _dual.split("; "):
                        if _cn_me in _seg:
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
        # 排序确定化（缓存友好）：亲密度降序、同分按 key 字典序——
        # 同一份关系文件下输出逐字节稳定，适合放进 system 稳定层命中前缀缓存。
        def _state_sort_key(_kv):
            try:
                _v = int(_kv[1].get("亲密度") or 0) if isinstance(_kv[1], dict) else 0
            except Exception:
                _v = 0
            return (-_v, _kv[0])

        for _pair, _st in sorted(_states.items(), key=_state_sort_key):
            if _pair.startswith(f"{_cn_me}<->") or _pair.endswith(f"<->{_cn_me}"):
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
                prov_id = (
                    self.config.get("daily_life_provider_id")
                    or self.config.get("glm4flash_provider_id", "")
                )
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

    # ── 二期：后台任务工具实现（壳方法在 main.py） ──────────────
    def _runner_or_none(self):
        return getattr(self, "_task_runner", None)

    async def task_status(self, event, task_id=None) -> str:
        """查任务状态。不传 task_id 则列出本会话的活跃任务。"""
        runner = self._runner_or_none()
        if runner is None:
            return json.dumps({"error": "后台任务未启用"}, ensure_ascii=False)
        if task_id:
            rec = runner.get(task_id)
            if rec is None:
                return json.dumps(
                    {"error": f"任务不存在或已过期: {task_id}"}, ensure_ascii=False
                )
            return json.dumps(rec.to_dict(), ensure_ascii=False)
        session_key = getattr(event, "unified_msg_origin", "") or "default"
        return json.dumps(
            {"active": runner.list_active(session_key), "stats": runner.stats()},
            ensure_ascii=False,
        )

    async def task_result(self, event, task_id, timeout: int = 60) -> str:
        """取任务结果。超时不报错，返回当前进度。"""
        runner = self._runner_or_none()
        if runner is None:
            return json.dumps({"error": "后台任务未启用"}, ensure_ascii=False)
        rec = runner.get(task_id)
        if rec is None:
            return json.dumps(
                {"error": f"任务不存在或已过期: {task_id}"}, ensure_ascii=False
            )
        rec = await runner.wait(task_id, timeout=timeout)
        if rec.status in _TERMINAL_STATES:
            # 子代理回复本身是 JSON 文本，解出来嵌进去，避免二次转义难读
            try:
                payload = json.loads(rec.result)
            except (json.JSONDecodeError, TypeError):
                payload = rec.result
            return json.dumps(
                {
                    "task_id": task_id,
                    "agent": rec.agent,
                    "status": rec.status,
                    "elapsed": round((rec.finished_at or 0) - rec.created_at, 1),
                    "result": payload,
                    **({"error": rec.error} if rec.error else {}),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "task_id": task_id,
                "agent": rec.agent,
                "status": rec.status,
                "elapsed": round(time.time() - rec.created_at, 1),
                "hint": "还没跑完，稍后再用 task_result 取；或先用 task_status 看进度",
            },
            ensure_ascii=False,
        )

    async def task_stop(self, event, task_id) -> str:
        """取消任务。"""
        runner = self._runner_or_none()
        if runner is None:
            return json.dumps({"error": "后台任务未启用"}, ensure_ascii=False)
        ok = runner.stop(task_id)
        return json.dumps(
            {
                "task_id": task_id,
                "stopped": ok,
                **({} if ok else {"hint": "任务不存在、已结束或已过期"}),
            },
            ensure_ascii=False,
        )

    async def _run_one_as_text(self, c, **kwargs) -> str:
        """后台任务入口：把 `_call_one` 的结构化结果转成 JSON 文本。

        `TaskRunner` 的 factory 约定返回 str，而 `_call_one` 返回 dict——
        这里做一层适配，免得 task_runner 去猜类型。异常也转成 JSON，
        这样失败原因能原样传到 `task_result`，而不是变成一个空洞的 traceback。
        """
        try:
            r = await self._call_one(c, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {
                    "success": False,
                    "agent_name": (c or {}).get("agent_name"),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )
        if isinstance(r, dict):
            return json.dumps(r, ensure_ascii=False)
        return json.dumps({"success": True, "response": str(r)}, ensure_ascii=False)

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
        # 中文名/大小写兼容：中文名 -> 英文 id，大写 -> 小写（写回 call，后续统一用英文 id）
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
        # 必须由主代理直接调用 transfer_to_xxx 直连（用户 2026-08-08 硬性指令）
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

        # 场景前缀消费点：由 _apply_scene_prefix 决定
        final_input = self._apply_scene_prefix(input_text, scene_prefix)

        # ── 用户身份注入（修复 2026-08-30） ──
        # 此前调用链从不传当前用户身份，子代理把用户当陌生人触发安全拦截（调情被拦）。
        # 将发送者 user_id/会话附在输入前，子代理对照 persona 白名单即可识别用户；
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
        #    对齐主代理格式，走 Asia/Shanghai 时区（主代理 2026-09-02 按用户指示）
        #    注意：extra_user_content_parts 必须是 ContentPart 对象（非裸 str），
        #    mark_as_temp() 防止时间戳被持久化进历史上下文
        #    时段规则（用户 2026-09-02 01:11 定稿）：1-6凌晨 | 6-10早上 | 10-13中午 | 13-18下午 | 18-20傍晚 | 其他深夜
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
            # 日志落盘：每次注入内容打印出来，方便用户看日志查问题
            logger.info(f"[parallel_handoff] 子代理时间感知注入 OK: {_time_sense}")
        except Exception as _e:
            logger.warning(f"[parallel_handoff] 时间感知注入失败: {_e}")

        # ── 预取线索注入（2026-09-15 用户驱动）──
        #    依据：实测子代理单次派单每步约 7.4 秒，≥3 步的派单里 93% 是
        #    「搜一次→看一眼→再搜一次」的串行搜索。这里趁派单的功夫，用本地
        #    ripgrep 把任务文本里已点名的实体先定位一遍（实测 6 个候选 232ms），
        #    把「文件:行」锚点并进 extra —— 她开局就能直接精读，省掉 1~2 步盲搜。
        #    纯本地、不调 LLM、不砍任何原有注入；失败静默跳过，绝不阻断派单。
        #    位置固定在时间感知之后（保持 extra 头部稳定）。
        try:
            if self._cfg("subagent_prefetch_enabled", True):
                _pf_parts = await self._prefetch_clues(final_input)
                if _pf_parts:
                    memory_extra_parts = (
                        list(memory_extra_parts[:1])
                        + _pf_parts
                        + list(memory_extra_parts[1:])
                    )
                    logger.info(
                        f"[parallel_handoff] 预取线索注入 OK: "
                        f"{len(getattr(_pf_parts[0], 'text', '') or '')} 字符"
                    )
        except Exception as _e:
            logger.warning(f"[parallel_handoff] 预取线索注入失败: {_e}")

        # ── 关系网注入：已迁移至 system 稳定层（2026-09-13）
        #    原实现在此走 extra_user_content（mark_as_temp、每轮重复注入）：
        #    (a) 匹配 bug——拿英文 id 去匹配中文 key，恒返回空串（上线以来从未生效）；
        #    (b) 每轮注入落在增量区，不享受前缀缓存。
        #    现由 _subagent_system_prompt 统一携带（见其 docstring），
        #    此处不再重复注入，避免双份 + 保持缓存友好。

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

        # ── 构建子代理工具集（memory.py 记忆工具 + 只读白名单） ──
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
            # 2026-09-11 改造（用户拍板）：历史走结构化 contexts，不再拼成一条
            # 纯文本 user 消息。原因是前缀缓存——拼成单条消息时它每轮都长得不一样，
            # 整条按新内容计费（实测子代理命中率中位 48.9%，主代理 98.1%）。
            # 改成 contexts 后历史占据前缀，每轮只在末尾追加新输入，逐轮累积复用。
            _ctx_contexts = []
            final_input, _ctx_contexts = await self._ctx_engine.inject(
                agent_name, event.unified_msg_origin, final_input,
            )

            # 超时上限：读配置 subagent_reply_timeout（默认 120）
            # 2026-09-12 改造（用户拍板）：原硬编码 120 改为可配置，
            # 用户在 WebUI 填的等待上限直接生效，不再被命令强锁。
            llm_timeout = int(self._cfg("subagent_reply_timeout", 120) or 120)
            # [工具循环 2026-09-11 用户定] 子代理改走 tool_loop_agent：
            # llm_generate 是一次性调用、不执行 tool_call（官方 docstring 明示），
            # 子代理伸手抓工具永远抓空 → 空回复 → 降级无工具重试，工具形同虚设。
            # tool_loop_agent 不设 ProviderRequest.extra_user_content_parts，
            # 故把旁轨记忆/状态注入文本直接并进 prompt。
            _extra_text = "\n".join(
                (getattr(p, "text", "") or "").strip()
                for p in (memory_extra_parts or [])
            ).strip()
            prompt_with_extra = (
                f"{final_input}\n\n{_extra_text}" if _extra_text else final_input
            )
            # [PROF-PROMPT 2026-09-12] 请求体分解打点（一次性实验，完事即撤）
            try:
                _sys_p = self._subagent_system_prompt(handoff, agent_name)
                _ctx_chars = sum(len(m.content or "") for m in (_ctx_contexts or []))
                _tl = getattr(subagent_tools, "tools", None) or subagent_tools or []
                _tools_chars = 0
                for _t in _tl:
                    _tools_chars += len(str(getattr(_t, "name", "") or "")) + len(
                        str(getattr(_t, "description", "") or "")
                    )
                    _p = getattr(_t, "parameters", None)
                    if _p:
                        try:
                            _tools_chars += len(
                                json.dumps(_p, ensure_ascii=False, default=str)
                            )
                        except Exception:
                            pass
                logger.info(
                    f"[PROF-PROMPT] agent={agent_name} sys={len(_sys_p)} "
                    f"ctx={_ctx_chars}({len(_ctx_contexts or [])}条) "
                    f"tools={len(_tl)}({_tools_chars}) "
                    f"extra={len(_extra_text)} input={len(final_input)} "
                    f"prompt={len(prompt_with_extra)}"
                )
            except Exception as _pe:  # 打点绝不拖垮主链
                logger.warning(f"[PROF-PROMPT] 打点失败: {_pe}")
                _sys_p = self._subagent_system_prompt(handoff, agent_name)
            _m_t0_sub = time.monotonic()
            llm_resp = await asyncio.wait_for(
                self.context.tool_loop_agent(
                    event=event,
                    chat_provider_id=prov_id,
                    prompt=prompt_with_extra,
                    contexts=_ctx_contexts or None,
                    system_prompt=_sys_p,
                    tools=subagent_tools,
                    max_steps=self._cfg("subagent_max_steps", 10),
                    tool_call_timeout=self._cfg("subagent_tool_call_timeout", 45),
                ),
                timeout=llm_timeout,
            )
            # token 计量（2026-09-11 C 步 / 2026-09-14 复活）：usage 取循环末轮值
            self._metrics_record(
                "sub",
                agent=agent_name,
                usage=getattr(llm_resp, "usage", None),
                latency_ms=int((time.monotonic() - _m_t0_sub) * 1000),
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)
            raw_response = llm_resp.completion_text or ""

            # ── 空回复诊断+兜底（2026-09-05） ──
            # 子代理带 recall/memorize 记忆工具，模型被问「今天聊了什么」常走
            # 工具调用分支 → completion_text=None，而 llm_generate 是一次性调用
            # 无工具循环，tool_call 无人执行无人续跑 → 空回复。
            # 兜底：诊断日志打出 tools_call_name；降级为无工具重试一次
            # （旁轨家常/记忆注入已在 extra_user_content_parts 里，模型可直接作答）。
            if not raw_response.strip():
                _tcn = list(getattr(llm_resp, "tools_call_name", []) or [])
                _rc = (getattr(llm_resp, "reasoning_content", None) or "").strip()
                logger.warning(
                    f"[parallel_handoff] 空回复诊断 [{agent_name}]: "
                    f"tool_calls={_tcn if _tcn else '无'} "
                    f"reasoning={'有(' + str(len(_rc)) + '字)' if _rc else '无'}"
                )
                try:
                    retry_resp = await asyncio.wait_for(
                        self.context.llm_generate(
                            chat_provider_id=prov_id,
                            prompt=final_input,
                            system_prompt=handoff.agent.instructions or "",
                            extra_user_content_parts=memory_extra_parts,
                            # 不带 tools：避免再次触发工具调用分支
                        ),
                        timeout=llm_timeout,
                    )
                    raw_response = retry_resp.completion_text or ""
                    if raw_response.strip():
                        logger.info(
                            f"[parallel_handoff] 空回复无工具重试成功 [{agent_name}]"
                        )
                    else:
                        _rc2 = (getattr(retry_resp, "reasoning_content", None) or "").strip()
                        logger.warning(
                            f"[parallel_handoff] 空回复重试仍空 [{agent_name}] "
                            f"reasoning={'有(' + str(len(_rc2)) + '字)' if _rc2 else '无'}"
                        )
                except Exception as retry_e:
                    logger.warning(
                        f"[parallel_handoff] 空回复重试失败 [{agent_name}]: {retry_e}"
                    )

            # ── 空回复兑底失败 → 短路返回失败（2026-09-11 审查发现修复） ──
            # 原逻辑：重试仍空也照样存记忆、追加上下文、返回 success=True，导致
            #   ①空串进长期记忆 = 垃圾记录，下次召回可能当有效内容命中
            #   ②空白回合占跨轮注入位，污染下一位子代理
            #   ③调用方按 success=True 分段转发，用户端看到一条空消息
            if not raw_response.strip():
                for _attr in ("persona_id", "_subagent_persona"):
                    if hasattr(event, _attr):
                        delattr(event, _attr)
                latency_ms = int((time.perf_counter() - t0) * 1000)
                logger.warning(
                    f"[parallel_handoff] 空回复兑底失败，已跳过记忆与上下文写入 [{agent_name}]"
                )
                return {
                    "agent_name": agent_name,
                    "success": False,
                    "response": "",
                    "latency_ms": latency_ms,
                    "order": order,
                }

            # ── 记忆存储：存入长期记忆（memory.py） ──
            await self._memory_store(
                livingmemory_plugin, event, agent_name, final_input, raw_response
            )

            # 清理 persona_id / _subagent_persona（记忆隔离标记），确保下轮调用不残留
            for _attr in ("persona_id", "_subagent_persona"):
                if hasattr(event, _attr):
                    delattr(event, _attr)

            # ── 上下文存储：追加到跨轮对话历史（ContextEngine） ──
            # 2026-09-15：落历史存 final_input 而不是 input_text —— 发送给 provider 的是
            # final_input（含 scene_prefix / [用户身份]），重放时若存的是裸 input_text，
            # 那条消息从第一个字符起就对不上，前缀缓存的断点被提前一个任务卡身位。
            # 与 767 行记忆存储（同样用 final_input）口径一致。
            self._ctx_engine.append(agent_name, event.unified_msg_origin, final_input, raw_response)

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
                f"[parallel_handoff] Subagent '{agent_name}' timed out after {llm_timeout}s"
            )
            for _attr in ("persona_id", "_subagent_persona"):
                if hasattr(event, _attr):
                    delattr(event, _attr)
            err_text = f"Timeout after {llm_timeout}s"
            err_text = self._maybe_prefix(agent_name, err_text, enable_name_prefix)
            return {
                "agent_name": agent_name,
                "success": False,
                "response": err_text,
                "latency_ms": latency_ms,
                "order": order,
            }
        except asyncio.CancelledError:
            # 取消路径不落 except Exception（CancelledError 自 Py3.8 起是 BaseException），
            # 必须自己清理 persona 标记，否则残留会污染下轮调用（2026-09-11 审查发现）
            for _attr in ("persona_id", "_subagent_persona"):
                if hasattr(event, _attr):
                    delattr(event, _attr)
            logger.info(
                f"[parallel_handoff] Subagent '{agent_name}' 被取消，已清理 persona 标记"
            )
            raise
        except Exception as e:
            for _attr in ("persona_id", "_subagent_persona"):
                if hasattr(event, _attr):
                    delattr(event, _attr)
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
    # ── 接龙长回复精简（2026-09-10 补实现） ──
    async def _summarize_chain_reply(
        self, display_name: str, text: str, provider_id: str, timeout: float = 120
    ) -> str:
        """把接龙中上一位的长回复压成「开头 + 中段摘要 + 结尾」。

        与 _conf_schema 的 chain_summary_* 四项配置配套：中段交模型摘要，首尾各
        保留 chain_summary_keep_head_tail 字符保剧情连贯（语气、动作、话头都在
        尾部，砍掉下一位接不住）。两级降级：模型不可用/超时 → 纯首尾截断；
        文本本身够短 → 原样返回。任何情况都返回可用文本，不向调用方抛异常
        （旧版只有调用点没有实现，接龙每轮打一条 WARN 后原样注入，开关形同虚设）。
        """
        try:
            keep = int(self._cfg("chain_summary_keep_head_tail", 120) or 120)
        except Exception:
            keep = 120
        keep = max(0, keep)
        body = str(text or "")
        # 首尾保留之后剩不下多少中段 → 压缩收益为负，原样返回
        if keep * 2 >= len(body) - 40:
            return body

        head, mid, tail = body[:keep], body[keep:-keep], body[-keep:]
        summary = ""
        if provider_id and mid.strip():
            try:
                resp = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=(
                            f"压缩下面这段【{display_name}】的发言，不超过"
                            f"{max(80, len(mid) // 4)}字，只保留剧情走向、情绪和"
                            f"对方必须接住的信息。不要评价，不要扩写，直接输出梗概：\n\n{mid}"
                        ),
                        system_prompt=(
                            "你是剧情压缩器。只做无损要点提炼，"
                            "不添加原文没有的信息，不输出任何解释。"
                        ),
                    ),
                    timeout=min(float(timeout or 60), 60),
                )
                summary = (getattr(resp, "completion_text", "") or "").strip()
            except Exception as e:
                logger.debug(
                    f"[parallel_handoff] 接龙摘要调用失败，降级首尾截断: {e}"
                )
        if summary:
            return f"{head}\n……（中段 {len(mid)} 字，压缩为）{summary}……\n{tail}"
        return f"{head}\n……（中段 {len(mid)} 字略）……\n{tail}"

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
    ) -> str:
        """并行调用多个子代理（如 agent_a、agent_b、agent_c 等）,
同时获取它们的回复并汇总。

使用场景：当需要多个子代理从不同角度回答同一个问题时使用此工具。
例如同时询问 agent_a 和 agent_b 对某件事的看法。

Args:
    calls(array[object]): 子代理调用列表。每个元素必须包含：
        - agent_name(string): 子代理名称,可选值: 助手A, 助手B 等（需在 name_display_map 中配置）
        - input(string): 传给该子代理的问题/指令
        - order(integer, 可选): 输出时的排序序号,越小越靠前
    timeout(number): 单个子代理的超时秒数,默认120秒（用户设定，永久生效）。超过此时间未返回则跳过该子代理。
    message(string): 当开启消息消歧且不传calls时,传入原始消息文本,工具会自动路由到最近对话的子代理。
    mode(string): 模式可选设置，'tech'或'affection'。传 'tech' 用技术干活模式配置（tech_mode_config，默认 relay+parallel 主代理统帅收卷）；传 'affection' 用贴贴模式配置（affection_mode_config，默认 direct+chained 直发）。不传则回落全局 route_mode/call_mode 配置。用户配置永远优先（2026-08-31 用户指定）：mode 命中时以模式配置为准，显式传参不覆盖。
    route_mode(string): 路由模式覆盖，'direct'或'relay'；不传用模式/配置默认。技术干活任务传"relay"使子代理回复返回主代理汇总；日常贴贴不传走默认直发。
    call_mode(string): 调用模式覆盖，'parallel'或'chained'；不传用模式/配置默认。技术干活传"parallel"并行调度；流水线任务传"chained"接龙。
    background(boolean): 是否后台执行（二期，默认 false）。true 时立即返回 task_id 不阻塞——主代理可以继续和用户对话，子代理做完再来取结果（用 task_result），适合耗时长或需要并行的任务。false 时等子代理全部做完再返回，与原行为完全一致。
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

        # ── 模式可选设置解析（2026-08-31 用户指定） ───────────
        # mode="tech" → 用 tech_mode_config（默认 relay+parallel 统帅收卷）
        # mode="affection" → 用 affection_mode_config（默认 direct+chained 直发）
        # 用户配置永远优先：mode 命中时模式配置无条件覆盖显式传参
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

        # ── 场景注入前缀（scene.py） ──────────
        scene_prefix = await self._build_scene_prefix(event, enable_scene_inject)

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
        direct_agents_str = self._cfg("direct_delivery_agents", "")
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
        # 更新 pending_batch + 在"用户只点名一人却误带多人"时给温和收敛建议。
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
            # ── 跨轮多代理续接：首发者防失忆（2026-09-08 用户实测）──────
            # 上一轮 chained 结束把「在场者+各自发言」按 session 存入 _chain_round_ctx。
            # 本轮若与上轮在场者有交集（续接场景），把上轮脉络注入首发者 input，
            # 让她知道「上一轮谁说了什么、谁还在场」——否则首发者只凭自己记忆召回，
            # 会把不在场的人叫进来（实测：误拉其他角色进场、在场者被晾）。
            _sess = event.unified_msg_origin
            _prev_note = self._build_prev_round_note(
                _sess, [c.get("agent_name") for c in calls]
            )
            # ── 全场剧情脉络（2026-09-07 修复多代理同场自说自话）──────────
            # 原 chained 只把上一位回复注入下一位（results[-1]），多人联动时
            # 丙只看乙、丁只看丙，看不见用户原话和更早的发言 → 各编各的。
            # 这里维护 transcript：用户原话(首行) + 到目前为止所有成功发言，
            # 每轮注入"全场脉络"而非只见上家，让每个角色接得住全场的话茬。
            # 变量是本方法局部作用域：一场 parallel_handoff 结束即销毁，
            # 多代理同场结束后绝无残留注入（配合 memory.py _strip_chain_injection 剥离）。
            transcript_blocks = []
            if message and str(message).strip():
                transcript_blocks.append(f"▍{self._get_user_address()}：{str(message).strip()}")
            for i, c in enumerate(calls):
                # ── 跨轮续接：首发者（i==0）也要看上一轮脉络 ──
                # chained 默认只把前面人的回复注入后面人，i==0 的首发者无注入；
                # 多代理续接场景下，她必须知道上轮每个人的发言，
                # 否则只能凭长期记忆瞎召回 → 拉错人（未在场角色乱入、在场者被晾）。
                if i == 0 and _prev_note:
                    c = dict(c)
                    c["input"] = _prev_note + (c.get("input") or "")
                if i > 0 and results:
                    c = dict(c)
                    # 重建全场：用户原话 + 当前角色前所有成功/失败发言（每人标注是谁说的）
                    transcript_blocks_cur = list(transcript_blocks)
                    for prev in results:
                        prev_display = self._display_name(prev.get("agent_name", ""))
                        if prev.get("success"):
                            prev_text = prev.get('response', '')
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
                                        prev_text = summarized
                                except Exception as e:
                                    logger.warning(
                                        f"[parallel_handoff] 接龙精简准备失败，降级原样注入: {e}"
                                    )
                            transcript_blocks_cur.append(f"【{prev_display}】{prev_text}")
                        else:
                            transcript_blocks_cur.append(f"【{prev_display}】（未接/超时）")
                    # 拼接注入块：全场脉络 包裹在当前角色的 [决策注入] 之前
                    cur_name = self._display_name(c.get("agent_name", ""))
                    chain_note = (
                        "（接龙·全场脉络，到目前为止）：\n"
                        + "\n".join(transcript_blocks_cur)
                        + f"\n\n请接续上文，现在轮到你（【{cur_name}】）回应，顺着全场的话茬自然往下："
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
                        # [2026-09-12 旁听窗] 流式路径补记录（原仅统一转发路径记录），
                        # 供主代理下一轮可见性注入
                        self._record_direct_reply(
                            event.unified_msg_origin,
                            r.get("agent_name", ""),
                            r.get("response", "") or "",
                        )
                    elif not r.get("success"):
                        # 失败：已通知，统一转发阶段跳过
                        r["_sent"] = True
                        await self._send_failure_notify(r, event)
                    # 非 direct 且成功：不打 _sent，
                    # 交由统一转发阶段收集进 return_agent_results 返回完整回复
            # ── 接龙结束：整场沉淀进各角色 livingmemory（可选，默认开）──
            # 用户拍板（2026-09-07）：多代理同场可以留长期记忆——记整场剧情脉络、
            # 落各角色 livingmemory、且每人存各自主观视角（不共享一份上帝视角）。
            # 但必须是"可自由回忆起"而非"日常强制浮现"：低 importance + 检索式召回，
            # 她主动检索/语境触及才捞到，日常对话不跳脸、不污染正常对话。
            # 开关 enable_chain_memory_persist 默认开，用户任何时刻可一键关。
            if self._cfg("enable_chain_memory_persist", True):
                try:
                    await self._persist_chain_memories(
                        message or "",
                        results,
                        livingmemory_plugin,
                        event,
                    )
                except Exception as _persist_e:
                    logger.warning(
                        f"[parallel_handoff] 整场记忆沉淀失败（非致命，不影响回复）: {_persist_e}"
                    )
            # ── 接龙结束：本场脉络写入会话级缓存（跨轮续接用）────────
            # 2026-09-08 用户实测：多代理同场续接第二轮时首发者失忆，
            # 误拉其他角色进场、在场者被晾——根因是 chained 全场脉络是单次调用局部变量。
            # 这里把「在场者组+每人发言」按 session 存下来，下一轮 chained 续接时
            # 注入给首发者（_build_prev_round_note），让她记得上一轮谁说过什么。
            try:
                _round_ctx = getattr(self, "_chain_round_ctx", None)
                if _round_ctx is None:
                    _round_ctx = self._chain_round_ctx = {}
                _round_ctx[_sess] = {
                    "ts": time.time(),
                    "agents": [
                        r.get("agent_name")
                        for r in results
                        if r.get("success") and r.get("agent_name")
                    ],
                    "speeches": [
                        {
                            "agent": r.get("agent_name"),
                            "display": self._display_name(r.get("agent_name", "")),
                            "text": (r.get("response", "") or "")[:600],
                        }
                        for r in results
                        if r.get("success") and r.get("response")
                    ],
                }
                logger.info(
                    f"[parallel_handoff] 跨轮脉络缓存 OK session={_sess} "
                    f"agents={_round_ctx[_sess]['agents']}"
                )
            except Exception as _ctx_e:
                logger.warning(
                    f"[parallel_handoff] 跨轮脉络缓存写入失败（非致命）: {_ctx_e}"
                )
            # 接龙模式保持调用顺序发送，不按 order 重排（order 仅对并行模式生效）
        elif background and getattr(self, "_task_runner", None) is not None:
            # ── 二期：后台并行（不阻塞主代理）─────────────────
            # 默认关闭（background=False 走下面的 gather 原路径，行为零变化）。
            # 开启后每个子代理各自一个后台任务，本方法立即返回 task_id，
            # 主代理可以继续跟用户说话，结果用 task_result 取。
            session_key = getattr(event, "unified_msg_origin", "") or "default"
            submitted = []
            for _c in calls:
                _agent = _c.get("agent_name") or ""
                _tid, _err = self._task_runner.submit(
                    session_key,
                    _agent,
                    (_c.get("input") or "")[:60],
                    (lambda _cc=_c: self._run_one_as_text(
                        _cc,
                        event=event,
                        handoff_map=handoff_map,
                        scene_prefix=scene_prefix,
                        livingmemory_plugin=livingmemory_plugin,
                        enable_name_prefix=enable_name_prefix,
                        timeout=timeout,
                    )),
                )
                submitted.append(
                    {"agent": _agent, "task_id": _tid, "error": _err}
                )
            return json.dumps(
                {
                    "background": True,
                    "tasks": submitted,
                    "hint": (
                        "任务已在后台执行，主代理无需等待。"
                        "用 task_result 取结果（会等），task_status 查进度，"
                        "task_stop 取消。"
                    ),
                },
                ensure_ascii=False,
            )
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
        # 2026-09-11 用户拍板：回传截断可配置化。
        # 原 preview 硬编码 120 字，segmented_forward 下主代理只拿得到 120 字摘要，
        # 无法做汇总/复核（今晚实测撞三次：子代理答卷、盲评表、G3 交付物）。
        _preview_chars = int(self._cfg("subagent_response_preview_chars", 4000))
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
                        "response_preview": (
                            (r.get("response", "") or "")[:_preview_chars]
                            + (
                                "…"
                                if len(r.get("response", "") or "") > _preview_chars
                                else ""
                            )
                        ),
                        # 被吞了多少一眼看得见：总量 + 是否截断
                        "response_chars": len(r.get("response", "") or ""),
                        "response_truncated": len(r.get("response", "") or "")
                        > _preview_chars,
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
            # 全失败时也必须返回摘要 JSON——以前返回 "✓" 会让主代理对失败完全无感知
            # （2026-09-03 实测：deepseek-v4-flash 端点挂起双双超时，主代理只见 "✓"）
            return json.dumps(summary, ensure_ascii=False)

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

    # ── 整场记忆沉淀（2026-09-07 用户拍板）──────────────────
    # 用户：多代理同场可以留长期记忆——记整场剧情脉络、落各角色 livingmemory，
    # 且"可以自由决定是否回忆起这段记忆，不剥夺回忆权"。
    # 实现：每个参与角色各存各自主观视角（不共享一份上帝视角剧本），
    # 落进她本人专属会话桩 persona 隔离，importance 由 livingmemory 检索式
    # 召回——日常不跳脸，语境触及才捞到，满足"自由决定回忆权"。
    async def _persist_chain_memories(
        self, message, results, livingmemory_plugin, event
    ):
        """多代理接龙结束后，把整场剧情按各角色视角沉淀进各自 livingmemory。

        只对成功发言的角色落库；每人只存"我"视角的脉络（我在场、我说了什么、
        谁回了我、用户怎么逗我），不共享一份上帝视角总剧本。落进
        {原会话}:subagent:{agent} 专属会话桩，persona 隔离，不串味。
        失败非致命，只记日志不影响回复。
        """
        if not livingmemory_plugin or not results:
            return
        # 用户原话
        user_line = (str(message) or "").strip()
        # 收集所有在场的角色视角。对每个成功角色：构造"我"视角脉络。
        success_names = [
            r.get("agent_name", "")
            for r in results
            if r.get("success") and r.get("agent_name")
        ]
        if not success_names:
            return
        # 一次写一个角色的视角，各自独立
        for agent_name in success_names:
            try:
                display = self._display_name(agent_name)
                lines = []
                if user_line:
                    lines.append(f"这一天{self._get_user_address()}把我们凑到一块儿，先起了个头：{user_line}")
                # 按发生顺序补全已发言的其他人（不含自己），拼成"我听到的"脉络
                others = []
                for r in results:
                    if not r.get("agent_name") or r.get("agent_name") == agent_name:
                        continue
                    if r.get("success"):
                        others.append(
                            f"{self._display_name(r['agent_name'])}对我说：{r.get('response','')}"
                        )
                if others:
                    lines.append("我还听到另一边：" + "；".join(others))
                if not lines:
                    continue
                pov_text = "\n".join(lines)
                # 结尾标记这是一段共同的剧情回顾（提醒提炼时归入剧情类而非日常事实）
                pov_text += (
                    "\n\n（这是我们一起经历过的一段，我记得它，日子照常过着。）"
                )
                await self._memory_store(
                    livingmemory_plugin,
                    event,
                    agent_name,
                    pov_text,
                    f"（{display} 记住了这一场）",
                )
                logger.info(
                    f"[parallel_handoff] 整场视角沉淀 OK [{display}] -> "
                    f"persona={agent_name}，共 {len(lines)} 段"
                )
            except Exception as e:
                logger.warning(
                    f"[parallel_handoff] 视角沉淀失败 [{agent_name}]: {e}"
                )

    # ── 跨轮脉络注入块构造（2026-09-08 用户实测：多代理续接首发者失忆）────────
    # ── 子代理检索纪律（2026-09-11 用户点名修复） ──────────────────
    # 实证：样本子代理拿到 14 个只读工具、也真的调了 rg_search，但一次搜出 150 条
    # 命中——关键词太宽 + 用中文描述词搜代码，信号被噪音淹没，导致 4 问只答上 2 问。
    # 工具没毛病，缺的是「怎么用」；把检索顺序写进系统提示，不依赖模型自悟。
    # [2026-09-15] 四条并发编排约束（实测依据：≥3 步的派单里 93% 是搜索/读取类串行，
    # 每步边际 7.3~7.8 秒、步数-延迟皮尔逊 r=0.716，p90 24.3s 全由多步累加而来）。
    # 只强化「怎么发工具」，不减任何工具、不动 extra。
    _SUBAGENT_RETRIEVAL_DISCIPLINE = (
        "\n\n【工具检索纪律——必读】\n"
        "查代码/文件事实时严格按下面顺序，不要一上来就读全文或凭印象作答：\n"
        "1. **同一步并发发多个候选**：rg_search / es_search / dir_list / safe_read 这类只读工具，"
        "同一意图下的多个候选（不同字面量、不同变体、不同目录/文件）**必须在同一回合内一次并发"
        "发出去**（一次 tool_calls 里带上 3~4 个）。实测每步往返约 7.4 秒：一次并发和四次单发拿到"
        "的是同样的证据——**禁止「搜一次→看一眼→再搜一次」的串行试探**。\n"
        "2. 关键词必须用代码里**真实存在的字面量**——函数名、变量名、字符串常量"
        "（例：`_call_one`、`llm_timeout`、`Timeout after`、`max_steps`）。"
        "**不要用中文描述词**（如「超时文案」「空回复逻辑」），中文词在代码里搜不到，只会浪费步数。\n"
        "3. 一批搜完**换维度再发下一批**（换工具、换层次、换文件），"
        "不要在同一维度上换着词连试——那等于把本该并发的候选拆成了串行。\n"
        "4. 命中位置后用 safe_read 带 start_line/end_line **只读那一段**上下文，"
        "不要读整个文件（大文件会被截断，关键段落在后面就漏了）；"
        "**要读的文件不确定时，一次并发读 2~3 个**，别一个一个试。\n"
        "5. **一批批完必须收口**：证据够了就给结论，不要再补一轮「顺手再搜一下」。"
        "确实没读到原文，就直说「未读到原文」——**绝对不要编造行号或原文**。\n"
        "6. 题目有多个小问时，每一问都要分别落实证据再作答，不要答完一问就收手。\n"
        "7. 步数预算有限：把步数花在**并发覆盖面上**，不是花在重复试探上；"
        "优先「定位→精读→核实」，少做无目标的宽泛搜索。"
    )

    def _subagent_system_prompt(self, handoff, agent_name: str = "") -> str:
        """子代理系统提示 = 人格指令 + 关系档案 + 检索纪律 + 任务卡解读。

        [2026-09-13] 关系档案（家庭关系/近况）从 extra_user_content 迁移到 system 稳定层：
        - 同一子代理的档案逐字节确定（无时间戳/随机），跨调用可命中前缀缓存；
        - 只有关系文件更新时才变化（低频），不会逐轮打断缓存；
        - 读失败自动退化为空段，绝不致命。
        """
        base = getattr(handoff.agent, "instructions", "") or ""
        _rel = self._relationship_inject(agent_name, "") if agent_name else ""
        _rel_block = ("\n\n" + _rel) if _rel else ""
        return (
            base
            + _rel_block
            + self._SUBAGENT_RETRIEVAL_DISCIPLINE
            + self._SUBAGENT_TASKCARD_GUIDE
        )

    # ── 任务卡解读（2026-09-11 用户拍板）──────────────────────────
    # 目的：把「派单格式」变成双方共识——下发侧按块写，接收侧按块读。
    # 靶点块存在的意义就是省步数：有靶点就直接用，别从全库开始搜。
    #
    # 2026-09-12 L2 层扩写：加【判断】【依据】两块 + 判断校验职责。
    #   实测教训：主代理在任务卡里**明说自己的判断**（"我认为 X 没实现"），
    #   才能被子代理反驳；只发纯指令（"实现 X"）时她会照做，错的那部分
    #   直接落进交付物。所以判断块不是礼貌，是校验入口。
    _SUBAGENT_TASKCARD_GUIDE = (
        "\n\n【任务卡解读】\n"
        "下发给你的任务可能按下面几块写，按块来读：\n"
        "- 【任务】要解决什么\n"
        "- 【靶点】已经给出的定位线索（文件路径、符号名、关键词、行号范围）"
        "——**优先从靶点入手，不要从全库开始搜**\n"
        "- 【判断】主代理当前的判断/假设——**这是待验证命题，不是事实**\n"
        "- 【依据】上述判断凭什么——线索来源，可能本身就有漏洞\n"
        "- 【产出】要交什么（结论 / 行号 / 原文 / 修正代码 / 清单）\n"
        "- 【边界】不许做什么（例如只读不改、不许动某文件）\n"
        "- 【完成标准】怎么算做完\n"
        "任务卡没写全的部分，按检索纪律自行补齐；**给了靶点就直接用，"
        "重复全库搜索是浪费步数**。\n"
        "\n【判断校验——你的职责包含证伪】\n"
        "带【判断】块的任务，你的产出**不是顺着它干活，是先验它**：\n"
        "1. 动手前先花一步确认这个判断成立不成立——去看【依据】指向的位置，"
        "是不是真如它所说\n"
        "2. 判断**不成立**：报告**开头第一句**就说「判断不成立」或「半对」+ 证据"
        "（文件、行号、原文），然后再给正确做法\n"
        "3. 判断**成立**：也要写一句「已核实，依据属实」——"
        "让主代理知道这是验过的，不是默认的\n"
        "4. 判断**部分成立**：分点说清哪部分对、哪部分错，"
        "**不要为了顺着主代理而含糊掉错的那部分**\n"
        "主代理的判断经常基于不完整视野（只看主路径、没往下翻 20 行），"
        "**你退回一个错误的判断，比多干十步活更值钱**。\n"
    )

    def _build_prev_round_note(self, session_id: str, cur_agents: list) -> str:
        """上一轮 chained 结束后，为新一轮续接构造「上一场脉络」注入块。

        多代理场次第二轮起，首发者（i==0）在 chained 里默认看不到任何前情
        （脉络是单次调用局部变量），只能凭长期记忆召回 → 拉错人。
        本方法把会话级缓存 _chain_round_ctx[session] 里的上轮在场者发言
        拼成注入块，返回给 parallel_handoff 拼到首发者 input 前。
        仅在：缓存存在 && 时间窗内（≤5min）&& 本轮名单与上轮在场者
        有交集（同一场次续接）时返回非空——避免串场污染。
        注入块以 _strip_chain_injection 可识别的 marker 包裹，用完即烧，
        不污染子代理长期记忆（memory.py markers 需同步）。
        """
        ctx = getattr(self, "_chain_round_ctx", None)
        if not ctx or session_id not in ctx:
            return ""
        round_info = ctx.get(session_id) or {}
        if not round_info.get("speeches"):
            return ""
        # 时间窗：超过 5 分钟视为旧场，不续接
        if time.time() - float(round_info.get("ts") or 0) > 300:
            return ""
        prev_agents = set(round_info.get("agents") or [])
        cur_set = {a for a in (cur_agents or []) if a}
        if not prev_agents or not (prev_agents & cur_set):
            return ""
        lines = ["（接龙·上一场脉络，你们还没散场）："]
        for sp in round_info.get("speeches", []):
            lines.append(f"【{sp.get('display', sp.get('agent', ''))}】{sp.get('text', '')}")
        lines.append("顺着上一场的话茬自然往下：")
        return "\n".join(lines)

    # ── 统一单代理路由实现（装饰器 @llm_tool 在 main.py 壳方法上） ──
    async def call_subagent(
        self,
        event: AstrMessageEvent,
        agent_name: str,
        input: str,
    ) -> str:
        """替代 transfer_to_* 工具的统一入口。调用单个子代理并将回复直接分段转发到用户。

使用场景：
- 用户明确要求与某子代理对话（如「agent_b，设备改造的事交给你了」）
- 用户提到子代理名字后说正事
- 相比 transfer_to_* 工具，本工具确保回复直接发到用户而不用主代理转述

Args:
    agent_name (string): 子代理名称。支持英文 id（如 agent_a、agent_b）和中文名（以 name_display_map 配置为准），大小写不敏感
    input (string): 传给子代理的完整问题或指令
"""
        calls = [{"agent_name": agent_name, "input": input}]
        return await self.parallel_handoff(event, calls=calls)

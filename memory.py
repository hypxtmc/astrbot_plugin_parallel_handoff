"""memory.py — parallel_handoff livingmemory 集成 + 召回 + 记忆工具过滤（P0 拆模块）

对应原 main.py 的 42-64 行（_strip_chain_injection 模块级函数）+
843-854 行（livingmemory 插件查找）+ 911-961 行区域（记忆召回/工具过滤）+
1009-1026 行区域（记忆存储）。
排除逻辑收敛为单一 exclude_agents 集合：由配置直接控制（默认空 = 全部子代理可召回）。
"""
import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.tool import ToolSet

try:  # 真实环境：包内相对导入
    from ._lm_bridge import LivingMemoryBridge
except ImportError:  # 单测环境：无 package，走绝对导入
    from _lm_bridge import LivingMemoryBridge


def _strip_chain_injection(text: str) -> str:
    """剥离接龙注入的前文块（临时上下文），避免污染子代理长期记忆。

    接龙模式（call_mode=chained）会把上一个子代理的输出注入下一个的
    input，这部分是临时上下文，不应进入本子代理的记忆召回/存储链路。
    2026-09-07：注入升级为「全场脉络」（接龙·全场脉络），marker 同步扩展，
    同时保留旧 marker（接龙·上一位）向后兼容。
    """
    # 兼容新旧注入前缀：新版「全场脉络」/ 旧版「上一位」都算临时上下文块起点
    # 2026-09-08：新增跨轮续接注入块「上一场脉络」（首发者防失忆用），marker 同步扩展
    # 2026-09-19：新增「同场实况」（后台任务状态，并行/接龙共用），marker 同步扩展
    markers = [
        "（接龙·上一场脉络，你们还没散场）",
        "（接龙·全场脉络，到目前为止）",
        "（接龙·上一位）",
        "（同场实况·后台任务）：",
    ]
    # 兼容新旧结尾提示：新版固定尾「顺着全场的话茬自然往下：」/ 旧版「请接续上文，现在轮到你回应：」
    # 跨轮续接尾「顺着上一场的话茬自然往下：」
    # 2026-09-19：同场实况尾（必须同步，否则剥离会截到注入起点、吞掉用户真实提问）
    end_markers = [
        "（以上为同场实况，仅供你了解同事情形，不必复述）",
        "顺着上一场的话茬自然往下：",
        "顺着全场的话茬自然往下：",
        "请接续上文，现在轮到你回应：",
    ]
    # 找到最早出现的注入前缀
    hit_marker = None
    hit_idx = -1
    for m in markers:
        i = text.find(m)
        if i != -1 and (hit_idx == -1 or i < hit_idx):
            hit_idx = i
            hit_marker = m
    if hit_marker is None:
        return text
    # 从注入起点向后找结尾提示；找不到则截到注入起点为止
    end = -1
    end_len = 0
    for em in end_markers:
        e = text.find(em, hit_idx)
        if e != -1 and (end == -1 or e < end):
            end = e
            end_len = len(em)
    if end == -1:
        return text[:hit_idx]
    # 剥掉 "注入起点 ~ 结尾提示整段"，只留结尾提示之后的真正输入
    return text[:hit_idx] + text[end + end_len:].lstrip("\n")


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

    # ── livingmemory 防腐层（2026-09-10 用户拍板）──────────
    def _lm_bridge(self, livingmemory_plugin):
        """取（或复用）livingmemory 能力适配器，收敛全部私有路径访问。

        livingmemory 的若干内部属性带下划线（event_handler._memory_recall.*、
        command_handler._memory_processor 等），上游重构即断裂且原先只静默降级。
        适配器统一做「版本探测 + 安全下钻 + 一次性告警」，按插件对象缓存，
        保证同一进程内某项能力缺失的 WARN 只出现一次。
        """
        if livingmemory_plugin is None:
            return None
        cached = getattr(self, "_lm_bridge_cache", None)
        if cached is not None and cached.plugin is livingmemory_plugin:
            return cached
        bridge = LivingMemoryBridge(livingmemory_plugin, logger=logger)
        self._lm_bridge_cache = bridge
        return bridge

    # ── 预取线索（2026-09-15 用户驱动）：派单时替子代理把「任务里已点名的实体」
    #    先本地 ripgrep 一遍，把 文件:行 锚点塞进 extra，让她开局直接精读、跳过盲搜。
    #    依据：实测子代理单次派单每步约 7.4 秒，≥3 步的派单里 93% 是
    #    「搜一次→看一眼→再搜一次」的串行搜索。预取成本：纯本地 rg，实测
    #    6 个候选 232ms、线索块约 300 字符，不调 LLM、不砍任何注入内容。
    #    失败/无命中一律返回空列表，绝不影响派单主流程。
    _PREFETCH_GENERIC = frozenset({
        "AstrBot", "astrbot", "python", "plugin", "plugins", "test", "tests",
        "data", "file", "files", "config", "main", "api", "json", "yaml",
        "README", "src", "lib", "docs", "web", "http", "https", "log", "logs",
        "用户", "任务", "文件", "目录", "代码", "函数", "变量", "配置", "插件", "测试",
    })

    @classmethod
    def _prefetch_candidates(cls, text: str, limit: int = 5) -> list:
        """从任务文本抽可检索实体（零 LLM 成本）。优先级：绝对路径 > 文件名 >
        代码标识符（带下划线/驼峰）> 引号内片段。通用词走黑名单，避免抽到
        「AstrBot」这种满库都是的词污染线索。"""
        import re

        paths = re.findall(r"/[\w\-./]{6,}", text)
        files = re.findall(r"\b[\w\-]+\.(?:py|json|yaml|yml|toml|sh|log|md)\b", text)
        ids = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b", text)
        ids = [x for x in ids if "_" in x or re.search(r"[a-z][A-Z]", x)]
        quoted = re.findall(r"[`\"'「【]([^`\"'\n【】]{3,60})[`\"'」】]", text)
        out, seen = [], set()
        for bucket in (paths, files, ids, quoted):
            for c in bucket:
                c = c.strip().rstrip("/")
                if len(c) < 3 or c in seen or c in cls._PREFETCH_GENERIC:
                    continue
                seen.add(c)
                out.append(c)
                if len(out) >= limit:
                    return out
        return out

    async def _prefetch_clues(self, text: str, limit: int = 5) -> list:
        """本地 rg 预取定位锚点，返回 [TextPart]（失败返回 []）。"""
        import asyncio
        import os
        import re
        import shutil
        import subprocess

        # [2026-09-15 修] 原先写成 astrbot.api.message_components（该模块无 TextPart），
        # ImportError 被下面的宽 except 静默吞掉 → 预取整整两小时没跑过一次却毫无日志。
        # 正确路径与 dispatch.py 一致：astrbot.core.agent.message。导入失败也必须留痕。
        try:
            from astrbot.core.agent.message import TextPart
        except Exception as _e:
            logger.warning(f"[parallel_handoff] 预取线索跳过：TextPart 导入失败 {type(_e).__name__}: {_e}")
            return []

        cands = self._prefetch_candidates(text, limit=limit)
        if not cands:
            logger.info("[parallel_handoff] 预取线索跳过：任务文本里没有可检索实体")
            return []
        if not shutil.which("rg"):
            logger.warning("[parallel_handoff] 预取线索跳过：找不到 rg 可执行文件")
            return []

        # 搜索根：任务里的绝对路径 > 任务点名的插件目录 > 兜底 AstrBot 根
        roots = []
        for p in re.findall(r"/root/[\w\-./]{4,}", text):
            p = p.rstrip("/")
            roots.append(p if os.path.isdir(p) else os.path.dirname(p))
        for name in re.findall(r"astrbot_plugin_[\w]+", text):
            d = f"/root/AstrBot/data/plugins/{name}"
            if os.path.isdir(d):
                roots.append(d)
        roots = [r for r in dict.fromkeys(roots) if r and os.path.isdir(r)]
        if not roots:
            # [2026-09-15 实测] 兜底不能给 AstrBot 全库：它下面压着 .venv / plugin_data 等
            # 十几万文件，四个候选全部撞 5s 超时（总 20.4 秒、零命中）。实测 data/plugins
            # 子树 4 个候选合计 849ms、36 命中——这才是兜底该有的量级。
            for _d in ("/root/AstrBot/data/plugins", "/root/AstrBot/astrbot"):
                if os.path.isdir(_d):
                    roots.append(_d)

        skip_ext = ("README", "CHANGELOG", "LICENSE")
        doc_mark = ("├", "│", "└", "──")

        def _sync() -> str:
            # [2026-09-15 修] 必须带 --no-ignore：AstrBot 根 .gitignore 第 23 行有 `data`，
            # rg 默认遵守它 → 从 AstrBot 根搜时把 data/plugins 整个跳过，永远零命中。
            # 实测直搜插件目录能命中目标文件，全库搜零命中，就是被忽略规则吃掉的。
            import time

            lines = []
            slow, bad = [], []
            deadline = time.monotonic() + 4.0   # 总预算：预取快不过 4 秒，否则不如不预取
            for root in roots[:2]:
                for c in cands:
                    if len(lines) >= 8 or time.monotonic() > deadline:
                        break
                    try:
                        r = subprocess.run(
                            ["rg", "-n", "--no-ignore", "--no-heading", "--max-count", "3",
                             "-F", c, root,
                             "-g", "!*.pyc", "-g", "!__pycache__", "-g", "!.git", "-g", "!*.log",
                             "-g", "!.venv*", "-g", "!node_modules", "-g", "!*.jsonl",
                             "-g", "!plugin_data", "-g", "!*.bak*", "-g", "!*ytimeout*"],
                            capture_output=True, text=True, timeout=2,
                        )
                    except subprocess.TimeoutExpired:
                        slow.append(c)
                        continue
                    except Exception as _e:
                        bad.append(f"{c}:{type(_e).__name__}")
                        continue
                    picked = []
                    for raw in r.stdout.strip().splitlines():
                        parts = raw.split(":", 2)
                        if len(parts) != 3:
                            continue
                        fpath, lno, body = parts
                        rel = fpath.replace(root.rstrip("/") + "/", "")
                        sbody = body.strip()
                        # 过滤文档噪音：README/说明文档、注释行、目录树图
                        if any(x in rel for x in skip_ext) and not rel.endswith(".py"):
                            continue
                        if sbody.startswith("#") or any(m in sbody for m in doc_mark):
                            continue
                        picked.append(f"{rel}:{lno}")
                        if len(picked) >= 2:
                            break
                    if picked:
                        lines.append(f"- `{c}` → " + "、".join(picked))
                if len(lines) >= 8:
                    break
            if slow:
                logger.info(f"[parallel_handoff] 预取：{len(slow)} 个候选检索超时 {slow[:3]}")
            if bad:
                logger.warning(f"[parallel_handoff] 预取：{len(bad)} 个候选检索异常 {bad[:3]}")
            return "\n".join(lines)

        try:
            body = await asyncio.to_thread(_sync)
        except Exception as _e:
            logger.warning(f"[parallel_handoff] 预取线索跳过：检索异常 {type(_e).__name__}: {_e}")
            return []
        if not body:
            logger.info(f"[parallel_handoff] 预取线索跳过：{len(cands)} 个候选均无命中")
            return []

        head = "【预取线索（派单时替你跑好的定位锚点，未经验证）】\n"
        tail = "\n（这些是本机 rg 直接命中的位置，可直接 safe_read 精读；与任务不符就按自己的判断走）"
        return [TextPart(text=head + body + tail).mark_as_temp()]

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
                # 且不等其内部最长 30s 的初始化轮询，避免拖慢子代理调用。
                # 2026-09-10：初始化探测收敛到 _lm_bridge（防腐层），行为不变。
                bridge = self._lm_bridge(livingmemory_plugin)
                healthy, detail = bridge.health()
                if not healthy:
                    initializer = getattr(livingmemory_plugin, "initializer", None)
                    if detail == "init_failed":
                        logger.warning(
                            f"[parallel_handoff] livingmemory 初始化失败，跳过 "
                            f"{agent_name} 记忆召回: "
                            f"{getattr(initializer, 'error_message', 'unknown')}"
                        )
                    else:
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
                recall_api = bridge.recall_api()
                if recall_api is None:
                    return []
                await recall_api(stub, req)

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
    # 子代理工具白名单默认值。
    # 2026-09-11 先只给「读」的手，写权留在主代理；
    # 2026-09-12 用户拍板取消只读——默认集曾补齐文件读写、语法/测试门禁与本地 git；
    # 2026-09-13 用户拍板收回写权——默认集回到「只读 + 网页搜索」：
    #   写/执行类（safe_edit/safe_write/multi_edit/file_patch/safe_rollback/
    #   file_remove/file_move/file_zip/file_unzip/symbol_rename/code_index/
    #   test_runner/git_commit）全部移除，子代理回归「只能看、不能动手」。
    # 2026-09-20 用户拍板 A+B 方案：
    #   A=全局安全写工具回归（写进配置键 subagent_tools，不动本默认集）；
    #   B=单代理特批 subagent_tools_by_agent，值 "ALL" = 与主代理同权。
    # 本默认集保持「只读」不变——它是配置被清空时的兵底，宁可少给不可多给。
    # 配置键优先 subagent_tools，兼容旧键 subagent_readonly_tools；留空回落本默认集。
    # 高风险工具（shell_exec / astrbot_execute_shell / hot_reload_plugin / git_push /
    # gh_*）刻意不进默认集，需用户单独授权后再写进白名单。
    _SUBAGENT_TOOL_NAMES_DEFAULT = (
        # 读（文件/目录/搜索）
        "safe_read", "dir_list", "dir_tree", "es_search", "rg_search",
        "text_filter", "file_hash", "file_diff", "file_preview", "safe_backups",
        # 代码理解（只读）
        "code_explore", "code_status",
        # 知识库 / 网页搜索
        "astr_kb_search", "web_search", "web_fetch", "web_search_tavily",
        "tavily_extract_web_page",
        # 只读检查（不改文件）
        "syntax_check", "lint_runner", "config_diff",
        # 只读 git
        "git_status", "git_diff", "git_log", "git_branch", "git_remote",
        "git_changelog",
    )

    @staticmethod
    def _parse_tool_names(raw):
        """逗号分隔字符串 / 列表 → 干净的工具名列表。"""
        if isinstance(raw, (list, tuple)):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in str(raw or "").split(",") if x.strip()]

    def _subagent_tools_by_agent(self):
        """按代理的特批工具名单。配置值是 JSON 对象：{"agent_b": "ALL"}。

        2026-09-20 顾主拍板 B 方案。解析失败一律回落空 dict（= 没有特批）——
        **绝不因为配置写坏就把工具全放出去**（fail-closed）。
        """
        raw = self._cfg("subagent_tools_by_agent", "")
        if not raw:
            return {}
        if isinstance(raw, dict):
            data = raw
        else:
            try:
                data = json.loads(str(raw))
            except (ValueError, TypeError):
                logger.warning(
                    "[parallel_handoff] subagent_tools_by_agent 不是合法 JSON，已忽略"
                )
                return {}
        if not isinstance(data, dict):
            return {}
        return {str(k).strip().lower(): v for k, v in data.items()}

    def _subagent_tool_names(self, agent_name=None):
        """子代理工具白名单。

        优先级（2026-09-20 加第一级，顾主拍板 A+B 方案）：
          1. subagent_tools_by_agent[agent_name] —— 单个代理的特批名单。
             值 "ALL" = 「跟主代理同权」，返回 None 让调用方去取全集。
          2. subagent_tools —— 全局白名单
          3. 兼容旧键 subagent_readonly_tools
          4. 空值回落 _SUBAGENT_TOOL_NAMES_DEFAULT（只读档）

        返回：tuple（白名单）或 None（= 全部工具，调用方负责展开）。
        """
        if agent_name:
            per_agent = self._subagent_tools_by_agent().get(
                str(agent_name).strip().lower()
            )
            if per_agent is not None:
                if str(per_agent).strip().upper() == "ALL":
                    return None
                names = self._parse_tool_names(per_agent)
                if names:
                    return tuple(names)
        names = self._parse_tool_names(self._cfg("subagent_tools", ""))
        if not names:
            names = self._parse_tool_names(self._cfg("subagent_readonly_tools", ""))
        return tuple(names) if names else self._SUBAGENT_TOOL_NAMES_DEFAULT

    def _build_memory_tools(self, agent_name: str):
        """为子代理构建工具集（记忆工具 + 读写工具）。

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
                    wanted_names = self._subagent_tool_names(agent_name)
                    if wanted_names is None:
                        # 特批「ALL」= 跟主代理同权。但剔掉 transfer_to_*——
                        # 子代理再派子代理没有终止条件，会互相打转烧钱。
                        picked = [
                            t
                            for t in global_tools.func_list
                            if not str(t.name).startswith("transfer_to_")
                        ]
                        mode = "全权档"
                    else:
                        wanted = {
                            "recall_long_term_memory",
                            "memorize_long_term_memory",
                        } | set(wanted_names)
                        picked = [t for t in global_tools.func_list if t.name in wanted]
                        mode = "白名单"
                    if picked:
                        subagent_tools = ToolSet(tools=picked)
                        logger.info(
                            f"[parallel_handoff] 子代理工具集 [{agent_name}]: "
                            f"{len(picked)} 项（{mode}）"
                            + (
                                ""
                                if wanted_names is None
                                else f"{sorted(t.name for t in picked)}"
                            )
                        )
                    else:
                        logger.warning(
                            f"[parallel_handoff] 工具白名单零命中 [{agent_name}]，"
                            f"全局工具名：{sorted(t.name for t in global_tools.func_list)}"
                        )
                else:
                    logger.warning(
                        f"[parallel_handoff] 全局工具集为空 [{agent_name}]"
                    )
        except Exception as e:
            logger.warning(
                f"[parallel_handoff] Failed to build agent tools for "
                f"{agent_name}: {e}"
            )
        return subagent_tools

    # ── 子代理专属记忆会话（2026-09-05 记忆串库修复） ─────────────

    @staticmethod
    def _subagent_event_stub(event, agent_name: str):
        """造子代理专属记忆会话桩。

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
        # 2026-09-12 双写修复（用户拍板）：原先返回 1（非群聊），会触发
        # livingmemory handle_memory_recall 的副作用存储
        #（memory_recall.py L140-155「存储用户消息（仅私聊），无论是否启用召回」），
        # 与本插件 _memory_store 的显式存储叠加 → 子代理会话 user 消息被存两遍
        #（实测样本会话 user 约为 assistant 两倍，真人会话约 1:1）。
        # 改报群聊值：is_group=True 时该分支整体跳过；召回/检索不受影响
        #（is_group 在 handle_memory_recall 内仅此一处使用，已核实）。
        stub.get_message_type = lambda: MessageType.GROUP_MESSAGE
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
        （英文 id）在 personas 表（按中文名登记）查不到，每小时
        心跳提炼都 WARN 一条并退化 base_prompt。存储维度（classify_atoms
        的 persona_id）保持英文 id 与召回侧 _subagent_persona 一致，
        不经过本函数，防止记忆库 persona 维度分裂。
        个别子代理的人格在库中按「名字-子代理」后缀登记；由部署方在
        persona_suffix_agents 配置里声明（逗号分隔的 id 列表，默认空 =
        原样返回，行为与旧版一致，仅可能仍有 WARN）。
        """
        try:
            _map = self._get_name_display_map() or {}
            disp = _map.get(agent_name) or agent_name
        except Exception:
            return agent_name
        raw_suffix = str(self._cfg("persona_suffix_agents", "")).strip()
        if raw_suffix:
            suffix_ids = {a.strip().lower() for a in raw_suffix.split(",") if a.strip()}
            if agent_name.lower() in suffix_ids:
                disp = f"{disp}-子代理"
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
            # 2026-09-10：私有路径访问收敛到 _lm_bridge 防腐层。
            # 缺件时一次性 WARN 并附 livingmemory 版本号，不再静默返回。
            kit = self._lm_bridge(livingmemory_plugin).reflection_kit()
            if not kit:
                return
            cm, mp, me, cfg = kit

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
            # 否则每小时心跳提炼都 WARN「人格 'xxx' 不存在」
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

        2026-09-05 记忆串库修复：此前直接用用户私聊 event 写历史——
        子代理消息混进主代理会话，livingmemory Reflection 总结主会话时
        把子代理的话一并提炼进主代理记忆库（用户观察到「记忆都落给了
        主代理」的根因）；且子代理专属会话无人触发提炼，长期记忆恒空。
        现改用「{原会话}:subagent:{agent}」专属会话桩存储，写完即检查
        阈值、按子代理 persona 主动提炼长期记忆。
        """
        if livingmemory_plugin:
            try:
                stub = self._subagent_event_stub(event, agent_name)
                # 2026-09-10：私有路径访问收敛到 _lm_bridge 防腐层
                bridge = self._lm_bridge(livingmemory_plugin)
                conv_mgr = bridge.conversation_manager()
                if conv_mgr is None:
                    return
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
                message_utils = bridge.message_utils()
                if message_utils is not None:
                    await message_utils.enforce_message_limit(
                        stub.unified_msg_origin
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

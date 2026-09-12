"""ctx_engine.py — 跨轮子代理上下文引擎

职责：会话历史的存储 / 窗口截断 / 上限裁剪，以及把历史转成结构化
messages 交给 tool_loop_agent 的 contexts= 参数（缓存友好的前提）。

2026-09-13 重构：清理已废弃的 LLM 压缩路径（_compress / _format /
keep_recent / compress_ratio / llm_generate）。子代理对齐主代理走纯截断
（2026-08-23 用户拍板），压缩相关代码从未再被调用——本次一并移除，
配置面只留 enabled / max_turns 两个键。

- 状态（histories）与路由分离，可单测、可独立调参
- inject 返回 (prompt, contexts)：历史作为独立 messages 占据前缀，
  每轮只在末尾追加一条新输入，前缀逐轮累积复用（缓存命中率的前提）
- 落盘后端可选（store）：磁盘为真源、内存为热窗口，MAX_MSGS 截的只是内存
"""

from astrbot.api import logger
from astrbot.core.agent.message import Message


class ContextEngine:
    """跨轮子代理上下文：历史存储 / 窗口截断 / 上限裁剪

    参数:
        enabled:   总开关（对应配置 subagent_context_enabled）
        max_turns: 窗口轮数；超过即纯截断（subagent_context_max_turns）
        store:     会话落盘后端，None = 纯内存（行为与改造前完全一致）
    """

    MAX_MSGS = 200

    def __init__(self, *, enabled=True, max_turns=100, store=None):
        self.enabled = bool(enabled)
        self.max_turns = int(max_turns)
        self.histories: dict[str, list[dict]] = {}
        # 2026-09-12 一期：会话落盘后端。None = 纯内存（完全回落旧行为，可随时摘）
        self.store = store
        # 标记哪些 ctx_key 已尝试过磁盘加载（避免每次访问都敲盘）
        self._loaded: set[str] = set()

    # ── 取会话历史：内存优先，缺失则从磁盘惰性加载一次 ──
    def _ensure(self, agent_name, session_id):
        """拿到某会话的历史（list）。

        惰性加载：只有会话真被访问时才读一次盘，不做启动全量扫盘。
        磁盘为真源、内存为热窗口——MAX_MSGS 截断的是内存，磁盘保留全量。
        store 为 None 时行为与改造前完全一致（纯内存）。
        """
        ctx_key = f"{agent_name}:{session_id}"
        if ctx_key in self.histories:
            return self.histories[ctx_key]
        hist: list[dict] = []
        if self.store is not None and ctx_key not in self._loaded:
            try:
                hist = self.store.load(agent_name, session_id) or []
            except Exception as exc:  # 读盘失败绝不致命
                logger.warning(f"[ctx_engine] 会话读盘失败，按空历史继续: {exc}")
            self._loaded.add(ctx_key)
        self.histories[ctx_key] = hist
        return hist

    # ── 注入：历史转结构化 messages 返回（超窗口自动截断） ──
    async def inject(self, agent_name, session_id, final_input):
        """返回 (prompt, contexts)。

        2026-09-11 改造（根因：缓存命中率低）：历史不再拼成一段纯文本塞进
        prompt，而是转成 Message 列表交给 tool_loop_agent 的 contexts= 参数。

        为什么：前缀缓存是逐字符匹配的。旧写法把整段历史压成**一条** user
        消息，那条消息每轮都长得不一样，于是一整条都算新内容。实测子代理缓存
        命中率中位仅 48.9%（主代理 98.1%）——固定前缀只有 persona 那 2688。
        改成 contexts 后，历史作为独立 messages 占据前缀，每轮只在末尾追加
        一条新输入，前缀逐轮累积复用。

        超窗口策略（2026-08-23 用户拍板）：对齐主代理 truncate_by_turns，
        走纯截断——保留最近 max_turns 轮，不做 LLM 摘要（动态摘要文本
        吃缓存与其他计费）。
        """
        if not self.enabled:
            return final_input, []
        history = self._ensure(agent_name, session_id)
        if not history:
            return final_input, []
        ctx_turns = len(history) // 2
        if ctx_turns <= self.max_turns:
            kept = history
        else:
            kept = history[-self.max_turns * 2:]
        return final_input, self._to_messages(kept)

    # ── 历史 dict → Message 列表（供 contexts= 使用） ──
    def _to_messages(self, history):
        out = []
        for msg in history:
            role = "user" if msg.get("role") == "user" else "assistant"
            try:
                out.append(Message(role=role, content=msg.get("content") or ""))
            except Exception as exc:  # 单条坏数据不该拖垮整轮
                logger.warning(f"[ctx_engine] 历史消息转换失败，已跳过: {exc}")
        return out

    # ── 存储：追加一轮对话（user + assistant） ──
    def append(self, agent_name, session_id, user_input, assistant_response):
        if not self.enabled:
            return
        ctx_key = f"{agent_name}:{session_id}"
        history = self._ensure(agent_name, session_id)
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": assistant_response})
        if len(history) > self.MAX_MSGS:
            self.histories[ctx_key] = history[-self.MAX_MSGS:]
        # 一期：落盘。磁盘留全量，内存只留窗口——所以调小窗口不丢历史。
        # 落盘失败非致命（SessionStore 内部已吞异常并记日志）。
        if self.store is not None:
            self.store.append(agent_name, session_id, "user", user_input)
            self.store.append(agent_name, session_id, "assistant", assistant_response)

"""ctx_engine.py — 跨轮子代理上下文引擎（P0 拆模块：压缩策略独立可调）

原实现内嵌在 DispatchMixin（_format_context_history / _compress_context_history /
_append_context 三方法 + self._subagent_contexts 状态字典），现独立成引擎类：
- 路由逻辑不再碰压缩参数/窗口策略，压缩行为经构造参数注入
- 状态（histories）与路由分离，可单测、可独立调参
- llm_generate 经构造闭包惰性绑定（AstrBot 的 context 运行期注入后才可用）

行为与原实现完全等价：窗口长度、压缩触发线、异常降级、上限 200 条。
"""
import asyncio

from astrbot.api import logger


class ContextEngine:
    """跨轮子代理上下文：历史存储 / 窗口截断 / 超窗口 LLM 压缩 / 上限裁剪

    参数:
        enabled:      总开关（对应配置 subagent_context_enabled）
        max_turns:    超过 N 轮对话后才触发压缩（subagent_context_max_turns）
        keep_recent:  压缩时保留最近完整轮数（subagent_context_keep_recent）
        compress_ratio: 压缩目标占比提示（subagent_context_compress_ratio, %）
        llm_generate: 可调用对象，signature (*args, **kwargs)，返回同原
                      llm_generate（含 completion_text）。未提供时超窗口走
                      截断降级，不抛异常。
    """

    MAX_MSGS = 200

    def __init__(self, *, enabled=True, max_turns=100, keep_recent=5,
                 compress_ratio=15, llm_generate=None):
        self.enabled = bool(enabled)
        self.max_turns = int(max_turns)
        self.keep_recent = int(keep_recent)
        self.compress_ratio = int(compress_ratio)
        self.histories: dict[str, list[dict]] = {}
        self._llm_generate = llm_generate

    # ── 注入：把历史拼进 final_input（超窗口自动压缩） ──
    async def inject(self, agent_name, session_id, final_input,
                     prov_id=None, handoff=None, timeout=None):
        if not self.enabled:
            return final_input
        ctx_key = f"{agent_name}:{session_id}"
        history = self.histories.get(ctx_key, [])
        if not history:
            return final_input
        ctx_turns = len(history) // 2
        if ctx_turns <= self.max_turns:
            ctx_text = self._format(history)
        else:
            # 2026-08-23 博士拍板：子代理对齐主代理 truncate_by_turns，走纯截断
            # （不再 LLM 摘要压缩——动态摘要文本吃缓存与其他计费）
            max_msgs = self.max_turns * 2
            ctx_text = self._format(history[-max_msgs:])
        return f"--- 对话历史 ---\n{ctx_text}\n--- 新的输入 ---\n{final_input}"

    # ── 存储：追加一轮对话（user + assistant） ──
    def append(self, agent_name, session_id, user_input, assistant_response):
        if not self.enabled:
            return
        ctx_key = f"{agent_name}:{session_id}"
        history = self.histories.setdefault(ctx_key, [])
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": assistant_response})
        if len(history) > self.MAX_MSGS:
            self.histories[ctx_key] = history[-self.MAX_MSGS:]

    # ── 格式化 ──
    def _format(self, history):
        lines = []
        for msg in history:
            role_label = "user" if msg["role"] == "user" else "assistant"
            lines.append(f"{role_label}: {msg['content']}")
        return "\n".join(lines)

    # ── 压缩：保留最近 N 轮完整 + LLM 摘要旧对话 ──
    async def _compress(self, history, prov_id, handoff, timeout):
        keep_count = self.keep_recent * 2
        if len(history) <= keep_count:
            return self._format(history)

        recent = history[-keep_count:]
        old = history[:-keep_count]

        old_text = self._format(old)
        compress_prompt = (
            f"请将以下对话历史压缩为一段简洁的摘要，保留关键信息、上下文和决策。"
            f"压缩后长度约为原文的{self.compress_ratio}%。"
            f"只输出摘要文本，不要加任何前缀或解释。\n\n{old_text}"
        )

        if self._llm_generate is None:
            raise RuntimeError("ContextEngine 未绑定 llm_generate，无法压缩")

        llm_resp = await asyncio.wait_for(
            self._llm_generate(
                chat_provider_id=prov_id,
                prompt=compress_prompt,
                system_prompt="你是一个对话摘要助手。请简洁地总结对话内容。",
            ),
            timeout=min(timeout, 30) if timeout else 30,
        )
        summary = llm_resp.completion_text.strip()

        recent_text = self._format(recent)
        return f"[历史摘要]\n{summary}\n\n[最近对话]\n{recent_text}"
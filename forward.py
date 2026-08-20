"""forward.py — parallel_handoff 分段转发 + 主代理前缀注入（P0 拆模块）

对应原 main.py 的 117-374 行区域。其中 "已分段清空链" 分支独立保留
（_inject_mainagent_prefix 内 _send_mainagent_segmented 命中即清空链）。
装饰器 @filter.on_decorating_result() 保留在 main.py 壳方法上，
本模块提供 _inject_mainagent_prefix 纯实现。
"""
import asyncio
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain, ResultContentType

# 怀孕插件文本自动识别接入（可选依赖：baby 插件未加载时静默跳过）
# 运行时插件挂载在 data.plugins 前缀下；顶层包名仅作兼容兜底
# 懒加载：首次实际分发时才探测 import，规避插件/热重载加载顺序不可控
_BABY_FEED_CACHE = None
_BABY_FEED_TRIED = False


def _get_baby_feed():
    global _BABY_FEED_CACHE, _BABY_FEED_TRIED
    if not _BABY_FEED_TRIED:
        _BABY_FEED_TRIED = True
        try:
            from data.plugins.astrbot_plugin_baby.analyzer import feed_text as _BABY_FEED_CACHE
        except Exception:
            try:
                from astrbot_plugin_baby.analyzer import feed_text as _BABY_FEED_CACHE
            except Exception:
                _BABY_FEED_CACHE = None
    return _BABY_FEED_CACHE



class ForwardMixin:
    """子代理分段转发 / 主代理分段转发 / 姓名前缀 / 主代理前缀注入"""

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
                _baby = _get_baby_feed()
                if _baby is not None:
                    try:
                        _baby(msg, sender_id=event.get_sender_id())
                    except Exception:
                        pass
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

    def _looks_like_markdown(self, text: str) -> bool:
        """[方案B 2026-08-19] 检测文本是否含 markdown 语法特征。

        含 markdown 特征时返回 True——调用方应走完整发送（不分段），
        避免拆碎的 markdown 片段无法渲染。
        """
        if not text:
            return False
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            # 标题：## 标题
            if re.match(r"^#{1,6}\s+\S", s):
                return True
            # 有序/无序列表
            if re.match(r"^[-*+]\s+\S", s) or re.match(r"^\d+[.、]\s+\S", s):
                return True
            # 引用
            if re.match(r"^>\s?", s):
                return True
            # 分割线
            # [2026-08-20] 分割线不再作为 md 特征：纯文本消息里的 --- 装饰线不再触发整条渲染
            # 水平线改由 _inject_section_dividers 自动插入，手写 --- 仅作普通文本/分段过滤
            # 代码块围栏
            if s.startswith("```"):
                return True
        # 行内特征：加粗 / 斜体 / 行内代码 / 链接 / 图片
        if re.search(r"\*\*[^*]+\*\*", text) or re.search(r"__[^_]+__", text):
            return True
        if re.search(r"`[^`\n]+`", text):
            return True
        if re.search(r"!?\[[^\]]*\]\([^)\s]+\)", text):
            return True
        return False

    def _looks_like_latex(self, text: str) -> bool:
        """[方案D 2026-08-19] 检测文本是否含 LaTeX 公式特征。"""
        if not text:
            return False
        if re.search(r"\$\$[\s\S]*?\$\$", text):
            return True
        if re.search(r"\\\([\s\S]*?\\\)", text):
            return True
        if re.search(r"(?<!\$)\$[^$\n]+\$(?!\$)", text):
            return True
        if re.search(r"\\begin\{[a-zA-Z]+\}", text):
            return True
        if re.search(
            r"\\(?:frac|sum|int|sqrt|lim|log|ln|sin|cos|tan|sec|csc|cot|"
            r"alpha|beta|gamma|delta|epsilon|zeta|eta|theta|iota|kappa|lambda|mu|nu|xi|"
            r"pi|rho|sigma|tau|upsilon|phi|chi|psi|omega|times|cdot|pm|mp|div|"
            r"le|ge|neq|approx|equiv|sim|propto|infty|partial|nabla|forall|exists|"
            r"rightarrow|leftarrow|Leftrightarrow|mapsto|subset|supset|cap|cup|"
            r"text|mathrm|mathbf|mathbb|mathcal|operatorname|left|right)\b",
            text,
        ):
            return True
        return False

    def _inject_section_dividers(self, result, full_text: str) -> None:
        """[方案D 2026-08-19] 一条消息内为纯文本/markdown/latex 区域插入分割线。

        按空行拆段后逐段分类，相邻不同类型的段之间插入 --- 分割线，
        重组后替换 result.chain，交给正常管线整条发送（markdown payload 渲染）。
        """
        segments = [s.strip() for s in full_text.split("\n\n") if s.strip() and not re.match(r"^(-{2,}|\*{2,}|_{2,})$", s.strip())]
        classified = []
        for seg in segments:
            if self._looks_like_latex(seg):
                seg_type = "latex"
            elif self._looks_like_markdown(seg):
                seg_type = "md"
            else:
                seg_type = "text"
            classified.append((seg_type, seg))
        # [简化 2026-08-19] 说明文字（text）后紧邻公式（latex）→ 合并为 formula 块，
        # 一体展示（无分割线、无标签），公式跟着说明走，减少繁杂
        merged = []
        i = 0
        while i < len(classified):
            t, seg = classified[i]
            if (t == "text" and i + 1 < len(classified) and classified[i + 1][0] == "latex"
                    and seg.rstrip().endswith(("：", ":"))):
                # 仅以冒号结尾的短说明文字才与公式合并（公式跟着说明走）
                merged.append(("formula", seg + "\n\n" + classified[i + 1][1]))
                i += 2
            else:
                merged.append((t, seg))
                i += 1
        new_parts = []
        prev_type = None
        for seg_type, seg in merged:
            if prev_type is not None:
                if seg_type != prev_type:
                    # 块类型转换处：formula/latex 边界用重等号线 ═×21，其余用粗线 ━×21
                    if seg_type in ("formula", "latex") or prev_type in ("formula", "latex"):
                        new_parts.append("---")
                    else:
                        new_parts.append("---")
                elif seg_type in ("formula", "latex") and prev_type in ("formula", "latex"):
                    # [2026-08-19] 连续不同公式之间用普通水平线 --- 分割（说明+公式内部保持一体不分割）
                    new_parts.append("---")
            # ◆ 公式 标签：只在独立公式（无说明文字跟随）时加，避免重复繁杂
            if seg_type == "latex":
                new_parts.append("◆ 公式")
            new_parts.append(seg)
            prev_type = seg_type
        new_content = "\n\n".join(new_parts)
        if new_content != full_text:
            # 注意：result.chain 是组件列表（list），不是 MessageChain 对象
            result.chain = [Plain(new_content)]

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
        # [普瑞赛斯亲修 03:08] 剥离首部【名字】前缀，防止与下方 add_prefix 叠加成双前缀
        full_text = re.sub(r"^\s*【[^】]*】\s*\n?", "", full_text, count=1)
        # [美化 2026-08-19] 状态符号简约化：emoji → 几何符号，简约风格统一
        normalized = (full_text
                      .replace("✅", "✓")
                      .replace("❌", "✗")
                      .replace("⚠️", "△")
                      .replace("🚧", "△")
                      .replace("⏳", "○"))
        if normalized != full_text:
            full_text = normalized
            result.chain = [Plain(full_text)]  # 同步链，保证 return False 路径也生效
        # [方案D 2026-08-19] 含 markdown/latex 时：一条消息内插入区域分割线（---），
        # 区分纯文本/markdown/latex 区域，交给正常管线整条发送渲染
        if self._looks_like_markdown(full_text) or self._looks_like_latex(full_text):
            self._inject_section_dividers(result, full_text)
            return False
        # [2026-08-20 01:46 重写] 分段规则（博士定稿）：按行扫描，横线行即分段信号——
        # 单行横线=软分隔（并入当前段，两句不拆，横线保留为普通文本）；
        # 连续两行及以上横线=硬分隔（当前段落盘，横线丢弃，前后拆开）。
        # 无横线行时退回按空行分段（原逻辑）。
        _hr_line = re.compile(r"^(-{1,}|\*{1,}|_{1,})$")
        lines = full_text.split("\n")
        has_hr = any(_hr_line.match(l.strip()) for l in lines if l.strip())
        if has_hr:
            segments = []
            current = []
            pending_hr = []
            for line in lines:
                s = line.strip()
                if not s:
                    # [2026-08-20 preL] 空行跳过：不打断横线连续性——
                    # "---\n\n---"（两个横线隔空行）仍视为连续两行横线，触发硬分隔
                    continue
                if _hr_line.match(s):
                    pending_hr.append(line)
                    continue
                if pending_hr:
                    if len(pending_hr) >= 2:
                        # 多行横线：硬分隔——落盘当前段
                        if current:
                            segments.append("\n".join(current).strip())
                            current = []
                    else:
                        # 单行横线：软分隔——并入当前段（或作前导），前后补空行供 md 渲染
                        # （避免 "文本\n---" 被 markdown 误判为 setext 二级标题）
                        if current:
                            current.append("")
                            current.append(pending_hr[0])
                            current.append("")
                        else:
                            current.append("")
                            current.append(pending_hr[0])
                            current.append("")
                    pending_hr = []
                current.append(line)
            # 尾部横线处理
            if pending_hr:
                if len(pending_hr) == 1:
                    if current:
                        current.append("")
                        current.append(pending_hr[0])
                        current.append("")
                    else:
                        segments.append(pending_hr[0])
                elif current:
                    segments.append("\n".join(current).strip())
                    current = []
            if current:
                segments.append("\n".join(current).strip())
            segments = [s for s in segments if s.strip()]
        else:
            segments = [s.strip() for s in full_text.split("\n\n") if s.strip()]
        if len(segments) <= 1:
            return False
        try:
            add_prefix = self._cfg("enable_mainagent_name_prefix", False)
            prefix_str = ""
            if add_prefix:
                prefix_str = f"【{self._cfg('main_agent_name', '普瑞赛斯')}】\n"
            for idx, seg_text in enumerate(segments):
                msg = seg_text
                if idx == 0 and prefix_str and not msg.startswith("【"):
                    msg = f"{prefix_str}{seg_text}"
                has_hr = any(_hr_line.match(l.strip()) for l in msg.split("\n") if l.strip())
                if has_hr:
                    # [2026-08-20 preK] 含横线段的段走被动 markdown 路径（event.send）：
                    # 主动 markdown 通道（send_markdown_content）不渲染 ---，
                    # 被动回复通道（_send_text_reply use_markdown=True）渲染为真水平线（方案 D 验证过）
                    chain = MessageChain([Plain(msg)])
                    chain.use_markdown_ = True
                    await event.send(chain)
                else:
                    await self.context.send_message(
                        event.unified_msg_origin,
                        MessageChain([Plain(msg)]),
                    )
                await asyncio.sleep(self.config.get("fragment_interval", 0.3))
        except Exception as e:
            logger.error(f"[inject_mainagent_prefix] 分段发送失败: {e}")
            # [修复 2026-08-16] 分段已发出部分段时禁止 return False——那会触发
            # 框架兜底重发整条，造成重复（NapCat sendMsg 超时 retcode 1200 场景
            # 消息可能实际已送达）。仅当一段都未发出（异常发生在循环前）才保留链
            # 交给调用方整条兜底，此时重发无重复风险。
            if "idx" not in locals():
                return False
            result.chain.clear()
            return True
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
        """规范化子代理名称：兼容中文名（阿米娅->amiya）和大小写（Amiya->amiya）。

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
            agent_name: 主代理的名称（如 "张三"、"李四"）

        Returns:
            加了前缀的文本（配置开启时）,或原文（配置关闭时）
        """
        if self._cfg("enable_mainagent_name_prefix", False):
            display_name = self._display_name(agent_name)
            return f"【{display_name}】\n{message}"
        return message

    # ── 主代理前缀自动注入（on_decorating_result 钩子实现）───
    async def _inject_mainagent_prefix(self, event: AstrMessageEvent):
        """在主代理消息发出前自动加【名字】前缀

        通过 on_decorating_result 钩子拦截所有即将发送的消息，
        当 enable_mainagent_name_prefix=true 且消息不以任何子代理前缀开头时，
        自动在消息正文前加 "【主代理名】\n" 前缀。
        子代理分段转发期间此钩子被抑制，避免误加。

        [流式守卫 2026-08-20] 借鉴 astrbot_plugin_custome_segment_reply 的
        "回放不抢发" 思想：流式终态（STREAMING_FINISH）时，LLM 文本已由流式
        通道逐 token 发给用户，此时再进行 event.send 分段重发必造成重复/乱序
        （根因）。故流式终态一律让位——直接 return，不 event.send 不重发，
        让流式那句作为唯一最终结果。子代理直发走独立链路（dispatch 直调
        _forward_segmented），不经本钩子，故不受流式影响仍完美工作。
        """
        # [流式守卫 2026-08-20] 流式终态必须让位：流式通道已把整段文本吐给用户，
        # 钩子此刻若再 event.send 分段重发，必然与流式已发内容重复/乱序。
        # 直接 return（既不 event.send 也不改 chain），把发送权完整交还流式通道。
        result = event.get_result()
        if (
            result is not None
            and getattr(result, "result_content_type", None) == ResultContentType.STREAMING_FINISH
        ):
            self._suppress_mainagent_prefix = False
            return

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
                main_agent_name = self._cfg("main_agent_name", "普瑞赛斯")
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

        main_agent_name = self._cfg("main_agent_name", "普瑞赛斯")
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

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

# 读空气·段五：主代理虚拟发言人标记（双分支导入，对齐 main.py try/except 哲学）
try:
    from .arbitrate import MAIN_SPEAKER
except ImportError:
    from arbitrate import MAIN_SPEAKER

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
            # [2026-08-30 普瑞赛斯] 子代理转发走 send_message 直发，
            # 不经过 on_decorating_result（那里只覆盖主代理回复），
            # 表格分隔行居中必须在此兜底，否则子代理表格全部左对齐。
            content = self._force_table_center(content)
            prefix_match = re.match(r"^(【[^】]+】)\n", text)
            if prefix_match:
                prefix = prefix_match.group(1)
                content = text[prefix_match.end():]
            # [2026-08-30 普瑞赛斯 v2] 代码块完整性（冻结-恢复）：分段前把 ``` 代码块
            # 冻结为哨兵 token，分段后还原——纯文本保持原叙述分段节奏（括号/空行/换行），
            # 代码块整块不被切断。
            _fences = []

            def _freeze_fence(t):
                def _rep(m):
                    _fences.append(m.group(0))
                    return f"\x00CB{len(_fences) - 1}\x00"
                # 1) 代码块整体冻结
                t = re.sub(r"```.*?```", _rep, t, flags=re.S)
                # 2) 表格块（连续 | 行）整体冻结，防叙述分段按行切碎表格
                _lines = t.split("\n")
                _out = []
                _i = 0
                while _i < len(_lines):
                    if _lines[_i].strip().startswith("|"):
                        _blk = [_lines[_i]]
                        _j = _i + 1
                        while _j < len(_lines) and _lines[_j].strip().startswith("|"):
                            _blk.append(_lines[_j])
                            _j += 1
                        _fences.append("\n".join(_blk))
                        _out.append(f"\x00CB{len(_fences) - 1}\x00")
                        _i = _j
                    else:
                        _out.append(_lines[_i])
                        _i += 1
                return "\n".join(_out)

            def _thaw_fence(t):
                for _i, _f in enumerate(_fences):
                    t = t.replace(f"\x00CB{_i}\x00", _f)
                return t

            content = _freeze_fence(content)
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
            segments = [_thaw_fence(s) for s in segments]
            if not segments:
                # 2026-09-05 修复：子代理返回空串（如 livingmemory 无记忆 + LLM 空输出）
                # 时此前静默跳过，博士端「Completed 但收不到」。改为发一条兜底提示。
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain([Plain("（她沉默了一会儿，没说出话来）")]),
                )
                return
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

    def _zh_fullwidth_sentinel(self, text: str) -> str:
        """[2026-08-27 标点哨兵] 中文叙述半角标点→全角兜底。

        把叙述文本中的半角标点（, . ? ! ; : ( )）转为全角。
        豁免区：代码块、行内代码、LaTeX 公式（$ $$ \( \) \begin{}）、
        URL、Windows/Linux 路径、数字串（保留小数点与千分位）。
        采用区间法：一次收集所有保护区间的起止，只转换区间外的叙述文本，
        避免“token 再被后续 pattern 污染”的嵌套问题。幂等。
        """
        if not text:
            return text

        # ── 保护区pattern（与分类逻辑同源,覆盖面一致） ──
        guards = [
            re.compile(r"```[\s\S]*?```"),                      # 代码块
            re.compile(r"`[^`\n]+`"),                            # 行内代码
            re.compile(r"\$\$[\s\S]*?\$\$"),                     # $$ 公式
            re.compile(r"(?<!\$)\$[^$\n]+\$(?!\$)"),            # $ 公式
            re.compile(r"\\\([\s\S]*?\\\)"),                     # \( \) 公式
            re.compile(r"\\begin\{[a-zA-Z]+\}[\s\S]*?\\end\{[a-zA-Z]+\}"),  # 环境
            re.compile(r"https?://[^\s<>\"'）\]]+"),            # URL
            re.compile(r"[A-Za-z]:\\[^\s\"'<>]+"),              # Win 路径
            re.compile(r"(?:/[\w\-.,/]+|~/[\w\-.,/]+|[\w\-.]+\.(?:py|js|ts|json|md|txt|sh|yaml|yml|ini|toml|xml|log|db|so|whl|zip|docx?|pptx?|xlsx?|pdf))"),  # 类路径
            re.compile(r"\d[\d.,]*"),                           # 数字串
        ]
        spans = []
        for pat in guards:
            for m in pat.finditer(text):
                spans.append((m.start(), m.end()))
        if spans:
            spans.sort()
            merged = []
            for s, e in spans:
                if merged and s <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append((s, e))
        else:
            merged = []

        # ── 对保护区外分段做转换 ──
        trans = str.maketrans({
            ",": "，", "?": "？", "!": "！", ";": "；",
            "(": "（", ")": "）",
        })
        out = []
        pos = 0
        for s, e in merged:
            if s > pos:
                out.append(self._convert_punct_segment(text[pos:s], trans))
            out.append(text[s:e])
            pos = e
        if pos < len(text):
            out.append(self._convert_punct_segment(text[pos:], trans))
        return "".join(out)

    @staticmethod
    def _convert_punct_segment(seg: str, trans: dict) -> str:
        """对一段不含保护区的纯叙述文本做全角化。静态方法,便于测试。"""
        seg = seg.translate(trans)
        # 冒号仅在(隔空白后)后面跟中文/中文引号时转(避开 a:b、时间 12:30、协议头)
        seg = re.sub(r":(?=\s*[\u4e00-\u9fff\u201c\u300c\u300e\uff08\u0028])", "：", seg)
        # 句点仅前后均非字母数字时转(避开版本号 1.2.3、扩展名)
        seg = re.sub(r"(?<![A-Za-z0-9])\.(?![A-Za-z0-9])", "。", seg)
        # 全角标点后紧邻半角空格清理(中文标点后不留空格)
        seg = re.sub(r"([，。！？；：）]) +", r"\1", seg)
        return seg

    @staticmethod
    def _force_table_center(text: str) -> str:
        """[2026-08-29] 表格分隔行统一居中：| --- | 变体 → |:---:|

        挂在 on_decorating_result 出口对链文本全量生效(非流式),不依赖模型记忆。
        幂等:重建后的分隔行再次经过本函数结果不变。
        只处理含管道符的分隔行(GFM 表格);单行裸 --- 是分割线,不碰;
``` 围栏代码块与该行含反引号的行内代码不碰。
        """
        # [2026-08-30 修正] 兼容 2 横线分隔行（|--|、|:--:|），模型常见写法
        if "|" not in text or "--" not in text:
            return text
        # GFM 表格分隔行：每格为 :?-{3,}:?，至少两格，整行无其他正文
        # [2026-08-29 修正] 格内前后允许空白，兼容 "| :---: | :---: |" 与混合对齐写法
        # tail 的 \s* 须在 :? 之前（与 cell 一致），否则 " :---" 空格+冒号开头无法匹配
        # [2026-08-30 修正] -{3,} → -{2,}：兼容 |:--|、|--| 等 2 横线分隔行
        sep = re.compile(
            r"^\s*\|?\s*(?:\s*:?\s*-{2,}\s*:?\s*\|)+\s*:?\s*-{2,}\s*:?\s*\|?\s*$"
        )
        out_lines = []
        in_fence = False
        for line in text.split("\n"):
            s = line.strip()
            if s.startswith("```"):
                in_fence = not in_fence
                out_lines.append(line)
                continue
            if in_fence or "`" in line:
                out_lines.append(line)
                continue
            if sep.match(line):
                cells_n = max(1, len([c for c in line.split("|") if c.strip()]))
                out_lines.append("|" + "|".join([":---:"] * cells_n) + "|")
            else:
                out_lines.append(line)
        return "\n".join(out_lines)

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
        # 行内特征：行内代码 / 链接 / 图片
        # 注意：**加粗** 和 __斜体__ 不触发 md 渲染路径，避免闲聊/色情误判
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
                # [2026-08-29 博士定规] markdown 渲染消息内每个分段之间无条件插分割线,
                # 不再限定类型变化处;同类型相邻段也插,看消息更整齐。纯文本消息不走本函数。
                new_parts.append("---")
            new_parts.append(seg)
            prev_type = seg_type
        new_content = "\n\n".join(new_parts)
        if new_content != full_text:
            # 注意：result.chain 是组件列表（list），不是 MessageChain 对象
            result.chain = [Plain(new_content)]

    _BLOCK_RE = re.compile(
        r"(?P<code>```[^\n]*\n[\s\S]*?```(?:\n|$))"
        r"|(?P<latexenv>\\begin\{[a-zA-Z]+\}[\s\S]*?\\end\{[a-zA-Z]+\}(?:\n|$))"
        r"|(?P<ddot>\$\$[\s\S]*?\$\$(?:\n|$))"
    )

    def _split_by_block_type(self, full_text: str) -> list:
        """[2026-08-27 类型拆条] 按块级代码/公式/叙述切分消息。

        返回 [(type, text)]：
          type ∈ {"text","code","latexenv","ddot"}
          text  = 叙述段(纯文本，可能含行内 md 但无块级)
          code  = ``` fenced 代码块整体
          latexenv = \begin{}...\end{} 块级公式
          ddot  = $$...$$ 块级公式
        相邻 text 自动合并，保证叙述是一个整体。
        """
        blocks = []
        pos = 0
        for m in self._BLOCK_RE.finditer(full_text):
            if m.start() > pos:
                blocks.append(("text", full_text[pos:m.start()].strip()))
            kind = next(k for k in ("code", "latexenv", "ddot") if m.group(k) is not None)
            blocks.append((kind, m.group(0).strip()))
            pos = m.end()
        if pos < len(full_text):
            blocks.append(("text", full_text[pos:].strip()))
        merged = []
        for k, v in blocks:
            if v and k == "text" and merged and merged[-1][0] == "text":
                merged[-1] = ("text", merged[-1][1] + "\n\n" + v)
            elif v:
                merged.append((k, v))
        return merged

    def _is_document_style(self, full_text: str) -> bool:
        """[2026-08-28 普瑞赛斯] 完整文档型消息判定。

        类型拆条的本意是"叙述+独立代码块"的闲聊式回复（像人发消息）；
        但结构化文档（多级标题/标题+表格+代码块）是排版整体，拆成
        碎条后纯文本段落失渲染、标题层级全毁——应走整条渲染。
        命中返回 True → _send_split_by_type 让位交回老路径。
        """
        try:
            if not full_text:
                return False
            lines = full_text.splitlines()
            headings = [ln for ln in lines if re.match(r"^#{1,6}\s+\S", ln.strip())]
            if len(headings) >= 2:
                return True
            if headings and "```" in full_text and "|" in full_text:
                return True
            return False
        except Exception:
            return False

    async def _send_split_by_type(self, result, event, full_text) -> bool:
        """[2026-08-27 类型拆条] 叙述/代码/公式各发各的，别揉成一条大 md 消息。

        触发条件:消息里同时存在块级代码/公式( code/latexenv/ddot )与叙述(text)，
        即"混排"。此时:
          - text 段 → 含 md 语法的走 markdown 渲染单条，纯叙述走纯文本单条
            （都像人正常发消息，不再是半屏卡片夹在长文里）
          - code/ddot/latexenv → 各自独立一条走 markdown 渲染
        不适用(纯叙述/纯块/仅表格)时返回 False，交回 _send_md_split_sections 等旧逻辑。
        """
        try:
            # [2026-08-28 普瑞赛斯] 完整文档型消息豁免类型拆条：
            # 多级标题/标题+表格+代码块的结构化文档走整条渲染，避免碎条失渲染
            if self._is_document_style(full_text):
                return False
            blocks = self._split_by_block_type(full_text)
            if not blocks:
                return False
            codeish = [b for b in blocks if b[0] != "text"]
            textish = [b for b in blocks if b[0] == "text"]
            # 必须"混排":没有强块(代码/latex环境)或没有叙述,不拆(老路径整条渲染)。
            # 2026-08-27 收紧:单独的 $$ 块公式算"轻块"——数学题解常见
            # "短句+$$公式"交错,拆开反断裂,应整条渲染;代码/强公式环境才拆条。
            strong = [b for b in blocks if b[0] in ("code", "latexenv")]
            textish = [b for b in blocks if b[0] == "text"]
            if not strong or not textish:
                return False

            sent_any = False
            fixed_iv = self.config.get("fragment_interval", None)
            for idx, (btype, btext) in enumerate(blocks):
                try:
                    if btype == "text":
                        if self._looks_like_markdown(btext) or self._looks_like_latex(btext):
                            chain = MessageChain([Plain(btext)])
                            chain.use_markdown_ = True
                            await event.send(chain)
                        else:
                            await self.context.send_message(
                                event.unified_msg_origin,
                                MessageChain([Plain(btext)]),
                            )
                    else:
                        chain = MessageChain([Plain(btext)])
                        chain.use_markdown_ = True
                        await event.send(chain)
                    sent_any = True
                except Exception as e:
                    logger.error(f"[inject_mainagent_prefix][type-split] 块{idx}发送失败，跳过: {e}")
                    continue
                if fixed_iv is not None:
                    iv = float(fixed_iv)
                elif idx == 0:
                    iv = 0.1
                else:
                    iv = min(0.12 + len(btext) * 0.005, 0.72)
                await asyncio.sleep(iv)
            if not sent_any:
                return False
            result.chain.clear()
            return True
        except Exception as e:
            logger.error(f"[inject_mainagent_prefix][type-split] 异常，回退旧逻辑: {e}")
            return False

    def _split_long_segment(self, seg_text: str, soft: int = 80, hard: int = 900) -> list:
        """[2026-08-27 03:08 优化①③] 段内句子级拆分+超长兜底。

        段无横线且超过 soft 字时按句末标点拆成语块（一句一消息）；
        单个语块再超 hard 字时硬切，防止顶到 QQ 富文本单条上限。
        含横线段保持原样——它走被动 markdown 渲染，拆开会毁水平线。
        """
        _hr_local = re.compile(r"^(-{1,}|\*{1,}|_{1,})$")
        for line in seg_text.split("\n"):
            if _hr_local.match(line.strip()):
                return [seg_text]
        if len(seg_text) <= soft:
            return [seg_text]
        parts = re.split(r"([。！？!?；;…]+)", seg_text)
        pieces, buf = [], ""
        for i in range(0, len(parts) - 1, 2):
            buf += parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
            if len(buf) >= soft:
                pieces.append(buf.strip())
                buf = ""
        if buf.strip():
            pieces.append(buf.strip())
        elif parts and parts[-1].strip():
            pieces.append(parts[-1].strip())
        if not pieces:
            pieces = [seg_text]
        out = []
        for piece in pieces:
            if len(piece) <= hard:
                out.append(piece)
            else:
                for i in range(0, len(piece), hard):
                    out.append(piece[i:i + hard])
        return [p for p in out if p.strip()]

    def _md_split_segments(self, full_text: str, budget: int) -> list:
        """[2026-08-27 03:1x 优化④] markdown 结构边界拆段。

        只在安全断点切：块级空行、ATX 标题、围栏代码块闭合/开启处、
        公式区($$ / \\(...\\) / $...$)、表格整块；
        列表/引用连续合并为一条，中间不拆。样式保证：单条内结构完整，
        代码块/表格/公式绝不被拦腰剁开，超预算时整块放行（渲染优先于切分）。
        """
        lines = full_text.split("\n")
        blocks = []          # 结构原子块
        cur = []             # 当前累积行
        cur_kind = None      # text / heading / list / quote / table / fence
        fence_active = False

        def flush():
            nonlocal cur, cur_kind
            if cur:
                blocks.append("\n".join(cur).rstrip())
                cur, cur_kind = [], None

        def is_fence(line):
            s = line.strip()
            return s.startswith("```") or s.startswith("~~~")

        def is_heading(line):
            return bool(re.match(r"^#{1,6}\s+", line.strip()))

        def is_table_line(line):
            # 表格要求含 | 且首行有分隔行（---）跟随——保守起见：连续含 | 行成块
            return "|" in line

        def is_list(line):
            return bool(re.match(r"^\s*(?:[-*+]|\d+[.、])\s+\S", line.strip()))

        def is_quote(line):
            return re.match(r"^>\s?", line.strip()) is not None

        def is_latex_fence(line):
            s = line.strip()
            return s.startswith("$$") or s.startswith("\\(") or s.startswith("\\[")

        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            s = line.strip()
            if fence_active:
                cur.append(line)
                if is_fence(line):
                    fence_active = False
                    flush()   # 代码块整体一条，闭合即落盘
                i += 1
                continue
            if not s:
                flush()  # 块级空行 = 安全断点
                i += 1
                continue
            if is_fence(line):
                flush()
                cur.append(line)
                cur_kind = "fence"
                fence_active = True
                i += 1
                continue
            if is_latex_fence(line):
                cur.append(line)
                cur_kind = cur_kind or "latex"
                # 块级公式整体吞行直到闭块（$$ 配对或 \)）
                j = i + 1
                while j < n:
                    cur.append(lines[j])
                    ls = lines[j].strip()
                    if s.startswith("$$") and ls.endswith("$$") and j > i:
                        break
                    if s.startswith("\\(") and ls.endswith("\\)"):
                        break
                    if s.startswith("\\[") and ls.endswith("\\]"):
                        break
                    j += 1
                flush()
                i = j + 1
                continue
            if is_heading(line):
                flush()
                cur.append(line)
                cur_kind = "heading"
                # 标题吞并后续同段文本直到空行/下一结构边界（标题=新条起点）
                k = i + 1
                while k < n and lines[k].strip():
                    nxt = lines[k].strip()
                    if is_fence(lines[k]) or is_heading(lines[k]) or is_latex_fence(lines[k]) or is_table_line(lines[k]):
                        break
                    if is_list(nxt) or is_quote(nxt):
                        break
                    cur.append(lines[k])
                    k += 1
                flush()
                i = k
                continue
            if is_table_line(line):
                if cur_kind != "table":
                    flush()
                    cur_kind = "table"
                cur.append(line)
                # 连续含 | 行归并为一张表，遇空行/下一结构 flush
                k = i + 1
                while k < n and is_table_line(lines[k]) and lines[k].strip():
                    cur.append(lines[k])
                    k += 1
                flush()
                i = k
                continue
            # 列表/引用：连续同类合并
            if is_list(line) or is_quote(line):
                kind = "list" if is_list(line) else "quote"
                if cur_kind != kind:
                    flush()
                    cur_kind = kind
                cur.append(line)
                k = i + 1
                while k < n:
                    nxt = lines[k].strip()
                    if not nxt:
                        break
                    if kind == "list" and (is_list(nxt) or nxt.startswith("  ") or nxt.startswith("\t")):
                        cur.append(lines[k]); k += 1; continue
                    if kind == "quote" and is_quote(lines[k]):
                        cur.append(lines[k]); k += 1; continue
                    break
                flush()
                i = k
                continue
            # 普通文本
            if cur_kind is None:
                cur_kind = "text"
            cur.append(line)
            i += 1
        flush()

        # 按预算打包：块不拆开，装不下就整块另起一条（代码块/表格/公式渲染优先）
        messages, buf = [], ""
        for blk in blocks:
            if buf and len(buf) + 1 + len(blk) <= budget:
                buf += "\n\n" + blk
            else:
                if buf:
                    messages.append(buf)
                buf = blk
        if buf:
            messages.append(buf)
        # 拆不出（整体<=预算）返回空表,由调用方走 prefK 整条渲染
        return messages if len(messages) > 1 else []

    async def _send_md_split_sections(self, result, event, full_text) -> bool:
        """[2026-08-27 03:1x 优化④] markdown 长消息按结构边界分段直发。

        依赖配置：mainagent_md_split_max_chars（默认 900）、
        mainagent_disable_md_split（默认 False,True 则永远走 prefK 整条渲染）。
        发送：全部走被动 markdown 路径（event.send + use_markdown_），
        与 prefK 验证过的渲染通道一致；第 2 条起加 ▍续 N/M 进度前缀。
        返回 True=已拆分发送并清链；False=不宜拆分（未开启/太短/未用 markdown），
        由调用方回退到 _inject_section_dividers 整条渲染。
        """
        try:
            if self.config.get("mainagent_disable_md_split", False):
                return False
            budget = int(self.config.get("mainagent_md_split_max_chars", 900))
            if budget <= 0:
                return False
            if not (self._looks_like_markdown(full_text) or self._looks_like_latex(full_text)):
                return False
            messages = self._md_split_segments(full_text, budget)
            if not messages:
                return False
            total = len(messages)
            # 进度前缀（2026-08-27 起默认关）：博士嫌 ▍续 N/M 打头傻。
            # 配置 mainagent_md_split_progress=True 可恢复旧行为。
            if self.config.get("mainagent_md_split_progress", False):
                for i in range(1, total):
                    messages[i] = f"\n▍续 {i + 1}/{total}\n\n" + messages[i]
            sent_any = False
            fixed_iv = self.config.get("fragment_interval", None)
            for idx, msg in enumerate(messages):
                try:
                    chain = MessageChain([Plain(msg)])
                    chain.use_markdown_ = True
                    await event.send(chain)
                    sent_any = True
                except Exception as e:
                    logger.error(f"[inject_mainagent_prefix][md] 第{idx + 1}条发送失败，跳过: {e}")
                    continue
                if fixed_iv is not None:
                    iv = float(fixed_iv)
                elif idx == 0:
                    iv = 0.1
                else:
                    iv = min(0.15 + len(msg) * 0.006, 0.85)
                await asyncio.sleep(iv)
            if not sent_any:
                return False
            result.chain.clear()
        except Exception as e:
            logger.error(f"[inject_mainagent_prefix][md] 拆分异常，回退整条渲染: {e}")
            return False
        return True

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
        # [方案D 2026-08-19 + 优化④ 2026-08-27 + 类型拆条 2026-08-27]
        # 含 markdown/latex 时:
        #   1) 先试"按块类型拆条"(叙述/代码/公式各发一条,像人发消息);
        #   2) 不适合时回退结构边界分段直发(_send_md_split_sections,拆出>=2条才走);
        #   3) 再不行回退 _inject_section_dividers 整条渲染,保持旧行为。
        if self._looks_like_markdown(full_text) or self._looks_like_latex(full_text):
            # [2026-08-28 普瑞赛斯] 文档型消息（多级标题/标题+表格+代码块）——
            # 完整排版整体，不参与任何拆条，直接整条渲染（类型拆条/900字分段都不碰）
            if self._is_document_style(full_text):
                self._inject_section_dividers(result, full_text)
                return False
            if await self._send_split_by_type(result, event, full_text):
                return True
            if await self._send_md_split_sections(result, event, full_text):
                return True
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
        # [2026-08-27 03:08 优化①③] 句子级拆分包：超长无横线段按句标点二次拆，
        # 单块超 900 字硬切——拆到最后真正的"段"再判数量
        segments = [p for seg in segments for p in self._split_long_segment(seg)]
        if len(segments) <= 1:
            return False
        try:
            add_prefix = self._cfg("enable_mainagent_name_prefix", False)
            prefix_str = ""
            if add_prefix:
                prefix_str = f"【{self._cfg('main_agent_name', '普瑞赛斯')}】\n"
            sent_any = False
            fixed_iv = self.config.get("fragment_interval", None)
            for idx, seg_text in enumerate(segments):
                msg = seg_text
                if idx == 0 and prefix_str and not msg.startswith("【"):
                    msg = f"{prefix_str}{seg_text}"
                has_hr = any(_hr_line.match(l.strip()) for l in msg.split("\n") if l.strip())
                try:
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
                    sent_any = True
                except Exception as e:
                    # [2026-08-27 03:08 优化⑤] 段级容错：单段失败只 log 跳过，
                    # 不中断后续段；所有段都发不出（sent_any=False）才交还链兜底
                    logger.error(f"[inject_mainagent_prefix] 段{idx}发送失败，跳过: {e}")
                    continue
                # [2026-08-27 03:08 优化②] 动态步长：未显式配置 fragment_interval 时
                # 按字数算——短句快、长句慢，像真人打字；首段固定 0.1s 抢首屏
                if fixed_iv is not None:
                    iv = float(fixed_iv)
                elif idx == 0:
                    iv = 0.1
                else:
                    iv = min(0.15 + len(msg) * 0.006, 0.85)
                await asyncio.sleep(iv)
        except Exception as e:
            logger.error(f"[inject_mainagent_prefix] 分段发送失败: {e}")
            # [修复 2026-08-16] 分段已发出部分段时禁止 return False——那会触发
            # 框架兜底重发整条，造成重复（NapCat sendMsg 超时 retcode 1200 场景
            # 消息可能实际已送达）。仅当一段都未发出（异常发生在循环前）才保留链
            # 交给调用方整条兜底，此时重发无重复风险。
            if not sent_any:
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

    # ── 读空气·段五：主代理发言入册（复活 R1/R4 两条死规则）─────
    def _presence_mark_main(self, event: AstrMessageEvent, text: str = "") -> None:
        """记录「主代理刚说过话」到在场状态（读空气 R1/R4 的唯一数据来源）。

        [段五 2026-09-10] 根因：_presence_update 全库只在 dispatch 子代理转发后
        被调用一次，主代理回复从未入册 → MAIN_SPEAKER("__main__") 从未写入 ——
        R1（last_speaker 是主代理→倾向克制）恒为 False，R4（旧怨组密集互抛）
        c_main 恒为 0，两条规则写了但从未生效过。

        本方法挂在 on_decorating_result 出口（主代理消息发出前唯一必经点）补齐记录。
        异常一律吞掉：状态记录绝不能影响发送主流程（安全优先）。
        """
        try:
            updater = getattr(self, "_presence_update", None)
            if updater is None:  # 单模块测试场景（ArbitrationMixin 未混入）静默跳过
                return
            updater(event, MAIN_SPEAKER, "main", text or "")
        except Exception as e:
            logger.debug(f"[read_air] mark main presence failed (non-fatal): {e}")

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
        # [读空气·段五 2026-09-10] 主代理发言入册（复活 R1/R4，详见 _presence_mark_main）。
        # 必须在流式守卫之前——流式终态会 early return，那之后就没机会记了。
        # 主代理静默场景（分段转发已直发+链将被清空）不入册，避免污染 last_speaker。
        try:
            _pa_result = event.get_result()
            _pa_text = ""
            if _pa_result is not None and hasattr(_pa_result, "chain") and _pa_result.chain:
                _pa_text = " ".join(
                    getattr(_c, "text", "") or "" for _c in _pa_result.chain
                )
            _pa_stream = (
                _pa_result is not None
                and getattr(_pa_result, "result_content_type", None)
                == ResultContentType.STREAMING_FINISH
            )
            _pa_suppressed_silent = (
                getattr(self, "_suppress_mainagent_prefix", False)
                and not self._cfg("allow_mainagent_after_direct", True)
            )
            if not _pa_suppressed_silent and (_pa_text.strip() or _pa_stream):
                self._presence_mark_main(event, _pa_text)
        except Exception as _pa_e:
            logger.debug(f"[read_air] main presence hook skipped (non-fatal): {_pa_e}")
        # [流式守卫 2026-08-20] 流式终态必须让位：流式通道已把整段文本吐给用户，
        # 钩子此刻若再 event.send 分段重发，必然与流式已发内容重复/乱序。
        # 直接 return（既不 event.send 也不改 chain），把发送权完整交还流式通道。
        result = event.get_result()
        if (
            result is not None
            and getattr(result, "result_content_type", None) == ResultContentType.STREAMING_FINISH
        ):
            # [流式尾巴兜底 2026-08-21] 流式终态时即使文本已逐 token 吐给用户，
            # 只要处于"直发后 + 禁主代理补话"状态，仍清掉链上残留内容，防止调度余料被重复发送。
            if (
                getattr(self, "_suppress_mainagent_prefix", False)
                and not self._cfg("allow_mainagent_after_direct", True)
            ):
                try:
                    result.chain.clear()
                except Exception:
                    pass
            self._suppress_mainagent_prefix = False
            return

        # [标点哨兵 2026-08-27] 主代理出口兜底:流式终态已让位,此处拦下所有剩余链,
        # 全角化后再走分段/整条/前缀注入,保证 2026-08-27 标点铁律落地。
        result = event.get_result()
        if result is not None and hasattr(result, "chain") and result.chain:
            for comp in result.chain:
                text = getattr(comp, "text", None)
                if isinstance(text, str) and text.strip():
                    text = self._zh_fullwidth_sentinel(text)
                    # [2026-08-29] 表格分隔行强制居中(不依赖模型记忆)
                    text = self._force_table_center(text)
                    comp.text = text

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

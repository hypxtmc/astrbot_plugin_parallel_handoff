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
from astrbot.core.message.message_event_result import MessageChain

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
        # [主代理亲修 03:08] 剥离首部【名字】前缀，防止与下方 add_prefix 叠加成双前缀
        full_text = re.sub(r"^\s*【[^】]*】\s*\n?", "", full_text, count=1)
        segments = [s.strip() for s in full_text.split("\n\n") if s.strip()]
        if len(segments) <= 1:
            return False
        try:
            add_prefix = self._cfg("enable_mainagent_name_prefix", False)
            prefix_str = ""
            if add_prefix:
                prefix_str = f"【{self._cfg('main_agent_name', '主代理')}】\n"
            for idx, seg_text in enumerate(segments):
                msg = seg_text
                if idx == 0 and prefix_str and not msg.startswith("【"):
                    msg = f"{prefix_str}{seg_text}"
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
        """规范化子代理名称：兼容中文名（助手A->agent_a）和大小写（助手->agent_a）。

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
            agent_name: 主代理的名称（如 "助手A"、"助手B"）

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
        """
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
                main_agent_name = self._cfg("main_agent_name", "主代理")
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

        main_agent_name = self._cfg("main_agent_name", "主代理")
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

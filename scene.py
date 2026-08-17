"""scene.py — parallel_handoff 场景上下文 + 剧情基线构建（P0 拆模块）

对应原 main.py 的 577-603 行（_build_scene_context）+
829-841 行区域（parallel_handoff 内的场景/基线前缀构建，抽成 _build_scene_prefix）。

语义固定：基线注入对所有非黑名单子代理统一生效（direct 与 relay 一致，
回复发送方式不影响输入侧预处理）；黑名单子代理在 _call_one 入口 return，
碰不到这段代码。基线与场景解耦：场景段归 enable_scene_inject，基线段归
enable_baseline_inject，消费点 _apply_scene_prefix 只认 scene_prefix 非空。
"""
import logging
from astrbot.api.event import AstrMessageEvent

_logger = logging.getLogger("parallel_handoff.scene")


class SceneMixin:
    """场景上下文构建 + 共用剧情基线注入"""

    # ── 场景/基线前缀消费点（Bug#1 修复）────────────
    def _apply_scene_prefix(self, input_text: str, scene_prefix: str) -> str:
        """消费点：scene_prefix 非空即拼接到 input 前，与场景开关解耦。

        基线拼接原来被 enable_scene_inject 短路：场景开关一关，基线也
        跟着被吞。现在基线受 enable_baseline_inject 独立控制，消费点
        只认 scene_prefix 非空（博士 2026-08-17 实锤修复）。
        """
        if scene_prefix:
            _logger.debug("scene/基线前缀注入: %d chars -> %.40s...", len(scene_prefix), scene_prefix.replace("\n", " ")[:40])
            return f"{scene_prefix}\n\n{input_text}"
        _logger.debug("scene/基线前缀为空，原样透传 input")
        return input_text

    # ── 场景注入 ─────────────────────────────────────────────
    def _build_scene_context(self, event: AstrMessageEvent) -> str:
        """根据 event 构建当前场景上下文文本"""
        parts = []

        sender = event.get_sender_name()
        sender_id = event.get_sender_id()
        if sender and sender_id:
            parts.append(f"当前对话对象：{sender}（ID: {sender_id}）")
        elif sender:
            parts.append(f"当前对话对象：{sender}")

        msg_type = event.get_message_type()
        type_name = msg_type.value if hasattr(msg_type, "value") else str(msg_type)
        if "group" in type_name.lower():
            group_id = event.get_group_id()
            if group_id:
                parts.append(f"当前场景：群聊（群号 {group_id}）")
            else:
                parts.append("当前场景：群聊")
        elif "friend" in type_name.lower() or "private" in type_name.lower():
            parts.append("当前场景：私聊")

        if parts:
            parts.insert(0, "[场景信息]")
        return "\n".join(parts)

    def _build_scene_prefix(self, event: AstrMessageEvent, enable_scene_inject: bool) -> str:
        """构建场景 + 共用剧情基线前缀（parallel_handoff 内场景注入代码块抽出的方法）。

        enable_scene_inject 控制场景上下文；enable_baseline_inject 控制共用剧情基线
        （shared_scene_baseline 配置），基线与场景前缀拼接后统一注入 relay 子代理 input。
        """
        scene_prefix = ""
        if enable_scene_inject:
            scene_prefix = self._build_scene_context(event)
        # ── 共用剧情基线注入（所有子代理共享的世界设定锚点） ──
        if self._cfg("enable_baseline_inject", True):
            baseline = (self._cfg("shared_scene_baseline", "") or "").strip()
            if baseline:
                scene_prefix = (
                    f"{scene_prefix}\n【共用剧情场景基线】\n{baseline}\n"
                    if scene_prefix
                    else f"【共用剧情场景基线】\n{baseline}\n"
                )
        return scene_prefix

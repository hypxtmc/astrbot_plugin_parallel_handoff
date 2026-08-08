"""scene.py — parallel_handoff 场景上下文 + 剧情基线构建（P0 拆模块）

对应原 main.py 的 577-603 行（_build_scene_context）+
829-841 行区域（parallel_handoff 内的场景/基线前缀构建，抽成 _build_scene_prefix）。

语义固定：enable_baseline_inject 只对 relay 子代理生效（direct 子代理回复
直接发送不经主代理，基线注入对它们无意义）；黑名单（tech/技术Agent）在
_call_one 入口 return，碰不到这段代码。
"""
from astrbot.api.event import AstrMessageEvent


class SceneMixin:
    """场景上下文构建 + 共用剧情基线注入"""

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

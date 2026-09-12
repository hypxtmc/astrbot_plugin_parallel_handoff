"""scene.py — parallel_handoff 场景上下文构建（P0 拆模块）

对应原 main.py 的 577-603 行（_build_scene_context）+
829-841 行区域（parallel_handoff 内的场景前缀构建，抽成 _build_scene_prefix）。

语义固定：场景注入对所有非黑名单子代理统一生效（direct 与 relay 一致，
回复发送方式不影响输入侧预处理）；黑名单子代理在 _call_one 入口 return，
碰不到这段代码。场景段归 enable_scene_inject，消费点 _apply_scene_prefix
只认 scene_prefix 非空。

2026-09-12 群身份升级：群场景在"对话对象/场景"之外，追加对方群内身份
（群名片或昵称 + 角色：群主/管理员/群成员）。数据走通用
bot.call_action('get_group_member_info')，自给自足不依赖外部插件；
查询失败静默降级（群身份是增强项，绝不影响主流程）；带 5 分钟进程内缓存。
"""
import logging
import time
from astrbot.api.event import AstrMessageEvent

_logger = logging.getLogger("parallel_handoff.scene")

# 群成员身份缓存 {(group_id, user_id): (ts, text)}，进程内共享；TTL 5 分钟
_MEMBER_CACHE: dict = {}
_MEMBER_CACHE_TTL = 300.0
# 群角色中文映射（平台无关的惯用称谓）
_ROLE_CN = {"owner": "群主", "admin": "管理员", "member": "群成员"}


class SceneMixin:
    """场景上下文构建 + 共用剧情基线注入"""

    # ── 场景/基线前缀消费点（Bug#1 修复）────────────
    def _apply_scene_prefix(self, input_text: str, scene_prefix: str) -> str:
        """消费点：scene_prefix 非空即拼接到 input 前。"""
        if scene_prefix:
            _logger.debug("场景前缀注入: %d chars -> %.40s...", len(scene_prefix), scene_prefix.replace("\n", " ")[:40])
            return f"{scene_prefix}\n\n{input_text}"
        _logger.debug("场景前缀为空，原样透传 input")
        return input_text

    # ── 群身份（2026-09-12 新增）──────────
    async def _fetch_group_member_identity(self, event) -> str:
        """群场景拉取对方群内身份文本（群名片/昵称 + 角色）。

        自给自足：仅用通用 bot.call_action('get_group_member_info')，
        不依赖任何外部插件。任何异常静默降级返回空串；带 5 分钟缓存。
        """
        try:
            group_id = event.get_group_id()
            user_id = event.get_sender_id()
            if not group_id or not user_id:
                return ""
            key = (str(group_id), str(user_id))
            now = time.time()
            hit = _MEMBER_CACHE.get(key)
            if hit and (now - hit[0]) < _MEMBER_CACHE_TTL:
                return hit[1]
            bot = getattr(event, "bot", None)
            call_action = getattr(bot, "call_action", None)
            text = ""
            if callable(call_action):
                info = await call_action(
                    "get_group_member_info",
                    group_id=group_id,
                    user_id=user_id,
                    no_cache=False,
                )
                if isinstance(info, dict):
                    card = str(info.get("card") or "").strip()
                    nick = str(info.get("nickname") or "").strip()
                    role = str(info.get("role") or "").strip()
                    name = card or nick
                    role_cn = _ROLE_CN.get(role, "")
                    bits = [b for b in (name, role_cn) if b]
                    text = "，".join(bits)
            _MEMBER_CACHE[key] = (now, text)
            return text
        except Exception as exc:  # 群身份是增强项：任何失败都必须静默降级
            _logger.debug("群成员身份查询失败（已降级）: %s", exc)
            return ""

    async def _build_scene_context(self, event: AstrMessageEvent) -> str:
        """根据 event 构建当前场景上下文文本（群场景含群内身份）"""
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
            identity = await self._fetch_group_member_identity(event)
            if identity:
                parts.append(f"对方群内身份：{identity}")
        elif "friend" in type_name.lower() or "private" in type_name.lower():
            parts.append("当前场景：私聊")

        if parts:
            parts.insert(0, "[场景信息]")
        return "\n".join(parts)

    async def _build_scene_prefix(self, event: AstrMessageEvent, enable_scene_inject: bool) -> str:
        """构建场景前缀（parallel_handoff 内场景注入代码块抽出的方法）。

        enable_scene_inject 控制场景上下文，注入 relay 子代理 input。
        2026-09-12 async 化：群场景需要拉群成员身份。
        """
        scene_prefix = ""
        if enable_scene_inject:
            scene_prefix = await self._build_scene_context(event)
        return scene_prefix

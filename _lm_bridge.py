"""_lm_bridge.py — livingmemory 私有路径防腐层（2026-09-10 博士拍板落地）

背景：
    parallel_handoff 依赖 livingmemory 的记忆能力，但其中若干访问点走的是
    「私有路径」——带下划线的属性、内部对象链。livingmemory 一旦重构内部
    结构，这些路径当场断裂；而断裂点原本只被 try/except 静默吞掉，不报警，
    表现为「子代理突然失忆」且无从排查。

本模块把全部私有路径访问收敛到单一适配器，提供三层保护：
    1) 版本探测：优先 star 对象属性，回落 metadata.yaml
    2) 链式安全下钻：任一层缺失返回 None，绝不抛 AttributeError
    3) 一次性告警：每项能力首次缺失只 WARN 一次，附版本号，不刷屏

铁律（2026-09-04 博士红线）：
    不改 livingmemory 一行。本层只做「读取 + 探测 + 降级」，返回值与原裸访问
    完全一致：能力可用时返回同一对象，不可用时返回 None，调用方沿用既有
    降级分支 → 对上层零行为变更。

已知私有路径清单（livingmemory v2.6.1 实测）：
    initializer.is_initialized / is_failed
    event_handler._memory_recall.conversation_manager
    event_handler._memory_recall.message_utils
    command_handler.{conversation_manager,_memory_processor,memory_engine,config_manager}
    handle_memory_recall（公开 API，但仍做存在性探测）
"""
from __future__ import annotations

import os
from typing import Any, Optional, Tuple

__all__ = ["LivingMemoryBridge", "dig"]


def dig(obj: Any, *attrs: str) -> Any:
    """链式属性安全下钻：任一层为 None 或缺失即返回 None，不抛异常。

    与裸 `obj.a.b.c` 的区别仅在于「缺失不抛」，可用时返回同一对象，
    因此对既有调用方零行为变更。MagicMock 等鸭子对象同样适用。
    """
    cur = obj
    for attr in attrs:
        if cur is None:
            return None
        cur = getattr(cur, attr, None)
    return cur


class LivingMemoryBridge:
    """livingmemory 能力适配器（只读探测 + 一次性告警，无副作用）"""

    # ── 私有路径（主路径）──────────────────────────────────
    PATH_CONV = ("event_handler", "_memory_recall", "conversation_manager")
    PATH_MESSAGE_UTILS = ("event_handler", "_memory_recall", "message_utils")
    # ── 保命回退路径（主路径断裂时使用）─────────────────────
    FALLBACK_CONV = ("command_handler", "conversation_manager")
    FALLBACK_MESSAGE_UTILS = ("command_handler", "message_utils")

    # 提炼链路四件套（全齐才可提炼）
    REFLECT_PARTS = (
        ("conversation_manager", "conversation_manager"),
        ("_memory_processor", "_memory_processor"),
        ("memory_engine", "memory_engine"),
        ("config_manager", "config_manager"),
    )

    def __init__(self, plugin: Any, logger: Any = None, tag: str = "parallel_handoff"):
        self.plugin = plugin
        self._logger = logger
        self._tag = tag
        self._warned: set = set()
        self._version: Optional[str] = None

    # ── 告警：同 key 只发一次，避免每轮调用刷屏 ──────────────
    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        if self._logger is not None:
            self._logger.warning(f"[{self._tag}] {message}")

    # ── 版本探测 ────────────────────────────────────────────
    def version(self) -> str:
        """livingmemory 版本号；探测不到返回 "unknown"（不抛）。"""
        if self._version is not None:
            return self._version
        ver = ""
        for attr in ("version", "__version__"):
            raw = getattr(self.plugin, attr, None)
            if isinstance(raw, str) and raw.strip():
                ver = raw.strip()
                break
        if not ver:
            ver = self._version_from_metadata()
        self._version = ver or "unknown"
        return self._version

    def _version_from_metadata(self) -> str:
        """从 livingmemory 插件目录的 metadata.yaml 读 version。"""
        try:
            import inspect

            cls = self.plugin if isinstance(self.plugin, type) else type(self.plugin)
            meta = os.path.join(os.path.dirname(inspect.getfile(cls)), "metadata.yaml")
            if not os.path.isfile(meta):
                return ""
            with open(meta, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip().startswith("version:"):
                        return line.split(":", 1)[1].strip().strip("'\"")
        except Exception:
            return ""
        return ""

    # ── 健康检查 ────────────────────────────────────────────
    def health(self) -> Tuple[bool, str]:
        """(可用, 原因)。无 initializer 属性视为可用（旧版兼容，与旧行为一致）。"""
        initializer = getattr(self.plugin, "initializer", None)
        if initializer is None:
            return True, "no_initializer"
        if getattr(initializer, "is_failed", False):
            return False, "init_failed"
        if not getattr(initializer, "is_initialized", False):
            return False, "not_initialized"
        return True, "ok"

    # ── 能力获取 ────────────────────────────────────────────
    def conversation_manager(self) -> Any:
        """会话管理器（存储链路）。主路径断裂时回退 command_handler 并告警。"""
        obj = dig(self.plugin, *self.PATH_CONV)
        if obj is not None:
            return obj
        self._warn_once(
            "conv",
            f"livingmemory 私有路径 {'.'.join(self.PATH_CONV)} 不可用"
            f"（版本 {self.version()}），子代理记忆存储可能受影响",
        )
        fallback = dig(self.plugin, *self.FALLBACK_CONV)
        if fallback is not None:
            self._warn_once(
                "conv_fallback",
                "已回退 command_handler.conversation_manager 保命，"
                "请核对 livingmemory 是否重构了 event_handler 内部结构",
            )
        return fallback

    def message_utils(self) -> Any:
        """消息工具（消息数限流）。主路径断裂时回退并告警。"""
        obj = dig(self.plugin, *self.PATH_MESSAGE_UTILS)
        if obj is not None:
            return obj
        self._warn_once(
            "message_utils",
            f"livingmemory 私有路径 {'.'.join(self.PATH_MESSAGE_UTILS)} 不可用"
            f"（版本 {self.version()}），会话消息数限流将跳过",
        )
        return dig(self.plugin, *self.FALLBACK_MESSAGE_UTILS)

    def recall_api(self) -> Any:
        """召回入口 handle_memory_recall（公开 API，仍做存在性探测）。"""
        fn = getattr(self.plugin, "handle_memory_recall", None)
        if fn is None:
            self._warn_once(
                "recall_api",
                f"livingmemory 未提供 handle_memory_recall（版本 {self.version()}），"
                "子代理记忆召回已跳过",
            )
        return fn

    def reflection_kit(self) -> Optional[Tuple[Any, Any, Any, Any]]:
        """提炼链路四件套 (cm, mp, me, cfg)；任一缺失返回 None 并一次性告警。"""
        command_handler = getattr(self.plugin, "command_handler", None)
        if command_handler is None:
            self._warn_once(
                "command_handler",
                f"livingmemory 未提供 command_handler（版本 {self.version()}），"
                "子代理长期记忆提炼已跳过",
            )
            return None
        parts = {
            name: getattr(command_handler, attr, None)
            for name, attr in self.REFLECT_PARTS
        }
        missing = [name for name, obj in parts.items() if obj is None]
        if missing:
            self._warn_once(
                "reflect_missing:" + ",".join(sorted(missing)),
                f"livingmemory 提炼链路缺件 {missing}（版本 {self.version()}），"
                "子代理长期记忆提炼已跳过",
            )
            return None
        return (
            parts["conversation_manager"],
            parts["_memory_processor"],
            parts["memory_engine"],
            parts["config_manager"],
        )

    # ── 诊断（只读，不产生告警）─────────────────────────────
    def diagnose(self) -> dict:
        """当前能力矩阵，供日志排查/命令输出。纯探测，无副作用、无告警。"""
        command_handler = getattr(self.plugin, "command_handler", None)
        caps = {
            "recall": getattr(self.plugin, "handle_memory_recall", None) is not None,
            "conversation_manager": (
                dig(self.plugin, *self.PATH_CONV) is not None
                or dig(self.plugin, *self.FALLBACK_CONV) is not None
            ),
            "message_utils": (
                dig(self.plugin, *self.PATH_MESSAGE_UTILS) is not None
                or dig(self.plugin, *self.FALLBACK_MESSAGE_UTILS) is not None
            ),
            "reflect": command_handler is not None
            and all(
                getattr(command_handler, attr, None) is not None
                for _, attr in self.REFLECT_PARTS
            ),
        }
        healthy, detail = self.health()
        return {
            "version": self.version(),
            "healthy": healthy,
            "detail": detail,
            "capabilities": caps,
            "missing": [name for name, ok in caps.items() if not ok],
        }

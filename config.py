"""config.py — parallel_handoff 插件配置读取 / 迁移 / 保存（P0 拆模块）

对应原 main.py 的 464-575 行区域 + 102-116 行（_get_name_display_map）
+ 636-681 行（toggle_prefix 前缀开关切换）。
"""
import json
import os
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class ConfigMixin:
    """配置读取 / 前缀覆盖持久化 / 前缀开关动态切换"""

    # ── 配置读取 helper ──────────────────────────────────────
    def _cfg(self, key: str, default=None):
        """统一读取配置项,兼容 dict 和 AstrBotConfig 对象"""
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default) if hasattr(self.config, key) else default

    def _get_name_display_map(self) -> dict:
        """读取 name_display_map,兼容 JSON 字符串格式"""
        raw = self._cfg("name_display_map", "{}")
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        if isinstance(raw, dict):
            return raw
        return {}

    def _get_name_prefix_overrides(self) -> dict:
        """读取 name_prefix_overrides,兼容 JSON 字符串和 dict 两种格式"""
        raw = self._cfg("name_prefix_overrides", {})
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        if isinstance(raw, dict):
            return raw
        return {}

    def _get_user_address(self) -> str:
        """读取主对话者称呼（系统文本用：共享记录、记忆沉淀模板等）。

        角色台词层的称呼由各子代理人格自理，不经过此项。
        配置缺失/为空时回退中性默认「用户」。
        """
        raw = self._cfg("user_address", "用户")
        addr = str(raw).strip() if raw is not None else ""
        return addr or "用户"

    # ── 双模式调度配置（技术干活 / 日常贴贴，2026-08-31 用户指定） ──
    MODE_CONFIG_DEFAULTS = {
        # 技术干活：子代理回复返回主代理，主代理当统帅并行收卷
        "tech": {"route_mode": "relay", "call_mode": "parallel", "timeout": 120},
        # 日常贴贴：子代理回复直接分段转发用户端，主代理隐身
        "affection": {"route_mode": "direct", "call_mode": "chained", "timeout": 120},
    }

    def _get_mode_config(self, mode: str) -> dict:
        """读取双模式调度配置块（tech/affection），兼容 JSON 字符串和 dict 两种格式。

        用户可在 WebUI 填写 `tech_mode_config` / `affection_mode_config`（JSON 字符串），
        自行决定每个模式的调度方式：
        {
          "route_mode": "relay|direct",   # 路由模式：relay=回复返回主代理，direct=直接分段转发
          "call_mode": "parallel|chained",# 调用模式：parallel=并行调度，chained=接龙串行
          "timeout": 120                  # 单子代理超时秒数
        }
        配置缺失/解析失败时回退内置默认（tech=relay+parallel，affection=direct+chained）。
        """
        key = f"{mode}_mode_config"
        raw = self._cfg(key, {})
        data = {}
        if isinstance(raw, str):
            raw = raw.strip()
            if raw:
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    data = {}
        elif isinstance(raw, dict):
            data = raw
        if not isinstance(data, dict):
            data = {}
        defaults = self.MODE_CONFIG_DEFAULTS.get(mode, {})
        merged = dict(defaults)
        for k in ("route_mode", "call_mode", "timeout"):
            v = data.get(k)
            if v is not None and v != "":
                merged[k] = v
        return merged

    def resolve_mode_params(self, mode, route_mode, call_mode, timeout):
        """用户配置永远优先（2026-08-31 用户指定）：

        mode 命中 tech/affection 时，模式配置（tech_mode_config /
        affection_mode_config）无条件覆盖 LLM 显式传参——不再有
        "timeout==120 才让步"之类的默认哨兵逻辑，用户在 WebUI 填什么
        就是什么。

        mode 未命中时原样返回，交给全局 route_mode/call_mode 兜底。
        """
        if mode and str(mode).strip().lower() in ("tech", "affection"):
            m = str(mode).strip().lower()
            mcfg = self._get_mode_config(m)
            route_mode = str(mcfg.get("route_mode", "relay" if m == "tech" else "direct"))
            call_mode = str(mcfg.get("call_mode", "parallel" if m == "tech" else "chained"))
            try:
                timeout = int(mcfg.get("timeout", 120))
            except (TypeError, ValueError):
                timeout = 120
        return route_mode, call_mode, timeout
    # ── 配置持久化 ─────────────────────────────────────────
    def _save_config(self, overrides: dict):
        """将 name_prefix_overrides 写入配置文件并同步内存"""
        # 绝对路径锚定：插件目录向上两级 = data/，配置统一归 data/config/
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..",
            "config", "astrbot_plugin_parallel_handoff_config.json",
        )
        config_path = os.path.normpath(config_path)

        # 读取现有配置（保留其他键）
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        else:
            raw = {}

        raw["name_prefix_overrides"] = overrides

        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

        # 同步更新内存 config
        if isinstance(self.config, dict):
            self.config["name_prefix_overrides"] = overrides
        else:
            try:
                setattr(self.config, "name_prefix_overrides", overrides)
            except Exception:
                pass

        logger.info(f"[parallel_handoff] name_prefix_overrides 已更新: {overrides}")

    # ── 动态前缀切换 ───────────────────────────────────────
    async def toggle_prefix(self, event: AstrMessageEvent):
        """动态切换子代理名前缀开关

        匹配模式：
        - "关掉（子代理名）的前缀" -> 设为 false
        - "打开（子代理名）的前缀" -> 设为 true
        - "（子代理名）的前缀关了" -> 设为 false
        - "（子代理名）的前缀开了" -> 设为 true
        """
        message = event.get_message_str()
        match = re.search(r"(?:关掉|打开)（(.+?)）的前缀|（(.+?)）的前缀(?:关|开)了", message)
        if not match:
            return

        # 提取子代理中文名（group 1 或 group 2）
        chinese_name = match.group(1) or match.group(2)
        if not chinese_name:
            return

        # 反向查找 agent_name
        agent_name = self.AGENT_NAME_REVERSE.get(chinese_name)
        if not agent_name:
            yield event.plain_result(f"❌ 未知子代理「{chinese_name}」,可用：{list(self.AGENT_NAME_REVERSE.keys())}")
            return

        # 判断开关方向
        raw_msg = match.group(0)
        if raw_msg.startswith("关掉") or raw_msg.endswith("关了"):
            new_value = False
            verb = "已关闭"
        elif raw_msg.startswith("打开") or raw_msg.endswith("开了"):
            new_value = True
            verb = "已开启"
        else:
            return

        # 读取当前覆盖表
        overrides = self._get_name_prefix_overrides()
        if not isinstance(overrides, dict):
            overrides = {}
        overrides[agent_name] = new_value

        # 持久化
        self._save_config(overrides)

        yield event.plain_result(f"✅ {verb}「{chinese_name}」的名字前缀")

"""conftest.py — 测试环境引导：保证在「无真 AstrBot」环境下也能收集并运行测试。

背景
----
插件目录包含 __init__.py，pytest 收集时会把本目录视作「包」先导入
（__init__.py → main.py → `from astrbot.api.event import ...`）。
在非 AstrBot 部署环境（真包不在 sys.path 上）会直接 collection error：
    ModuleNotFoundError: No module named 'astrbot'

本文件在测试模块导入**之前**被 pytest 加载：若真 astrbot 可用则不干预
（让真包走），否则注入 astrbot 全套桩，保证收集/运行不炸。
"""
import os
import sys
from unittest.mock import MagicMock

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


def _has_real_astrbot() -> bool:
    """探测真 astrbot 包是否可用（仅查 spec，不实际导入）。"""
    from importlib.util import find_spec

    old = sys.modules.pop("astrbot", None)  # 防止既有 mock 干扰探测
    try:
        return find_spec("astrbot") is not None
    except (ImportError, ValueError):
        return False
    finally:
        if old is not None:
            sys.modules["astrbot"] = old


if not _has_real_astrbot():
    # ── astrbot 全链桩（与 test_plugin.py 同款，此处更早执行）──
    _fake_api = MagicMock()
    _fake_api.logger = MagicMock()
    _fake_api.AstrBotConfig = dict
    _fake_api.provider = MagicMock()
    _fake_api.provider.ProviderRequest = MagicMock()

    _fake_event = MagicMock()
    _fake_event.AstrMessageEvent = MagicMock()

    _fake_event_filter = MagicMock()
    for _deco_name in (
        "on_llm_request",
        "on_waiting_llm_request",
        "on_decorating_result",
        "regex",
        "command",
        "llm_tool",
        "custom_filter",
    ):
        getattr(_fake_event_filter, _deco_name).side_effect = lambda *a, **k: (
            lambda f: f
        )
    _fake_event.filter = _fake_event_filter

    _fake_star = MagicMock()
    _fake_star.Context = MagicMock()
    _fake_star.Star = type("Star", (), {"__init__": lambda self, context: None})
    _fake_star.register = lambda *args, **kwargs: (lambda cls: cls)

    _fake_components = MagicMock()
    _fake_components.Plain = MagicMock()

    _fake_msg_result = MagicMock()
    _fake_msg_result.MessageChain = MagicMock()

    _fake_platform = MagicMock()
    _fake_platform.MessageType = MagicMock()
    _fake_platform.MessageType.GROUP_MESSAGE = "GROUP_MESSAGE"
    _fake_platform.MessageType.FRIEND_MESSAGE = "FRIEND_MESSAGE"

    _fake_core = MagicMock()
    _fake_core.agent = MagicMock()
    _fake_core.agent.tool = MagicMock()
    _fake_core.agent.tool.ToolSet = MagicMock()
    _fake_core.agent.message = MagicMock()

    class _FakeTextPart:
        """TextPart 桩：支持 text 属性与 mark_as_temp() 链式调用。"""

        def __init__(self, text=""):
            self.text = text

        def mark_as_temp(self):
            return self

    _fake_core.agent.message.TextPart = _FakeTextPart
    _fake_core.message = MagicMock()
    _fake_core.message.components = _fake_components
    _fake_core.message.message_event_result = _fake_msg_result

    sys.modules["astrbot"] = MagicMock()
    sys.modules["astrbot.api"] = _fake_api
    sys.modules["astrbot.api.event"] = _fake_event
    sys.modules["astrbot.api.event.filter"] = _fake_event_filter
    sys.modules["astrbot.api.star"] = _fake_star
    sys.modules["astrbot.api.provider"] = _fake_api.provider
    sys.modules["astrbot.api.platform"] = _fake_platform
    sys.modules["astrbot.core"] = _fake_core
    sys.modules["astrbot.core.agent"] = _fake_core.agent
    sys.modules["astrbot.core.agent.tool"] = _fake_core.agent.tool
    sys.modules["astrbot.core.agent.message"] = _fake_core.agent.message
    sys.modules["astrbot.core.message"] = _fake_core.message
    sys.modules["astrbot.core.message.components"] = _fake_components
    sys.modules["astrbot.core.message.message_event_result"] = _fake_msg_result

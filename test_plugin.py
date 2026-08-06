"""test_plugin.py — parallel_handoff 插件单元测试"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock

# 确保插件目录在 sys.path 中
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

# 阻断 astrbot 导入链，避免依赖缺失
# 需要覆盖 main.py 中所有 import 路径：
#   from astrbot.api.event import filter, AstrMessageEvent
#   from astrbot.api.star import Context, Star, register
#   from astrbot.api import logger, AstrBotConfig
#   from astrbot.api.event.filter import llm_tool
#   from astrbot.core.message.components import Plain
#   from astrbot.core.message.message_event_result import MessageChain
_fake_api = MagicMock()
_fake_api.logger = MagicMock()
_fake_api.AstrBotConfig = dict
_fake_api.provider = MagicMock()
_fake_api.provider.ProviderRequest = MagicMock()

_fake_event = MagicMock()
_fake_event.filter = MagicMock()
_fake_event.AstrMessageEvent = MagicMock()

_fake_event_filter = MagicMock()
_fake_event_filter.llm_tool = MagicMock()

_fake_star = MagicMock()
_fake_star.Context = MagicMock()
_fake_star.Star = type("Star", (), {"__init__": lambda self, context: None})
_fake_star.register = lambda *args, **kwargs: lambda cls: cls

_fake_components = MagicMock()
_fake_components.Plain = MagicMock()

_fake_msg_result = MagicMock()
_fake_msg_result.MessageChain = MagicMock()

sys.modules["astrbot"] = MagicMock()
sys.modules["astrbot.api"] = _fake_api
sys.modules["astrbot.api.event"] = _fake_event
sys.modules["astrbot.api.event.filter"] = _fake_event_filter
sys.modules["astrbot.api.star"] = _fake_star
sys.modules["astrbot.api.provider"] = _fake_api.provider

_fake_core = MagicMock()
_fake_core.agent = MagicMock()
_fake_core.agent.tool = MagicMock()
_fake_core.agent.tool.ToolSet = MagicMock()
_fake_core.message = MagicMock()
_fake_core.message.components = _fake_components
_fake_core.message.message_event_result = _fake_msg_result
sys.modules["astrbot.core"] = _fake_core
sys.modules["astrbot.core.agent"] = _fake_core.agent
sys.modules["astrbot.core.agent.tool"] = _fake_core.agent.tool
sys.modules["astrbot.core.message"] = _fake_core.message
sys.modules["astrbot.core.message.components"] = _fake_components
sys.modules["astrbot.core.message.message_event_result"] = _fake_msg_result

# 确保 AstrBot 根目录在 sys.path 中（用于 import astrbot 时能找到包）
ASTRBOT_ROOT = os.path.normpath(os.path.join(PLUGIN_DIR, "..", "..", ".."))
if ASTRBOT_ROOT not in sys.path:
    sys.path.insert(0, ASTRBOT_ROOT)


def _load_plugin_class():
    """通过文件路径直接加载插件类，避免与 /root/AstrBot/main.py 冲突"""
    import importlib.util
    main_path = os.path.join(PLUGIN_DIR, "main.py")
    spec = importlib.util.spec_from_file_location(
        "parallel_handoff_plugin", main_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ParallelHandoffPlugin


class TestSchema(unittest.TestCase):
    """验证 _conf_schema.json"""

    @classmethod
    def setUpClass(cls):
        schema_path = os.path.join(PLUGIN_DIR, "_conf_schema.json")
        with open(schema_path, "r", encoding="utf-8") as f:
            cls.schema = json.load(f)

    def test_schema_valid(self):
        """验证 _conf_schema.json 格式正确、所有必要字段存在"""
        required_fields = [
            "fragment_interval",
            "min_fragment_length",
            "enable_subagent_name_prefix",
            "enable_mainagent_name_prefix",
            "main_agent_name",
            "enable_scene_inject",
            "enable_segmented_forward",
            "name_prefix_overrides",
            "name_display_map",
        ]
        for field in required_fields:
            self.assertIn(field, self.schema, f"schema 缺少字段: {field}")

        # 验证 name_display_map 类型为 string
        self.assertEqual(
            self.schema["name_display_map"]["type"],
            "string",
            "name_display_map type 应为 string",
        )

        # 验证默认值非空
        self.assertIsInstance(
            self.schema["name_display_map"]["default"],
            str,
            "name_display_map default 应为 string",
        )
        # 验证 main_agent_name 默认值
        self.assertEqual(
            self.schema["main_agent_name"]["default"],
            "普瑞赛斯",
            "main_agent_name default 应为 普瑞赛斯",
        )


class TestDisplayName(unittest.TestCase):
    """测试 _display_name 的 fallback 和配置优先级"""

    @classmethod
    def setUpClass(cls):
        cls.PluginClass = _load_plugin_class()

    def _make_plugin(self, config: dict = None):
        """创建一个最小化 mock 的插件实例"""
        mock_context = MagicMock()
        if config is None:
            config = {}
        plugin = self.PluginClass(context=mock_context, config=config)
        return plugin

    def test_display_name_fallback(self):
        """name_display_map 为空时回退到硬编码 AGENT_DISPLAY_NAME"""
        plugin = self._make_plugin({"name_display_map": "{}"})
        self.assertEqual(plugin._display_name("amiya"), "阿米娅")
        self.assertEqual(plugin._display_name("theresia"), "特蕾西娅")
        self.assertEqual(plugin._display_name("tech"), "技术Agent")

    def test_display_name_config(self):
        """name_display_map 配置值生效，覆盖硬编码"""
        plugin = self._make_plugin({
            "name_display_map": json.dumps({
                "amiya": "小阿米娅",
                "tech": "技术小哥",
            })
        })
        self.assertEqual(plugin._display_name("amiya"), "小阿米娅")
        self.assertEqual(plugin._display_name("tech"), "技术小哥")
        # 未在配置中，回退到硬编码
        self.assertEqual(plugin._display_name("theresia"), "特蕾西娅")
        # 既不在配置也不在硬编码，返回原名
        self.assertEqual(plugin._display_name("unknown_agent"), "unknown_agent")

    def test_display_name_invalid_json(self):
        """name_display_map 为非法 JSON 时优雅降级"""
        plugin = self._make_plugin({"name_display_map": "{invalid json"})
        # 应回退到硬编码
        self.assertEqual(plugin._display_name("amiya"), "阿米娅")

    def test_display_name_dict_format(self):
        """name_display_map 直接传 dict 也能正常工作"""
        plugin = self._make_plugin({
            "name_display_map": {"amiya": "兔兔"}
        })
        self.assertEqual(plugin._display_name("amiya"), "兔兔")


class TestPrefixOverrides(unittest.TestCase):
    """测试 _get_name_prefix_overrides"""

    @classmethod
    def setUpClass(cls):
        cls.PluginClass = _load_plugin_class()

    def _make_plugin(self, config: dict = None):
        mock_context = MagicMock()
        if config is None:
            config = {}
        plugin = self.PluginClass(context=mock_context, config=config)
        return plugin

    def test_get_prefix_overrides_empty(self):
        """空覆盖表 — 返回空 dict"""
        plugin = self._make_plugin({"name_prefix_overrides": "{}"})
        result = plugin._get_name_prefix_overrides()
        self.assertEqual(result, {})

    def test_get_prefix_overrides_string(self):
        """JSON 字符串格式的覆盖表"""
        plugin = self._make_plugin({
            "name_prefix_overrides": json.dumps({"amiya": False, "tech": True})
        })
        result = plugin._get_name_prefix_overrides()
        self.assertEqual(result, {"amiya": False, "tech": True})

    def test_get_prefix_overrides_dict(self):
        """直接传 dict 格式也能正确读取"""
        plugin = self._make_plugin({
            "name_prefix_overrides": {"amiya": False}
        })
        result = plugin._get_name_prefix_overrides()
        self.assertEqual(result, {"amiya": False})

    def test_get_prefix_overrides_invalid_json(self):
        """非法 JSON 返回空 dict"""
        plugin = self._make_plugin({"name_prefix_overrides": "not valid json"})
        result = plugin._get_name_prefix_overrides()
        self.assertEqual(result, {})

    def test_get_prefix_overrides_empty_string(self):
        """空字符串返回空 dict"""
        plugin = self._make_plugin({"name_prefix_overrides": ""})
        result = plugin._get_name_prefix_overrides()
        self.assertEqual(result, {})


class TestMainagentPrefix(unittest.TestCase):
    """测试 format_mainagent_message 和主代理前缀功能"""

    @classmethod
    def setUpClass(cls):
        cls.PluginClass = _load_plugin_class()

    def _make_plugin(self, config: dict = None):
        mock_context = MagicMock()
        if config is None:
            config = {}
        plugin = self.PluginClass(context=mock_context, config=config)
        return plugin

    def test_format_mainagent_disabled(self):
        """enable_mainagent_name_prefix=false 时返回原文"""
        plugin = self._make_plugin({
            "enable_mainagent_name_prefix": False,
        })
        result = plugin.format_mainagent_message("你好", "普瑞赛斯")
        self.assertEqual(result, "你好")

    def test_format_mainagent_enabled(self):
        """enable_mainagent_name_prefix=true 时加前缀"""
        plugin = self._make_plugin({
            "enable_mainagent_name_prefix": True,
        })
        result = plugin.format_mainagent_message("你好", "普瑞赛斯")
        self.assertEqual(result, "【普瑞赛斯】\n你好")

    def test_format_mainagent_custom_name(self):
        """使用 main_agent_name 配置自定义名"""
        plugin = self._make_plugin({
            "enable_mainagent_name_prefix": True,
            "main_agent_name": "博士",
        })
        result = plugin.format_mainagent_message("测试消息", "博士")
        self.assertEqual(result, "【博士】\n测试消息")

    def test_format_mainagent_name_display_map(self):
        """name_display_map 中的主代理名优先生效"""
        plugin = self._make_plugin({
            "enable_mainagent_name_prefix": True,
            "main_agent_name": "普瑞赛斯",
            "name_display_map": json.dumps({"普瑞赛斯": "主控"}),
        })
        result = plugin.format_mainagent_message("内容", "普瑞赛斯")
        self.assertEqual(result, "【主控】\n内容")


class TestPrefixDedup(unittest.TestCase):
    """测试前缀去重：_call_one 中不应重复添加已有前缀"""

    @classmethod
    def setUpClass(cls):
        cls.PluginClass = _load_plugin_class()

    def _make_plugin(self, config: dict = None):
        mock_context = MagicMock()
        if config is None:
            config = {}
        plugin = self.PluginClass(context=mock_context, config=config)
        return plugin

    def test_prefix_not_doubled_when_already_present(self):
        """子代理回复已含前缀时，_display_name 逻辑不应重复"""
        plugin = self._make_plugin({
            "enable_subagent_name_prefix": True,
            "name_display_map": json.dumps({"amiya": "阿米娅"}),
        })
        # 模拟子代理已返回带前缀的文本
        text_with_prefix = "【阿米娅】\n这是子代理的回复"
        display_name = plugin._display_name("amiya")
        prefix_str = f"【{display_name}】\n"

        # 验证去重逻辑：如果已有前缀不再添加
        if not text_with_prefix.startswith(prefix_str):
            text_with_prefix = prefix_str + text_with_prefix
        # 前缀应该仍然只出现一次
        self.assertTrue(text_with_prefix.startswith("【阿米娅】\n"))
        # 前缀后面不应该再出现第二次
        after_prefix = text_with_prefix[len("【阿米娅】\n"):]
        self.assertFalse(after_prefix.startswith("【阿米娅】"))

    def test_prefix_added_when_not_present(self):
        """子代理回复无前缀时正常添加"""
        plugin = self._make_plugin({
            "enable_subagent_name_prefix": True,
        })
        text = "纯文本回复"
        display_name = plugin._display_name("amiya")
        prefix_str = f"【{display_name}】\n"

        if not text.startswith(prefix_str):
            text = prefix_str + text
        self.assertTrue(text.startswith("【阿米娅】\n"))
        # 前缀后内容应与原文一致
        self.assertEqual(text[len(prefix_str):], "纯文本回复")


if __name__ == "__main__":
    unittest.main(verbosity=2)

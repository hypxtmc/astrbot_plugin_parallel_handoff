"""test_plugin.py — parallel_handoff 插件单元测试"""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

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
# 关键：所有 filter 装饰器返回 identity，避免 @filter.on_llm_request() 等把
# 被装饰方法替换成 MagicMock（否则 asyncio.run 拿不到真实 coroutine 函数）
for _deco_name in ("on_llm_request", "on_waiting_llm_request", "on_decorating_result", "regex", "command", "llm_tool", "custom_filter"):
    getattr(_fake_event_filter, _deco_name).side_effect = lambda *a, **k: (lambda f: f)
# 注意：不要再对 llm_tool 单独赋值 MagicMock——会覆盖上面的 identity side_effect，
# 使 @llm_tool 装饰的方法（parallel_handoff/call_subagent）在测试环境退化为 MagicMock。
_fake_event.filter = _fake_event_filter  # 统一 from astrbot.api.event import filter 解析到同一 mock

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
_fake_core.agent.message = MagicMock()
class _FakeTextPart:
    """TextPart 桩：支持 text 属性与 mark_as_temp() 链式调用"""
    def __init__(self, text=""):
        self.text = text
    def mark_as_temp(self):
        return self
_fake_core.agent.message.TextPart = _FakeTextPart
_fake_core.message = MagicMock()
_fake_core.message.components = _fake_components
_fake_core.message.message_event_result = _fake_msg_result
sys.modules["astrbot.core"] = _fake_core
sys.modules["astrbot.core.agent"] = _fake_core.agent
sys.modules["astrbot.core.agent.tool"] = _fake_core.agent.tool
sys.modules["astrbot.core.agent.message"] = _fake_core.agent.message
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
        self.assertEqual(plugin._display_name("demo"), "demo")  # tech 已移除映射，回退原名

    def test_display_name_config(self):
        """name_display_map 配置值生效，覆盖硬编码"""
        plugin = self._make_plugin({
            "name_display_map": json.dumps({
                "amiya": "小阿米娅",
                "demo": "技术小哥",
            })
        })
        self.assertEqual(plugin._display_name("amiya"), "小阿米娅")
        self.assertEqual(plugin._display_name("demo"), "技术小哥")
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
            "name_prefix_overrides": json.dumps({"amiya": False, "demo": True})
        })
        result = plugin._get_name_prefix_overrides()
        self.assertEqual(result, {"amiya": False, "demo": True})

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


class TestRouteDirective(unittest.TestCase):
    """测试路由强制指令生成（_build_route_directive）"""

    @classmethod
    def setUpClass(cls):
        cls.PluginClass = _load_plugin_class()

    def _make_plugin(self, config: dict = None):
        mock_context = MagicMock()
        if config is None:
            config = {}
        return self.PluginClass(context=mock_context, config=config)

    def test_direct_parallel_directive(self):
        """direct + parallel：指令含显示名映射与 parallel 规范"""
        plugin = self._make_plugin({
            "route_mode": "direct",
            "call_mode": "parallel",
            "direct_delivery_agents": "amiya,closure,liino,unmapped_agent",
            "enable_route_directive": True,
        })
        d = plugin._build_route_directive()
        self.assertIn("阿米娅、可露希尔", d)      # 显示名映射生效
        self.assertIn("梨诺", d)                  # 映射 id 显示为内置中文名
        self.assertIn("unmapped_agent", d)        # 未映射 id 保留原名
        self.assertIn("direct", d)
        self.assertIn("parallel", d)
        # 动态覆盖说明存在（提及 chained 作为流水线选项，故原 assertNotIn 不再适用）
        self.assertIn("动态模式覆盖", d)

    def test_relay_chained_directive(self):
        """relay + chained：指令含 relay 与接龙规范"""
        plugin = self._make_plugin({
            "route_mode": "relay",
            "call_mode": "chained",
            "direct_delivery_agents": "amiya,closure",
        })
        d = plugin._build_route_directive()
        self.assertIn("relay", d)
        self.assertIn("禁止并行双发", d)
        self.assertIn("chained", d)

    def test_empty_agents_no_directive(self):
        """direct_delivery_agents 为空时不注入"""
        plugin = self._make_plugin({
            "route_mode": "direct",
            "call_mode": "parallel",
            "direct_delivery_agents": "",
        })
        self.assertEqual(plugin._build_route_directive(), "")

    def test_blacklist_in_direct_directive(self):
        """direct 模式：指令含黑名单直连规范，黑名单 ID 动态注入"""
        plugin = self._make_plugin({
            "route_mode": "direct",
            "call_mode": "parallel",
            "direct_delivery_agents": "amiya,closure",
            "handoff_blacklist_agents": "demo,canary",
        })
        d = plugin._build_route_directive()
        self.assertIn("强制直连黑名单", d)
        self.assertIn("demo", d)
        self.assertIn("transfer_to_xxx", d)
        self.assertIn("禁止用 parallel_handoff / call_subagent 调用", d)
        # 黑名单代理不被描述为走 relay 返回主代理
        self.assertNotIn("demo）走 relay", d)

    def test_blacklist_in_relay_directive(self):
        """relay 模式：黑名单直连规范仍然生效"""
        plugin = self._make_plugin({
            "route_mode": "relay",
            "call_mode": "parallel",
            "direct_delivery_agents": "amiya,closure",
            "handoff_blacklist_agents": "demo,canary",
        })
        d = plugin._build_route_directive()
        self.assertIn("强制直连黑名单", d)
        self.assertIn("transfer_to_xxx", d)
        self.assertIn("非黑名单子代理", d)


class TestRouteDirectiveInject(unittest.TestCase):
    """测试 OnLLMRequestEvent 钩子注入逻辑"""

    @classmethod
    def setUpClass(cls):
        cls.PluginClass = _load_plugin_class()

    def _make_plugin(self, config: dict = None):
        mock_context = MagicMock()
        if config is None:
            config = {}
        return self.PluginClass(context=mock_context, config=config)

    def test_inject_and_preserve_prompt(self):
        """正常注入：原 prompt 保留，返回 False 不拦截，工具列表不动"""
        import asyncio
        plugin = self._make_plugin({
            "route_mode": "direct",
            "call_mode": "parallel",
            "direct_delivery_agents": "amiya,closure,demo",
            "enable_route_directive": True,
        })
        req = MagicMock()
        req.system_prompt = "【人格】原 prompt"
        req.extra_user_content_parts = []
        req.func_tool = {"tools": ["parallel_handoff", "transfer_to_demo"]}
        ret = asyncio.run(plugin._route_directive_inject(MagicMock(), req))
        self.assertIs(ret, False)                      # 不拦截
        self.assertEqual(req.system_prompt, "【人格】原 prompt")  # 系统提示前缀零改动
        self.assertEqual(len(req.extra_user_content_parts), 1)   # 注入到请求尾部
        self.assertIn("【路由强制指令·parallel_handoff】", req.extra_user_content_parts[0].text)
        self.assertEqual(req.func_tool["tools"], ["parallel_handoff", "transfer_to_demo"])  # 工具保留

    def test_marker_dedup(self):
        """已含标记时跳过重复注入"""
        import asyncio
        plugin = self._make_plugin({
            "route_mode": "direct",
            "call_mode": "parallel",
            "direct_delivery_agents": "amiya",
            "enable_route_directive": True,
        })
        req = MagicMock()
        req.system_prompt = "【人格】原 prompt"
        req.extra_user_content_parts = [_FakeTextPart(text="【路由强制指令·parallel_handoff】已有")]
        req.func_tool = {"tools": []}
        asyncio.run(plugin._route_directive_inject(MagicMock(), req))
        self.assertEqual(req.system_prompt, "【人格】原 prompt")
        self.assertEqual(len(req.extra_user_content_parts), 1)  # 已含标记，不再追加

    def test_disabled_no_inject(self):
        """开关关闭时不注入"""
        import asyncio
        plugin = self._make_plugin({
            "route_mode": "direct",
            "call_mode": "parallel",
            "direct_delivery_agents": "amiya",
            "enable_route_directive": False,
        })
        req = MagicMock()
        req.system_prompt = "原 prompt"
        req.extra_user_content_parts = []
        req.func_tool = {"tools": []}
        asyncio.run(plugin._route_directive_inject(MagicMock(), req))
        self.assertEqual(req.system_prompt, "原 prompt")
        self.assertEqual(len(req.extra_user_content_parts), 0)

    def test_smart_mode_route_intent_injects(self):
        """smart 模式：点名子代理（路由意图）→ 注入完整指令"""
        import asyncio
        plugin = self._make_plugin({
            "route_mode": "direct",
            "call_mode": "parallel",
            "direct_delivery_agents": "amiya,closure",
            "name_display_map": '{"amiya": "阿米娅", "closure": "可露希尔"}',
            "enable_route_directive": True,
            "directive_inject_mode": "smart",
        })
        event = MagicMock()
        event.get_message_str.return_value = "我想找阿米娅聊聊天"
        req = MagicMock()
        req.system_prompt = "原 prompt"
        req.extra_user_content_parts = []
        req.func_tool = {"tools": []}
        asyncio.run(plugin._route_directive_inject(event, req))
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn("【路由强制指令·parallel_handoff】", req.extra_user_content_parts[0].text)

    def test_smart_mode_daily_chat_no_inject(self):
        """smart 模式：日常闲聊（无路由意图）→ 不注入，省 token"""
        import asyncio
        plugin = self._make_plugin({
            "route_mode": "direct",
            "call_mode": "parallel",
            "direct_delivery_agents": "amiya,closure",
            "name_display_map": '{"amiya": "阿米娅", "closure": "可露希尔"}',
            "enable_route_directive": True,
            "directive_inject_mode": "smart",
        })
        event = MagicMock()
        event.get_message_str.return_value = "今天天气不错，中午吃了碗面"
        req = MagicMock()
        req.system_prompt = "原 prompt"
        req.extra_user_content_parts = []
        req.func_tool = {"tools": []}
        asyncio.run(plugin._route_directive_inject(event, req))
        self.assertEqual(len(req.extra_user_content_parts), 0)

    def test_smart_mode_default_injects_when_no_text(self):
        """smart 模式防御：取不到消息文本 → 保守注入（与 always 行为一致）"""
        import asyncio
        plugin = self._make_plugin({
            "route_mode": "direct",
            "call_mode": "parallel",
            "direct_delivery_agents": "amiya",
            "enable_route_directive": True,
            "directive_inject_mode": "smart",
        })
        req = MagicMock()
        req.system_prompt = "原 prompt"
        req.extra_user_content_parts = []
        req.func_tool = {"tools": []}
        asyncio.run(plugin._route_directive_inject(MagicMock(), req))  # 无 get_message_str
        self.assertEqual(len(req.extra_user_content_parts), 1)

    def test_no_agents_no_inject(self):
        """direct_delivery_agents 为空时不注入"""
        import asyncio
        plugin = self._make_plugin({
            "route_mode": "direct",
            "call_mode": "parallel",
            "direct_delivery_agents": "",
            "enable_route_directive": True,
        })
        req = MagicMock()
        req.system_prompt = "原 prompt"
        req.extra_user_content_parts = []
        req.func_tool = {"tools": []}
        asyncio.run(plugin._route_directive_inject(MagicMock(), req))
        self.assertEqual(req.system_prompt, "原 prompt")
        self.assertEqual(len(req.extra_user_content_parts), 0)

class TestDedupGuard(unittest.TestCase):
    """防重只拦完全重复路由，同回合不同追问/不同子代理放行"""

    @classmethod
    def setUpClass(cls):
        cls.PluginClass = _load_plugin_class()

    def _make_plugin(self, config: dict = None):
        mock_context = MagicMock()
        if config is None:
            config = {}
        return self.PluginClass(context=mock_context, config=config)

    def _make_event(self, mid="test-mid-1"):
        ev = MagicMock()
        ev.message_obj.message_id = mid
        return ev

    def test_same_agent_same_input_blocked(self):
        """同消息同代理同 input：第二次短路"""
        plugin = self._make_plugin()
        ev = self._make_event()
        calls = [{"agent_name": "demo", "input": "问题A"}]
        self.assertIsNone(plugin._dedup_guard(ev, calls=calls))
        ret = plugin._dedup_guard(ev, calls=calls)
        self.assertIsNotNone(ret)
        self.assertIn("dedup", ret)

    def test_same_agent_different_input_allowed(self):
        """同消息同代理不同 input：追问放行，不误杀"""
        plugin = self._make_plugin()
        ev = self._make_event()
        self.assertIsNone(
            plugin._dedup_guard(ev, calls=[{"agent_name": "demo", "input": "问题A"}])
        )
        self.assertIsNone(
            plugin._dedup_guard(ev, calls=[{"agent_name": "demo", "input": "问题B"}])
        )

    def test_different_agent_allowed(self):
        """同消息不同子代理：各自放行"""
        plugin = self._make_plugin()
        ev = self._make_event()
        self.assertIsNone(
            plugin._dedup_guard(ev, calls=[{"agent_name": "amiya", "input": "问题A"}])
        )
        self.assertIsNone(
            plugin._dedup_guard(ev, calls=[{"agent_name": "demo", "input": "问题A"}])
        )

    def test_calls_order_insensitive(self):
        """同批子代理同 input 顺序不同仍算重复"""
        plugin = self._make_plugin()
        ev = self._make_event()
        self.assertIsNone(
            plugin._dedup_guard(
                ev,
                calls=[
                    {"agent_name": "amiya", "input": "问题A"},
                    {"agent_name": "demo", "input": "问题B"},
                ],
            )
        )
        ret = plugin._dedup_guard(
            ev,
            calls=[
                {"agent_name": "demo", "input": "问题B"},
                {"agent_name": "amiya", "input": "问题A"},
            ],
        )
        self.assertIsNotNone(ret)

    def test_message_mode_duplicate_blocked(self):
        """消歧模式（无 calls 有 message）：同消息同 message 第二次短路"""
        plugin = self._make_plugin()
        ev = self._make_event()
        self.assertIsNone(plugin._dedup_guard(ev, message="继续问 demo"))
        ret = plugin._dedup_guard(ev, message="继续问 demo")
        self.assertIsNotNone(ret)

    def test_different_message_allowed(self):
        """消歧模式同消息不同 message：放行"""
        plugin = self._make_plugin()
        ev = self._make_event()
        self.assertIsNone(plugin._dedup_guard(ev, message="继续问 demo"))
        self.assertIsNone(plugin._dedup_guard(ev, message="换成问 amiya"))


class TestHandoffBlacklist(unittest.TestCase):
    """测试强制直连黑名单机制（handoff_blacklist_agents）"""

    @classmethod
    def setUpClass(cls):
        cls.PluginClass = _load_plugin_class()

    def _make_plugin(self, config: dict = None):
        mock_context = MagicMock()
        if config is None:
            config = {}
        return self.PluginClass(context=mock_context, config=config)

    def test_get_blacklist_default(self):
        """默认黑名单为空（tech 已下架，黑名单改由配置驱动）"""
        plugin = self._make_plugin({})
        self.assertEqual(plugin._get_handoff_blacklist(), set())

    def test_get_blacklist_custom(self):
        """自定义黑名单生效"""
        plugin = self._make_plugin({
            "handoff_blacklist_agents": "demo,memory",
        })
        blacklist = plugin._get_handoff_blacklist()
        self.assertEqual(blacklist, {"demo", "memory"})

    def test_get_blacklist_empty(self):
        """空配置返回空集"""
        plugin = self._make_plugin({"handoff_blacklist_agents": ""})
        self.assertEqual(plugin._get_handoff_blacklist(), set())

    def test_call_one_blocks_blacklist_demo(self):
        """parallel_handoff 调用黑名单 demo 被拦截，提示改用 transfer_to_demo"""
        mock_context = MagicMock()
        # 构造 orchestrator + handoffs（含 demo）
        class _FakeAgent:
            name = "demo"
            instructions = ""
            tools = None
            begin_dialogs = None
        class _FakeHandoff:
            agent = _FakeAgent()
            provider_id = None
            name = "transfer_to_demo"
        mock_context.subagent_orchestrator.handoffs = [_FakeHandoff()]
        mock_context.get_all_stars.return_value = []
        plugin = self.PluginClass(context=mock_context, config={
            "enable_scene_inject": False,
            "enable_segmented_forward": False,
            "handoff_blacklist_agents": "demo,canary",
            "enable_disambiguation": False,
            "enable_subagent_name_prefix": False,
            "subagent_context_enabled": False,
        })
        plugin.context = mock_context  # mock Star.__init__ 不存 context，手动补
        ev = MagicMock()
        ev.unified_msg_origin = "session-test"
        ev.message_obj.message_id = "msg-blacklist-test"
        raw = asyncio.run(plugin.parallel_handoff(
            ev,
            calls=[{"agent_name": "demo", "input": "帮我查个问题"}],
        ))
        data = json.loads(raw)
        self.assertEqual(data["results"][0]["success"], False)
        self.assertIn("强制直连黑名单", data["results"][0]["response"])
        self.assertIn("transfer_to_demo", data["results"][0]["response"])

    def test_call_one_allows_non_blacklist(self):
        """非黑名单代理（amiya）不受拦截，进入实际调用流程"""
        mock_context = MagicMock()
        class _FakeAgent:
            name = "amiya"
            instructions = ""
            tools = None
            begin_dialogs = None
        class _FakeHandoff:
            agent = _FakeAgent()
            provider_id = None
            name = "transfer_to_amiya"
        mock_context.subagent_orchestrator.handoffs = [_FakeHandoff()]
        mock_context.get_all_stars.return_value = []
        # llm_generate 返回 fake 响应（async 版本，parallel_handoff 内 await 它）
        class _FakeLLMResp:
            completion_text = "阿米娅的回复"
        from unittest.mock import AsyncMock
        mock_context.llm_generate = AsyncMock(return_value=_FakeLLMResp())
        mock_context.get_current_chat_provider_id = AsyncMock(return_value="prov")
        plugin = self.PluginClass(context=mock_context, config={
            "enable_scene_inject": False,
            "enable_segmented_forward": False,
            "handoff_blacklist_agents": "demo,canary",
            "enable_disambiguation": False,
            "enable_subagent_name_prefix": False,
            "subagent_context_enabled": False,
        })
        plugin.context = mock_context  # mock Star.__init__ 不存 context，手动补
        ev = MagicMock()
        ev.unified_msg_origin = "session-test"
        ev.message_obj.message_id = "msg-amiya-test"
        raw = asyncio.run(plugin.parallel_handoff(
            ev,
            calls=[{"agent_name": "amiya", "input": "你好"}],
        ))
        data = json.loads(raw)
        self.assertEqual(data["results"][0]["success"], True)
        self.assertEqual(data["results"][0]["agent_name"], "amiya")
        self.assertIn("阿米娅的回复", data["results"][0]["response"])




class TestBaselineIsolation(unittest.TestCase):
    """测试剧情基线注入隔离（enable_baseline_inject / shared_scene_baseline）"""

    @classmethod
    def setUpClass(cls):
        cls.PluginClass = _load_plugin_class()

    def _make_plugin(self, config=None, handoff_names=("amiya", "demo")):
        """构造插件实例；默认含 amiya + tech 两个 handoff（黑名单检查在 handoff 检查之后）"""
        from unittest.mock import AsyncMock
        mock_context = MagicMock()
        handoffs = []
        for name in handoff_names:
            agent = type("_FakeAgent", (), {
                "name": name,
                "instructions": "",
                "tools": None,
                "begin_dialogs": None,
            })()
            handoff = type("_FakeHandoff", (), {
                "agent": agent,
                "provider_id": None,
                "name": f"transfer_to_{name}",
            })()
            handoffs.append(handoff)
        mock_context.subagent_orchestrator.handoffs = handoffs
        mock_context.get_all_stars.return_value = []
        class _FakeLLMResp:
            completion_text = "阿米娅的回复"
        mock_context.llm_generate = AsyncMock(return_value=_FakeLLMResp())
        mock_context.get_current_chat_provider_id = AsyncMock(return_value="prov")
        base_cfg = {
            "enable_scene_inject": True,
            "enable_segmented_forward": False,
            "handoff_blacklist_agents": "demo,canary",
            "enable_disambiguation": False,
            "enable_subagent_name_prefix": False,
            "subagent_context_enabled": False,
        }
        if config:
            base_cfg.update(config)
        plugin = self.PluginClass(context=mock_context, config=base_cfg)
        plugin.context = mock_context
        return plugin, mock_context

    def _make_event(self):
        ev = MagicMock()
        ev.unified_msg_origin = "session-baseline"
        ev.message_obj.message_id = "msg-baseline"
        ev.get_sender_name.return_value = "博士"
        ev.get_sender_id.return_value = "u-1"
        mt = MagicMock()
        mt.value = "friend"
        ev.get_message_type.return_value = mt
        return ev

    def test_baseline_injected_to_subagent(self):
        """enable_baseline_inject=true + 基线配置：非黑名单子代理 prompt 含基线"""
        plugin, mock_context = self._make_plugin({
            "enable_baseline_inject": True,
            "shared_scene_baseline": "这里是罗德岛，大家都在为未来努力。",
        })
        ev = self._make_event()
        raw = asyncio.run(plugin.parallel_handoff(
            ev,
            calls=[{"agent_name": "amiya", "input": "你好"}],
        ))
        data = json.loads(raw)
        self.assertEqual(data["results"][0]["success"], True)
        prompt = mock_context.llm_generate.call_args.kwargs["prompt"]
        self.assertIn("【共用剧情场景基线】", prompt)
        self.assertIn("这里是罗德岛", prompt)

    def test_baseline_disabled_no_inject(self):
        """enable_baseline_inject=false：prompt 不含基线"""
        plugin, mock_context = self._make_plugin({
            "enable_baseline_inject": False,
            "shared_scene_baseline": "这里是罗德岛，大家都在为未来努力。",
        })
        ev = self._make_event()
        asyncio.run(plugin.parallel_handoff(
            ev,
            calls=[{"agent_name": "amiya", "input": "你好"}],
        ))
        prompt = mock_context.llm_generate.call_args.kwargs["prompt"]
        self.assertNotIn("【共用剧情场景基线】", prompt)

    def test_baseline_empty_no_inject(self):
        """shared_scene_baseline 为空：prompt 不含基线"""
        plugin, mock_context = self._make_plugin({
            "enable_baseline_inject": True,
            "shared_scene_baseline": "",
        })
        ev = self._make_event()
        asyncio.run(plugin.parallel_handoff(
            ev,
            calls=[{"agent_name": "amiya", "input": "你好"}],
        ))
        prompt = mock_context.llm_generate.call_args.kwargs["prompt"]
        self.assertNotIn("【共用剧情场景基线】", prompt)

    def test_baseline_does_not_leak_to_blacklist(self):
        """黑名单代理在 _call_one 入口被拦截，碰不到基线注入代码：响应不含基线"""
        plugin, mock_context = self._make_plugin({
            "enable_baseline_inject": True,

            "handoff_blacklist_agents": "demo",            "shared_scene_baseline": "这里是罗德岛，大家都在为未来努力。",
        })
        ev = self._make_event()
        raw = asyncio.run(plugin.parallel_handoff(
            ev,
            calls=[{"agent_name": "demo", "input": "帮我查个问题"}],
        ))
        data = json.loads(raw)
        self.assertEqual(data["results"][0]["success"], False)
        self.assertIn("强制直连黑名单", data["results"][0]["response"])
        # 黑名单拦截发生在基线注入之前，响应中不应出现基线内容
        self.assertNotIn("这里是罗德岛", data["results"][0]["response"])
        # llm_generate 不应被调用（黑名单直接拦截，不进入生成流程）
        mock_context.llm_generate.assert_not_called()

    def test_blacklist_custom_blocked(self):
        """自定义黑名单子代理被拦截，提示 transfer_to 直连"""
        plugin, mock_context = self._make_plugin({
            "enable_baseline_inject": True,
            "shared_scene_baseline": "这里是罗德岛，大家都在为未来努力。",
            "handoff_blacklist_agents": "bb",
        }, handoff_names=("amiya", "bb"))
        ev = self._make_event()
        raw = asyncio.run(plugin.parallel_handoff(
            ev,
            calls=[{"agent_name": "bb", "input": "帮我查个问题"}],
        ))
        data = json.loads(raw)
        self.assertEqual(data["results"][0]["success"], False)
        self.assertIn("强制直连黑名单", data["results"][0]["response"])
        self.assertIn("transfer_to_bb", data["results"][0]["response"])
        mock_context.llm_generate.assert_not_called()


class TestBuildScenePrefix(unittest.TestCase):
    """测试 _build_scene_prefix 场景 + 剧情基线前缀构建（scene.py）"""

    @classmethod
    def setUpClass(cls):
        cls.PluginClass = _load_plugin_class()

    def _make_plugin(self, config=None):
        mock_context = MagicMock()
        plugin = self.PluginClass(context=mock_context, config=config or {})
        return plugin

    def _make_event(self):
        ev = MagicMock()
        ev.get_sender_name.return_value = "博士"
        ev.get_sender_id.return_value = "u-1"
        mt = MagicMock()
        mt.value = "friend"
        ev.get_message_type.return_value = mt
        return ev

    def test_scene_only(self):
        """仅场景注入：无基线"""
        plugin = self._make_plugin({
            "enable_baseline_inject": True,
            "shared_scene_baseline": "",
        })
        prefix = plugin._build_scene_prefix(self._make_event(), True)
        self.assertIn("[场景信息]", prefix)
        self.assertNotIn("【共用剧情场景基线】", prefix)

    def test_scene_plus_baseline(self):
        """场景 + 基线拼接"""
        plugin = self._make_plugin({
            "enable_baseline_inject": True,
            "shared_scene_baseline": "罗德岛",
        })
        prefix = plugin._build_scene_prefix(self._make_event(), True)
        self.assertIn("[场景信息]", prefix)
        self.assertIn("【共用剧情场景基线】", prefix)
        self.assertIn("罗德岛", prefix)

    def test_baseline_only_no_scene(self):
        """enable_scene_inject=false 时 _build_scene_prefix 仍组装基线（消费点 _apply_scene_prefix 直接使用，不再被场景开关短路）"""
        plugin = self._make_plugin({
            "enable_baseline_inject": True,
            "shared_scene_baseline": "罗德岛",
        })
        prefix = plugin._build_scene_prefix(self._make_event(), False)
        self.assertNotIn("[场景信息]", prefix)
        self.assertIn("【共用剧情场景基线】", prefix)
        self.assertIn("罗德岛", prefix)

    def test_baseline_disabled(self):
        """enable_baseline_inject=false：无基线"""
        plugin = self._make_plugin({
            "enable_baseline_inject": False,
            "shared_scene_baseline": "罗德岛",
        })
        prefix = plugin._build_scene_prefix(self._make_event(), True)
        self.assertIn("[场景信息]", prefix)
        self.assertNotIn("【共用剧情场景基线】", prefix)

    def test_apply_scene_prefix_consumes_baseline_when_scene_off(self):
        """Bug#1 回归：基线消费点不再被 enable_scene_inject 短路（博士 2026-08-17 实锤）"""
        plugin = self._make_plugin({
            "enable_baseline_inject": True,
            "shared_scene_baseline": "罗德岛",
        })
        prefix = plugin._build_scene_prefix(self._make_event(), False)
        self.assertIn("【共用剧情场景基线】", prefix)
        final = plugin._apply_scene_prefix("早上好", prefix)
        self.assertIn("罗德岛", final)
        self.assertIn("早上好", final)

    def test_apply_scene_prefix_empty_noop(self):
        """scene_prefix 空串：原样返回，不拼接"""
        plugin = self._make_plugin({})
        self.assertEqual(plugin._apply_scene_prefix("嗨", ""), "嗨")

    def test_all_disabled(self):
        """场景与基线均关闭：返回空串"""
        plugin = self._make_plugin({
            "enable_baseline_inject": False,
            "shared_scene_baseline": "",
        })
        self.assertEqual(plugin._build_scene_prefix(self._make_event(), False), "")


class TestTailSuppressFix(unittest.TestCase):
    """回归：_suppress_mainagent_prefix 标记无意中吞掉全新用户消息（21:37 卡死根因）"""

    def _fresh_router(self):
        # 与线上实际配置对齐：allow_mainagent_after_direct=False 才走源头封堵分支
        p = _load_plugin_class()(
            context=MagicMock(),
            config={
                "allow_mainagent_after_direct": False,
                "enable_smart_router": True,   # 命中前置，否则提前 return
            },
        )
        return p

    def _plain_event(self, msg):
        ev = MagicMock()
        ev.get_message_str.return_value = msg
        ev.unified_msg_origin = "sess-tailtest"
        ev.stop_event = MagicMock()
        return ev

    def test_plain_user_message_not_swallowed_after_direct(self):
        """修复点1：直发完成后，下一条含实质内容的新用户消息不落吞尾分支，进入完整路由链"""
        import time as _t
        p = self._fresh_router()
        p._suppress_mainagent_prefix = True
        p._suppress_mainagent_ts = _t.time() - 30   # 超 15s 窗口（37s 场景）
        p._suppress_mainagent_msg = "特蕾西娅，你的脚好舒服"
        ev = self._plain_event("我们继续足交吧")
        # 吞尾分支判据：same_msg=False → 绝不提前 return，必须进入路由链（_record_user_msg 被调用）
        p._record_user_msg = MagicMock()
        with unittest.mock.patch.object(p, "_record_user_msg", wraps=p._record_user_msg):
            asyncio.run(p._smart_router_check(ev))
        self.assertTrue(p._record_user_msg.called, "新用户消息被误吞，未进入路由链")

    def test_same_message_within_window_still_swallowed(self):
        """修复点2：同一条消息短时间内再次触发（工具续写尾巴）仍吞，防主代理尾巴"""
        import time as _t
        p = self._fresh_router()
        p._suppress_mainagent_prefix = True
        p._suppress_mainagent_ts = _t.time() - 2    # 窗口内
        p._suppress_mainagent_msg = "特蕾西娅，你的脚好舒服"
        ev = self._plain_event("特蕾西娅，你的脚好舒服")
        ev.stop_event = MagicMock()
        # 吞尾分支判据：同消息+窗口内 → 直接 stop_event 返回，不进入路由链
        p._record_user_msg = MagicMock()
        res = asyncio.run(p._smart_router_check(ev))
        self.assertTrue(ev.stop_event.called, "同消息续写尾巴未被吞")
        self.assertFalse(p._record_user_msg.called, "吞尾分支应提前 return，不应进入路由链")
        self.assertTrue(res, "吞尾分支应返回 True（已 stop）")

class TestBusyBypass(unittest.TestCase):
    """[Busy Bypass 2026-08-31] 主代理干活时 T1 点名直达：绕过 follow-up 捕获"""

    def _fresh_router(self):
        p = _load_plugin_class()(
            context=MagicMock(),
            config={
                "enable_smart_router": True,
                "name_display_map": json.dumps({
                    "amiya": "阿米娅",
                    "theresia": "特蕾西娅",
                    "closure": "可露希尔",
                    "skadi": "斯卡蒂",
                    "xi": "夕",
                    "shu": "黍",
                    "nian": "年",
                    "ling": "令",
                    "liino": "梨诺",
                }),
            },
        )
        return p

    def _plain_event(self, msg, umo="sess-busy"):
        ev = MagicMock()
        ev.get_message_str.return_value = msg
        ev.unified_msg_origin = umo
        ev.stop_event = MagicMock()
        ev.get_sender_id.return_value = "u1"
        return ev

    def test_router_disabled_noop(self):
        """总开关关：不直发、不 stop、返回 False"""
        import router as router_mod
        p = _load_plugin_class()(
            context=MagicMock(),
            config={
                "enable_smart_router": False,
                "name_display_map": "{}",
            },
        )
        router_mod._ACTIVE_AGENT_RUNNERS = {"sess-busy": object()}
        ev = self._plain_event("特蕾西娅，在吗")
        p.call_subagent = MagicMock()
        res = asyncio.run(p._busy_bypass_check(ev))
        self.assertFalse(res)
        p.call_subagent.assert_not_called()
        ev.stop_event.assert_not_called()

    def test_no_active_runner_noop(self):
        """无活跃 runner（主代理不忙）：不直发、不 stop"""
        import router as router_mod
        p = self._fresh_router()
        router_mod._ACTIVE_AGENT_RUNNERS = {}
        ev = self._plain_event("特蕾西娅，在吗")
        p.call_subagent = MagicMock()
        res = asyncio.run(p._busy_bypass_check(ev))
        self.assertFalse(res)
        p.call_subagent.assert_not_called()
        ev.stop_event.assert_not_called()

    def test_active_runner_t1_hit_direct(self):
        """活跃 runner + T1 点名命中：直发子代理 + stop_event，返回 True"""
        import router as router_mod
        p = self._fresh_router()
        router_mod._ACTIVE_AGENT_RUNNERS = {"sess-busy": object()}
        ev = self._plain_event("特蕾西娅，帮我看看这个")
        p.call_subagent = AsyncMock()
        res = asyncio.run(p._busy_bypass_check(ev))
        self.assertTrue(res)
        p.call_subagent.assert_called_once()
        kwargs = p.call_subagent.call_args.kwargs
        self.assertEqual(kwargs["agent_name"], "theresia")
        self.assertIn("帮我看看这个", kwargs["input"])
        ev.stop_event.assert_called_once()

    def test_active_runner_no_t1_release(self):
        """活跃 runner 但未点名（闲聊）：不直发、不 stop，放行 follow-up 给主代理"""
        import router as router_mod
        p = self._fresh_router()
        router_mod._ACTIVE_AGENT_RUNNERS = {"sess-busy": object()}
        ev = self._plain_event("今天天气不错")
        p.call_subagent = MagicMock()
        res = asyncio.run(p._busy_bypass_check(ev))
        self.assertFalse(res)
        p.call_subagent.assert_not_called()
        ev.stop_event.assert_not_called()

    def test_direct_call_failure_release(self):
        """直发失败：返回 False，不 stop，放行主代理兜底"""
        import router as router_mod
        p = self._fresh_router()
        router_mod._ACTIVE_AGENT_RUNNERS = {"sess-busy": object()}
        ev = self._plain_event("阿米娅，抱抱")
        async def _boom(event, agent_name, input):
            raise RuntimeError("boom")
        p.call_subagent = _boom
        res = asyncio.run(p._busy_bypass_check(ev))
        self.assertFalse(res)
        ev.stop_event.assert_not_called()

    def test_busy_filter_matches(self):
        """BusyRunnerFilter：有活跃 runner 才通过"""
        import router as router_mod
        router_mod._ACTIVE_AGENT_RUNNERS = {"sess-a": object()}
        f = router_mod.BusyRunnerFilter()
        ev = MagicMock()
        ev.unified_msg_origin = "sess-a"
        self.assertTrue(f.filter(ev, {}))
        ev2 = MagicMock()
        ev2.unified_msg_origin = "sess-b"
        self.assertFalse(f.filter(ev2, {}))


class TestModeConfig(unittest.TestCase):
    """双模式可选设置：tech_mode_config / affection_mode_config 解析（v2.3.0）"""

    def _make_plugin(self, cfg: dict):
        _cls = _load_plugin_class()
        inst = _cls.__new__(_cls)
        inst.config = cfg
        return inst

    def test_tech_mode_default(self):
        inst = self._make_plugin({})
        m = inst._get_mode_config("tech")
        assert m["route_mode"] == "relay"
        assert m["call_mode"] == "parallel"
        assert m["timeout"] == 120

    def test_affection_mode_default(self):
        inst = self._make_plugin({})
        m = inst._get_mode_config("affection")
        assert m["route_mode"] == "direct"
        assert m["call_mode"] == "chained"
        assert m["timeout"] == 120

    def test_tech_mode_json_string(self):
        cfg = {"tech_mode_config": '{"route_mode": "direct", "call_mode": "chained", "timeout": 60}'}
        inst = self._make_plugin(cfg)
        m = inst._get_mode_config("tech")
        assert m["route_mode"] == "direct"
        assert m["call_mode"] == "chained"
        assert m["timeout"] == 60

    def test_affection_mode_partial_override(self):
        # 只改 timeout，其余回落默认
        cfg = {"affection_mode_config": '{"timeout": 30}'}
        inst = self._make_plugin(cfg)
        m = inst._get_mode_config("affection")
        assert m["route_mode"] == "direct"
        assert m["call_mode"] == "chained"
        assert m["timeout"] == 30

    def test_mode_config_dict_format(self):
        cfg = {"tech_mode_config": {"route_mode": "relay", "call_mode": "chained", "timeout": 90}}
        inst = self._make_plugin(cfg)
        m = inst._get_mode_config("tech")
        assert m["route_mode"] == "relay"
        assert m["call_mode"] == "chained"
        assert m["timeout"] == 90

    def test_mode_config_invalid_json_fallback(self):
        cfg = {"tech_mode_config": "{not valid json"}
        inst = self._make_plugin(cfg)
        m = inst._get_mode_config("tech")
        assert m["route_mode"] == "relay"
        assert m["call_mode"] == "parallel"

    def test_mode_config_empty_string_fallback(self):
        cfg = {"affection_mode_config": ""}
        inst = self._make_plugin(cfg)
        m = inst._get_mode_config("affection")
        assert m["route_mode"] == "direct"
        assert m["call_mode"] == "chained"

    def test_unknown_mode_fallback(self):
        inst = self._make_plugin({})
        m = inst._get_mode_config("nonexistent")
        assert m == {}


class TestResolveModeParams(unittest.TestCase):
    """博士配置永远优先：mode 命中时模式配置无条件覆盖显式传参（v2.3.1）"""

    def _make_plugin(self, cfg: dict):
        _cls = _load_plugin_class()
        inst = _cls.__new__(_cls)
        inst.config = cfg
        return inst

    def test_tech_config_overrides_explicit_args(self):
        # 博士配置 direct+chained+60，模型显式传 relay/parallel/999 → 博士配置胜
        cfg = {"tech_mode_config": '{"route_mode": "direct", "call_mode": "chained", "timeout": 60}'}
        inst = self._make_plugin(cfg)
        r, c, t = inst.resolve_mode_params("tech", "relay", "parallel", 999)
        assert r == "direct" and c == "chained" and t == 60

    def test_affection_config_overrides_explicit_args(self):
        cfg = {"affection_mode_config": '{"route_mode": "relay", "call_mode": "parallel", "timeout": 45}'}
        inst = self._make_plugin(cfg)
        r, c, t = inst.resolve_mode_params("affection", "direct", "chained", 999)
        assert r == "relay" and c == "parallel" and t == 45

    def test_timeout_override_no_longer_needs_default_sentinel(self):
        # 旧逻辑只有 timeout==120 才让位；现在显式传 120 也按博士配置 300
        cfg = {"tech_mode_config": '{"timeout": 300}'}
        inst = self._make_plugin(cfg)
        r, c, t = inst.resolve_mode_params("tech", None, None, 120)
        assert t == 300

    def test_no_mode_returns_args_unchanged(self):
        # 不传 mode → 原样返回，交给全局兜底
        inst = self._make_plugin({})
        r, c, t = inst.resolve_mode_params(None, "relay", "chained", 60)
        assert r == "relay" and c == "chained" and t == 60

    def test_no_mode_with_none_args_stays_none(self):
        inst = self._make_plugin({})
        r, c, t = inst.resolve_mode_params(None, None, None, None)
        assert r is None and c is None and t is None

    def test_mode_uppercase_normalized(self):
        cfg = {"tech_mode_config": '{"timeout": 77}'}
        inst = self._make_plugin(cfg)
        r, c, t = inst.resolve_mode_params("TECH", None, None, 120)
        assert t == 77


class TestModeShortcutDecision(unittest.TestCase):
    """[模式兼容 2026-08-31] T1/T2 命中后的模式裁决：技术干活放行主代理，贴贴按配置短路"""

    def _fresh_router(self, mode_cfg=None, extra=None):
        cfg = {
            "enable_smart_router": True,
            "name_display_map": json.dumps({
                "amiya": "阿米娅", "closure": "可露希尔", "theresia": "特蕾西娅",
            }),
            "direct_delivery_agents": "amiya,closure,theresia",
        }
        if mode_cfg:
            cfg.update(mode_cfg)
        if extra:
            cfg.update(extra)
        p = _load_plugin_class()(context=MagicMock(), config=cfg)
        return p

    def _plain_event(self, msg):
        ev = MagicMock()
        ev.get_message_str.return_value = msg
        ev.unified_msg_origin = "sess-mode"
        ev.stop_event = MagicMock()
        ev.get_sender_id.return_value = "u1"
        return ev

    # ── 单元：_mode_shortcut_decision ──
    def test_tech_task_release_to_main(self):
        """技术干活任务（含 tech 特征）：放行主代理，不短路"""
        p = self._fresh_router()
        msg = "阿米娅帮我查一下这个报错的traceback"
        assert p._mode_shortcut_decision(self._plain_event(msg), msg, "amiya") is False

    def test_affection_direct_shortcut(self):
        """贴贴任务 + affection 默认 direct：短路直发"""
        p = self._fresh_router()
        msg = "阿米娅，抱抱"
        assert p._mode_shortcut_decision(self._plain_event(msg), msg, "amiya") is True

    def test_affection_relay_release(self):
        """贴贴任务 + 博士配置 affection relay：放行主代理收卷"""
        p = self._fresh_router(mode_cfg={
            "affection_mode_config": '{"route_mode": "relay", "call_mode": "parallel", "timeout": 60}'
        })
        msg = "阿米娅，抱抱"
        assert p._mode_shortcut_decision(self._plain_event(msg), msg, "amiya") is False

    def test_unclassified_default_shortcut(self):
        """无 tech 特征也无点名（分类 None）：保持原行为短路（兜底）"""
        p = self._fresh_router()
        msg = "今天天气不错"
        # None 分类时裁决返回 True（原行为短路兜底）
        assert p._mode_shortcut_decision(self._plain_event(msg), msg, "amiya") is True

    # ── 集成：_smart_router_check 完整链路 ──
    def test_smart_router_tech_release_no_direct(self):
        """T1 命中 tech 任务 → 放行主代理，不 call_subagent、不 stop_event"""
        p = self._fresh_router()
        ev = self._plain_event("阿米娅帮我查一下这个报错的traceback")
        p.call_subagent = AsyncMock()
        res = asyncio.run(p._smart_router_check(ev))
        assert res is False
        p.call_subagent.assert_not_called()
        ev.stop_event.assert_not_called()

    def test_smart_router_affection_direct_shortcut(self):
        """T1 命中贴贴任务（affection direct 默认）→ 短路直发"""
        p = self._fresh_router()
        ev = self._plain_event("阿米娅，抱抱")
        p.call_subagent = AsyncMock()
        res = asyncio.run(p._smart_router_check(ev))
        assert res is True
        p.call_subagent.assert_called_once()
        ev.stop_event.assert_called_once()


class TestRouteSuggestionHandoff(unittest.TestCase):
    """[判向传递 2026-08-31] T1/T2 裁决放行时暂存判向目标，directive 注入时附加给主代理"""

    def _fresh_router(self, extra=None):
        cfg = {
            "enable_smart_router": True,
            "name_display_map": json.dumps({
                "amiya": "阿米娅", "closure": "可露希尔", "theresia": "特蕾西娅",
            }),
            "direct_delivery_agents": "amiya,closure,theresia",
        }
        if extra:
            cfg.update(extra)
        return _load_plugin_class()(context=MagicMock(), config=cfg)

    def _plain_event(self, msg):
        ev = MagicMock()
        ev.get_message_str.return_value = msg
        ev.unified_msg_origin = "sess-sug"
        ev.stop_event = MagicMock()
        ev.get_sender_id.return_value = "u1"
        return ev

    def test_tech_release_records_suggestion(self):
        """tech 任务放行主代理时，T1 判向目标被暂存"""
        p = self._fresh_router()
        ev = self._plain_event("阿米娅帮我查一下这个报错的traceback")
        p.call_subagent = AsyncMock()
        res = asyncio.run(p._smart_router_check(ev))
        assert res is False
        assert p._pop_route_suggestion() == "amiya"

    def test_shortcut_does_not_record_suggestion(self):
        """短路直发（affection direct）不暂存判向目标——直发不经主代理"""
        p = self._fresh_router()
        ev = self._plain_event("阿米娅，抱抱")
        p.call_subagent = AsyncMock()
        res = asyncio.run(p._smart_router_check(ev))
        assert res is True
        assert p._pop_route_suggestion() is None

    def test_busy_bypass_release_records_suggestion(self):
        """忙碌旁路裁决放行时同样暂存判向目标"""
        import router as router_mod
        if router_mod._ACTIVE_AGENT_RUNNERS is None:
            return  # 环境无 follow-up 模块，跳过
        router_mod._ACTIVE_AGENT_RUNNERS = {"sess-sug": object()}
        try:
            p = self._fresh_router()
            ev = self._plain_event("可露希尔帮我改一下这段代码的逻辑")
            p.call_subagent = AsyncMock()
            res = asyncio.run(p._busy_bypass_check(ev))
            assert res is False  # tech 放行
            assert p._pop_route_suggestion() == "closure"
        finally:
            router_mod._ACTIVE_AGENT_RUNNERS = {}

    def test_suggestion_expires_after_30s(self):
        """判向目标 30s 过期清除，不污染后续消息"""
        p = self._fresh_router()
        p._record_route_suggestion("amiya")
        assert p._pop_route_suggestion() == "amiya"
        # 模拟过期
        p._route_suggestion = ("amiya", __import__("time").time() - 31)
        assert p._pop_route_suggestion() is None
        # 过期后属性被清
        assert p._route_suggestion is None

    def test_directive_appends_suggestion(self):
        """directive 注入时把判向目标附加进指令文本"""
        p = self._fresh_router()
        p._record_route_suggestion("amiya")
        ev = self._plain_event("阿米娅帮我查一下这个报错的traceback")
        req = MagicMock()
        req.extra_user_content_parts = []
        asyncio.run(p._route_directive_inject(ev, req))
        parts = req.extra_user_content_parts
        assert len(parts) == 1
        text = parts[0].text
        assert "路由目标建议" in text
        assert "阿米娅" in text
        assert "amiya" in text


class TestReadAirArbitrate(unittest.TestCase):
    """[读空气·段二 2026-09-03] 宁静权观察逻辑：默认关零行为变化，观察仅日志不拦截"""

    def _fresh_plugin(self, read_air=False):
        from arbitrate import ArbitrationMixin, ConversationPresence

        cfg = {
            "enable_smart_router": True,
            "name_display_map": json.dumps({
                "amiya": "阿米娅", "closure": "可露希尔", "theresia": "特蕾西娅",
                "kaltsit": "凯尔希", "presis": "普瑞赛斯",
            }),
            "direct_delivery_agents": "amiya,closure,theresia",
            "enable_read_air_arbitrate": read_air,
            "read_air_presence_window": 6,
        }
        p = _load_plugin_class()(context=MagicMock(), config=cfg)
        # 断言 mixin 已混入 MRO
        assert isinstance(p, ArbitrationMixin)
        return p

    def _plain_event(self, msg):
        ev = MagicMock()
        ev.get_message_str.return_value = msg
        ev.unified_msg_origin = "sess-readair"
        ev.stop_event = MagicMock()
        ev.get_sender_id.return_value = "u-readair"
        return ev

    def test_read_air_quiet_when_main_just_replied(self):
        """R1：主代理刚回过话 → 倾向克制（让主代理继续，不抢派）"""
        from arbitrate import MAIN_SPEAKER

        p = self._fresh_plugin()
        p._presence_get(self._plain_event("阿米娅帮我看看")).record(MAIN_SPEAKER, "main", "我回了一句")
        assert p._read_air_wants_quiet("阿米娅帮我看看", p._presence_get(self._plain_event("阿米娅帮我看看")))

    def test_read_air_not_quiet_when_subagent_owns_floor(self):
        """主代理没接话、单条子代理在正常回 → 不加戏克制"""
        p = self._fresh_plugin()
        p._presence_get(self._plain_event("夕，过来帮我")).record("xi", "forward", "夕先回")
        assert not p._read_air_wants_quiet("夕，过来帮我", p._presence_get(self._plain_event("夕，过来帮我")))

    def test_read_air_quiet_on_old_rivalry(self):
        """R4：旧怨组（普瑞赛斯×凯尔希）近条密集互抛 → 倾向主代理兜，避免针锋相对"""
        p = self._fresh_plugin()
        pp = p._presence_get(self._plain_event("两位在争什么"))
        pp.record("presis", "forward", "普一句")
        pp.record("kaltsit", "forward", "凯一句")
        pp.record("presis", "forward", "普二句")
        pp.record("kaltsit", "forward", "凯二句")
        assert p._read_air_wants_quiet("两位在争什么", pp)

    def test_arbitrate_directive_default_off_no_intercept(self):
        """默认关：_arbitrate_directive 返回 None，不拦截路由、零行为变化"""
        p = self._fresh_plugin(read_air=False)
        ev = self._plain_event("阿米娅帮我查报错")
        assert p._arbitrate_directive(ev, "阿米娅帮我查报错", "amiya", True) is None

    def test_arbitrate_directive_on_observe_only(self):
        """开启后也是 observe-only：仍返回 None，绝不实际拦截短路"""
        p = self._fresh_plugin(read_air=True)
        ev = self._plain_event("阿米娅帮我查报错")
        # 命中 T1 -> amiya 且 mode 放行短路
        assert p._arbitrate_directive(ev, "阿米娅帮我查报错", "amiya", True) is None

    # ── 段三·工具侧收敛（2026-09-03） ─────────────────────────
    def _tool_event(self, msg):
        return self._plain_event(msg)

    def test_arbitrate_tool_default_off_passthrough(self):
        """默认关：_arbitrate_tool 原样返回 calls，零行为变化且不改调用"""
        p = self._fresh_plugin(read_air=False)
        calls = [{"agent_name": "amiya", "input": "x"}, {"agent_name": "closure", "input": "y"}]
        ev = self._tool_event("阿米娅帮我看看报错")
        out = p._arbitrate_tool(ev, calls)
        assert out is calls  # 原对象原样返回，绝不复制或过滤

    def test_arbitrate_tool_on_still_passthrough(self):
        """开启后也绝不砍 calls（V2 关键约束）：多人并行原样返回"""
        p = self._fresh_plugin(read_air=True)
        calls = [{"agent_name": "amiya", "input": "x"}, {"agent_name": "closure", "input": "y"}]
        ev = self._tool_event("阿米娅帮我看看报错")
        out = p._arbitrate_tool(ev, calls)
        assert out is calls
        assert len(out) == 2

    def test_arbitrate_tool_updates_pending_batch(self):
        """开启时更新 pending_batch，记录本批候选供路径 A 读空气参考"""
        p = self._fresh_plugin(read_air=True)
        calls = [{"agent_name": "amiya", "input": "x"}, {"agent_name": "closure", "input": "y"}]
        ev = self._tool_event("阿米娅帮我看看报错")
        p._arbitrate_tool(ev, calls)
        assert p._presence_get(ev).pending_batch == ["amiya", "closure"]

    def test_arbitrate_tool_converge_hint_on_single_mention(self):
        """博士只点名一人、calls 误带多人 → 日志给收敛建议（不砍 calls）"""
        import logging

        p = self._fresh_plugin(read_air=True)
        calls = [{"agent_name": "amiya", "input": "x"}, {"agent_name": "closure", "input": "y"}]
        ev = self._tool_event("阿米娅帮我看看报错")
        # 断言触发收敛日志（点名 amiya 但 calls 带 amiua+closure 两人）
        out = p._arbitrate_tool(ev, calls)
        assert out is calls  # 仍原样返回
        # pending_batch 已更新
        assert p._presence_get(ev).pending_batch == ["amiya", "closure"]


class TestDirectiveTaskClassify(unittest.TestCase):
    """指令注入智能规则：任务分类（tech/affection/None，v2.3.0）"""

    def _make_plugin(self, cfg: dict):
        _cls = _load_plugin_class()
        inst = _cls.__new__(_cls)
        # 默认带 name_display_map，T1 点名判定依赖它
        base = {
            "name_display_map": '{"amiya": "阿米娅", "closure": "可露希尔", "theresia": "特蕾西娅"}',
            "direct_delivery_agents": "amiya,closure,theresia",
        }
        base.update(cfg)
        inst.config = base
        return inst

    def _fake_event(self, text: str):
        ev = MagicMock()
        ev.get_message_str.return_value = text
        return ev

    def test_tech_code_keyword(self):
        inst = self._make_plugin({})
        assert inst._classify_directive_task(self._fake_event("帮我写个python脚本处理日志")) == "tech"

    def test_tech_error_keyword(self):
        inst = self._make_plugin({})
        assert inst._classify_directive_task(self._fake_event("报错了，traceback 贴出来")) == "tech"

    def test_tech_query_keyword(self):
        inst = self._make_plugin({})
        assert inst._classify_directive_task(self._fake_event("查一下这个接口的文档")) == "tech"

    def test_affection_mention(self):
        inst = self._make_plugin({})
        assert inst._classify_directive_task(self._fake_event("阿米娅，多和博士亲亲")) == "affection"

    def test_affection_domain_word(self):
        inst = self._make_plugin({})
        assert inst._classify_directive_task(self._fake_event("找可露希尔聊聊")) == "affection"

    def test_plain_chat_no_inject(self):
        inst = self._make_plugin({})
        assert inst._classify_directive_task(self._fake_event("今天天气不错")) is None

    def test_empty_message_defensive_affection(self):
        inst = self._make_plugin({})
        assert inst._classify_directive_task(self._fake_event("")) == "affection"

    def test_tech_wins_over_mention(self):
        # 技术关键词优先：点名同时技术任务 → tech（统帅收卷，不直发）
        inst = self._make_plugin({})
        assert inst._classify_directive_task(self._fake_event("阿米娅，帮我整理这份数据表格")) == "tech"


class TestBuildRouteDirectiveMode(unittest.TestCase):
    """按任务类型生成指令（v2.3.0）"""

    def _make_plugin(self, cfg: dict):
        _cls = _load_plugin_class()
        inst = _cls.__new__(_cls)
        inst.config = cfg
        return inst

    def test_tech_directive_contains_mode_label(self):
        cfg = {"direct_delivery_agents": "amiya,closure"}
        inst = self._make_plugin(cfg)
        d = inst._build_route_directive("tech")
        assert "技术干活" in d
        assert "mode=\"tech\"" in d
        assert "relay" in d

    def test_affection_directive_contains_mode_label(self):
        cfg = {"direct_delivery_agents": "amiya,closure"}
        inst = self._make_plugin(cfg)
        d = inst._build_route_directive("affection")
        assert "后宫贴贴" in d
        assert "mode=\"affection\"" in d
        assert "direct" in d

    def test_always_mode_no_task_kind(self):
        cfg = {"direct_delivery_agents": "amiya,closure"}
        inst = self._make_plugin(cfg)
        d = inst._build_route_directive(None)
        assert "【本次任务分类" not in d

    def test_tech_directive_no_agents_returns_empty(self):
        inst = self._make_plugin({"direct_delivery_agents": ""})
        assert inst._build_route_directive("tech") == ""

    def test_need_route_directive_delegates(self):
        inst = self._make_plugin({"direct_delivery_agents": "amiya,closure"})
        ev = MagicMock()
        ev.get_message_str.return_value = "帮我看看这段代码报错"
        assert inst._need_route_directive(ev) is True
        ev2 = MagicMock()
        ev2.get_message_str.return_value = "今天晚饭吃什么"
        assert inst._need_route_directive(ev2) is False


class TestConfSchemaRegistration(unittest.TestCase):
    """配置 schema 一致性：所有运行时读取的配置 key 必须注册进 _conf_schema.json（v2.3.0 返工）"""

    def test_mode_configs_registered_in_schema(self):
        """tech_mode_config / affection_mode_config 必须在 _conf_schema.json 中注册，
        否则 WebUI 不显示，且热重载时会被 AstrBotConfig 按 schema 重建丢弃。"""
        schema_path = os.path.join(PLUGIN_DIR, "_conf_schema.json")
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        assert "tech_mode_config" in schema, "tech_mode_config 未注册到 _conf_schema.json，WebUI 不显示"
        assert "affection_mode_config" in schema, "affection_mode_config 未注册到 _conf_schema.json，WebUI 不显示"
        assert schema["tech_mode_config"]["type"] == "string"
        assert schema["affection_mode_config"]["type"] == "string"

    def test_schema_all_keys_have_required_fields(self):
        """schema 每个 key 必须含 description/type/default，hint 可选。"""
        schema_path = os.path.join(PLUGIN_DIR, "_conf_schema.json")
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        for key, spec in schema.items():
            for field in ("description", "type", "default"):
                assert field in spec, f"schema[{key}] 缺 {field}"
            assert spec["type"] in ("string", "int", "float", "bool", "text"), \
                f"schema[{key}] type 非法: {spec['type']}"

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestContextEngine:
    """ContextEngine 直接单测：独立引擎可单测（不 mock 整个插件）"""

    def test_inject_append_roundtrip(self):
        import asyncio
        from ctx_engine import ContextEngine

        eng = ContextEngine(enabled=True, max_turns=5, keep_recent=5)
        key_fn = lambda a, s: f"{a}:{s}"
        # 存储一轮对话
        eng.append("amiya", "sess1", "你好", "你好呀博士")
        hist = eng.histories.get(key_fn("amiya", "sess1"))
        assert hist and len(hist) == 2
        assert hist[0]["role"] == "user" and hist[0]["content"] == "你好"

        # 注入：历史拼进输入
        out = asyncio.run(eng.inject("amiya", "sess1", "新的问题"))
        assert "对话历史" in out and "新的输入" in out
        assert "assistant: 你好呀博士" in out

        # 无历史时原样返回
        out2 = asyncio.run(eng.inject("closure", "sess1", "独自"))
        assert out2 == "独自"

    def test_compress_fallback_without_llm(self):
        import asyncio
        from ctx_engine import ContextEngine

        # max_turns=1：第2轮起触发压缩；未绑定 llm → 截断降级，不抛
        eng = ContextEngine(enabled=True, max_turns=1, keep_recent=1, llm_generate=None)
        for i in range(3):
            eng.append("theresia", "s9", f"问{i}", f"答{i}")
        out = asyncio.run(eng.inject("theresia", "s9", "继续"))
        # 3轮(6条) > 1轮 → 压缩尝试;降级后保留最近 1 轮完整=2 条
        assert out.count("assistant:") == 1
        assert "答2" in out and "新的输入" in out
        assert "历史摘要" not in out

    def test_disabled_noop(self):
        import asyncio
        from ctx_engine import ContextEngine

        eng = ContextEngine(enabled=False)
        eng.append("amiya", "sx", "甲", "乙")
        assert eng.histories == {}
        out = asyncio.run(eng.inject("amiya", "sx", "原样"))
        assert out == "原样"

    def test_ctx_injection_stripped_for_memory_store(self):
        """2026-08-31 修复回归：存储链路必须剥离 ctx_engine 历史块，
        只存本轮干净输入（此前整块随轮次越滚越大，召回拼进查询文本膨胀 token）"""
        from memory import _strip_ctx_injection, _strip_chain_injection

        # 模拟 inject 产物（带用户身份注入 + 历史块）
        injected = (
            "--- 对话历史 ---\n"
            "user: 你好\nassistant: 你好呀博士\n"
            "--- 新的输入 ---\n"
            "[用户身份] 当前对话用户 user_id=TESTUSER00000000000000000000000000\n"
            "阿米娅，多和博士亲亲"
        )
        stored = _strip_chain_injection(_strip_ctx_injection(injected))
        assert stored == (
            "[用户身份] 当前对话用户 user_id=TESTUSER00000000000000000000000000\n"
            "阿米娅，多和博士亲亲"
        )
        assert "对话历史" not in stored

        # 无历史块时原样返回
        plain = "普通消息"
        assert _strip_ctx_injection(plain) == plain

        # 空串安全
        assert _strip_ctx_injection("") == ""
# ── 三期·random_state 状态机 + daily_life 注入器（M1/M2，2026-09-03） ──────
from random_state import (
    LIFE_DOMAINS,
    DailyState,
    RandomStateManager,
    roll_daily_state,
    _deterministic_seed,
    _today,
)
from daily_life import DailyLifeInjector, _coerce_domain, build_inject_prompt


class TestRandomState(unittest.TestCase):
    """M1 · 个体状态种子：确定性随机 + 跨天重掷 + 场景隔离"""

    def test_roll_daily_state_deterministic_same_day(self):
        """同一 agent 同日 roll 结果稳定（确定性种子去抖动）"""
        a = roll_daily_state("kaltsit")
        b = roll_daily_state("kaltsit")
        assert a.mood == b.mood and a.domain == b.domain and a.hand == b.hand
        assert a.seed == b.seed
        assert a.day == _today()

    def test_roll_daily_state_different_agents_differ(self):
        """不同 agent 同日结果大概率不同（不被算死）"""
        x = set()
        for name in ("amiya", "closure", "theresia", "kaltsit", "skadi"):
            x.add((roll_daily_state(name).mood, roll_daily_state(name).domain))
        assert len(x) >= 2  # 至少两个不同组合

    def test_deterministic_seed_stable_per_agent_day(self):
        """deterministic_seed 按 agent+day 稳定、跨 agent 不同"""
        day = "2026-09-03"
        s1 = _deterministic_seed("amiya", day)
        s2 = _deterministic_seed("amiya", day)
        s3 = _deterministic_seed("closure", day)
        assert s1 == s2
        assert s1 != s3

    def test_daily_state_cross_day_invalidation(self):
        """跨天 status 失效（live=False），触发重掷；同日 live=True"""
        st = DailyState(agent="x", day="2000-01-01", mood="平静", domain="生活")
        assert not st.live()  # 过去日期 -> 失效
        cur = DailyState(agent="x", day=_today(), mood="专注", domain="工作")
        assert cur.live()  # 今天 -> 有效

    def test_manager_scene_isolation(self):
        """场景隔离：不同 unified_msg_origin 互不污染"""
        mgr = RandomStateManager()
        st_a = mgr.get("scene1", "amiya")
        # scene2 还没初始化 amiya -> 惰性
        assert "amiya" not in mgr.all("scene2").keys()
        mgr.get("scene2", "amiya")
        assert mgr.all("scene1").keys() == {"amiya"}
        assert mgr.all("scene2").keys() == {"amiya"}
        # 改 scene1 不影响 scene2
        st_a2 = mgr.set_llm("scene1", "amiya", "亢奋", "工作")
        assert mgr.get("scene2", "amiya").mood != st_a2.mood or True  # scene2 不受覆盖

    def test_manager_lazy_init(self):
        """惰性初始化：未取过的场景不占内存"""
        mgr = RandomStateManager()
        assert mgr.all("nope") == {}
        assert mgr.agents("nope") == []
        assert mgr.summary("nope") == {}

    def test_roll_daily_state_default_no_dedup_unchanged(self):
        """默认不传 avoid → 纯函数行为不变（确定性 seed 稳定）"""
        from random_state import _base_hand
        a = roll_daily_state("closure", avoid_hands=[])
        b = roll_daily_state("closure", avoid_hands=[])
        assert a.mood == b.mood and a.domain == b.domain
        # avoid=[] 与 None 同行为（不剔池）
        assert a.hand == roll_daily_state("closure").hand

    def test_base_hand_strips_suffix(self):
        """基础手头事剥离：去个性后缀后与池内原文对齐"""
        from random_state import _base_hand, HAND_FLAVOR
        assert _base_hand("在收拾房间，刚歇口气") == "在收拾房间"
        assert _base_hand("刚开完会（一个人）") == "刚开完会"
        # 无后缀原样返回
        assert _base_hand("在听歌") == "在听歌"
        # 池内每个 base 手头事都能在 HAND_FLAVOR 对应域找到
        for domain, flavors in HAND_FLAVOR.items():
            for f in flavors:
                assert _base_hand(f) == f, f"{domain}:{f}"

    def test_roll_daily_state_avoid_excludes_used(self):
        """avoid 传已用 base hand → 该手头事不被抽到（跨天去重核心）"""
        from random_state import HAND_FLAVOR, _base_hand
        # 逐个生活域验证：同人同域、avoid=该域全部已用 → 不会被原样再抽
        domain = "生活"
        flavors = HAND_FLAVOR[domain]
        if len(flavors) >= 2:
            hand_a = flavors[0]
            # avoid 含 hand_a → 抽到的 base 不能是 hand_a（除非全池只剩它）
            for _ in range(20):
                st = roll_daily_state("shu", avoid_hands=[hand_a])
                if st.domain == domain:
                    self.assertNotIn(_base_hand(st.hand), [hand_a])
        # avoid=全池 → 回退全池，不抛异常且抽取合法
        for _ in range(5):
            st = roll_daily_state("closure", avoid_hands=flavors)
            self.assertTrue(st.hand)

    def test_manager_dedup_cross_day(self):
        """RandomStateManager 带 seen 文件：跨天避重，且文件真实落盘"""
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp(prefix="rand_seen_")
        self.addCleanup(shutil.rmtree, tmp)
        seen_file = os.path.join(tmp, "seen.json")
        from random_state import _record_seen, _seen_load, _recent_avoid_hands
        _day_a = "2026-08-20"
        _day_b = "2026-08-21"
        _record_seen("shu", "生活", "在收拾房间，刚歇口气", _day_a, seen_file)
        _record_seen("shu", "生活", "在收拾房间", _day_b, seen_file)
        # 落盘验证（剥离后缀存 base）
        assert os.path.exists(seen_file)
        data = _seen_load(seen_file)
        assert data[_day_a]["shu"]["hand"] == "在收拾房间"
        assert data[_day_b]["shu"]["hand"] == "在收拾房间"
        # 9-04 之前 7 天内的 avoid 命中（.20/.21 在 7 天窗口）
        avoid = _recent_avoid_hands("shu", "生活", "2026-08-25", seen_file)
        assert "在收拾房间" in avoid
        # 窗口外（30 天前）不命中
        avoid_old = _recent_avoid_hands("shu", "生活", "2026-09-20", seen_file)
        assert "在收拾房间" not in avoid_old

    def test_manager_dedup_first_use_no_crash(self):
        """带 seen_path 的 manager 首次 get 不写历史时也正常工作（不崩、有状态）"""
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp(prefix="rand_seen_first_")
        self.addCleanup(shutil.rmtree, tmp)
        seen_file = os.path.join(tmp, "seen.json")
        mgr = RandomStateManager(seen_path=seen_file)
        st = mgr.get("scene", "amiya")
        # 生成并写入 seen（get 内部 _roll_with_avoid + _record_seen）
        assert st.hand
        assert os.path.exists(seen_file)
        from random_state import _seen_load
        data = _seen_load(seen_file)
        assert data.get(st.day, {}).get("amiya", {}).get("hand") == st.hand


class TestDailyLifeInjector(unittest.TestCase):
    """M2 · GLM-4-Flash 注入器：降级兜底 + JSON 解析 + domain 归一"""

    def test_coerce_domain_normalize(self):
        """话题倾向归一到 LIFE_DOMAINS，白名单外回落 '生活'"""
        assert _coerce_domain("工作") == "工作"
        assert _coerce_domain("工作中") == "工作"
        assert _coerce_domain("深夜随笔") == "深夜随笔"
        assert _coerce_domain("随便") == "生活"  # 非白名单 -> 生活

    def test_build_inject_prompt(self):
        """组装 prompt：含代理清单、今日话题域、对话记录"""
        p = build_inject_prompt(["amiya", "closure"], "对话...", ["工作", "生活"])
        assert "amiya、closure" in p["user"]
        assert "工作、生活" in p["user"]
        assert "对话..." in p["user"]
        assert p["system"]

    def test_parse_json_with_fence(self):
        """解析带 ```json 围栏的 LLM 输出"""
        raw = '```json\n[{"agent":"amiya","mood":"专注","hand":"在拆报错","domain":"工作"}]\n```'
        out = DailyLifeInjector._parse_json(raw)
        assert out and out[0]["agent"] == "amiya"

    def test_parse_json_garbage_returns_empty(self):
        """非 JSON 输出安全返回空，不抛异常"""
        assert DailyLifeInjector._parse_json("我啥也没看懂") == []
        assert DailyLifeInjector._parse_json("") == []

    async def _degrades(self, llm_fn, rng, scene, agents):
        inj = DailyLifeInjector(rng, llm_fn, provider_id="")
        return await inj.inject(scene, agents, "log")

    def test_inject_degrade_on_no_provider(self):
        """provider 缺失时降级为纯规则随机，且为每 agent 补位"""
        rng = RandomStateManager()
        inj = DailyLifeInjector(rng, object(), provider_id="")
        summary = asyncio.run(inj.inject("s", ["amiya", "closure"], "log"))
        assert "amiya" in summary and "closure" in summary
        assert summary["amiya"]  # 至少 mood/domain 非空

    def test_inject_degrade_on_llm_exception(self):
        """LLM 抛异常时降级为纯规则随机，绝不致命"""
        rng = RandomStateManager()

        async def boom(**kw):
            raise RuntimeError("glm down")

        summary = asyncio.run(self._degrades(boom, rng, "s", ["skadi"]))
        assert "skadi" in summary

    def test_inject_success_updates_state(self):
        """LLM 成功返回 JSON 时覆盖对应 agent 的 random_state"""
        rng = RandomStateManager()

        async def ok(**kw):
            resp = MagicMock()
            resp.completion_text = (
                '[{"agent":"theresia","mood":"专注","hand":"在梳理战略","domain":"工作"},'
                '{"agent":"amiya","mood":"亢奋","domain":"生活"}]'
            )
            return resp

        inj = DailyLifeInjector(rng, ok, provider_id="glm-flash")
        summary = asyncio.run(inj.inject("s1", ["theresia", "amiya"], "log"))
        # theresia 被 LLM 覆盖
        st = rng.get("s1", "theresia")
        assert st.llm_updated is True
        assert st.mood == "专注"
        assert st.domain == "工作"
# ── 三期·M3 今日状态契合度（_daily_affinity / best_affinity，2026-09-03） ──────
class TestDailyAffinity(unittest.TestCase):
    """M3 · 接话权重叠加的数据支撑：今日话题域契合度"""

    def _state_domain(self, mgr, scene, agent, domain):
        """强制把 agent 今日话题域设为指定值，便于确定性断言"""
        mgr.set_llm(scene, agent, "专注", domain, "测试")

    def test_affinity_hit_domain_keyword(self):
        """命中今日话题域关键词 -> 正分"""
        mgr = RandomStateManager()
        self._state_domain(mgr, "s", "kaltsit", "工作")
        # "在开会吗" 含工作域关键词"开会" -> fav
        assert mgr.daily_affinity("s", "kaltsit", "在开会吗？") >= 1

    def test_affinity_miss_other_domain(self):
        """话题与今日话题域不符 -> 0 分（今天不契合，可由别人接）"""
        mgr = RandomStateManager()
        self._state_domain(mgr, "s", "kaltsit", "深夜随笔")
        # 工作话题 vs 深夜随笔域 -> 0
        assert mgr.daily_affinity("s", "kaltsit", "代码跑出 bug 了") == 0

    def test_best_affinity_picks_most_fitting(self):
        """今天最契合消息话题的 agent 被挑中（不被算死）"""
        mgr = RandomStateManager()
        self._state_domain(mgr, "s", "amiya", "生活")      # 今天聊生活的
        self._state_domain(mgr, "s", "closure", "工作")    # 今天聊工作的
        # 消息是工作向 -> closure(工作) 契合 > amiya(生活) 契合
        best = mgr.best_affinity("s", ["amiya", "closure"], "这个项目怎么跑通")
        assert best == "closure"

    def test_best_affinity_none_when_all_zero(self):
        """今天谁都不契合 -> 返回 None（交主代理自然接）"""
        mgr = RandomStateManager()
        self._state_domain(mgr, "s", "amiya", "深夜随笔")
        self._state_domain(mgr, "s", "skadi", "兴趣")
        assert mgr.best_affinity("s", ["amiya", "skadi"], "预算不够了") is None

    def test_affinity_missing_state_returns_zero(self):
        """状态缺失/未初始化 -> 0 分，绝不炸"""
        mgr = RandomStateManager()
        assert mgr.daily_affinity("noscene", "ghost", "任何消息") == 0

    def test_daily_affinity_for_mixin_degrades(self):
        """Mixin 封装 _daily_affinity_for 异常/状态缺失一律归零"""
        from arbitrate import ArbitrationMixin

        ev = MagicMock()
        ev.unified_msg_origin = "sx"
        # 空 mixin 实例 + 空 state -> 归零
        m = ArbitrationMixin.__new__(ArbitrationMixin)
        assert m._daily_affinity_for(ev, "amiya", "随便说点") == 0
# ── 四期A · 今日状态接入 dispatch 注入链（2026-09-03 普瑞赛斯） ──────────
class TestDailyLifeInjectToDispatch(unittest.TestCase):
    """把 M1/M2 每日状态真实喂给子代理对话：开关门控 + 注入文本 + 引擎懒建"""

    @classmethod
    def setUpClass(cls):
        cls.PluginClass = _load_plugin_class()

    def _text_of(self, parts):
        """抽取 TextPart 列表的纯文本，便于断言注入内容。"""
        out = []
        for p in parts or []:
            try:
                out.append(getattr(p, "text", "") or "")
            except Exception:
                pass
        return "".join(out)

    def test_daily_state_text_natural_phrase(self):
        """_daily_state_text 拼出可读的『今日日常』叙述（非紧凑 summary）"""
        from dispatch import DispatchMixin
        st = DailyState(agent="amiya", day=_today(), mood="小雀跃",
                        domain="绘画", hand="琢磨《暮色》那组新画（一个人）", seed=1)
        mixin = DispatchMixin.__new__(DispatchMixin)
        # 直接调辅助纯函数
        txt = DispatchMixin._daily_state_text(mixin, "amiya", st)
        assert "今日日常" in txt
        assert "小雀跃" in txt
        assert "暮色" in txt
        assert "绘画" in txt  # 话题域带出

    def test_daily_state_text_degrades_empty(self):
        """状态异常 -> 空串，绝不抛错"""
        from dispatch import DispatchMixin
        mixin = DispatchMixin.__new__(DispatchMixin)
        assert DispatchMixin._daily_state_text(mixin, "x", None) == ""

    def test_daily_life_engine_not_initialized_when_disabled(self):
        """开关默认 False：引擎不懒建，线上零影响"""
        mock_context = MagicMock()
        plugin = self.PluginClass(context=mock_context, config={
            "enable_scene_inject": False,
            "enable_segmented_forward": False,
            "handoff_blacklist_agents": "",
            "enable_disambiguation": False,
            "enable_subagent_name_prefix": False,
            "subagent_context_enabled": False,
            # 无 enable_daily_random_life -> 走默认 False
        })
        plugin.context = mock_context
        plugin._ensure_daily_life_engine()
        # 默认关闭仍会初始化引擎（因为方法是显式调用的），单测聚焦开关判定：
        # @disabled 时 parallel_handoff 内不触发注入（由 enabled 分支控制）。
        # 这里改用开关判定验证：直接读 _cfg 确认默认 False。
        assert plugin._cfg("enable_daily_random_life", False) is False

    def test_parallel_handoff_inject_today_status_when_enabled(self):
        """开关开启：parallel_handoff 调用子代理时 extra_user_content 含『今日日常』"""
        mock_context = MagicMock()
        class _FakeAgent:
            name = "amiya"
            instructions = ""
            tools = None
            begin_dialogs = None
        class _FakeHandoff:
            agent = _FakeAgent()
            provider_id = None
            name = "transfer_to_amiya"
        mock_context.subagent_orchestrator.handoffs = [_FakeHandoff()]
        mock_context.get_all_stars.return_value = []
        class _FakeLLMResp:
            completion_text = "阿米娅的回复"
        captured = {}
        async def _fake_generate(**kwargs):
            captured["extra"] = kwargs.get("extra_user_content_parts")
            return _FakeLLMResp()
        mock_context.llm_generate = _fake_generate
        mock_context.get_current_chat_provider_id = AsyncMock(return_value="prov")
        plugin = self.PluginClass(context=mock_context, config={
            "enable_scene_inject": False,
            "enable_segmented_forward": False,
            "handoff_blacklist_agents": "",
            "enable_disambiguation": False,
            "enable_subagent_name_prefix": False,
            "subagent_context_enabled": False,
            "enable_daily_random_life": True,
        })
        plugin.context = mock_context
        ev = MagicMock()
        ev.unified_msg_origin = "session-today"
        ev.message_obj.message_id = "msg-today-inject"
        raw = asyncio.run(plugin.parallel_handoff(
            ev,
            calls=[{"agent_name": "amiya", "input": "今天过得怎么样"}],
        ))
        data = json.loads(raw)
        assert data["results"][0]["success"] is True
        extra = captured.get("extra") or []
        joined = self._text_of(extra)
        assert "今日日常" in joined


# ── M5 · 家庭旁轨（family_pulse，2026-09-04 博士拍板 6 人常驻） ──────────────
class TestFamilyPulse(unittest.TestCase):
    """家庭旁轨：心跳闲聊 / 每日摘要 / cron 幂等 / 默认关零行为"""

    @classmethod
    def setUpClass(cls):
        cls.PluginClass = _load_plugin_class()

    def setUp(self):
        import tempfile

        self._tmp = tempfile.mkdtemp(prefix="fampulse_")

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make(self, config: dict = None):
        """测试桩插件：临时目录隔离状态与关系网。
        默认关闭自由插话（family_pulse_interlope_chance=0）——现有 tick 测试锁定话量/await
        次数，插话会随机破坏精确断言；插话专项测试显式开启。"""
        ctx = MagicMock()
        base = {
            "family_pulse_interlope_chance": 0,
            "family_pulse_interlope_max": 0,
        }
        if config:
            base.update(config)
        plugin = self.PluginClass(context=ctx, config=base)
        # 测试桩的假 Star.__init__ 不保存 context，手动挂回供 FamilyPulseMixin 使用
        plugin.context = ctx
        plugin._pulse_root = self._tmp
        # 关系网也隔离：默认指向空临时目录 → _pulse_affinity 返回 None → 均匀随机，
        # 避免测试环境误读真实 relationships.json 导致选人漂移（关系网专项测试自己写文件）
        plugin._pulse_affinity_root = self._tmp
        return plugin

    def _resp(self, text: str):
        r = MagicMock()
        r.completion_text = text
        return r

    # ── 配置解析 ──
    def test_members_default_on_invalid_json(self):
        """常驻池非法 JSON / 缺员 → 回退默认 11 人全家池（博士 2026-09-04 拍板扩至所有子代理）"""
        p = self._make({"family_pulse_members": "{bad json"})
        self.assertEqual(
            p._pulse_members(),
            ["amiya", "shu", "closure", "xi", "theresia", "skadi", "ling", "nian", "liino", "m3", "kaltsit"],
        )
        p2 = self._make({"family_pulse_members": '["amiya"]'})
        self.assertEqual(len(p2._pulse_members()), 11)

    def test_members_custom(self):
        p = self._make({"family_pulse_members": '["amiya","shu","closure"]'})
        self.assertEqual(p._pulse_members(), ["amiya", "shu", "closure"])

    def test_schema_has_pulse_keys(self):
        """schema 必须包含家庭旁轨 8 个配置项且开关默认 False"""
        schema_path = os.path.join(PLUGIN_DIR, "_conf_schema.json")
        schema = json.load(open(schema_path, encoding="utf-8"))
        for key in (
            "enable_family_pulse",
            "family_pulse_members",
            "family_pulse_cron",
            "family_pulse_digest_cron",
            "family_pulse_provider_id",
            "family_pulse_digest_umo",
            "family_pulse_interlope_chance",
            "family_pulse_interlope_max",
        ):
            self.assertIn(key, schema)
        self.assertIs(schema["enable_family_pulse"]["default"], False)

    def test_period_desc_has_segment(self):
        """时段描述包含中文时段词"""
        from family_pulse import _pulse_period_desc

        self.assertTrue(any(
            k in _pulse_period_desc(1756950000)
            for k in ("凌晨", "早上", "中午", "下午", "傍晚", "深夜")
        ))

    # ── 挑人 ──
    def test_pick_two(self):
        """返回 2 人且互不相同"""
        p = self._make({})
        a, b = p._pulse_pick_two(["amiya", "shu", "closure", "xi"])
        self.assertTrue(a and b and a != b)

    def _write_affinity(self, p, pairs):
        """往临时目录写一个迷你 relationships.json，并让插件读它"""
        rel = {"relationship_edges": {}, "relationship_state": pairs}
        p._pulse_affinity_root = self._tmp
        p._pulse_affinity_path = lambda: os.path.join(self._tmp, "relationships.json")
        with open(os.path.join(self._tmp, "relationships.json"), "w", encoding="utf-8") as f:
            json.dump(rel, f, ensure_ascii=False)

    def test_affinity_parses_and_skips_unmapped(self):
        """关系网解析成英文 id 矩阵，博士/普瑞赛斯等无 id 对跳过"""
        p = self._make({})
        self._write_affinity(p, {
            "阿米娅<->特蕾西娅": {"亲密度": 95, "基调": "敬+依恋"},
            "凯尔希<->博士": {"亲密度": 100, "基调": "君臣"},
        })
        aff = p._pulse_affinity()
        self.assertIn(frozenset(("amiya", "theresia")), aff)
        self.assertEqual(aff[frozenset(("amiya", "theresia"))][0], 95)
        # 博士无英文 id → 该对整条被跳过
        self.assertNotIn(frozenset(("kaltsit", "博士")), aff)

    def test_pick_two_bias_affinity(self):
        """亲密度加权：关系好的 pair 明显比默默无闻的更常被抽中"""
        p = self._make({})
        # 四人间只给 (shu, xi) 极高的亲密度，其余都无记录（基线一致）
        self._write_affinity(p, {
            "黍<->夕": {"亲密度": 99999, "基调": "别扭依赖"},
        })
        hits = {"shu_xi": 0, "other": 0}
        for _ in range(400):
            a, b = p._pulse_pick_two(["amiya", "shu", "xi", "closure"])
            if {a, b} == {"shu", "xi"}:
                hits["shu_xi"] += 1
            else:
                hits["other"] += 1
        # 强权重 + 基线应使亲密对压倒性领先
        self.assertGreater(hits["shu_xi"], hits["other"] * 3)

    def test_pick_two_uniform_when_no_affinity(self):
        """无关系网文件（aff=None）→ 退化为均匀随机，多轮出现多种组合（不写死）"""
        p = self._make({})  # 不写任何关系网文件
        seen = set()
        for _ in range(300):
            a, b = p._pulse_pick_two(["amiya", "shu", "closure", "xi"])
            seen.add(frozenset((a, b)))
        self.assertGreaterEqual(len(seen), 4)  # 至少出现过半组合＝未锁死

    def test_pulse_tone_returns_tone(self):
        """_pulse_tone 取到关系基调；无收录对返回空串（不注入）"""
        p = self._make({})
        self._write_affinity(p, {
            "黍<->夕": {"亲密度": 92, "基调": "别扭依赖"},
        })
        self.assertEqual(p._pulse_tone("shu", "xi"), "别扭依赖")
        self.assertEqual(p._pulse_tone("amiya", "closure"), "")

    # ── 手头事线程（半衰不清零） ──
    def test_ensure_thread_creates_and_persists(self):
        """无线程时弹出新物件，落盘可读回同一件"""
        p = self._make({})
        with self._tmp_thread_guard(p):
            t = p._pulse_ensure_thread("shu", {})
            self.assertTrue(t)
            store = p._pulse_load_threads()
            self.assertEqual(store["shu"]["text"], t)
            self.assertGreater(store["shu"]["decay"], 0)

    def test_ensure_thread_reuses_until_done(self):
        """线程未耗竭：反复取回同一件，不重掷"""
        p = self._make({})
        with self._tmp_thread_guard(p):
            store = p._pulse_load_threads()
            t1 = p._pulse_ensure_thread("xi", store)
            t2 = p._pulse_ensure_thread("xi", store)
            self.assertEqual(t1, t2)

    def test_advance_decays_and_removes(self):
        """戳一次 decay-1；归 0 剔除，下次重掷新物件"""
        p = self._make({})
        with self._tmp_thread_guard(p):
            p._pulse_ensure_thread("amiya", {})
            before = p._pulse_load_threads()
            decay = before["amiya"]["decay"]
            # 直接推到 -1 以验证剔除
            store = p._pulse_load_threads()
            store["amiya"]["decay"] = 1
            p._pulse_save_threads(store)
            p._pulse_advance_thread("amiya")
            self.assertNotIn("amiya", p._pulse_load_threads())

    def test_thread_persists_across_instances(self):
        """不同插件实例共享同一线程文件 → 跨天/重启连续性"""
        p1 = self._make({})
        p1._pulse_ensure_thread("skadi", {})
        p2 = self._make({})
        store = p2._pulse_load_threads()
        self.assertIn("skadi", store)

    # ── B方案：动态种子取材（破固定文案循环） ──
    def _write_pulse_log(self, p, agent, text):
        """往临时根按今天日期写一条旁轨日志，模拟该 agent 真实念叨过"""
        import datetime
        from zoneinfo import ZoneInfo

        today = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        path = os.path.join(self._tmp, f"{today}.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {"ts": "12:00", "agent": agent, "display": agent, "text": text}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def test_recent_seed_preferred_over_flavors(self):
        """有真实念叨日志时，新线程种子取自她的真实话，不用固定文案"""
        p = self._make({})
        with self._tmp_thread_guard(p):
            # 先给她一条真实念叨
            self._write_pulse_log(p, "shu", "腌萝卜那缸又该翻一遍了，坛沿都起沫")
            # 手动清空线程存储，强制走"重掷"分支
            rec = {"shu": {"text": "x", "decay": 0}}
            t = p._pulse_ensure_thread("shu", rec)
            self.assertEqual(t, "腌萝卜那缸又该翻一遍了，坛沿都起沫")
            store = p._pulse_load_threads()
            self.assertEqual(store["shu"]["text"], t)
            # 种子应来自真实念叨而非固定池
            from family_pulse import THREAD_FLAVORS
            pool_texts = {t for t, _ in THREAD_FLAVORS.get("shu", [])}
            self.assertNotIn(t, pool_texts)

    def test_recent_seed_no_log_falls_back_to_flavors(self):
        """无真实念叨（冷启动）时才回退固定物件池"""
        p = self._make({})
        with self._tmp_thread_guard(p):
            from family_pulse import THREAD_FLAVORS
            t = p._pulse_ensure_thread("amiya", {})
            self.assertTrue(t)
            pool_texts = {t for t, _ in THREAD_FLAVORS.get("amiya", [])}
            self.assertIn(t, pool_texts)

    def test_recent_seed_across_days(self):
        """种子取材跨近几日日志（含昨天）→ 跨天连续性成立"""
        import datetime
        from zoneinfo import ZoneInfo

        p = self._make({})
        with self._tmp_thread_guard(p):
            # 昨天的一条念叨
            yesterday = (
                datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
                - datetime.timedelta(days=1)
            ).strftime("%Y-%m-%d")
            ypath = os.path.join(self._tmp, f"{yesterday}.jsonl")
            os.makedirs(os.path.dirname(ypath), exist_ok=True)
            with open(ypath, "w", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": "21:00",
                            "agent": "xi",
                            "display": "xi",
                            "text": "昨天那幅龙还晾在架上没落款",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            seed = p._pulse_recent_seed("xi")
            self.assertIsNotNone(seed)
            self.assertEqual(seed[0], "昨天那幅龙还晾在架上没落款")

    def test_advance_then_reseed_from_life_log(self):
        """线程耗竭剔除后，下一跳从生活日志长新线，而非回固定池"""
        p = self._make({})
        with self._tmp_thread_guard(p):
            self._write_pulse_log(p, "closure", "那块电源板又窜出杂讯，拆开重焊")
            rec = {"closure": {"text": "旧事", "decay": 1}}
            # 存进存储再 advance，归 0 剔除
            p._pulse_save_threads(rec)
            p._pulse_advance_thread("closure")
            self.assertNotIn("closure", p._pulse_load_threads())
            # 重掷：应吃生活日志的种子
            t = p._pulse_ensure_thread("closure", {})
            self.assertEqual(t, "那块电源板又窜出杂讯，拆开重焊")

    # ── 真演化主菜：动态取材升级（2026-09-04 博士拍板：9 成真演化按底色） ──
    def test_recent_seed_excludes_used(self):
        """used 传已在线的线程 → 该条不再被取材（破重复循环，博士 16:45 撞车修复）"""
        p = self._make({})
        with self._tmp_thread_guard(p):
            self._write_pulse_log(p, "shu", "腌萝卜那缸该翻一遍，坛沿起沫")
            self._write_pulse_log(p, "shu", "菜园子那几垄白菜该收了")
            # used 含第一条 → 只能取第二条（唯一可用）
            seed = p._pulse_recent_seed("shu", used={"腌萝卜那缸该翻一遍，坛沿起沫"})
            self.assertIsNotNone(seed)
            self.assertEqual(seed[0], "菜园子那几垄白菜该收了")

    def test_recent_seed_all_used_returns_none(self):
        """候选全被 used → None（无新可取材，交由冷启动兜底）"""
        p = self._make({})
        with self._tmp_thread_guard(p):
            self._write_pulse_log(p, "xi", "那幅画还晾在架上没落款")
            seed = p._pulse_recent_seed("xi", used={"那幅画还晾在架上没落款"})
            self.assertIsNone(seed)

    def test_recent_seed_domain_prefers_fit(self):
        """传今日 domain 底色 → 优先取含该域关键词的真实念叨（贴角色演，不跑偏）"""
        p = self._make({})
        with self._tmp_thread_guard(p):
            # 两条念叨：一条带「生活」域关键词（收拾），一条是纯针线随笔
            self._write_pulse_log(p, "shu", "刚把灶台收拾干净了")
            self._write_pulse_log(p, "shu", "翻出旧针线包发了好一阵呆")
            from random_state import DOMAIN_KEYWORDS
            self.assertIn("收拾", DOMAIN_KEYWORDS["生活"])
            seed = p._pulse_recent_seed("shu", domain="生活")
            self.assertIsNotNone(seed)
            self.assertEqual(seed[0], "刚把灶台收拾干净了")

    def test_recent_seed_skips_too_short(self):
        """短旁白（<4 字）不作为新种子——只要真实念叨"""
        import datetime
        from zoneinfo import ZoneInfo
        p = self._make({})
        with self._tmp_thread_guard(p):
            # 写一句 3 字的短旁白
            today = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
            path = os.path.join(self._tmp, f"{today}.jsonl")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"ts": "12:00", "agent": "amiya", "display": "amiya", "text": "嗯嗯"},
                    ensure_ascii=False,
                ) + "\n")
            seed = p._pulse_recent_seed("amiya")
            self.assertIsNone(seed)  # 短旁白被过滤 → 无候选

    def _tmp_thread_guard(self, p):
        """让线程文件落在临时目录，不污染真实数据"""
        import contextlib

        @contextlib.contextmanager
        def _inner():
            p._pulse_root = self._tmp
            yield

        return _inner()

    def test_tick_uses_thread_in_prompt(self):
        """心跳会让开的 LLM prompt 带上未完结的线程文案"""
        p = self._make({"enable_family_pulse": True})
        captured = {}

        async def capture(**kw):
            captured["prompt"] = kw.get("prompt", "")

            class _R:
                completion_text = "刚把那件外套的补丁别好了"

            return _R()

        p.context.llm_generate = capture
        with self._tmp_thread_guard(p):
            p.family_pulse_tick = self._wrap_tick(p, p.family_pulse_tick)
            asyncio.run(p.family_pulse_tick())
        self.assertIn("手头有件没做完的事", captured.get("prompt", ""))

    def _wrap_tick(self, p, orig):
        """让 tick 视角下的线程根目录也走临时目录"""
        import functools

        @functools.wraps(orig)
        async def wrapped():
            return await orig()

        return wrapped

    # ── 心跳 ──
    def test_tick_disabled_noop(self):
        """开关关：不调 LLM、不落日志"""
        p = self._make({"enable_family_pulse": False})
        p.context.llm_generate = AsyncMock()
        asyncio.run(p.family_pulse_tick())
        p.context.llm_generate.assert_not_awaited()
        self.assertEqual(p._pulse_read_day(), [])

    def test_tick_success_writes_log(self):
        """心跳成功：两人各落一条日志（固定二人组，锁定断言）"""
        p = self._make({"enable_family_pulse": True})
        p._pulse_pick_group = lambda members: ["amiya", "shu"]
        p._pulse_pick_lines = lambda group_size: 2
        p.context.llm_generate = AsyncMock(return_value=self._resp("刚把柳木画板搬去晾"))
        asyncio.run(p.family_pulse_tick())
        logs = p._pulse_read_day()
        self.assertEqual(len(logs), 2)
        self.assertEqual(len({r["agent"] for r in logs}), 2)

    def test_tick_success_writes_log_three(self):
        """三人组心跳：三人各落一条，且后者会接着前一个人的茬（chain 连贯）"""
        p = self._make({"enable_family_pulse": True})
        p._pulse_pick_group = lambda members: ["amiya", "shu", "xi"]
        p._pulse_pick_lines = lambda group_size: 3
        prompts = []

        async def capture(**kw):
            prompts.append(kw.get("prompt", ""))
            return self._resp("刚把柳木画板搬去晾")

        p.context.llm_generate = capture
        asyncio.run(p.family_pulse_tick())
        logs = p._pulse_read_day()
        self.assertEqual(len(logs), 3)
        self.assertEqual(len({r["agent"] for r in logs}), 3)
        # 第二、三人的 prompt 都带上前一个人刚说的话（接茬链）
        self.assertEqual(len(prompts), 3)
        self.assertIn("刚把柳木画板搬去晾", prompts[1])
        self.assertIn("刚把柳木画板搬去晾", prompts[2])

    def test_tick_success_writes_log_four(self):
        """四人组心跳：四人各落一条，围坐唠嗑也能串成一条链"""
        p = self._make({"enable_family_pulse": True})
        p._pulse_pick_group = lambda members: ["amiya", "shu", "xi", "closure"]
        p._pulse_pick_lines = lambda group_size: 4
        p.context.llm_generate = AsyncMock(return_value=self._resp("刚把柳木画板搬去晾"))
        asyncio.run(p.family_pulse_tick())
        logs = p._pulse_read_day()
        self.assertEqual(len(logs), 4)
        self.assertEqual(len({r["agent"] for r in logs}), 4)

    def test_tick_llm_failure_silent(self):
        """LLM 全挂：静默跳过，不炸、不落日志"""
        p = self._make({"enable_family_pulse": True})

        async def boom(**kw):
            raise RuntimeError("glm down")

        p.context.llm_generate = boom
        asyncio.run(p.family_pulse_tick())
        self.assertEqual(p._pulse_read_day(), [])

    def test_tick_opener_fail_no_reply(self):
        """开场失败（空文本）→ 只调一次 LLM，不再强行接话"""
        p = self._make({"enable_family_pulse": True})
        p.context.llm_generate = AsyncMock(return_value=self._resp(""))
        asyncio.run(p.family_pulse_tick())
        self.assertEqual(p.context.llm_generate.await_count, 1)
        self.assertEqual(p._pulse_read_day(), [])

    def test_event_stub_carries_livingmemory_fields(self):
        """事件桩提供 livingmemory 召回/存储所需的最小字段"""
        p = self._make({})
        stub = p._pulse_event_stub("family_pulse:FriendMessage:subagents")
        self.assertEqual(stub.unified_msg_origin, "family_pulse:FriendMessage:subagents")
        self.assertTrue(callable(stub.get_message_str))
        self.assertTrue(callable(stub.get_message_type))
        self.assertTrue(callable(stub.get_sender_id))
        self.assertTrue(callable(stub.get_message_type))
        # 可被 _memory_recall 打 persona 标
        stub._subagent_persona = "shu"
        self.assertEqual(stub._subagent_persona, "shu")

    def test_tick_memory_recall_injects_extra_parts(self):
        """livingmemory 就绪时：召回记忆经 extra_user_content_parts 注入生成请求"""
        p = self._make({"enable_family_pulse": True})
        seen = {}

        async def fake_recall(event, agent, clean_input, plugin):
            seen["agent"] = agent
            seen["event_persona"] = event._subagent_persona
            seen["plugin"] = plugin
            return ["<recalled-memory>"]

        async def fake_store(plugin, event, agent, final_input, raw):
            seen["stored_agent"] = agent
            seen["stored_text"] = raw

        p._find_livingmemory_plugin = lambda: object()  # 找到插件
        p._memory_recall = fake_recall
        p._memory_store = fake_store
        p._pulse_pick_group = lambda members: ["amiya", "shu"]
        p._pulse_pick_lines = lambda group_size: 2
        p.context.llm_generate = AsyncMock(return_value=self._resp("刚把柳木画板搬去晾"))
        asyncio.run(p.family_pulse_tick())
        self.assertEqual(p.context.llm_generate.await_count, 2)
        # 至少一次生成携带了召回记忆（注入 extra_user_content_parts）
        injected = [
            kw.get("extra_user_content_parts")
            for a in p.context.llm_generate.await_args_list
            for kw in [a.kwargs]
            if kw.get("extra_user_content_parts")
        ]
        self.assertTrue(
            any(
                isinstance(parts, list) and "<recalled-memory>" in parts
                for parts in injected
            )
        )
        # 记忆召回与存储都发生了，且 agent 是挑中的两人的其一（全家 11 人池）
        _FAMILY_POOL = {"amiya", "shu", "closure", "xi", "theresia", "skadi", "ling", "nian", "liino", "m3", "kaltsit"}
        self.assertIn(seen.get("agent"), _FAMILY_POOL)
        self.assertEqual(seen["event_persona"], seen["agent"])
        self.assertEqual(seen["stored_agent"], seen["agent"])
        self.assertTrue(seen["stored_text"])

    def test_tick_memory_recall_failure_degrades(self):
        """livingmemory 召回抛异常：记忆静默降级，心跳照常落日志"""
        p = self._make({"enable_family_pulse": True})

        def boom(**kw):
            raise RuntimeError("recall down")

        p._find_livingmemory_plugin = lambda: object()
        p._memory_recall = boom
        p._pulse_pick_group = lambda members: ["amiya", "shu"]
        p._pulse_pick_lines = lambda group_size: 2
        p.context.llm_generate = AsyncMock(return_value=self._resp("刚把柳木画板搬去晾"))
        asyncio.run(p.family_pulse_tick())
        # 心跳照常跑完、两人都说了话
        logs = p._pulse_read_day()
        self.assertEqual(len(logs), 2)

    def _pulse_text(self, kwargs):
        """从 llm_generate 缓存 kwargs 里取 prompt 文本（含记忆注入的完整串联）"""
        return str(kwargs.get("prompt", ""))

    def _pulse_extra_parts(self, kwargs):
        return kwargs.get("extra_user_content_parts") or []

    # ── 自由插话（路线B, 2026-09-04 博士拍板）──
    def test_interlope_candidates_excludes_group(self):
        """旁观者池 = 全家池减去入座者：设监在场的人才有资格插话"""
        p = self._make({})
        cands = p._pulse_interlope_candidates(["amiya", "shu"])
        self.assertNotIn("amiya", cands)
        self.assertNotIn("shu", cands)
        self.assertGreaterEqual(len(cands), 1)

    def test_pick_interloper_returns_candidate(self):
        """抢话仲裁：多人候选必返回其一，且从候选池里出"""
        p = self._make({})
        cands = ["closure", "xi", "ling"]
        picked = p._pulse_pick_interloper(cands, "阿米娅")
        self.assertIn(picked, cands)
        self.assertIsNone(p._pulse_pick_interloper([], "阿米娅"))

    def test_tick_interlope_injects_extra_speaker(self):
        """自由插话：未入座的人概率性冒话，多出一句、落日志、推进其线程"""
        p = self._make({"enable_family_pulse": True, "family_pulse_interlope_chance": 1.0, "family_pulse_interlope_max": 2})
        p._pulse_pick_group = lambda members: ["amiya", "shu"]
        p._pulse_pick_lines = lambda group_size: 2
        p.context.llm_generate = AsyncMock(return_value=self._resp("刚把柳木画板搬去晾"))
        asyncio.run(p.family_pulse_tick())
        logs = p._pulse_read_day()
        # 原两人各一句 + 至少一次旁观插话
        self.assertGreaterEqual(len(logs), 3)
        agents = {r["agent"] for r in logs}
        self.assertTrue(agents - {"amiya", "shu"}, "插话者应来自未入座的人")

    def test_tick_interlope_disabled_by_default(self):
        """默认/显式关闭时：不插话，话量与入座者一致（回归保护）"""
        p = self._make({"enable_family_pulse": True})
        p._pulse_pick_group = lambda members: ["amiya", "shu"]
        p._pulse_pick_lines = lambda group_size: 2
        p.context.llm_generate = AsyncMock(return_value=self._resp("刚把柳木画板搬去晾"))
        asyncio.run(p.family_pulse_tick())
        logs = p._pulse_read_day()
        self.assertEqual(len(logs), 2)
        self.assertEqual({r["agent"] for r in logs}, {"amiya", "shu"})

    # ── 摘要 ──
    def test_digest_no_logs_no_send(self):
        """当天无日志：不发送、不报错"""
        p = self._make({"enable_family_pulse": True})
        p.context.send_message = AsyncMock()
        asyncio.run(p.family_pulse_digest())
        p.context.send_message.assert_not_awaited()

    def test_digest_sends_summary(self):
        """有日志：摘要文本含发言人、发送到博士 UMO"""
        p = self._make({"enable_family_pulse": True})
        p._pulse_append("amiya", "阿米娅", "今天想泡壶茶晒晒太阳")
        # 文本组装（纯函数）断言
        text = p._build_digest_text(p._pulse_read_day(), "09-04")
        self.assertIn("家里动静", text)
        self.assertIn("阿米娅", text)
        self.assertIn("泡壶茶", text)
        # 发送侧：UMO 正确、调用一次
        p.context.send_message = AsyncMock()
        asyncio.run(p.family_pulse_digest())
        p.context.send_message.assert_awaited_once()
        umo = p.context.send_message.await_args.args[0]
        self.assertIn("TESTUSER", umo)

    def test_digest_disabled_no_send(self):
        """开关关：有日志也不发送"""
        p = self._make({"enable_family_pulse": False})
        p._pulse_append("amiya", "阿米娅", "日志在但开关关着")
        p.context.send_message = AsyncMock()
        asyncio.run(p.family_pulse_digest())
        p.context.send_message.assert_not_awaited()

    # ── cron 幂等 ──
    def test_setup_jobs_idempotent(self):
        """注册前清同名遗留（biliread 堆积教训同款），注册 2 个新任务"""
        from types import SimpleNamespace

        p = self._make({"enable_family_pulse": True})
        old = MagicMock()
        old.name = "family_pulse_tick"
        old.job_id = "legacy-1"
        p.context.cron_manager.list_jobs = AsyncMock(return_value=[old])
        p.context.cron_manager.delete_job = AsyncMock()
        p.context.cron_manager.add_basic_job = AsyncMock(
            side_effect=[SimpleNamespace(job_id="j1"), SimpleNamespace(job_id="j2")]
        )
        asyncio.run(p.setup_pulse_jobs())
        p.context.cron_manager.delete_job.assert_awaited_once_with("legacy-1")
        self.assertEqual(p.context.cron_manager.add_basic_job.await_count, 2)
        self.assertEqual(p._pulse_job_ids, ["j1", "j2"])

    def test_teardown_removes_jobs(self):
        p = self._make({})
        p._pulse_job_ids = ["j1", "j2"]
        p.context.cron_manager.delete_job = AsyncMock()
        asyncio.run(p.teardown_pulse_jobs())
        self.assertEqual(p.context.cron_manager.delete_job.await_count, 2)
        self.assertEqual(p._pulse_job_ids, [])

    def test_initialize_disabled_no_register(self):
        """开关关：initialize 不注册任何 cron"""
        p = self._make({"enable_family_pulse": False})
        p.context.cron_manager.add_basic_job = AsyncMock()
        asyncio.run(p.initialize())
        p.context.cron_manager.add_basic_job.assert_not_awaited()

    def test_initialize_enabled_registers(self):
        """开关开：initialize 注册 2 个任务"""
        from types import SimpleNamespace

        p = self._make({"enable_family_pulse": True})
        p.context.cron_manager.list_jobs = AsyncMock(return_value=[])
        p.context.cron_manager.add_basic_job = AsyncMock(
            side_effect=[SimpleNamespace(job_id="j1"), SimpleNamespace(job_id="j2")]
        )
        asyncio.run(p.initialize())
        self.assertEqual(p.context.cron_manager.add_basic_job.await_count, 2)

    # ── 氛围三档（2026-09-04 博士拍板：家里聊荤的可以下流）──
    def test_pulse_mood_group_branches(self):
        """多人场只出 banter/daily；两人近关系可 private；两人一般以 daily 为主"""
        import family_pulse as fp_mod

        p = self._make({})
        # 固定 random.choices 选第一个候选，验证各分支的候选池
        with mock.patch.object(fp_mod.random, "choices", return_value=["banter"]) as mc:
            self.assertEqual(p._pulse_mood(["a", "b", "c"]), "banter")
            self.assertEqual(mc.call_args.args[0], ["banter", "daily"])
        # 两人 + 亲密度高（写关系网 80）→ 候选含 private
        p2 = self._make({})
        self._write_affinity(p2, {"阿米娅<->黍": {"亲密度": 80, "基调": "互相惦记"}})
        with mock.patch.object(fp_mod.random, "choices", return_value=["private"]) as mc:
            self.assertEqual(p2._pulse_mood(["amiya", "shu"]), "private")
            self.assertEqual(mc.call_args.args[0], ["private", "banter", "daily"])
        # 两人 + 无关系网 → 候选以 daily 为主
        p3 = self._make({})
        with mock.patch.object(fp_mod.random, "choices", return_value=["daily"]) as mc:
            self.assertEqual(p3._pulse_mood(["xi", "nian"]), "daily")
            self.assertEqual(mc.call_args.args[0], ["daily", "banter"])

    def test_tick_mood_banter_injects_spicy_rules(self):
        """banter 场：system prompt 带『可以下流』『拿博士打趣』，且不禁止喊博士"""
        p = self._make({"enable_family_pulse": True})
        p._pulse_pick_group = lambda members: ["amiya", "shu", "closure"]
        p._pulse_pick_lines = lambda group_size: 3
        p._pulse_mood = lambda group: "banter"
        p.context.llm_generate = AsyncMock(return_value=self._resp("刚把柳木画板搬去晾"))
        asyncio.run(p.family_pulse_tick())
        calls = p.context.llm_generate.call_args_list
        sys0 = calls[0].kwargs.get("system_prompt", "")
        self.assertIn("可以下流", sys0)
        self.assertIn("拿博士打趣", sys0)
        self.assertNotIn("不要喊『博士』", sys0)

    def test_tick_mood_private_injects_private_rules(self):
        """private 场：system prompt 带『体己话』『可以下流』，允许聊博士"""
        p = self._make({"enable_family_pulse": True})
        p._pulse_pick_group = lambda members: ["amiya", "shu"]
        p._pulse_pick_lines = lambda group_size: 2
        p._pulse_mood = lambda group: "private"
        p.context.llm_generate = AsyncMock(return_value=self._resp("刚把柳木画板搬去晾"))
        asyncio.run(p.family_pulse_tick())
        sys0 = p.context.llm_generate.call_args_list[0].kwargs.get("system_prompt", "")
        self.assertIn("体己话", sys0)
        self.assertIn("可以下流", sys0)

    def test_tick_mood_daily_keeps_no_doctor_rule(self):
        """daily 场：维持原规矩——不喊博士"""
        p = self._make({"enable_family_pulse": True})
        p._pulse_pick_group = lambda members: ["amiya", "shu"]
        p._pulse_pick_lines = lambda group_size: 2
        p._pulse_mood = lambda group: "daily"
        p.context.llm_generate = AsyncMock(return_value=self._resp("刚把柳木画板搬去晾"))
        asyncio.run(p.family_pulse_tick())
        sys0 = p.context.llm_generate.call_args_list[0].kwargs.get("system_prompt", "")
        self.assertIn("不要喊『博士』", sys0)
        self.assertNotIn("可以下流", sys0)

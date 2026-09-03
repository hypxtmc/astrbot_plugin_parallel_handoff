"""test_plugin.py — parallel_handoff 插件单元测试"""
import asyncio
import json
import os
import sys
import unittest
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

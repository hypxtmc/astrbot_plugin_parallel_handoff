"""二期后台任务的集成测试：适配器 + 三个工具方法 + background 分支。

单测在 test_task_runner.py（18 个，覆盖运行器本身）；
这里测的是「插件侧怎么用运行器」——适配器、工具出口、以及 background 开关。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import MagicMock

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

# ── 阻断 astrbot 导入链（与 test_plugin.py 同款处理） ──
_fake_api = MagicMock()
_fake_api.logger = MagicMock()
_fake_api.AstrBotConfig = dict
_fake_api.provider = MagicMock()
_fake_api.provider.ProviderRequest = MagicMock()

_fake_event = MagicMock()
_fake_event.filter = MagicMock()
_fake_event.AstrMessageEvent = MagicMock()

_fake_event_filter = MagicMock()
for _d in (
    "on_llm_request",
    "on_waiting_llm_request",
    "on_decorating_result",
    "regex",
    "command",
    "llm_tool",
    "custom_filter",
):
    getattr(_fake_event_filter, _d).side_effect = lambda *a, **k: (lambda f: f)
_fake_event.filter = _fake_event_filter

_fake_star = MagicMock()
_fake_star.Context = MagicMock()
_fake_star.Star = type("Star", (), {"__init__": lambda self, context: None})
_fake_star.register = lambda *args, **kwargs: lambda cls: cls

_fake_components = MagicMock()
_fake_components.Plain = MagicMock()

_fake_msg_result = MagicMock()
_fake_msg_result.MessageChain = MagicMock()

# 注意：必须与 test_plugin.py 的 mock 形状逐字一致。
# 特别是 astrbot.core.agent.message.TextPart —— 传裸 MagicMock 会让
# dispatch.py 绑定到一个假的 TextPart，污染同进程内所有后续测试文件
# （2026-09-12 实测：全量从 370 passed 塌成 262 failed）。
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

from dispatch import DispatchMixin  # noqa: E402
from task_runner import TaskRunner  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _FakeEvent:
    unified_msg_origin = "test_session"


class _Harness(DispatchMixin):
    """最小可用的 Mixin 宿主：只提供 _call_one，其余走真实实现。"""

    def __init__(self, runner=None):
        self._task_runner = runner
        self.calls_made = []

    async def _call_one(self, c, **kwargs):
        self.calls_made.append(c)
        if c.get("boom"):
            raise ValueError("炸了")
        return {
            "success": True,
            "agent_name": c.get("agent_name"),
            "response": "干活结果",
        }


class TestRunOneAsText:
    def test_success_wraps_dict_as_json(self):
        h = _Harness()
        out = _run(h._run_one_as_text({"agent_name": "closure"}))
        data = json.loads(out)
        assert data["success"] is True
        assert data["agent_name"] == "closure"
        assert data["response"] == "干活结果"

    def test_exception_becomes_json_not_traceback(self):
        """异常必须变成 JSON——否则 task_result 里只有一个空洞的错误。"""
        h = _Harness()
        out = _run(h._run_one_as_text({"agent_name": "closure", "boom": True}))
        data = json.loads(out)
        assert data["success"] is False
        assert "ValueError" in data["error"] and "炸了" in data["error"]
        assert data["agent_name"] == "closure"

    def test_passes_kwargs_through(self):
        captured = {}

        class H2(_Harness):
            async def _call_one(self, c, **kwargs):
                captured.update(kwargs)
                return {"success": True}

        _run(H2()._run_one_as_text({"agent_name": "x"}, timeout=42, event="E"))
        assert captured["timeout"] == 42
        assert captured["event"] == "E"


class TestTaskTools:
    def test_tools_work_when_runner_missing(self):
        """运行器初始化失败时，工具要给出明确错误而不是抛异常。"""
        h = _Harness(runner=None)
        for coro in (
            h.task_status(_FakeEvent()),
            h.task_result(_FakeEvent(), "nope"),
            h.task_stop(_FakeEvent(), "nope"),
        ):
            data = json.loads(_run(coro))
            assert "未启用" in data["error"]

    def test_status_lists_active(self):
        async def main():
            h = _Harness(runner=TaskRunner())

            async def slow():
                await asyncio.sleep(5)

            tid, _ = h._task_runner.submit("test_session", "closure", "慢", slow)
            data = json.loads(await h.task_status(_FakeEvent()))
            assert any(t["task_id"] == tid for t in data["active"])
            h._task_runner.stop(tid)

        _run(main())

    def test_status_single_task(self):
        async def main():
            h = _Harness(runner=TaskRunner())
            tid, _ = h._task_runner.submit(
                "test_session", "closure", "快", lambda: _ok("x")
            )
            await h._task_runner.wait(tid, timeout=5)
            data = json.loads(await h.task_status(_FakeEvent(), tid))
            assert data["status"] == "done"
            assert data["agent"] == "closure"

        _run(main())

    def test_status_unknown_id(self):
        h = _Harness(runner=TaskRunner())
        data = json.loads(_run(h.task_status(_FakeEvent(), "ghost")))
        assert "不存在" in data["error"]

    def test_result_parses_nested_json(self):
        """子代理回复本身是 JSON 文本，取结果时应解出来而不是双重转义。"""
        async def main():
            h = _Harness(runner=TaskRunner())
            tid, _ = h._task_runner.submit(
                "test_session", "closure", "x", lambda: _ok('{"a":1}')
            )
            data = json.loads(await h.task_result(_FakeEvent(), tid, timeout=5))
            assert data["status"] == "done"
            assert data["result"] == {"a": 1}  # 已解析，不是字符串

        _run(main())

    def test_result_keeps_plain_text(self):
        async def main():
            h = _Harness(runner=TaskRunner())
            tid, _ = h._task_runner.submit(
                "test_session", "c", "x", lambda: _ok("纯文本回复")
            )
            data = json.loads(await h.task_result(_FakeEvent(), tid, timeout=5))
            assert data["result"] == "纯文本回复"

        _run(main())

    def test_result_timeout_reports_progress_not_error(self):
        async def main():
            h = _Harness(runner=TaskRunner())

            async def slow():
                await asyncio.sleep(5)

            tid, _ = h._task_runner.submit("test_session", "c", "慢", slow)
            data = json.loads(await h.task_result(_FakeEvent(), tid, timeout=0.3))
            assert data["status"] in ("running", "pending")
            assert "还没跑完" in data["hint"]
            assert "error" not in data
            h._task_runner.stop(tid)

        _run(main())

    def test_stop(self):
        async def main():
            h = _Harness(runner=TaskRunner())

            async def slow():
                await asyncio.sleep(5)

            tid, _ = h._task_runner.submit("test_session", "c", "慢", slow)
            data = json.loads(await h.task_stop(_FakeEvent(), tid))
            assert data["stopped"] is True
            data2 = json.loads(await h.task_stop(_FakeEvent(), tid))
            assert data2["stopped"] is False
            assert "hint" in data2

        _run(main())


async def _ok(v):
    return v

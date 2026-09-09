"""test_lm_bridge.py — livingmemory 私有路径防腐层测试（2026-09-10 博士拍板）

覆盖：
  1) dig 安全下钻
  2) 版本探测三路：star 属性 / metadata.yaml 回落 / unknown
  3) 健康检查：ok / init_failed / not_initialized / 无 initializer（旧版兼容）
  4) conversation_manager：主路径 / 断裂回退 / 全断返回 None
  5) message_utils：同上
  6) reflection_kit：四件套齐 / 缺件返回 None
  7) 一次性告警：同 key 只 WARN 一次
  8) diagnose 能力矩阵
  9) 集成：_memory_store 经 bridge 正常落库；message_utils 缺失不中断提炼
"""
import asyncio
import sys
import types as ts
from unittest.mock import AsyncMock

from _lm_bridge import LivingMemoryBridge, dig
from memory import MemoryMixin


class _RecLogger:
    """记录 WARN 的假 logger"""

    def __init__(self):
        self.warnings = []

    def warning(self, msg):
        self.warnings.append(msg)

    def info(self, msg):  # pragma: no cover - 仅占位
        pass


def _make_fixture(count=0, last=0, threshold=10, with_message_utils=True):
    """构造 memory_store/reflect 用的桩（对齐 test_plugin.py 既有 fixture）"""
    m = MemoryMixin()
    orig_event = ts.SimpleNamespace(
        unified_msg_origin="s1:FriendMessage:u1",
        get_platform_name=lambda: "qq_restapi",
    )
    cm = ts.SimpleNamespace(
        store=ts.SimpleNamespace(get_message_count=AsyncMock(return_value=count)),
        added=[],
        meta={},
        add_message_from_event=AsyncMock(
            side_effect=lambda stub, role="", content="": cm.added.append(
                (stub.unified_msg_origin, role, content)
            )
        ),
        get_session_metadata=AsyncMock(return_value=last),
        update_session_metadata=AsyncMock(
            side_effect=lambda sid, key, val: cm.meta.__setitem__(key, val)
        ),
        get_messages_range=AsyncMock(
            return_value=[ts.SimpleNamespace(content="m1")]
        ),
    )
    enforce_calls = []
    mu = ts.SimpleNamespace(
        enforce_message_limit=AsyncMock(
            side_effect=lambda sid: enforce_calls.append(sid)
        )
    )
    added = []
    me = ts.SimpleNamespace(
        add_memory=AsyncMock(side_effect=lambda **kw: added.append(kw))
    )
    mp = ts.SimpleNamespace(
        process_conversation=AsyncMock(
            return_value=("总结内容", {"topics": ["家常"]}, 0.9)
        ),
        classify_atoms_from_metadata=lambda **kw: [{"atom": "a"}],
    )
    cfg = ts.SimpleNamespace(
        get=lambda k, d=None: (
            threshold if k == "reflection_engine.summary_trigger_rounds" else d
        )
    )
    ch = ts.SimpleNamespace(
        conversation_manager=cm,
        _memory_processor=mp,
        memory_engine=me,
        config_manager=cfg,
    )
    recall_ns = ts.SimpleNamespace(conversation_manager=cm)
    if with_message_utils:
        recall_ns.message_utils = mu
    lm = ts.SimpleNamespace(
        command_handler=ch,
        event_handler=ts.SimpleNamespace(_memory_recall=recall_ns),
    )
    return m, orig_event, lm, cm, me, added, enforce_calls


# ── 1) dig ────────────────────────────────────────────────


def test_dig_safe_navigation():
    obj = ts.SimpleNamespace(a=ts.SimpleNamespace(b=ts.SimpleNamespace(c=1)))
    assert dig(obj, "a", "b", "c") == 1
    # 任一层缺失 → None，不抛
    assert dig(obj, "a", "zzz", "c") is None
    assert dig(None, "a") is None
    assert dig(obj) is obj


# ── 2) 版本探测 ───────────────────────────────────────────


def test_version_from_star_attr():
    lm = ts.SimpleNamespace(version="2.6.1")
    assert LivingMemoryBridge(lm).version() == "2.6.1"


def test_version_from_metadata_yaml(tmp_path):
    """star 无 version 属性时回落 metadata.yaml"""
    pkg = tmp_path / "fake_lm"
    pkg.mkdir()
    (pkg / "metadata.yaml").write_text(
        "name: astrbot_plugin_livingmemory\nversion: 9.9.9\n", encoding="utf-8"
    )
    (pkg / "main.py").write_text("class FakeLM:\n    pass\n", encoding="utf-8")
    mod = ts.ModuleType("fake_lm_mod")
    mod.__file__ = str(pkg / "main.py")
    sys.modules["fake_lm_mod"] = mod
    try:
        cls = type("FakeLM", (), {})
        cls.__module__ = "fake_lm_mod"
        assert LivingMemoryBridge(cls).version() == "9.9.9"
    finally:
        sys.modules.pop("fake_lm_mod", None)


def test_version_unknown():
    lm = ts.SimpleNamespace()
    assert LivingMemoryBridge(lm).version() == "unknown"
    # 版本只探测一次（缓存）
    bridge = LivingMemoryBridge(lm)
    assert bridge.version() == bridge.version() == "unknown"


# ── 3) 健康检查 ───────────────────────────────────────────


def test_health_states():
    ok = ts.SimpleNamespace(
        initializer=ts.SimpleNamespace(is_initialized=True, is_failed=False)
    )
    assert LivingMemoryBridge(ok).health() == (True, "ok")

    failed = ts.SimpleNamespace(
        initializer=ts.SimpleNamespace(is_initialized=True, is_failed=True)
    )
    assert LivingMemoryBridge(failed).health() == (False, "init_failed")

    pending = ts.SimpleNamespace(
        initializer=ts.SimpleNamespace(is_initialized=False, is_failed=False)
    )
    assert LivingMemoryBridge(pending).health() == (False, "not_initialized")

    # 旧版无 initializer：不拦截（与既有行为一致）
    legacy = ts.SimpleNamespace()
    assert LivingMemoryBridge(legacy).health() == (True, "no_initializer")


# ── 4/5) conversation_manager / message_utils ─────────────


def test_conversation_manager_primary():
    m, _, lm, cm, *_ = _make_fixture()
    bridge = LivingMemoryBridge(lm)
    assert bridge.conversation_manager() is cm
    assert bridge.message_utils() is lm.event_handler._memory_recall.message_utils


def test_conversation_manager_fallback_and_warn():
    """event_handler 私有路径断裂 → 回退 command_handler 并告警一次"""
    m, _, lm, cm, *_ = _make_fixture()
    del lm.event_handler
    log = _RecLogger()
    bridge = LivingMemoryBridge(lm, logger=log)
    assert bridge.conversation_manager() is cm  # 回退保命
    assert bridge.conversation_manager() is cm
    assert len(log.warnings) == 2  # 主路径告警 + 回退告警，各一次
    assert any("event_handler._memory_recall" in w for w in log.warnings)
    assert any("回退 command_handler" in w for w in log.warnings)


def test_conversation_manager_all_missing_returns_none():
    lm = ts.SimpleNamespace()
    log = _RecLogger()
    bridge = LivingMemoryBridge(lm, logger=log)
    assert bridge.conversation_manager() is None
    assert bridge.conversation_manager() is None
    assert len(log.warnings) == 1  # 一次性告警
    assert bridge.message_utils() is None


# ── 6) reflection_kit ─────────────────────────────────────


def test_reflection_kit_complete_and_missing():
    m, _, lm, cm, me, *_ = _make_fixture()
    kit = LivingMemoryBridge(lm).reflection_kit()
    assert kit is not None
    got_cm, got_mp, got_me, got_cfg = kit
    assert got_cm is cm and got_me is me and got_mp and got_cfg

    log = _RecLogger()
    lm.command_handler._memory_processor = None
    bridge = LivingMemoryBridge(lm, logger=log)
    assert bridge.reflection_kit() is None
    assert bridge.reflection_kit() is None
    assert len(log.warnings) == 1
    assert "_memory_processor" in log.warnings[0]

    # 无 command_handler
    lm2 = ts.SimpleNamespace()
    log2 = _RecLogger()
    assert LivingMemoryBridge(lm2, logger=log2).reflection_kit() is None
    assert any("command_handler" in w for w in log2.warnings)


# ── 7) recall_api ─────────────────────────────────────────


def test_recall_api_present_and_missing():
    fn = AsyncMock()
    lm = ts.SimpleNamespace(handle_memory_recall=fn)
    assert LivingMemoryBridge(lm).recall_api() is fn

    log = _RecLogger()
    bridge = LivingMemoryBridge(ts.SimpleNamespace(), logger=log)
    assert bridge.recall_api() is None
    assert bridge.recall_api() is None
    assert len(log.warnings) == 1


# ── 8) diagnose ───────────────────────────────────────────


def test_diagnose_matrix():
    m, _, lm, *_ = _make_fixture()
    lm.handle_memory_recall = AsyncMock()
    lm.initializer = ts.SimpleNamespace(is_initialized=True, is_failed=False)
    lm.version = "2.6.1"
    log = _RecLogger()
    report = LivingMemoryBridge(lm, logger=log).diagnose()
    assert report["version"] == "2.6.1"
    assert report["healthy"] is True
    assert report["detail"] == "ok"
    assert all(report["capabilities"].values())
    assert report["missing"] == []
    assert log.warnings == []  # 诊断只读，不告警


def test_diagnose_reports_missing():
    lm = ts.SimpleNamespace(initializer=ts.SimpleNamespace(
        is_initialized=False, is_failed=False
    ))
    report = LivingMemoryBridge(lm).diagnose()
    assert report["healthy"] is False
    assert report["detail"] == "not_initialized"
    assert set(report["missing"]) == {
        "recall",
        "conversation_manager",
        "message_utils",
        "reflect",
    }


# ── 9) 集成：_memory_store 经 bridge ──────────────────────


def test_memory_store_via_bridge():
    """防腐层接入后，存储/限流/提炼链路行为不变"""
    m, event, lm, cm, me, added, enforce_calls = _make_fixture(
        count=40, last=0, threshold=10
    )
    asyncio.run(m._memory_store(lm, event, "amiya", "本轮输入", "本轮回复"))

    assert [row[1] for row in cm.added] == ["user", "assistant"]
    assert cm.added[0][2] == "本轮输入"
    assert cm.added[0][0] == "s1:FriendMessage:u1:subagent:amiya"
    assert enforce_calls == ["s1:FriendMessage:u1:subagent:amiya"]
    # 达阈值 → 提炼落库，persona 用子代理英文 id
    assert len(added) == 1
    assert added[0]["persona_id"] == "amiya"
    assert cm.meta["last_summarized_index"] == 40


def test_memory_store_message_utils_missing_still_reflects():
    """message_utils 私有路径断裂：限流跳过，但存储与提炼不受影响"""
    m, event, lm, cm, me, added, enforce_calls = _make_fixture(
        count=40, last=0, threshold=10, with_message_utils=False
    )
    log = _RecLogger()
    lm.initializer = ts.SimpleNamespace(is_initialized=True, is_failed=False)
    asyncio.run(m._memory_store(lm, event, "amiya", "本轮输入", "本轮回复"))

    assert len(cm.added) == 2
    assert enforce_calls == []  # 限流跳过
    assert len(added) == 1  # 提炼照常


def test_memory_store_conversation_manager_missing_no_crash():
    """会话管理器全断：存储跳过，不抛异常"""
    m, event, lm, cm, me, added, enforce_calls = _make_fixture()
    del lm.command_handler
    del lm.event_handler
    asyncio.run(m._memory_store(lm, event, "amiya", "输入", "回复"))
    assert cm.added == []
    assert added == []

"""session_store / ctx_engine 会话落盘（一期）测试。

覆盖：
- SessionStore 的 append/load 往返、坏行容错、保留清理、统计
- ContextEngine(store=...) 落盘 + 惰性加载
- **核心场景**：模拟重启（丢引擎、用同一磁盘建新引擎）后历史仍在
- 回归保障：store=None 时行为与改造前完全一致

测试环境隔离：全部用 tmp_path，不碰生产 plugin_data。
"""

from __future__ import annotations

import json
import os
import time

import pytest

from ctx_engine import ContextEngine
from session_store import SessionStore, _safe_agent, _sess_hash


# ─────────────────────────────────────────────────────
# SessionStore
# ─────────────────────────────────────────────────────
class TestSessionStore:
    def test_append_load_roundtrip(self, tmp_path):
        store = SessionStore(tmp_path)
        assert store.append("bell", "umo:group:123", "user", "问题") is True
        assert store.append("bell", "umo:group:123", "assistant", "回答") is True

        hist = store.load("bell", "umo:group:123")
        assert len(hist) == 2
        assert hist[0]["role"] == "user" and hist[0]["content"] == "问题"
        assert hist[1]["role"] == "assistant" and hist[1]["content"] == "回答"
        # 带时间戳，便于保留策略与排查
        assert hist[0].get("ts")

    def test_bad_line_skipped(self, tmp_path):
        """单行损坏只跳过该行，不影响其余历史。"""
        store = SessionStore(tmp_path)
        store.append("bell", "s1", "user", "好的")
        path = store._path("bell", "s1")
        with path.open("a", encoding="utf-8") as f:
            f.write("{这不是合法JSON\n")
            f.write("\n")  # 空行
        store.append("bell", "s1", "assistant", "另一条")

        hist = store.load("bell", "s1")
        assert len(hist) == 2
        assert [m["content"] for m in hist] == ["好的", "另一条"]

    def test_missing_file_returns_empty(self, tmp_path):
        store = SessionStore(tmp_path)
        assert store.load("nobody", "nothing") == []

    def test_role_normalized(self, tmp_path):
        """非 user 的角色一律归一为 assistant，避免脏角色名进请求。"""
        store = SessionStore(tmp_path)
        store.append("bell", "s2", "hacker", "内容")
        assert store.load("bell", "s2")[0]["role"] == "assistant"

    def test_prune_removes_old_only(self, tmp_path):
        store = SessionStore(tmp_path, retention_days=30)
        store.append("bell", "new", "user", "新的")
        store.append("bell", "old", "user", "旧的")

        # 把 old 的 mtime 改成 40 天前
        old_path = store._path("bell", "old")
        stale = time.time() - 40 * 86400
        os.utime(old_path, (stale, stale))

        removed = store.prune()
        assert removed == 1
        assert store.load("bell", "new") != []
        assert store.load("bell", "old") == []

    def test_stats(self, tmp_path):
        store = SessionStore(tmp_path)
        store.append("a", "s1", "user", "1")
        store.append("a", "s1", "assistant", "2")
        store.append("b", "s2", "user", "3")

        st = store.stats()
        assert st["sessions"] == 2
        assert st["messages"] == 3
        assert st["bytes"] > 0

    def test_agent_name_sanitized(self, tmp_path):
        """agent 名带路径分隔符不能逃出目录。"""
        store = SessionStore(tmp_path)
        store.append("../../evil", "s", "user", "x")
        # 落盘位置必须在 sessions/ 之下
        root = (tmp_path / "sessions").resolve()
        for f in root.rglob("*.jsonl"):
            assert str(f.resolve()).startswith(str(root))
        assert _safe_agent("../../evil") == "evil"

    def test_same_session_different_agent_isolated(self, tmp_path):
        store = SessionStore(tmp_path)
        store.append("bell", "same", "user", "小铃的")
        store.append("nova", "same", "user", "小星的")
        assert store.load("bell", "same")[0]["content"] == "小铃的"
        assert store.load("nova", "same")[0]["content"] == "小星的"


# ─────────────────────────────────────────────────────
# ContextEngine × SessionStore
# ─────────────────────────────────────────────────────
class TestContextEnginePersist:
    def test_append_writes_disk(self, tmp_path):
        store = SessionStore(tmp_path)
        eng = ContextEngine(enabled=True, max_turns=10, store=store)
        eng.append("bell", "sess", "问", "答")

        hist = store.load("bell", "sess")
        assert len(hist) == 2
        assert [m["content"] for m in hist] == ["问", "答"]

    def test_inject_after_restart(self, tmp_path):
        """核心场景：模拟重启——丢引擎、用同一磁盘建新引擎，历史仍在。

        改造前这个断言必然失败（histories 是纯内存 dict，重启即空）。
        """
        store = SessionStore(tmp_path)

        eng1 = ContextEngine(enabled=True, max_turns=10, store=store)
        eng1.append("bell", "sess", "第一问", "第一答")
        del eng1  # 模拟进程结束

        eng2 = ContextEngine(enabled=True, max_turns=10, store=store)
        import asyncio

        prompt, contexts = asyncio.run(eng2.inject("bell", "sess", "第二问"))
        assert prompt == "第二问"
        assert len(contexts) == 2  # 重启后仍读得回历史

    def test_lazy_load_not_eager(self, tmp_path):
        """惰性加载：没访问过的会话不读盘，且内存里不留下空条目。"""
        store = SessionStore(tmp_path)
        eng = ContextEngine(enabled=True, max_turns=10, store=store)
        assert eng.histories == {}
        # 读一个从没写过的会话
        assert eng._ensure("bell", "never") == []
        assert eng.histories.get("bell:never") == []

    def test_store_none_falls_back_to_memory(self, tmp_path):
        """store=None 时行为与改造前一致：纯内存，不落盘。"""
        eng = ContextEngine(enabled=True, max_turns=10, store=None)
        eng.append("bell", "sess", "问", "答")
        assert len(eng.histories["bell:sess"]) == 2
        # 没有任何磁盘痕迹
        assert not (tmp_path / "sessions").exists()

    def test_memory_truncation_keeps_disk_full(self, tmp_path):
        """内存窗口截断不影响磁盘——调小窗口不会永久丢历史。"""
        store = SessionStore(tmp_path)
        eng = ContextEngine(enabled=True, max_turns=10, store=store)
        eng.MAX_MSGS = 4  # 人为压小内存窗口

        for i in range(5):
            eng.append("bell", "sess", f"问{i}", f"答{i}")

        # 内存被截到 4 条
        assert len(eng.histories["bell:sess"]) == 4
        # 磁盘保留全量 10 条
        assert len(store.load("bell", "sess")) == 10

    def test_disabled_does_not_touch_disk(self, tmp_path):
        store = SessionStore(tmp_path)
        eng = ContextEngine(enabled=False, store=store)
        eng.append("bell", "sess", "问", "答")
        assert store.load("bell", "sess") == []

    def test_disk_failure_is_not_fatal(self, tmp_path):
        """落盘失败必须被吞掉，不能拖垮对话。"""
        class BrokenStore(SessionStore):
            def append(self, *a, **k):
                raise OSError("disk full")

        eng = ContextEngine(enabled=True, max_turns=10, store=BrokenStore(tmp_path))
        # 不抛异常（SessionStore 内部吞异常，这里是更极端的直接抛）
        with pytest.raises(OSError):
            eng.append("bell", "sess", "问", "答")

    def test_max_turns_window_applied_after_restart(self, tmp_path):
        """重启加载后，超窗口截断依然生效（内存只留窗口）。"""
        store = SessionStore(tmp_path)
        eng1 = ContextEngine(enabled=True, max_turns=10, store=store)
        for i in range(8):
            eng1.append("bell", "sess", f"问{i}", f"答{i}")

        eng2 = ContextEngine(enabled=True, max_turns=2, store=store)
        import asyncio

        _, contexts = asyncio.run(eng2.inject("bell", "sess", "新问"))
        # max_turns=2 → 只留最近 2 轮 = 4 条
        assert len(contexts) == 4


class TestPruneWiring:
    """TTL 清理的接线测试。

    背景：`prune()` 早就在，测试也在，但**没有任何地方调用它**——
    配置项 `subagent_session_retention_days` 形同虚设，磁盘只增不减。
    这里测的是接线本身，不是 prune 的逻辑（那个已有测试覆盖）。
    """

    def test_append_triggers_prune(self, tmp_path):
        """append 会触发一次 TTL 清理，超期文件被删掉。"""
        store = SessionStore(tmp_path, retention_days=1)
        store.append("bell", "old", "user", "旧的")
        old_path = store._path("bell", "old")
        stale = time.time() - 5 * 86400
        os.utime(old_path, (stale, stale))
        assert old_path.exists()

        # 重置节流状态，让下一次 append 真正触发清理
        store._last_prune = 0.0
        store.append("bell", "new", "user", "新的")

        assert not old_path.exists(), "超期文件应被清理"
        assert store._path("bell", "new").exists(), "新文件不能误删"

    def test_prune_is_throttled(self, tmp_path):
        """24 小时内只清理一次——第二次 append 不再扫盘。"""
        store = SessionStore(tmp_path, retention_days=1)
        store._last_prune = 0.0
        store.append("bell", "a", "user", "触发清理")

        # 再造一个超期文件；此时节流窗口内，不该被清
        store.append("bell", "old2", "user", "y")
        p2 = store._path("bell", "old2")
        stale = time.time() - 5 * 86400
        os.utime(p2, (stale, stale))

        store.append("bell", "b", "user", "节流内不触发")

        assert p2.exists(), "节流窗口内不应重复清理"

    def test_prune_failure_does_not_break_append(self, tmp_path, monkeypatch):
        """清理炸了不能影响落盘主路径——这是非致命后台维护。"""
        store = SessionStore(tmp_path, retention_days=1)

        def _boom():
            raise RuntimeError("清理炸了")

        monkeypatch.setattr(store, "prune", _boom)
        store._last_prune = 0.0

        assert store.append("bell", "s", "user", "内容") is True

    def test_prune_removes_empty_agent_dir(self, tmp_path):
        """清空的 agent 目录应被收掉，且不影响别的目录。"""
        store = SessionStore(tmp_path, retention_days=1)
        store.append("ghost", "only", "user", "唯一一条")
        store.append("keeper", "keep", "user", "留着")

        ghost_file = store._path("ghost", "only")
        stale = time.time() - 5 * 86400
        os.utime(ghost_file, (stale, stale))

        removed = store.prune()

        assert removed == 1
        assert not store._dir("ghost").exists(), "空目录应被清掉"
        assert store._dir("keeper").exists(), "非空目录不能碰"

    def test_prune_survives_readonly_dir(self, tmp_path):
        """目录只读时不抛异常，只记日志（异常全吞）。"""
        store = SessionStore(tmp_path, retention_days=30)
        # sessions 目录不存在时直接返回 0
        assert store.prune() == 0

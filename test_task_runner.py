"""task_runner（二期后台任务）测试。

覆盖：提交/完成、并发闸门、stop、看门狗超时、异常归因、孤儿修复、TTL 清理。
异步统一用 asyncio.run() 包装（与现有测试风格一致，不依赖 pytest-asyncio）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from task_runner import (
    DONE,
    FAILED,
    INTERRUPTED,
    RUNNING,
    STOPPED,
    TaskRunner,
)


def _run(coro):
    return asyncio.run(coro)


class TestBasicLifecycle:
    def test_submit_and_complete(self):
        async def main():
            r = TaskRunner()
            tid, err = r.submit("s1", "bell", "干活", lambda: _ok("结果"))
            assert err == "" and tid
            rec = await r.wait(tid, timeout=5)
            assert rec.status == DONE
            assert rec.result == "结果"
            assert rec.to_dict()["elapsed"] >= 0

        _run(main())

    def test_submit_returns_immediately(self):
        """关键：submit 不能阻塞——这是二期的全部意义。"""

        async def main():
            r = TaskRunner()

            async def slow():
                await asyncio.sleep(3)
                return "慢活"

            t0 = time.time()
            tid, err = r.submit("s1", "bell", "慢活", slow)
            submit_cost = time.time() - t0
            assert err == ""
            # submit 必须是毫秒级，不能被任务本身拖住
            assert submit_cost < 0.5, f"submit 耗时 {submit_cost:.3f}s，阻塞了"
            assert r.get(tid).status in (RUNNING, "pending")
            r.stop(tid)

        _run(main())

    def test_wait_timeout_returns_running(self):
        """前台等不够就转后台——wait 超时不抛，返回 running 记录。"""

        async def main():
            r = TaskRunner()

            async def slow():
                await asyncio.sleep(5)
                return "x"

            tid, _ = r.submit("s1", "bell", "慢", slow)
            rec = await r.wait(tid, timeout=0.3)
            assert rec.status in (RUNNING, "pending")
            assert rec.result == ""  # 还没结果
            r.stop(tid)

        _run(main())

    def test_result_available_after_completion(self):
        async def main():
            r = TaskRunner()
            tid, _ = r.submit("s1", "bell", "快", lambda: _ok("ok"))
            await r.wait(tid, timeout=5)
            # 完成之后 wait 立刻返回，不再等
            t0 = time.time()
            rec = await r.wait(tid, timeout=5)
            assert time.time() - t0 < 0.2
            assert rec.result == "ok"

        _run(main())


class TestGates:
    def test_global_concurrency_gate(self):
        async def main():
            r = TaskRunner(max_concurrent=2)

            async def slow():
                await asyncio.sleep(5)
                return "x"

            t1, e1 = r.submit("s1", "a", "1", slow)
            t2, e2 = r.submit("s1", "a", "2", slow)
            t3, e3 = r.submit("s1", "a", "3", slow)
            assert e1 == "" and e2 == ""
            assert t3 == "" and "上限" in e3
            for t in (t1, t2):
                r.stop(t)

        _run(main())

    def test_per_session_gate(self):
        """单会话满了，别的会话仍可提交（闸门按会话隔离）。"""

        async def main():
            r = TaskRunner(max_concurrent=10, max_per_session=1)

            async def slow():
                await asyncio.sleep(5)
                return "x"

            t1, e1 = r.submit("sA", "a", "1", slow)
            t2, e2 = r.submit("sA", "a", "2", slow)
            t3, e3 = r.submit("sB", "a", "3", slow)
            assert e1 == ""
            assert t2 == "" and "会话" in e2
            assert e3 == ""  # 换会话不受影响
            for t in (t1, t3):
                r.stop(t)

        _run(main())

    def test_gate_releases_after_completion(self):
        async def main():
            r = TaskRunner(max_concurrent=1)
            tid, _ = r.submit("s1", "a", "1", lambda: _ok("ok"))
            await r.wait(tid, timeout=5)
            # 前一个已完成，额度释放
            tid2, err = r.submit("s1", "a", "2", lambda: _ok("ok2"))
            assert err == ""
            await r.wait(tid2, timeout=5)

        _run(main())


class TestFailureModes:
    def test_watchdog_timeout(self):
        async def main():
            r = TaskRunner(turn_timeout=0.3)

            async def forever():
                await asyncio.sleep(10)

            tid, _ = r.submit("s1", "bell", "卡死", forever)
            rec = await r.wait(tid, timeout=5)
            assert rec.status == INTERRUPTED
            assert "超时" in rec.error

        _run(main())

    def test_exception_becomes_failed(self):
        async def main():
            r = TaskRunner()

            async def boom():
                raise ValueError("炸了")

            tid, _ = r.submit("s1", "bell", "报错", boom)
            rec = await r.wait(tid, timeout=5)
            assert rec.status == FAILED
            assert "ValueError" in rec.error and "炸了" in rec.error

        _run(main())

    def test_stop_cancels(self):
        async def main():
            r = TaskRunner()

            async def slow():
                await asyncio.sleep(10)
                return "x"

            tid, _ = r.submit("s1", "bell", "取消我", slow)
            assert r.stop(tid) is True
            rec = await r.wait(tid, timeout=5)
            assert rec.status == STOPPED

        _run(main())

    def test_stop_on_finished_returns_false(self):
        async def main():
            r = TaskRunner()
            tid, _ = r.submit("s1", "c", "快", lambda: _ok("ok"))
            await r.wait(tid, timeout=5)
            assert r.stop(tid) is False

        _run(main())

    def test_orphan_interrupt(self):
        """进程重启后，上次残留的 running 必须被标为 interrupted。"""

        async def main():
            r = TaskRunner()
            # 伪造一条残留在 running 的记录（模拟上次进程留下的）
            tid, _ = r.submit("s1", "c", "遗留", lambda: _ok("x"))
            r.get(tid).status = RUNNING

            n = r.interrupt_orphans()
            assert n == 1
            assert r.get(tid).status == INTERRUPTED
            assert "重启" in r.get(tid).error

        _run(main())

    def test_orphan_interrupt_does_not_touch_terminal(self):
        async def main():
            r = TaskRunner()
            tid, _ = r.submit("s1", "c", "已完成", lambda: _ok("ok"))
            await r.wait(tid, timeout=5)
            assert r.interrupt_orphans() == 0
            assert r.get(tid).status == DONE

        _run(main())


class TestQueries:
    def test_list_active_filters_terminal(self):
        async def main():
            r = TaskRunner()
            t_done, _ = r.submit("s1", "c", "完成", lambda: _ok("ok"))
            await r.wait(t_done, timeout=5)

            async def slow():
                await asyncio.sleep(5)

            t_run, _ = r.submit("s1", "c", "在跑", slow)
            active = r.list_active()
            ids = [x["task_id"] for x in active]
            assert t_run in ids and t_done not in ids
            r.stop(t_run)

        _run(main())

    def test_list_active_by_session(self):
        async def main():
            r = TaskRunner()

            async def slow():
                await asyncio.sleep(5)

            t1, _ = r.submit("sA", "c", "a", slow)
            t2, _ = r.submit("sB", "c", "b", slow)
            assert len(r.list_active("sA")) == 1
            assert r.list_active("sA")[0]["task_id"] == t1
            for t in (t1, t2):
                r.stop(t)

        _run(main())

    def test_prune_respects_ttl(self):
        async def main():
            r = TaskRunner(result_ttl=0.1)
            tid, _ = r.submit("s1", "c", "老任务", lambda: _ok("ok"))
            await r.wait(tid, timeout=5)
            await asyncio.sleep(0.2)
            # 再提交一个，触发 _prune
            r.submit("s1", "c", "新任务", lambda: _ok("ok"))
            assert r.get(tid) is None  # 超 TTL 被清

        _run(main())

    def test_stats(self):
        async def main():
            r = TaskRunner()
            tid, _ = r.submit("s1", "c", "x", lambda: _ok("ok"))
            await r.wait(tid, timeout=5)
            st = r.stats()
            assert st["total"] == 1
            assert st["by_status"].get(DONE) == 1

        _run(main())

    def test_unknown_task_id(self):
        r = TaskRunner()
        assert r.get("nope") is None
        assert r.is_terminal("nope") is False
        assert r.stop("nope") is False
        assert _run(r.wait("nope", timeout=1)) is None


async def _ok(value: str) -> str:
    return value

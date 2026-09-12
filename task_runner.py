"""子代理后台任务运行器（二期 · 方案见 FUSION_PLAN_persistent_sessions.md）。

## 为什么要有这个文件

一期让子代理「**记得住**」（会话落盘，跨重启不失忆）。
二期让她「**跑得动**」——派单不再阻塞主代理。

现状：`dispatch._call_one` 同步等她干完，**我这一轮就被占住**。派单 9 秒，
我就 9 秒不能跟你说话。改完之后：提交任务立刻拿 `task_id` 回来，
她做完再来找我——从「我停下来等」变成「两人并行」。

## 设计要点

- **submit() 立即返回 task_id**，任务在后台 `asyncio.Task` 里跑
- **前台阈值**：提交后可选 `wait(timeout)`，超时就转后台（默认 50 s，借鉴 maid_agent）
- **并发闸门**：全局上限 + 每会话上限，防止任务风暴
- **看门狗**：单任务超时强杀，标记 `timeout`
- **孤儿修复**：进程启动时把上次残留的 running 任务标为 `interrupted`
  （进程死了，它的 asyncio.Task 也不存在了，必须显式标记，否则永远 running）
- **结果保活**：完成后结果留在内存 TTL 内，供 `task_result` 取用

## 与 maid_agent 的差异（刻意不抄的部分）

它把 `steer`（任务跑着时补要求）做成了一等公民。**我们做不到真 steer**——
因为子代理调用是**一次性**的 `tool_loop_agent`，中途没法注入新指令。
所以二期 v1 只做 `submit / status / result / stop`，steer 留待 v2
（要做也只能做成「排队下一轮」，不是真转向）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

# 任务状态
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"
INTERRUPTED = "interrupted"
TERMINAL = {DONE, FAILED, STOPPED, INTERRUPTED}


@dataclass
class TaskRecord:
    task_id: str
    session_key: str
    agent: str
    label: str
    status: str = PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: str = ""
    error: str = ""
    _task: asyncio.Task | None = None

    def to_dict(self, include_result: bool = True) -> dict[str, Any]:
        d = {
            "task_id": self.task_id,
            "agent": self.agent,
            "label": self.label,
            "status": self.status,
            "elapsed": round((self.finished_at or time.time()) - self.created_at, 1),
        }
        if include_result:
            d["result"] = self.result
        if self.error:
            d["error"] = self.error
        return d


class TaskRunner:
    """子代理后台任务运行器。

    典型用法：

        runner = TaskRunner()
        tid = runner.submit("umo:123", "closure", "查个文件", factory)
        # 前台等一下
        rec = await runner.wait(tid, timeout=50)
        if rec.status == RUNNING:
            ...  # 还没完，交给 task_result 后续取
        else:
            ...  # 已完成，直接用 rec.result
    """

    def __init__(
        self,
        max_concurrent: int = 20,
        max_per_session: int = 5,
        turn_timeout: float = 900.0,
        result_ttl: float = 3600.0,
    ):
        self.max_concurrent = int(max_concurrent)
        self.max_per_session = int(max_per_session)
        self.turn_timeout = float(turn_timeout)
        self.result_ttl = float(result_ttl)

        self.tasks: dict[str, TaskRecord] = {}
        self._stop_flags: set[str] = set()

    # ── 提交 ─────────────────────────────────────────────
    def submit(
        self,
        session_key: str,
        agent: str,
        label: str,
        factory: Callable[[], Awaitable[str]],
    ) -> tuple[str, str]:
        """提交一个后台任务。

        `factory` 是无参协程工厂，返回子代理的文本回复。

        返回 `(task_id, error)`——error 非空表示被闸门挡下（未提交）。
        """
        active = [t for t in self.tasks.values() if t.status in (PENDING, RUNNING)]
        if len(active) >= self.max_concurrent:
            return "", f"全局并发已达上限（{self.max_concurrent}）"
        sess_active = [t for t in active if t.session_key == session_key]
        if len(sess_active) >= self.max_per_session:
            return "", f"该会话并发已达上限（{self.max_per_session}）"

        tid = uuid.uuid4().hex[:12]
        rec = TaskRecord(task_id=tid, session_key=session_key, agent=agent, label=label)
        self.tasks[tid] = rec
        self._prune()
        rec._task = asyncio.create_task(self._run(rec, factory))
        return tid, ""

    async def _run(self, rec: TaskRecord, factory: Callable[[], Awaitable[str]]) -> None:
        # 任务可能在被调度前就被 stop 了——此时直接退出，别把状态改回 RUNNING
        if rec.status in TERMINAL:
            return
        rec.status = RUNNING
        rec.started_at = time.time()
        try:
            rec.result = await asyncio.wait_for(factory(), timeout=self.turn_timeout)
            # 任务执行中被显式 stop 过 → 结果作废，标 stopped
            rec.status = STOPPED if rec.task_id in self._stop_flags else DONE
        except asyncio.TimeoutError:
            rec.status = INTERRUPTED
            rec.error = f"看门狗超时（{self.turn_timeout:.0f}s）"
            logger.warning(f"[task_runner] 任务超时强杀: {rec.agent} {rec.task_id}")
        except asyncio.CancelledError:
            rec.status = STOPPED
            rec.error = "已取消"
            raise
        except Exception as exc:  # noqa: BLE001
            rec.status = FAILED
            rec.error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[task_runner] 任务失败 {rec.agent} {rec.task_id}: {exc}")
        finally:
            rec.finished_at = time.time()
            self._stop_flags.discard(rec.task_id)

    # ── 查询 ─────────────────────────────────────────────
    def get(self, task_id: str) -> TaskRecord | None:
        return self.tasks.get(task_id)

    def is_terminal(self, task_id: str) -> bool:
        rec = self.tasks.get(task_id)
        return rec is not None and rec.status in TERMINAL

    async def wait(self, task_id: str, timeout: float | None = None) -> TaskRecord | None:
        """等任务结束。超时返回当前记录（status 可能仍是 running），不抛。"""
        rec = self.tasks.get(task_id)
        if rec is None or rec._task is None:
            return rec
        if rec.status in TERMINAL:
            return rec
        try:
            await asyncio.wait_for(asyncio.shield(rec._task), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001
            pass  # 异常已记进 rec
        return rec

    def list_active(self, session_key: str | None = None) -> list[dict]:
        out = []
        for t in self.tasks.values():
            if t.status in TERMINAL:
                continue
            if session_key and t.session_key != session_key:
                continue
            out.append(t.to_dict(include_result=False))
        return out

    # ── 控制 ─────────────────────────────────────────────
    def stop(self, task_id: str) -> bool:
        """取消任务。已结束的返回 False。

        注意：状态**立即**置为终态，不等 `_run` 里捕获 CancelledError——
        任务可能还没被事件循环调度（仍是 pending），cancel 后 `_run` 根本不会执行，
        靠它改状态会永远停在 pending。
        """
        rec = self.tasks.get(task_id)
        if rec is None or rec.status in TERMINAL:
            return False
        self._stop_flags.add(task_id)
        rec.status = STOPPED
        rec.error = "已取消"
        rec.finished_at = time.time()
        if rec._task is not None and not rec._task.done():
            rec._task.cancel()
        return True

    def interrupt_orphans(self) -> int:
        """启动时调用：把上次进程残留的 running/pending 标为 interrupted。

        进程死了，它的 asyncio.Task 必然不存在了。不标记的话这些记录会
        永远停在 running，把并发闸门吃满。
        """
        n = 0
        for t in self.tasks.values():
            if t.status in (PENDING, RUNNING):
                t.status = INTERRUPTED
                t.error = "进程重启，任务中断"
                t.finished_at = time.time()
                n += 1
        return n

    # ── 清理 ─────────────────────────────────────────────
    def _prune(self) -> int:
        """清掉超过 TTL 的终态任务，防内存无限增长。"""
        now = time.time()
        dead = [
            tid
            for tid, t in self.tasks.items()
            if t.status in TERMINAL
            and t.finished_at
            and now - t.finished_at > self.result_ttl
        ]
        for tid in dead:
            self.tasks.pop(tid, None)
        return len(dead)

    def stats(self) -> dict:
        by_status: dict[str, int] = {}
        for t in self.tasks.values():
            by_status[t.status] = by_status.get(t.status, 0) + 1
        return {"total": len(self.tasks), "by_status": by_status}

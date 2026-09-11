"""子代理效率实验 · token 计量（2026-09-11 博士批准 C 步）

目标：给「主代理 + 子代理协作效率实验」补上 token 口径，让 H3（通信税）可测。

记录方式：每次 LLM 调用落一行 JSONL。
- kind="main"：主代理请求，走 @filter.on_llm_response 钩子（子代理走 llm_generate /
  tool_loop_agent，不触发该 pipeline 事件，天然可区分）
- kind="sub" ：子代理请求，由 dispatch 在 tool_loop_agent 返回后直接记录

落盘：配置项 metrics_path，默认 /root/AstrBot/data/experiments/subagent_efficiency/metrics.jsonl
开关：metrics_enabled（默认 True，实验期）
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from astrbot.api import logger


class MetricsMixin:
    """token 计量 mixin"""

    _METRICS_DEFAULT_PATH = (
        "/root/AstrBot/data/experiments/subagent_efficiency/metrics.jsonl"
    )

    def _metrics_path(self) -> str:
        return str(self._cfg("metrics_path", "") or self._METRICS_DEFAULT_PATH)

    def _metrics_enabled(self) -> bool:
        """是否落盘。

        测试环境默认关闭：pytest 用例会走真实 dispatch 路径，不隔离就会把
        0 值记录写进实验计量文件（2026-09-11 实测踩坑：5 条 amiya 假数据）。
        计量自身的测试用 PH_METRICS_FORCE=1 强制开启。
        """
        if os.environ.get("PH_METRICS_FORCE"):
            return True
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return False
        try:
            return bool(self._cfg("metrics_enabled", True))
        except Exception:
            return True

    def _metrics_record(self, kind, agent="", usage=None, latency_ms=None, extra=None):
        """落一行计量记录。

        任何异常都吞掉——计量是旁路，绝不因它挂掉主流程。
        """
        if not self._metrics_enabled():
            return
        try:
            row = {
                "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "kind": kind,
                "agent": agent or "",
                "in_other": 0,
                "in_cached": 0,
                "out": 0,
                "total": 0,
                "latency_ms": latency_ms,
            }
            if usage is not None:
                row["in_other"] = int(getattr(usage, "input_other", 0) or 0)
                row["in_cached"] = int(getattr(usage, "input_cached", 0) or 0)
                row["out"] = int(getattr(usage, "output", 0) or 0)
                row["total"] = int(getattr(usage, "total", 0) or 0)
            if extra:
                row["extra"] = extra
            path = self._metrics_path()
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[parallel_handoff] metrics 记录失败（已忽略）：{e}")

    async def on_llm_response_metrics(self, event, response):
        """主代理 LLM 响应钩子：只记主代理侧用量。"""
        try:
            self._metrics_record(
                "main",
                agent="__main__",
                usage=getattr(response, "usage", None),
                extra={"umo": str(getattr(event, "unified_msg_origin", "") or "")},
            )
        except Exception:
            pass

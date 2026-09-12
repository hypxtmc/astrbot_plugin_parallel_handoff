"""子代理会话历史持久化（一期：会话落盘）。

## 为什么要有这个文件

`ctx_engine.histories` 原本是纯内存 dict，进程一重启就归零——"重启的是主代理，
失忆的是子代理"。用户设的 85 轮上下文因此只在单次进程生命周期内有效，这让
子代理永远当不成"常驻的同事"，只能当"临时工"。

本模块把对话历史落到磁盘，让 85 轮上下文跨重启有效。

## 设计（复用插件内既有模式，不另起炉灶）

插件的 `side_pulse.py` 已经有一套成熟的 append-only JSONL 落盘 + 容错读回
（`_pulse_append` / `_pulse_read_day`）。本模块沿用同一套路子：

- **append-only JSONL**：一行一条，只追加不重写
- **原子写**：临时文件 + `os.replace`，避免半截文件
- **读回容错**：文件缺失、单行损坏 → 跳过该行，绝不致命
- **按 (agent, session) 分文件**：避免单文件无限膨胀

## 磁盘形状

    <plugin_data>/sessions/<agent>/<sess_hash>.jsonl
    {"role": "user", "content": "...", "ts": "2026-09-12T00:30:00+08:00"}

`agent` 走目录、`session` 走文件名（hash 化，避免 umo 里的特殊字符污染路径）。

## 与内存的关系

**磁盘为真源，内存为热窗口。** `ctx_engine` 按 `MAX_MSGS` 截断的是**内存**，
磁盘保留全量——所以调小窗口不会导致历史永久丢失，重启后仍可恢复。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from astrbot.api import logger as _logger
except Exception:  # pragma: no cover - 测试环境可能无 astrbot
    import logging

    _logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
DEFAULT_RETENTION_DAYS = 30


def _safe_agent(agent: str) -> str:
    """agent 名做目录名前的净化：只留字母数字下划线连字符。"""
    return "".join(c for c in (agent or "unknown") if c.isalnum() or c in "_-") or "unknown"


def _sess_hash(session_id: str) -> str:
    """会话 id（umo）hash 化，取前 16 位，避免特殊字符污染路径。"""
    raw = (session_id or "").encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:16]


class SessionStore:
    """子代理会话历史的磁盘存储。

    典型用法：

        store = SessionStore(root)
        store.append("nova", umo, "user", "问题")
        store.append("nova", umo, "assistant", "回答")
        hist = store.load("nova", umo)        # → [{"role":..., "content":...}, ...]
        all_hist = store.restore_all()           # → {"nova:<hash>": [...]}
    """

    def __init__(self, root: str | os.PathLike, retention_days: int = DEFAULT_RETENTION_DAYS):
        self.root = Path(root)
        self.retention_days = int(retention_days)
        # key 格式与 ctx_engine 内部保持一致："{agent}:{session_id}"
        self._sess_to_hash: dict[str, str] = {}
        # TTL 清理的节流时间戳（见 _maybe_prune）
        self._last_prune: float = 0.0

    # ── 路径 ─────────────────────────────────────────────
    def _dir(self, agent: str) -> Path:
        return self.root / "sessions" / _safe_agent(agent)

    def _path(self, agent: str, session_id: str) -> Path:
        return self._dir(agent) / f"{_sess_hash(session_id)}.jsonl"

    # ── 写 ───────────────────────────────────────────────
    def append(self, agent: str, session_id: str, role: str, content: str) -> bool:
        """追加一条消息。失败非致命（返回 False），不阻断对话。"""
        try:
            path = self._path(agent, session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "role": "user" if role == "user" else "assistant",
                "content": content or "",
                "ts": datetime.now(CST).isoformat(timespec="seconds"),
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._maybe_prune()
            return True
        except Exception as exc:  # noqa: BLE001
            _logger.warning("[session_store] 落盘失败(%s/%s): %s", agent, session_id, exc)
            return False

    # ── 读 ───────────────────────────────────────────────
    def _read_file(self, path: Path) -> list[dict]:
        """读单个 JSONL；缺行/坏行跳过，绝不抛。"""
        out: list[dict] = []
        if not path.is_file():
            return out
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict) and rec.get("content") is not None:
                        out.append(rec)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("[session_store] 读盘失败(%s): %s", path, exc)
        return out

    def load(self, agent: str, session_id: str) -> list[dict]:
        """读回指定会话的全部历史。

        **这是主路径**：ctx_engine 惰性调用——只有某个会话被访问时才读它的盘，
        不做启动全量扫盘。这样 key 空间天然对齐（都用 agent+session 算），
        也无需把原始 session_id 反解出来。
        """
        return self._read_file(self._path(agent, session_id))

    def restore_all(self) -> dict[str, list[dict]]:
        """扫盘返回全部会话（可选预热用，非主路径）。

        注意 key 空间是 `{agent}:{hash}`，与 ctx_engine 的 `{agent}:{session_id}`
        不同——因为文件名只留了 hash，拿不回原始 session_id。故本方法**不用于**
        重建 ctx_engine 状态，仅用于统计/巡检/清理前的盘点。
        """
        result: dict[str, list[dict]] = {}
        base = self.root / "sessions"
        if not base.is_dir():
            return result
        for agent_dir in sorted(base.iterdir()):
            if not agent_dir.is_dir():
                continue
            agent = agent_dir.name
            for f in sorted(agent_dir.glob("*.jsonl")):
                msgs = self._read_file(f)
                if msgs:
                    result[f"{agent}:{f.stem}"] = msgs
        return result

    def stats(self) -> dict:
        """盘点：会话数、消息总数、占用字节。"""
        base = self.root / "sessions"
        sessions = 0
        messages = 0
        size = 0
        if base.is_dir():
            for f in base.glob("*/*.jsonl"):
                sessions += 1
                size += f.stat().st_size
                messages += sum(1 for _ in self._read_file(f))
        return {"sessions": sessions, "messages": messages, "bytes": size}

    def key_of(self, agent: str, session_id: str) -> str:
        """给 ctx_engine 用的规范 key（与 restore_all 的 key 空间对齐）。"""
        return f"{_safe_agent(agent)}:{_sess_hash(session_id)}"

    # ── 清理 ─────────────────────────────────────────────
    def _maybe_prune(self) -> None:
        """节流触发一次 TTL 清理：每 24 小时最多跑一遍。

        为什么不放在 `__init__`：插件是长驻进程，几个月不重启很正常，
        启动时清一次等于没清。挂在 `append` 上能覆盖长期运行，
        而 24 小时的节流把 `glob` 的开销压到可以忽略（每次 append 只多一次
        float 比较）。

        任何异常只记日志——清理是后台维护，绝不能让它影响落盘主路径。
        """
        now = time.time()
        if now - self._last_prune < 86400:
            return
        self._last_prune = now
        try:
            removed = self.prune()
            if removed:
                _logger.info("[session_store] TTL 清理：删除 %d 个超期会话文件", removed)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("[session_store] 定期清理失败: %s", exc)

    def prune(self) -> int:
        """按 mtime 清理超期会话文件。返回清理数量。"""
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        base = self.root / "sessions"
        if not base.is_dir():
            return 0
        for f in base.glob("*/*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except Exception:  # noqa: BLE001
                continue
        # 清空的 agent 目录也顺手收掉
        # 用 rmdir 而非 rmtree：语义更硬——目录非空直接报错，
        # 一条不该删的路径都碰不着（rmtree 配 ignore_errors 会静默吞掉意外）
        for d in base.iterdir():
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except Exception:  # noqa: BLE001
                continue
        return removed

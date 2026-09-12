"""router.py — 小模型路由层（三层降级：T1 规则 / T2 小模型 / T3 兜底主代理）

策略：在 OnWaitingLLMRequestEvent（internal.py:217，主代理 LLM 调用前最早停点）里
判断本条消息该找谁。命中直接子代理直发 + event.stop_event()，整个主代理流程
（记忆召回/req 构建/LLM 调用）短路跳过；未命中一律落回原主代理路径，行为零变化。

层级设计：
- T1 规则层：点名（中文名/英文 id + 边界）或强领域词命中，0 LLM 成本，毫秒级
- T2 小模型路由：glm-4-flash（默认 dmxapi/glm-4-flash），单条消息判向，
  JSON 输出 {route, confidence}；confidence >= 阈值(默认0.8)才直连，其余落 main
- T3 兜底层：任何异常/超时/低置信 -> 不拦截，主代理原路径全量接管

安全边界：
- enable_smart_router 默认关闭，显式开启才生效（防止误伤现网行为）
- 子代理直发复用 dispatch._call_one -> call_subagent -> direct 转发链路，
  与手动调 call_subagent 完全同构，无新发送通道
- T2 只读本消息，不注入历史不召回记忆，超时 5s 直接放行主代理
"""
import asyncio
import json
import os
import re
import time
from collections import deque

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from astrbot.core.star.filter.custom_filter import CustomFilter
except ImportError:  # pragma: no cover - 单测 mock 环境
    CustomFilter = None
try:
    from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS
except ImportError:  # pragma: no cover - 单测 mock 环境
    _ACTIVE_AGENT_RUNNERS = None

if CustomFilter is not None:  # pragma: no cover - 线上分支
    _BusyFilterBase = CustomFilter
else:  # pragma: no cover - 单测 mock 环境
    class _BusyFilterBase:
        """mock 环境的空基类：仅保证类可定义、可实例化，filter 逻辑不变"""

        def __init__(self, raise_error: bool = True, **kwargs) -> None:
            self.raise_error = raise_error


class BusyRunnerFilter(_BusyFilterBase):
    """[Busy Bypass 2026-08-31] 忙碌旁路过滤器：仅当主代理有活跃 agent runner
    （正在干活/工具链执行中）时接管该消息的检查。

    在 waking_check 阶段评估（消息入口最早一站）。通过 → handler 被收集进
    activated_handlers → 在 star_request_sub_stage（先于 follow-up 捕获）执行。
    未通过 → handler 完全不激活，消息路径零变化。
    """

    def filter(self, event, cfg) -> bool:
        if _ACTIVE_AGENT_RUNNERS is None:
            return False
        return event.unified_msg_origin in _ACTIVE_AGENT_RUNNERS


def _load_router_tables() -> dict:
    """从插件 data/ 目录加载路由数据表（不进仓库；缺文件时返回空表）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "router_tables.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            logger.info("[router] 已加载 router_tables.json（%d 组数据）", len(data))
            return data
        return {}
    except FileNotFoundError:
        logger.info("[router] 数据文件 router_tables.json 不存在，使用空表")
        return {}
    except Exception as exc:
        logger.warning("[router] 数据文件 router_tables.json 加载失败（用空表继续）: %s", exc)
        return {}


# ── 主代理锁持久化（2026-09-12 用户 bug 治本）─────────────────
# 背景：主代理锁此前是纯内存态，每次热重载/重启即丢失，用户看到「刚锁上又被
# T1 抢走」。根因不是锁逻辑，而是锁没落盘。此处加写穿持久化：建锁/解锁即时
# 写 data/main_lock.json，实例重启或热重载后首次访问时懒加载恢复。
_MAIN_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "main_lock.json"
)


def _load_main_lock_file() -> dict:
    """从磁盘加载主代理锁（热重载/重启后恢复）。缺文件返回空 dict。"""
    try:
        with open(_MAIN_LOCK_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("[router] 主代理锁文件加载失败（按空锁继续）: %s", exc)
    return {}


def _save_main_lock_file(lock: dict) -> None:
    """把主代理锁写盘（原子替换，杜绝热重载丢失）。"""
    try:
        os.makedirs(os.path.dirname(_MAIN_LOCK_PATH), exist_ok=True)
        tmp = _MAIN_LOCK_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lock, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _MAIN_LOCK_PATH)
    except Exception as exc:
        logger.warning("[router] 主代理锁写盘失败（内存依旧生效）: %s", exc)


_ROUTER_TABLES = _load_router_tables()
# [2026-09-12 用户指定] "主代理"是全体部署者通用的强锁入口词，不依赖
# 各自部署的主代理名（如"主代理"）；两者并存，重复去重。别的用户装插件直接用
# /主代理 即可建锁，无需知道部署者的主代理叫什么。
_GENERIC_MAIN_TOKENS = ("主代理", "主agent")
_MAIN_TOKEN_SET = tuple(
    dict.fromkeys([*_ROUTER_TABLES.get("main_token_set", []), *_GENERIC_MAIN_TOKENS])
)



class RouterMixin:
    """三层路由实现（装饰器 @filter.on_waiting_llm_request 保留在 main.py 壳方法上）"""

    # ── 会话延续判词（T1.5 层，2026-08-21 新增） ──
    # 纯承接句检测：剥离这些承接词 + 标点空白后应无残留。
    # 用于"好舒服，继续""再来""嗯"等上一条已路由给子代理的短承接消息，
    # 时间窗内无新点名时直接续接上次路由对象，避免 T2 因单消息无上下文误放行主代理
    T1_CONTINUE_WORDS = tuple(_ROUTER_TABLES.get("continue_words", []))
    _T1_CONTINUE_WORD_RE = re.compile("|".join(T1_CONTINUE_WORDS)) if T1_CONTINUE_WORDS else None

    T1_KEYWORDS = _ROUTER_TABLES.get("keywords", {})
    # 爱称别名（2026-08-30 用户指定）：直呼爱称 → 对应子代理 T1 命中
    T1_ALIASES = _ROUTER_TABLES.get("aliases", {})
    # 最高优先级令牌（2026-09-03 用户指定）：只要消息含主代理的专属令牌词，
    # 无论 T1 点名 / T1.5 续接 / T2 小模型判定结果如何，一律放行主代理。
    # 放在路由链最前，任何子代理都不允许接管主代理。
    # [2026-09-12] 正则 = 部署者主代理名 + 通用入口词（"主代理"），
    # 任何部署环境都能用通用词强锁主代理；未配置 main_token 时仅通用词生效。
    _MAIN_TOKEN_RE = (
        re.compile(f"({_ROUTER_TABLES['main_token']}|主代理|主agent)")
        if _ROUTER_TABLES.get("main_token")
        else re.compile(r"(主代理|主agent)")
    )
    # T2 判向时给模型看的子代理职责简介（简写，不涉及人格机密）
    T2_AGENT_BRIEF = _ROUTER_TABLES.get("t2_brief", {})

    # ── 配置读取（全走 _cfg 兜底，未配置项全部返回安全默认） ──
    def _router_enabled(self) -> bool:
        return bool(self._cfg("enable_smart_router", False))

    def _router_threshold(self) -> float:
        try:
            return float(self._cfg("router_confidence_threshold", 0.8))
        except (TypeError, ValueError):
            return 0.8

    def _router_provider(self) -> str:
        return str(self._cfg("router_provider_id", "dmxapi/glm-4-flash")).strip()

    def _router_timeout(self) -> float:
        try:
            return float(self._cfg("router_timeout", 5))
        except (TypeError, ValueError):
            return 5.0

    def _router_agent_pool(self) -> dict:
        """路由目标池：直发名单 ∩ 有负责人格的子代理（默认 9 人）"""
        pool = dict(self.T2_AGENT_BRIEF)
        raw = str(self._cfg("direct_delivery_agents", "")).strip()
        ids = {a.strip().lower() for a in raw.split(",") if a.strip()}
        if ids:
            pool = {k: v for k, v in pool.items() if k in ids}
        return pool

    # ── T0 命令式触发层（2026-09-08 用户指定）────────────
    # 以 / 开头 + 名字（可 + 连接多名字）的显式命令，直接指定目标子代理/主代理，
    # 取代「从自然语言关键词猜测路由目标」的旧机制。命令命中 → 最高优先级短路，
    # 跳过 T1/T0.5/T2 全部猜测层，零正则歧义、零误触发。
    # 格式：/助手A · /助手A+助手C · /助手C+助手D+助手G · /主代理+助手A+助手C
    # 不设上限，点名几个就锁定几个；命令持续生效（写入粘滞锁，之后无需再发命令）。
    # Python 同款命令见系统提示「以 / 开头指定」，与下方实现保持一致。
    # 2026-09-11 用户报 bug 治本：AstrBot 唤醒层命中全局 wake_prefix（默认 ["/"]）后
    # 会把前缀从 event.message_str 里剥掉（waking_check/stage.py:123），所以 T0 判定
    # 不能只看 message_str——统一改用 _raw_command_text(event) 从消息段拼回原文。
    # 同时把可用前缀扩成一组（/ 全角／ # ！ ! 、），用户在 QQ 上发哪个都能锁。
    _CMD_PREFIXES = ("/", "／", "#", "！", "!", "、")
    _CMD_RE = re.compile(
        r"^[/／#！!、]([^\s/／#！!、]+(?:[/+、，,，][^\s/／#！!、]+)*)\s*$", re.M
    )
    # ── T0 管理命令（软入口·A 方案 2026-09-12 用户指定）──────────────
    # /谁在 /复位 /列表 —— 会话柜台三件套：查锁、放锁、看名单。
    # 判定先于点名（管理词不是 agent 名，天然不冲突）；且必须整条消息只有命令本体，
    # 「/谁在 顺便说个事」这类带尾巴的不拦截，照常进消息流（防误吞正式内容）。
    _ADMIN_RE = re.compile(
        r"^[/／#！!、](谁在|状态|复位|解除|放开|释放|列表|名单)\s*$", re.M
    )

    def _extract_original_from_raw(self, raw) -> str:
        """从平台原始事件里抠出未经改写的纯文本（2026-09-12 加固）。

        为什么需要它：唤醒层只改 message_str，09-11 我以为消息段没被碰，
        实测发现段里那份也可能是剥过的（用户实测「/子代理名」仍落 T1，
        日志里原文已无斜杠）。而 message_obj.raw_message 是平台原始事件，
        唤醒层绝不碰，是最后一份可信原文。

        兼容两种形态：
          - dict（aiocqhttp/OneBot）：取 raw_message / message 字段
          - 对象式适配器：取同名属性
        只认纯文本（type 为 None/text）；拿不到返回 ""。
        """
        if not raw:
            return ""
        if isinstance(raw, dict):
            for key in ("raw_message", "message"):
                v = raw.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
                if isinstance(v, list):
                    parts = []
                    for seg in v:
                        if not isinstance(seg, dict):
                            continue
                        if seg.get("type") not in (None, "text"):
                            continue
                        data = seg.get("data")
                        t = data.get("text") if isinstance(data, dict) else seg.get("text")
                        if isinstance(t, str) and t:
                            parts.append(t)
                    if parts:
                        return "".join(parts).strip()
            return ""
        for key in ("raw_message", "message"):
            v = getattr(raw, key, None)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def _raw_command_text(self, event: AstrMessageEvent) -> str:
        """还原含前缀的原始用户消息文本（2026-09-11 初版 / 2026-09-12 加固）。

        bug 复现：用户发「/助手A」，插件里 message_str 却是「助手A」，T0 命令式
        (startswith("/")) 永远不成立 → 命令强锁形同虚设，消息落回 T1 猜名字。
        根因：AstrBot 唤醒层命中全局 wake_prefix（["/"]）后执行
        `event.message_str = event.message_str[len(wake_prefix):].strip()`
        （astrbot/core/pipeline/waking_check/stage.py:123），前缀被剥掉。

        2026-09-12 二次报 bug：按 09-11 方案从消息段拼回，仍然拿不到斜杠——
        说明段里那份也是剥过的。改为三层取值，从最原始到最加工：
          ① message_obj.raw_message —— 平台原始事件，唤醒层绝不碰（最可信）
          ② event.get_messages() 段拼回 —— 09-11 的老路，能拿到就用
          ③ 都拿不到 → 返回 ""，由调用方回落 message_str
        """
        # ① 平台原始事件（最可信）
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            txt = self._extract_original_from_raw(raw)
            if txt:
                return txt
        except Exception as e:
            logger.debug(f"[parallel_handoff] raw_message 取原文失败: {e}")
        # ② 消息段拼回
        try:
            segs = event.get_messages()
        except Exception:
            return ""
        buf = []
        for seg in segs or []:
            txt = getattr(seg, "text", None)
            if isinstance(txt, str) and txt:
                buf.append(txt)
        return "".join(buf).strip()

    def _resolve_command_text(self, event: AstrMessageEvent, message: str) -> str:
        """解析出用于 T0 命令判定的文本（2026-09-12 四修）。

        三次踩坑史（都栽在「想把被剥掉的前缀还原回去」）：
          ① 直接用 message_str → 唤醒层已剥掉 "/"，startswith 永假
          ② 从 get_messages() 段拼回 → 段里那份同样是剥过的
          ③ 从 message_obj.raw_message 取 → 实测平台侧就已经没有 "/"

        四修改用「框架自己的判据」：唤醒层命中 wake_prefix 时，会同时置
        `event.is_at_or_wake_command = True`
        （astrbot/core/pipeline/waking_check/stage.py:113-122）——
        这个标志才是「用户显式用了前缀 / @」的权威信号，**不依赖任何文本还原**。
        而 message_str 被剥后剩下的，正是命令正文。

        所以：标志为真且文本无前缀 → 补一个虚拟前缀，
        让下游 _CMD_PREFIXES / _CMD_RE 的现有逻辑原封不动地跑。
        """
        raw = self._raw_command_text(event) or message
        if raw.startswith(self._CMD_PREFIXES):
            return raw
        # 严格 `is True`：唤醒层赋的就是字面 True（waking_check/stage.py:114）。
        # 不能用宽松判真——MagicMock 的任意属性访问都返回 truthy 的 Mock 对象，
        # 宽松写会把测试与任何 mock 场景误补前缀，把自然语言点名错当命令锁。
        if getattr(event, "is_at_or_wake_command", False) is True:
            return "/" + raw
        return raw

    # 呼叫词（锁仲裁用：锁在场时，只有「前缀命令」或「呼叫词 + 已知名字」才算换人）
    _TRANSFER_WORDS = ("换", "找", "叫", "让", "请", "喊", "召", "切到", "换成", "过来")

    def _is_explicit_transfer(self, message: str) -> bool:
        """本条是否构成「显式转移」（换人意图）——2026-09-11 用户口径。

        是：前缀命令（/、#、！、全角／…）、呼叫短语（换 X / 找 X / 叫 X 过来）、
            极短消息且只含一个已知代理名（≤6 字）
        不是：长句叙述里顺带出现名字（这正是过去把锁带跑的元凶）
        """
        m = (message or "").strip()
        if not m:
            return False
        if m.startswith(self._CMD_PREFIXES):
            return True
        if len(m) <= 6 and self._t1_route(m):
            return True
        hits = self._t1_route(m) or self._t1_route_multi(m)
        if not hits:
            return False
        if any(w in m for w in self._TRANSFER_WORDS):
            return True
        # 句首点名：名字/爱称出现在开头且后接分隔标点（如「助手C，你来接」）；
        # 名字作主语的长句叙述（「助手D刚才说的那个 bug…」）不算。
        for sep in ("，", "、", ",", "：", ":", " "):
            idx = m.find(sep)
            if 0 < idx <= 8 and self._t1_mentions(m[:idx]):
                return True
        return False

    def _sticky_group_of(self, event: AstrMessageEvent):
        """取当前会话粘滞在册组（池内有效成员），用于锁仲裁；无锁返回 []。"""
        last, _ = self._route_mem()
        group = last.get(event.unified_msg_origin) or {}
        if not group:
            return []
        pool = self._router_agent_pool()
        return [aid for aid in group if aid in pool]

    def _parse_agent_command(self, message: str):
        """解析命令式触发。返回 (agents_list, has_main)。

        agents_list: 命令点名的子代理 aid 列表（排除主代理，主代理是主代理本体）
        has_main: 命令里是否含「主代理」（标记主代理在场：不调度、放行主代理，
                    其余点名子代理由主代理并行调度（relay 汇总或直发按 mode））。
        非命令 / 全未知 → (None, False)。
        """
        if not message or not message.startswith(self._CMD_PREFIXES):
            return None, False
        m = self._CMD_RE.match(message)
        if not m:
            return None, False
        # 解析各段：/A+B+C（兼容 +、/、中英文顿逗号 作分隔）
        raw_seg = m.group(1)
        parts = re.split(r"[/+、，,，]", raw_seg)
        parts = [p.strip() for p in parts if p.strip()]
        if not parts:
            return None, False
        # 名字→aid 映射池（中文显示名 + agent id + 爱称别名）
        pool = self._router_agent_pool()
        name2aid = {aid: aid for aid in pool}
        disp_map = self._get_name_display_map() or {}
        for aid, cn in disp_map.items():
            name2aid[str(cn)] = aid
            name2aid[str(aid)] = aid
        for aid, aliases in self.T1_ALIASES.items():
            for al in aliases:
                name2aid[str(al)] = aid
        agents = []
        has_main = False
        unknown = []
        for p in parts:
            if p in _MAIN_TOKEN_SET:
                has_main = True
                continue
            aid = name2aid.get(p)
            if aid and aid in pool and aid not in agents:
                agents.append(aid)
            else:
                unknown.append(p)
        if not agents and not has_main:
            # 全未知（如 /foo /bar）→ 非有效命令，回退 T1 自然语言路径
            return None, False
        if unknown:
            logger.info(f"[parallel_handoff] 命令式含未知目标 {unknown}，已忽略（已知目标照常锁定）")
        return (agents or None), has_main

    def _parse_admin_command(self, message: str) -> str:
        """[软入口·A 方案 2026-09-12] 解析会话管理命令。

        返回 'status' / 'reset' / 'list'；非管理命令返回 ""。
        与 T0 点名互斥：管理词先判（_ADMIN_RE 整句匹配），命中即短路返回。
        """
        if not message:
            return ""
        m = self._ADMIN_RE.match(message)
        if not m:
            return ""
        word = m.group(1)
        if word in ("谁在", "状态"):
            return "status"
        if word in ("复位", "解除", "放开", "释放"):
            return "reset"
        return "list"

    def _admin_reply_text(self, event, cmd: str) -> str:
        """生成管理命令回执文案；reset 顺带清锁（命令锁 + 粘滞锁 + 直发参照）。

        拆成同步纯逻辑便于测试；发送由 _send_admin_reply 的 async 薄壳负责。
        """
        disp = self._get_name_display_map() or {}

        def _cn(aid):
            return str(disp.get(aid, aid))

        if cmd == "status":
            locked = []
            if getattr(self, "_cmd_lock", None):
                locked = list(self._cmd_lock.get(event.unified_msg_origin, {}).keys())
            if locked:
                names = " + ".join(_cn(a) for a in locked)
                return f"📍 现在锁着：{names}\n（换人：/名字 · 放开：/复位）"
            if self._main_locked(event):
                return "📍 现在锁着：主代理\n（换人：/名字 · 放开：/复位）"
            return "📍 没锁着谁，消息自动分派\n（指定：/名字 · 名单：/列表）"

        if cmd == "reset":
            if getattr(self, "_cmd_lock", None):
                self._cmd_lock.pop(event.unified_msg_origin, None)
            self._clear_main_lock(event)
            last, _ = self._route_mem()
            last.pop(event.unified_msg_origin, None)
            if getattr(self, "_route_reply", None):
                self._route_reply.pop(event.unified_msg_origin, None)
            return "✅ 放开了，消息回到自动分派"

        if cmd == "lock_main":
            # [主代理锁回执 2026-09-12 20:57 顾主要求] 发 /主代理 后明确回报已锁定，
            # 而不是默默放行让消息落进主代理 LLM。
            return (
                "🔒 已锁定：主代理\n"
                "（消息直通主代理，提子代理名也不会切 · 换人：/名字 · 放开：/复位）"
            )

        pool = self._router_agent_pool()
        names = " · ".join(_cn(a) for a in pool)
        return f"📋 可点名：{names}\n（发 /名字 直接锁定）"

    async def _send_admin_reply(self, event, cmd: str) -> None:
        """发送管理命令回执（发送失败不炸路由，仅记日志）。"""
        try:
            text = self._admin_reply_text(event, cmd)
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain
            await event.send(MessageChain([Plain(text)]))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[parallel_handoff] 管理命令回执发送失败: {e}")

    # ── T1 规则层 ────────────────────────────────────────
    # 报错/日志/代码强特征：命中且整条无呼叫词（找/叫/让/喊…）→ 判定为技术文本，
    # 子代理名此时多为报错主体/路径/引用，不构成点名，直接放行 main/T2；
    # 防止 "module 'agent_c' not found" 这类把日志里的名字当呼叫乱路由
    _T1_ERR_RE = re.compile(
        r"(?:traceback|exception|error|failed|failure|report|warning|panic|crash|"
        r"stderr|stdout|报错|异常|超时|timeout|not\s+found|import\s+error|"
        r"module\s|missing\s|undefined|(?:file|line)\s+\d+|```|`[^`]*`)", re.I)
    # 叙述尾：名字后面直接紧跟这些 → 是"提及"，不是"呼叫"
    # 2026-08-30："又"收紧为必须后接叙述动词（"奸商又在坑我钱"是抱怨非叙述，"助手A又做饭了"才是）；
    # 疑问句（吗/么/呢/吧/？）不视为叙述（"助手A在画室吗"是询问非转述）
    _T1_NARR_TAIL_RE = re.compile(
        r"^(?:的|了|过|说|说过|说道|提到|提起|曾经|上次|之前|昨天|刚才|"
        r"和他|和她|和他们|和她们|跟|与|同|也|还|在|去|来|过|离开|走了|"
        r"不在|回来|她说|他说|他俩|以前|我记得|好像|确实|她|他|她们|他们|"
        r"又(?=(?:说|聊|讲|做|来|走|去|提|问|约|找|给|学|看|听|读|写|唱|画|吃|喝|玩|买|卖|拿|送|教|带|陪|叫|喊|唤|让|请|劝|骂|夸|记|忘))|"
        r"聊(?:了|过|起|到|的)|谈(?:了|过|起|到|的)|讲(?:了|过|起|到|的)|"
        r"说(?:得|道|的|了)|讲到|提起)", )
    _T1_QUESTION_RE = re.compile(r"[吗么呢吧？?]")
    # 呼叫尾：名字后跟 称呼语/逗号 + 语气词簇（那/再/想/要/还…）+ 拜请动词 → 强点名
    # 语气词簇吞掉"那再亲亲"里拦路的"那再"，让"可可爱爱XX，那再亲亲好不好"这类
    # 委婉祈使也命中；纯称呼（"想我了吗"）依然被动词表拒绝，安全
    _T1_CALL_TAIL_RE = re.compile(
        r"(?:，|,|：|:|！|!|？|\?|\s)*(?:(?:那|再|想|要|还|又|也|快|就|现在|我|人家|好想|真的)*"
        r"(?:来|过来|帮我|帮|陪|给|教|带|看看|看|唱|画|做|写|念|读|听|拿|弄|泡|抱|亲|贴|"
        r"在吗|在么|理理|理我|讲讲|推荐|安排|约|去|回来|来一下|出来|睡了没|吃饭没|快|快来|"
        r"聊(?:聊|会|两句|个|天)|讲(?:讲|个|点)|说吧|说两句))", )
    # 呼叫前缀：名字前面直接是 找/叫/喊/唤/让/请/问/约/带 等 → 强点名
    _T1_CALL_PREFIX_RE = re.compile(
        r"(?:想找|去找|帮我找|叫她|叫他|叫|喊|唤|找|让|请|问|约|召唤|"
        r"去\s*(?:找|叫)|快叫|帮我叫)", )
    # [多点名专用 CALL_TAIL 2026-09-07] 在原修饰词簇追加 我们/你们/咱们/一起，
    # 使「助手A，助手D，我们一起来玩吧」里"我们一起来"能命中"来"作拜请动词。
    # 仅 `_t1_route_multi` 使用，单点名路径仍用原 `_T1_CALL_TAIL_RE`（零回归）。
    _T1_MULTI_CALL_TAIL_RE = re.compile(
        r"(?:，|,|：|:|！|!|？|\?|\s)*(?:(?:那|再|想|要|还|又|也|快|就|现在|我|人家|好想|真的|我们|你们|咱们|一起)*"
        r"(?:来|过来|帮我|帮|陪|给|教|带|看看|看|唱|画|做|写|念|读|听|拿|弄|泡|抱|亲|贴|"
        r"在吗|在么|理理|理我|讲讲|推荐|安排|约|去|回来|来一下|出来|睡了没|吃饭没|快|快来|"
        r"聊(?:聊|会|两句|个|天)|讲(?:讲|个|点)|说吧|说两句))",
    )

    def _t1_route(self, message: str):
        """点名/领域词直判（语境感知版）。命中返回 agent_name，否则 None。

        改进点（2026-08-21）：
        1. 技术文本整体拦截：报错/日志/代码块里出现子代理名不视为点名
        2. 语境区分：呼叫（找/叫/喊/称呼+祈使/单独称谓）才路由；
           叙述性提及（"某人说过""昨天和某人聊了"）放行 main/T2
        3. 多点名歧义：一次消息中出现多个子代理名 → 交 T2/main 仲裁，不盲选
        4. 单字名（单字名）强制词边界，杜绝"今年/除夕/命令"误伤
        """
        if not message:
            return None
        stripped = message.strip()
        if not stripped:
            return None
        disp_map = self._get_name_display_map() or {}
        candidates = {}
        for aid, cn in disp_map.items():
            candidates[cn] = aid
            candidates[aid] = aid
        # 爱称别名（2026-08-30 用户指定）：T1 直呼爱称同样命中对应子代理
        for aid, aliases in self.T1_ALIASES.items():
            for al in aliases:
                candidates[al] = aid
        sorted_names = sorted(
            {n for n in candidates if n and len(str(n)) > 0}, key=lambda s: len(str(s)), reverse=True
        )
        appeared = set()      # 消息中出现过的所有子代理名（无论语境）
        call_hits = set()     # 强呼叫（找/叫/让/祈使）命中的
        weak_hits = set()     # 弱点名（句首/极短消息）命中的
        narr_context = False  # 消息存在叙述性提及（含子代理名却非呼叫）
        for name in sorted_names:
            aid = candidates[name]
            name_s = str(name)
            if len(name_s) >= 2:
                # 多字名：前置边界只排字母数字——允许"叫助手B""找助手A"；
                # 防叙述交给语境判定（名后 的/了/过/说过/提到…）。
                # ASCII 英文 id（xi/nian…）加后置边界，防 "axios" 里误匹配 "xi"
                if name_s.isascii():
                    pattern = re.compile(
                        rf"(?<![0-9A-Za-z]){re.escape(name_s)}(?![0-9A-Za-z])"
                    )
                else:
                    pattern = re.compile(rf"(?<![0-9A-Za-z]){re.escape(name_s)}")
            else:
                # 单字名（单字名…）：前后都查，杜绝"今年/除夕/命令"误伤
                pattern = re.compile(
                    rf"(?<![0-9A-Za-z\u4e00-\u9fff]){re.escape(name_s)}(?![0-9A-Za-z\u4e00-\u9fff])"
                )
                # 单字名叙述锚 A：紧贴后续 说过/聊过/讲到/提到… 即转述语境
                # （"助手A聊过药膳""助手B说想去画画"→ 提及，非点名）
                cue = re.compile(
                    rf"(?<![0-9A-Za-z\u4e00-\u9fff])"
                    rf"{re.escape(name_s)}(?=(?:说|说过|说道|说了|聊|聊过|聊了|讲|讲过|讲到?|提|提到|提起|记得))"
                )
                if cue.search(stripped):
                    appeared.add(aid)
                    narr_context = True
                    continue
                # 单字名叙述锚 B：伴随介词（和/跟/与/同）+ 名字 + 转述动词
                # （"之前和助手A聊了药膳" → "和助手A"因词边界不相邻，此锚接住转述语境）
                cue_with = re.compile(
                    rf"(?:和|跟|与|同){re.escape(name_s)}"
                    rf"(?=(?:说过|说道|说|聊过|聊了|聊|讲过|讲|提到|提起|记得))"
                )
                if cue_with.search(stripped):
                    appeared.add(aid)
                    narr_context = True
                    continue
            for m in pattern.finditer(stripped):
                appeared.add(aid)
                pre = stripped[:m.start()]
                post = stripped[m.end():]
                # 1) 呼叫检测：名前呼叫动词 OR 名后称呼+拜请动词（最高优先）
                if self._T1_CALL_PREFIX_RE.search(pre[-8:]) or self._T1_CALL_TAIL_RE.match(post):
                    call_hits.add(aid)
                    continue
                # 2) 叙述尾：名字后紧跟叙述结构 → 提及，非呼叫（疑问句除外："助手A在画室吗"是询问）
                if self._T1_NARR_TAIL_RE.match(post) and not self._T1_QUESTION_RE.search(post):
                    narr_context = True
                    continue
                # 3) 弱点名：名字为消息头，或整条消息极短（"助手A"、"助手B"）→ 认
                if not pre.strip() or (not post.strip() and len(stripped) <= 12):
                    weak_hits.add(aid)
        # ── 多点名歧义 → 不盲选，交 T2/main ──
        if len(appeared) >= 2:
            return None
        # ── 技术文本拦截（报错/日志）→ 有强呼叫才放行，否则让 main 处理 ──
        if self._T1_ERR_RE.search(stripped) and not call_hits:
            return None
        # ── 单点名路由（2026-08-30 放宽：名字出现即路由，位置无关）──
        # 原来要求名字在句首/有呼叫动词才命中，导致"帮我看一下助手D""最近怎么样"
        # 这类名字在句中/句尾的消息落 T2。现放宽为：单点名 + 非叙述语境 → 直接路由。
        if len(appeared) == 1 and not narr_context:
            return next(iter(appeared))
        # ── 领域词降级：存在叙述语境不收，报错文本不收 ──
        if not (narr_context or self._T1_ERR_RE.search(stripped)):
            for aid, words in self.T1_KEYWORDS.items():
                for w in words:
                    if w in stripped:
                        return aid
        return None

    def _t1_route_multi(self, message: str):
        """[多点名强呼叫 2026-09-07 方案A] 返回强呼叫子代理列表（按原文出现顺序，有序）。

        当消息里**明确强呼叫 ≥2 个子代理**（如「助手A，助手D，我们一起来玩」）
        时返回保序 aid 列表；否则返回 None（交给单点名/T2/main）。

        背景：原 `_t1_route` 遇到多点名直接 `return None`（router.py:300-301），
        把「明确多点名呼叫」跟「叙述性提到多个名字」一刀切全挡回主代理，
        主代理被迫当主持人转述——违背用户 2026-09-07 记忆#4「点几个名就几个延续、
        主代理不插嘴」。本方法独立实现并复用相同判定正则，不动 `_t1_route` 返回契约，
        零侵入单点名/模糊提及路径。
        """
        if not message:
            return None
        stripped = message.strip()
        if not stripped:
            return None
        disp_map = self._get_name_display_map() or {}
        candidates = {}
        for aid, cn in disp_map.items():
            candidates[cn] = aid
            candidates[aid] = aid
        for aid, aliases in self.T1_ALIASES.items():
            for al in aliases:
                candidates[al] = aid
        # 强呼叫命中 → 记录 aid -> 最小匹配位置（同一名多次出现取最先）
        hits: dict = {}
        for name, aid in candidates.items():
            name_s = str(name)
            if not name_s:
                continue
            if len(name_s) >= 2:
                if name_s.isascii():
                    pattern = re.compile(rf"(?<![0-9A-Za-z]){re.escape(name_s)}(?![0-9A-Za-z])")
                else:
                    pattern = re.compile(rf"(?<![0-9A-Za-z]){re.escape(name_s)}")
            else:
                # 单字名（单字中文名）：强制词边界防误伤
                pattern = re.compile(
                    rf"(?<![0-9A-Za-z\u4e00-\u9fff]){re.escape(name_s)}"
                    rf"(?![0-9A-Za-z\u4e00-\u9fff])"
                )
            for m in pattern.finditer(stripped):
                pre = stripped[:m.start()]
                post = stripped[m.end():]
                # 叙述尾免疫：名字后紧跟叙述结构（且无比语气）→ 提及非呼叫
                if (
                    self._T1_NARR_TAIL_RE.match(post)
                    and not self._T1_QUESTION_RE.search(post)
                ):
                    continue
                call = bool(
                    self._T1_CALL_PREFIX_RE.search(pre[-8:])
                    or self._T1_MULTI_CALL_TAIL_RE.match(post)
                )
                if not call:
                    # 弱点名（句首/极短消息）也算强呼叫：如「助手A，助手D，来」/「小黑，小白」
                    if not pre.strip() or (not post.strip() and len(stripped) <= 12):
                        call = True
                    else:
                        continue
                if aid not in hits or m.start() < hits[aid]:
                    hits[aid] = m.start()
        if len(hits) < 2:
            return None
        # 按原文出现位置排序，返回保序 aid 列表
        return [aid for aid, _ in sorted(hits.items(), key=lambda kv: kv[1])]

    # ── 会话记忆（供 T1.5 续接 / T2 上下文注入）──────────────
    def _route_mem(self):
        """惰性初始化会话级路由记忆（main.py __init__ 不感知 mixin 私有状态）。

        2026-09-07 方案①升级：_route_last 从「单 agent 锁」升级为「在场者组」，
        支持 3P/多P 场次后无点名消息按多人组接龙（根因：旧结构只留最后一个 agent，
        部分子代理在多角色场次里天然掉队）。
        """
        if not hasattr(self, "_route_last"):
            self._route_last = {}      # session -> {aid: ts}（在场者组）
            self._route_msgs = {}      # session -> deque(最近用户消息)
            self._route_reply = {}     # session -> (agent_id, ts, reply_tail)
        return self._route_last, self._route_msgs

    def _record_cmd_lock(self, event, agents):
        """[T0 命令式强制锁定 2026-09-08 用户指定] 记录会话级命令锁定在场者组。

        命令锁定是独立于粘滞锁 _route_last 的最高权重强制锁：/<名A>+<名B> 一经建立，
        该会话直到被新命令或「主代理」文本解除前，永远只跟锁定组对话。
        与 _route_last 的区别：锁定组内出现任何其他子代理名都不会触发路由切换
        （点名只是对话内容，不是目标），T1/T0.5/T2 全部失效。
        """
        if not hasattr(self, "_cmd_lock"):
            self._cmd_lock = {}        # session -> {aid: ts}（命令强制在场者组）
        disp_map = self._get_name_display_map() or {}
        group = self._cmd_lock.setdefault(event.unified_msg_origin, {})
        for raw in agents:
            aid = raw
            for _aid, cn in disp_map.items():
                if str(cn) == raw or str(_aid) == raw:
                    aid = _aid
                    break
            group[aid] = time.time()

    def _cmd_locked_group(self, event):
        """[T0 命令式 2026-09-08] 取命令锁定在场者组；无命令锁返回 None。

        命令锁 = 最高权重强制锁，命中时 _smart_router_check 主流程直接短路，
        跳过 T1/T0.5/T2 全部判向，彻底杜绝「对话里点其他子代理名」触发切换。
        """
        if not hasattr(self, "_cmd_lock"):
            return None
        grp = self._cmd_lock.get(event.unified_msg_origin)
        if not grp:
            return None
        pool = self._router_agent_pool()
        live = {a: ts for a, ts in grp.items() if a in pool}
        if not live:
            return None
        if len(live) < 2:
            return [next(iter(live.keys()))]
        return list(live.keys())

    # ── 主代理锁（2026-09-12 用户指定）─────────────────
    def _record_main_lock(self, event):
        """建立会话级主代理锁：后续消息直通主代理，T1/T0.5/T2 全部让位。

        动机（2026-09-12 顾主反馈）：消息里提到子代理名（如「叫个子代理一起
        陪你看 bug」）会被 T1 抢走路由——用户明明是在对主代理说话。
        /主代理（或 /主代理）一经建立，整个会话直通主代理；
        仅新命令（/子代理名）或 /复位 能解除。与子代理命令锁互斥。
        """
        lock = self._main_lock_data()
        lock[event.unified_msg_origin] = time.time()
        _save_main_lock_file(lock)   # [持久化 2026-09-12] 建锁即时落盘，热重载/重启不丢
        # 与子代理锁互斥：清掉旧的子代理命令锁 / 粘滞锁
        if getattr(self, "_cmd_lock", None):
            self._cmd_lock.pop(event.unified_msg_origin, None)
        last, _ = self._route_mem()
        last.pop(event.unified_msg_origin, None)

    def _main_lock_data(self) -> dict:
        """懒加载主代理锁（首次访问从盘恢复；热重载/重启后依旧有效）。"""
        if not hasattr(self, "_main_lock"):
            self._main_lock = _load_main_lock_file()
            if self._main_lock:
                logger.info(
                    "[router] 主代理锁已从磁盘恢复（%d 条）", len(self._main_lock)
                )
        return self._main_lock

    def _main_locked(self, event) -> bool:
        """该会话是否被主代理锁锁定（锁定后非命令消息直通主代理）。"""
        return event.unified_msg_origin in self._main_lock_data()

    def _is_real_command(self, raw_message: str) -> bool:
        """锁仲裁用：raw_message 是否「真命令」——带 / 且能解析出已知目标。

        [2026-09-12 21:25 根因修复] 唤醒层会把「被唤醒消息」重写为 '/原文'
        （"你和助手B..." → "/你和助手B..."）。此时 startswith("/") 为真、
        但解析不出任何已知代理/管理命令——那是被重写的普通消息，对主代理锁
        必须按「非命令」处理（直通主代理），否则消息会被 T0/T1 扫出子代理名
        路由走（21:06 实测 bug：锁在场仍被转给 agent_b）。
        """
        if not raw_message.startswith("/"):
            return False
        if self._parse_admin_command(raw_message):
            return True
        ca, cp = self._parse_agent_command(raw_message)
        return bool(ca or cp)

    def _clear_main_lock(self, event):
        """解除主代理锁（/复位 或建立新子代理命令锁时调用）。同步落盘。"""
        lock = self._main_lock_data()
        if event.unified_msg_origin in lock:
            lock.pop(event.unified_msg_origin, None)
            _save_main_lock_file(lock)

    def _record_direct_reply(self, session_id: str, agent_name: str, reply_text: str):
        """记录该 session 最近一次子代理直发回复尾部（供 T2 剧情参照，避免承接句判失）。"""
        if not session_id or not agent_name or not reply_text:
            return
        self._route_mem()  # 兜底初始化惰性记忆
        tail = reply_text.strip()[-300:]
        self._route_reply[session_id] = (agent_name, time.time(), tail)

    def _last_direct_reply(self, session_id: str, max_age: float = 600.0):
        """返回 (agent_name, reply_tail|None)；无记录或超时返回 (None, None)。

        max_age：参照有效期（秒），默认 600s。超时后不注入 prompt，
        避免数小时前的旧剧情误导 T2 判定（宽松于 T1.5 的 300s 续接窗）。
        """
        hit = self._route_reply.get(session_id)
        if not hit:
            return None, None
        agent, ts, tail = hit
        if time.time() - ts > max_age:
            return None, None
        return agent, tail

    def _record_user_msg(self, event: AstrMessageEvent, message: str):
        """把用户消息追加进会话最近消息环形缓冲（供 T2 上下文注入）。"""
        _, msgs = self._route_mem()
        sid = event.unified_msg_origin
        buf = msgs.get(sid)
        if buf is None:
            buf = msgs[sid] = deque(maxlen=6)
        buf.append(message)

    def _dedup_shortcircuit(
        self, event: AstrMessageEvent, message: str, raw_message: str = ""
    ) -> bool:
        """会话级消息去重屏障（2026-09-09 用户 bug 治本）。

        bug 复现：/<名A>+<名B> 命令锁 → 发"继续做爱"，T0.5 粘滞命中 nian+xi chained，
        但某位助手回了两遍。根因：_smart_router_check 挂在 OnWaitingLLMRequestEvent 上，
        同一条用户消息可能被顺序触发两次（follow-up / 管道二次遍历），且代码里
        唯一防重 _suppress_mainagent_prefix 只管"主代理工具续写尾巴"那个特定场景，
        对命令式/命令锁/粘滞chained/多点名等纯子代理短路路径完全不设防。
        第二次触发时 _route_last/_cmd_lock 都还在，粘滞重新命中 → 整条 chained
        再跑一遍 → 每个在场者都回两遍。

        治本：同一条完整消息文本，在短时间窗（_shortcircuit_dedup_window_sec，
        默认 12s）内对同一 session 第二次出现，直接 stop_event 吞掉，绝不重复
        路由。覆盖所有短路路径的通用屏障。
        返回 True = 已吞（去重），False = 正常放行。

        2026-09-11：显式命令豁免——用户连发两条相同前缀命令（如 /助手A）时，
        第二条必须照常执行，不能被去重窗口吃掉。
        """
        if (raw_message or message or "").lstrip().startswith(self._CMD_PREFIXES):
            return False
        if not hasattr(self, "_shortcircuit_last"):
            self._shortcircuit_last = {}  # session -> (hash, ts)
        sid = event.unified_msg_origin
        msg_hash = hash(message)
        now = time.time()
        prev = self._shortcircuit_last.get(sid)
        try:
            window = float(self._cfg("_shortcircuit_dedup_window_sec", 12))
        except (TypeError, ValueError):
            window = 12.0
        if prev and prev[0] == msg_hash and (now - prev[1]) <= window:
            logger.info(
                f"[parallel_handoff] SmartRouter: 同消息去重窗口 {window}s，吞掉重复触发 "
                f"（session={sid}，杜绝子代理重复回话）"
            )
            event.stop_event()
            return True
        self._shortcircuit_last[sid] = (msg_hash, now)
        return False

    def _continue_window(self) -> float:
        """会话续接时间窗（秒），默认 300s（5 分钟）。"""
        try:
            return float(self._cfg("router_continue_window_sec", 300))
        except (TypeError, ValueError):
            return 300.0

    def _t1_sticky_route(self, event: AstrMessageEvent, message: str):
        """[T0.5 层 2026-09-07 方案A粘滞锁定] 会话级连续路由：本次会话点名锁定了某代理
        → 其后每条消息默认由该代理直发处理（含技术请求，不再甩回主代理统帅），
        久聊不释放；除非本条出现新的子代理名（交给 T1 重新点名覆盖），
        或消息含「主代理」最高级令牌（由上层先行清锁，本层不接管）。

        取代旧 T1.5 的「纯承接短句 + 300s 时间窗 + 无新点名」弱续接设计，
        按用户 2026-09-07 指定改为会话级硬锁定（agent → 一路粘着，久聊不解放绑）。

        2026-09-07 方案①升级：_route_last 现为「在场者组」（session -> {aid: ts}）。
        返回：单人组 -> str（该 agent）；多人组（≥2 全在池）-> list[str]（整组，
        由 _smart_router_check 识别后走 chained 接龙，多P不掉队）；空/失效 -> None。
        """
        if not message:
            return None
        # 1) 会话存在已锁定的路由目标组
        last, _ = self._route_mem()
        group = last.get(event.unified_msg_origin)
        if not group:
            return None
        pool = self._router_agent_pool()
        # 2) 清理不在可用池的目标，并剔除过期者（沿用“能续多久”语义）
        now = time.time()
        live = {}
        for aid, ts in group.items():
            if aid in pool:
                live[aid] = ts
        if not live:
            return None
        # 3) 本条出现新的子代理名 → 仅当构成「显式转移」才让位给 T1 重新点名；
        #    长句里顺带提及则继续沿用旧锁（2026-09-11 用户口径：锁不被 T1 夺走）
        if self._t1_mentions(message) and self._is_explicit_transfer(message):
            return None
        # 4) 未点名 → 按在场者组粘滞
        if len(live) >= 2:
            # 多P场次：返回整组，交 _smart_router_check 走 chained 接龙（不掉队）
            return list(live.keys())
        # 单人：返回该 agent 直发
        return next(iter(live))

    def _record_route_hit(self, event: AstrMessageEvent, agent: str) -> None:
        """记录本次成功路由（供粘滞锁定续接：点名建立/切换后一路沿用）。
        2026-09-07 方案A：记录即会话锁定，久聊不释放；由新点名或主代理令牌覆盖/清除。
        2026-09-07 方案①：并入「在场者组」（session -> {aid: ts}），同名归一化。
        2026-09-09 用户 bug 治本（点名=换组）：单点名命中 → 覆盖在场者组为仅此一人。
        旧实现是并入（group[aid]=ts 不清旧成员），导致锁住一组后自然语言点「助手D」
        被 T1 当单点名追加进旧组，而不是换锁成助手D。用户心智：点名谁就是谁，
        新点名重定义在场者组，旧成员退出。无点名承接句才沿用原组（见 _t1_sticky_route）。
        """
        last, _ = self._route_mem()
        # 名字显示映射：存稳定 agent id 便于池校验与切回
        disp_map = self._get_name_display_map() or {}
        aid = agent
        for _aid, cn in disp_map.items():
            if str(cn) == agent or str(_aid) == agent:
                aid = _aid
                break
        group = last.get(event.unified_msg_origin, {})
        if aid in group:
            # [多代理保组 2026-09-09] 点名者是旧在场者组成员（如多场次中点名）→ 只刷新
            # 时间戳、不拆散整组。多P场次后续无点名消息仍按整组 chained 续接不掉队
            # （记忆#4 Bug B 修复语义，防 regression）。
            group[aid] = time.time()
            return
        # [点名=换锁 2026-09-09 用户 bug 治本] 单点名命中新面孔 → 覆盖在场者组为仅此
        # 一人。旧实现是并入（group[aid]=ts 不清旧成员），导致锁 nian+xi 后自然语言点
        # 「助手D」被 T1 当单点名追加进旧组，而不是换锁成助手D（用户 2026-09-09
        # 报：点名换锁失效，仍停在旧组）。用户心智：点名新面孔 = 换锁到 TA，
        # 旧在场者退出；点名在场者 = 只是跟 TA 说话，不拆 3P。
        last[event.unified_msg_origin] = {aid: time.time()}

    def _record_route_hits(self, event: AstrMessageEvent, agents) -> None:
        """[方案① 2026-09-07] 把一整组在场者写入粘滞记忆（多P场次用）。

        行内注意：agents 可为 list/tuple 等多点名返回值，统一并入同组。
        2026-09-09 用户 bug 治本（点名=换组）：整组覆盖写入，清掉旧在场者。
        与 _record_route_hit 对齐——多点名（如「助手A，助手D，一起来玩」）就是
        重新定义在场者组；若沿用并入，则「甲+乙」后再点「丙+丁」会变成
        4P 而用户心智是要换到新组。
        """
        last, _ = self._route_mem()
        disp_map = self._get_name_display_map() or {}
        fresh = {}
        for raw in agents:
            aid = raw
            for _aid, cn in disp_map.items():
                if str(cn) == raw or str(_aid) == raw:
                    aid = _aid
                    break
            fresh[aid] = time.time()
        last[event.unified_msg_origin] = fresh

    def _t1_mentions(self, message: str) -> set:
        """返回消息中出现过的所有子代理名集合（仅供 T1.5 防误续，维度与 T1 名称判定一致）。"""
        disp_map = self._get_name_display_map() or {}
        out = set()
        for aid, cn in disp_map.items():
            for name in (aid, cn):
                name_s = str(name)
                if not name_s:
                    continue
                if len(name_s) >= 2 and name_s.isascii():
                    p = re.compile(rf"(?<![0-9A-Za-z]){re.escape(name_s)}(?![0-9A-Za-z])")
                    if p.search(message):
                        out.add(aid)
                elif len(name_s) >= 2:
                    p = re.compile(rf"(?<![0-9A-Za-z]){re.escape(name_s)}")
                    if p.search(message):
                        out.add(aid)
                else:
                    # 单字名：三种语境任一命中即算提及（宁可多抓，兜底用于拒接）
                    # 1) 叙述锚：说/聊过/提到…  2) 介词前导：和助手A说…  3) 独立出现（后置仅排英文数字）
                    cue = re.compile(
                        rf"(?<![0-9A-Za-z\u4e00-\u9fff]){re.escape(name_s)}"
                        rf"(?=(?:说过|说道|说了|说|聊过|聊了|聊|讲过|讲|提到|提起|记得))"
                    )
                    cue_with = re.compile(
                        rf"(?:和|跟|与|同){re.escape(name_s)}"
                        rf"(?=(?:说过|说道|说|聊过|聊了|聊|讲过|讲|提到|提起|记得))"
                    )
                    standalone = re.compile(
                        rf"(?<![0-9A-Za-z\u4e00-\u9fff]){re.escape(name_s)}(?![0-9A-Za-z])"
                    )
                    if cue.search(message) or cue_with.search(message) or standalone.search(message):
                        out.add(aid)
        return out

    # ── T2 小模型层 ──────────────────────────────────────
    async def _t2_route(self, event: AstrMessageEvent, message: str):
        """glm-4-flash 判向。返回 (agent_name|None, confidence)。异常一律 (None, 0)。

        [上下文注入 2026-08-21] T2 不再只看单条消息：把最近会话用户消息序列
        （_route_msgs 环形缓冲，最近 4 条）注入 prompt，模型可据此判断
        "继续/再来" 是对上文的承接，避免零上下文必判 main 的缺陷。
        """
        pool = self._router_agent_pool()
        if not pool:
            return None, 0.0
        brief_lines = "\n".join(pool.values())
        _, msgs = self._route_mem()
        buf = msgs.get(event.unified_msg_origin)
        recent_lines = "\n".join(f"- {m[:120]}" for m in (list(buf)[-4:] if buf else []))
        reply_agent, reply_tail = self._last_direct_reply(event.unified_msg_origin)
        sys_prompt = (
            "你是消息路由判定器。根据用户最新一条消息判断该交给哪位角色回复。\n"
            "可选角色：\n"
            f"{brief_lines}\n"
            "- main：普通日常对话、跨角色问询、无法确定对象、技术任务（默认）\n"
            "最近对话（时间正序，仅用户消息）：\n"
            f"{recent_lines or '（无）'}\n"
            f"最近一次子代理直发回复尾部（剧情参照，可能正是用户承接的对象）：\n"
            f"{('（'+reply_agent+'）'+reply_tail) if reply_agent else '（无）'}\n"
            "只输出一行 JSON（禁止多余文字）：{\"route\": \"角色id或main\", \"confidence\": 0到1的小数}\n"
            "判定准则：用户明确点名或消息内容强相关才给高分；日常随意闲聊一律 main，confidence 给 0.1-0.3。\n"
            "若当前消息明显是对上文某位角色的承接（如继续/再来/嗯/然后呢/让我舒服/用力/爱你），"
            "route 应延续上文最后提到的角色；若最近一次子代理直发回复尾部语境强相关，也优先延续其子代理。"
        )
        provider = self._router_provider()
        try:
            llm_resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider,
                    prompt=message,
                    system_prompt=sys_prompt,
                ),
                timeout=self._router_timeout(),
            )
        except asyncio.TimeoutError:
            logger.info(f"[parallel_handoff] T2 router timeout after {self._router_timeout()}s -> main")
            return None, 0.0
        except Exception as e:
            logger.warning(f"[parallel_handoff] T2 router error: {e} -> main")
            return None, 0.0

        raw = (getattr(llm_resp, "completion_text", "") or "").strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            logger.warning(f"[parallel_handoff] T2 router non-JSON resp: {raw[:80]!r} -> main")
            return None, 0.0
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None, 0.0
        route = str(data.get("route") or "").strip().lower()
        try:
            conf = float(data.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        if route not in pool:
            route = None
        return route, conf

    # ── 主入口（main.py 壳方法 super() 转发到此处） ────────
    def _mode_shortcut_decision(self, event, message: str, route: str) -> bool:
        """[模式兼容 2026-08-31] T1/T2 命中后的模式裁决：是否允许短路直发。

        返回 True = 保持短路直发（原行为，call_subagent + stop_event）
        返回 False = 放行主代理（不 stop，让 directive 注入 + parallel_handoff 模式调度）

        冲突背景：T1/T2 命中直接 call_subagent 直发，绕过 tech_mode_config /
        affection_mode_config 模式调度——技术干活任务被单发直连，用户配置形同虚设；
        短路后主代理 LLM 不调用，directive 强制路由指令根本没机会注入。

        裁决规则（用户配置永远优先；2026-09-07 方案A 调整 tech 分支）：
        - 任务分类 tech（技术特征）+ 路由命中（点名/粘滞）→ **短路直发被点名者处理**
          （用户 2026-09-07 拍板：点名粘滞期间技术请求也归被点名子代理直发处理，
          不再放行主代理统帅收卷——旧 8-31 规则作废，因为统帅形态实际未生效）
        - 任务分类 affection → 按 affection_mode_config.route_mode：
            direct → 短路直发（贴贴快速直达）；relay → 放行主代理（回复返回主代理汇总）
        - 无法分类（None，纯点名无特征）→ 保持短路（原行为兜底）
        """
        try:
            task_kind = self._classify_directive_task(event)
        except Exception:
            return True
        if task_kind == "tech":
            # [2026-09-07 方案A粘滞锁定] 技术请求短路直发给被点名/粘滞的子代理处理，
            # 不再放行主代理统帅收卷。route 参数即 T1/T2/粘滞判定的目标代理。
            return True
        if task_kind == "affection":
            mcfg = self._get_mode_config("affection")
            rmode = str(mcfg.get("route_mode", "direct")).strip().lower()
            return rmode == "direct"
        return True

    # ── 判向目标传递（2026-08-31）────────────────────────
    # T1/T2 命中但模式裁决放行主代理时（tech 统帅收卷 / affection-relay），
    # 把 T1/T2 判定的路由目标暂存，directive 注入时附加给主代理，
    # 避免"裁决放行 → 判向目标丢失 → 主代理调错人/不调子代理"的断链。
    def _record_route_suggestion(self, agent: str):
        """记录 T1/T2 判向目标（供 directive 注入附加），30s 有效期。"""
        self._route_suggestion = ([agent], time.time())

    def _record_route_suggestions(self, agents):
        """[T0 命令式 2026-09-08] 记录多子代理判向目标（供 directive 注入附加），30s 有效期。"""
        self._route_suggestion = (list(agents), time.time())

    def _pop_route_suggestion(self) -> str | None:
        """读取未过期的判向目标建议（≤30s）的第一位，过期清除。"""
        sug = getattr(self, "_route_suggestion", None)
        if not sug:
            return None
        agents, ts = sug
        if time.time() - ts > 30:
            self._route_suggestion = None
            return None
        self._route_suggestion = (agents[1:], ts)
        return agents[0] if agents else None

    def _pop_route_suggestions(self) -> list:
        """[T0 命令式 2026-09-08] 读取全部未过期判向目标（供 directive 一次附加多代理），过期清除。"""
        sug = getattr(self, "_route_suggestion", None)
        self._route_suggestion = None
        if not sug:
            return []
        agents, ts = sug
        if time.time() - ts > 30:
            return []
        return list(agents)

    async def _busy_bypass_check(self, event: AstrMessageEvent) -> bool:
        """[Busy Bypass 2026-08-31] 消息入口旁路：主代理正在干活（活跃 runner）时，
        点名子代理的消息直接直发子代理，绕过 follow-up 捕获。

        背景：主代理工具链执行中（agent run 活跃）时，用户发来的新消息会被
        internal.py:194 的 try_capture_follow_up 吞进当前 run 的 follow-up ticket，
        OnWaitingLLMRequestEvent（T1/T2 路由唯一入口）根本不触发，消息混入主代理
        上下文，主代理只能边干活边手动调子代理。

        本方法挂在 @filter.custom_filter(BusyRunnerFilter) 的 AdapterMessageEvent
        handler 上（消息入口最早一站，waking_check 收集 → star_request_sub_stage
        执行，先于 agent_sub_stage 的 follow-up 捕获）：
        - filter 通过（该 UMO 有活跃 runner）→ 本 handler 执行
        - T1 点名命中 → call_subagent 直发 + stop_event()，
          stop_event 自动 set_result(MessageEventResult().stop_event())，
          ProcessStage 后半段 `(get_result and not is_stopped) or not get_result`
          判 False → agent_sub_stage 不再进入，follow-up 捕获被完整绕过
        - 未命中 → 不 stop，原样放行（消息照常进 follow-up 给主代理）
        """
        if not self._router_enabled():
            return False
        message = (event.get_message_str() or "").strip()
        if not message:
            return False
        # [唤醒来路 2026-09-12 四修] 唤醒层剥前缀时会置 is_at_or_wake_command，
        # 拿它当判据，不再赌「原文还能还原回来」
        raw_message = self._resolve_command_text(event, message)
        if raw_message != message:
            logger.info(
                f"[parallel_handoff] T0 命令原文解析: {message!r} → {raw_message!r} "
                f"(at_or_wake={getattr(event, 'is_at_or_wake_command', None)})"
            )
        # [软入口·A 方案 2026-09-12] 柜台命令 /谁在 /复位 /列表：
        # busy（主代理干活中）同样可查锁、放锁、看名单。
        admin_cmd = self._parse_admin_command(raw_message)
        if admin_cmd:
            logger.info(f"[parallel_handoff] BusyBypass: 管理命令 {admin_cmd} → 回执并短路")
            await self._send_admin_reply(event, admin_cmd)
            event.stop_event()
            return True
        # [主代理锁 2026-09-12 用户指定] busy 场景主代理锁同样直通：
        # 非命令消息全部放行主代理（含子代理名的句子只是对话，不切换路由）。
        if self._main_locked(event) and not self._is_real_command(raw_message):
            logger.info("[parallel_handoff] BusyBypass: 主代理锁在场 → 直通主代理")
            return False
        # [T0 命令式触发 2026-09-08 用户指定] busy 场景同样启用 / 命令：
        # 主代理正干活时，/<名A>、/<名A>+<名B> 等显式命令仍锁定并短路，不被 follow-up 吞。
        cmd_agents, cmd_main = self._parse_agent_command(raw_message)
        if cmd_agents and not cmd_main:
            logger.info(
                f"[parallel_handoff] BusyBypass: T0 命令式 {cmd_agents} → "
                f"锁定并短路（runner active 同样生效）"
            )
            calls = [{"agent_name": a, "input": message} for a in cmd_agents]
            try:
                await self.parallel_handoff(
                    event,
                    calls=calls,
                    call_mode="chained" if len(calls) > 1 else "direct",
                    route_mode="direct",
                    mode="affection",
                )
            except Exception as e:
                logger.error(
                    f"[parallel_handoff] BusyBypass: T0 命令式调用失败 {e}; release to main"
                )
                return False
            self._cmd_lock = {}
            self._record_cmd_lock(event, cmd_agents)
            event.stop_event()
            return True
        # 含主代理的命令（busy 时主代理在场优先；纯主代理建锁+回执短路，20:57 强化）
        if cmd_main:
            if getattr(self, "_cmd_lock", None):
                self._cmd_lock.pop(event.unified_msg_origin, None)
            if cmd_agents:
                self._record_route_hits(event, cmd_agents)
                self._record_route_suggestions(cmd_agents)
                logger.info(
                    f"[parallel_handoff] BusyBypass: T0 命令式含主代理令牌 → 放行主代理 "
                    f"(record lock for {cmd_agents})"
                )
                return False
            # 纯 /主代理 → 建锁 + 回执并短路（2026-09-12 20:57 顾主要求明确锁定标识）
            logger.info(
                "[parallel_handoff] BusyBypass: T0 命令式（纯主代理）→ "
                "建立主代理锁，回执并短路"
            )
            self._record_main_lock(event)
            await self._send_admin_reply(event, "lock_main")
            event.stop_event()
            return True
        # 再次确认活跃 runner（filter 通过后可能已结束，兜底）
        if _ACTIVE_AGENT_RUNNERS is None or event.unified_msg_origin not in _ACTIVE_AGENT_RUNNERS:
            return False
        # [T0 命令式强制锁定 2026-09-08 用户指定] busy 场景命令锁同样最高权重：
        # 锁定到场者组按锁组路由，对话提及他名也不切换。
        cmd_locked = self._cmd_locked_group(event)
        if cmd_locked:
            logger.info(
                f"[parallel_handoff] BusyBypass: T0 命令强制锁在场组 {cmd_locked} → "
                f"短路路由（runner active 也锁死，治本）"
            )
            calls = [{"agent_name": a, "input": message} for a in cmd_locked]
            try:
                await self.parallel_handoff(
                    event,
                    calls=calls,
                    call_mode="chained" if len(calls) > 1 else "direct",
                    route_mode="direct",
                    mode="affection",
                )
            except Exception as e:
                logger.error(
                    f"[parallel_handoff] BusyBypass: T0 命令锁调用失败 {e}; release to main"
                )
                return False
            event.stop_event()
            return True
        # [多点名强呼叫短路 2026-09-07 方案A] 主代理 busy 时多点名同走 chained 接龙
        #（若非 runner active 分支，本方法不会走到；这里补上保证 busy 也不被 follow-up 吞）
        multi = self._t1_route_multi(message)
        if multi and len(multi) >= 2:
            logger.info(
                f"[parallel_handoff] BusyBypass: 多点名强呼叫 {multi} → 短路 chained 接龙"
            )
            calls = [{"agent_name": a, "input": message} for a in multi]
            try:
                await self.parallel_handoff(
                    event,
                    calls=calls,
                    call_mode="chained",
                    route_mode="direct",
                    mode="affection",
                )
            except Exception as e:
                logger.error(
                    f"[parallel_handoff] BusyBypass: 多点名接龙调用失败 {e}; release to main"
                )
                return False
            event.stop_event()
            return True
        # [方案① 2026-09-07] 忙路旁路补 T0.5 粘滞多人组续接：
        # 主代理 busy 时，多P场次的「继续做爱/再来」等无点名承接句也会被 follow-up 吞，
        # 需在此（follow-up 捕获之前）按在场者组 chained 续接，不掉队、不靠手动调。
        sticky_multi = self._t1_sticky_route(event, message)
        if isinstance(sticky_multi, list) and len(sticky_multi) >= 2:
            logger.info(
                f"[parallel_handoff] BusyBypass: T0.5 粘滞多人组 {sticky_multi} → "
                f"短路 chained 接龙（多P场次续接，runner active 不掉队）"
            )
            calls = [{"agent_name": a, "input": message} for a in sticky_multi]
            try:
                await self.parallel_handoff(
                    event,
                    calls=calls,
                    call_mode="chained",
                    route_mode="direct",
                    mode="affection",
                )
            except Exception as e:
                logger.error(
                    f"[parallel_handoff] BusyBypass: 粘滞多人接龙调用失败 {e}; release to main"
                )
                return False
            event.stop_event()
            return True
        # [方案① 2026-09-07] 忙路旁路补 T0.5 粘滞单人续接（与 _smart_router_check 对齐）
        if isinstance(sticky_multi, str):
            route = sticky_multi
            if not self._mode_shortcut_decision(event, message, route):
                logger.info(
                    f"[parallel_handoff] BusyBypass: T0.5 route -> {route} "
                    f"但模式配置要求放行主代理（统帅收卷/relay），不短路"
                )
                self._record_route_suggestion(route)
                return False
            self._record_route_hit(event, route)
            logger.info(
                f"[parallel_handoff] BusyBypass: T0.5 route -> {route} "
                f"(runner active, skip follow-up capture)"
            )
            try:
                await self.call_subagent(event, agent_name=route, input=message)
            except Exception as e:
                logger.error(
                    f"[parallel_handoff] BusyBypass T0.5 direct call failed: {e}; release to main"
                )
                return False
            event.stop_event()
            return True
        route = self._t1_route(message)
        if not route:
            return False
        # [模式兼容 2026-08-31] 忙碌旁路同样按用户模式配置裁决：技术干活/relay 放行主代理
        if not self._mode_shortcut_decision(event, message, route):
            logger.info(
                f"[parallel_handoff] BusyBypass: T1 route -> {route} "
                f"但模式配置要求放行主代理，跳过旁路直发"
            )
            # [判向传递 2026-08-31] 放行时把 T1 判向目标暂存，供 directive 注入附加
            self._record_route_suggestion(route)
            return False
        self._record_route_hit(event, route)
        logger.info(
            f"[parallel_handoff] BusyBypass: T1 route -> {route} "
            f"(runner active, skip follow-up capture)"
        )
        try:
            await self.call_subagent(event, agent_name=route, input=message)
        except Exception as e:
            logger.error(
                f"[parallel_handoff] BusyBypass direct call failed: {e}; release to main"
            )
            return False
        event.stop_event()
        return True

    async def _smart_router_check(self, event: AstrMessageEvent) -> bool:
        """on_waiting_llm_request 钩子实现。命中返回 True 并已 stop_event。

        路由链（2026-08-21 起）：T1 点名/领域词 → T1.5 会话续接 → T2 小模型（带上下文）→ T3 落主代理。
        """
        # [源头封堵 2026-08-21] 子代理直发后、主代理继续生成工具结果续写时，waiting 钩子
        # 会再次被触发（带 _suppress_mainagent_prefix 标记）。此时直接 stop_event 消费标记，
        # 从源头掐掉"已传给她/她在回你了"这类尾巴，不等结果管线流式清链（流式已发出的收不回）。
        if (
            getattr(self, "_suppress_mainagent_prefix", False)
            and not self._cfg("allow_mainagent_after_direct", True)
        ):
            # [修复 2026-08-21] 只有"同一条用户消息在短时间内再次触发 waiting"
            # （主代理工具续写尾巴）才吞；全新用户消息或超时间窗一律消费标记后放行，
            # 继续走完整路由链。避免下一条用户消息被误当尾巴吞掉（根因：21:37 卡死）。
            same_msg = (event.get_message_str() or "").strip() == getattr(
                self, "_suppress_mainagent_msg", None
            )
            fresh = time.time() - getattr(self, "_suppress_mainagent_ts", 0) <= 15
            if same_msg and fresh:
                self._suppress_mainagent_prefix = False
                event.stop_event()
                return True
            # 非续写触发（新用户消息/过期）：消费标记后走完整路由链 T1→T1.5→T2→主代理
            self._suppress_mainagent_prefix = False
        if not self._router_enabled():
            return False
        message = (event.get_message_str() or "").strip()
        if not message:
            return False
        # [唤醒来路 2026-09-12 四修] 唤醒层剥前缀时会置 is_at_or_wake_command，
        # 拿它当判据，不再赌「原文还能还原回来」
        raw_message = self._resolve_command_text(event, message)
        if raw_message != message:
            logger.info(
                f"[parallel_handoff] T0 命令原文解析: {message!r} → {raw_message!r} "
                f"(at_or_wake={getattr(event, 'is_at_or_wake_command', None)})"
            )
        self._record_user_msg(event, message)
        # [去重屏障 2026-09-09 用户 bug 治本] 同消息二次触发（OnWaitingLLMRequestEvent
        # 可能对同一消息顺序跑两次）→ 直接吞掉，杜绝子代理重复回话。
        # 2026-09-11：显式命令豁免（连发两条 /助手A，第二条必须照常执行）
        if self._dedup_shortcircuit(event, message, raw_message):
            return True
        # [软入口·A 方案 2026-09-12] 柜台命令 /谁在 /复位 /列表：回执并短路，
        # 不进 T0 点名 / T1 / T0.5 / T2 任何一层。
        admin_cmd = self._parse_admin_command(raw_message)
        if admin_cmd:
            logger.info(f"[parallel_handoff] SmartRouter: 管理命令 {admin_cmd} → 回执并短路")
            await self._send_admin_reply(event, admin_cmd)
            event.stop_event()
            return True
        # [主代理锁 2026-09-12 用户指定] 会话被主代理锁锁定 → 非命令消息直通主代理，
        # 不跑 T0 点名/T1/T1.5/T0.5/T2；命令字（/ 开头）放行给下方 T0 处理
        # （/子代理名 换锁、/复位 解绑仍可执行）。
        if self._main_locked(event) and not self._is_real_command(raw_message):
            logger.info(
                "[parallel_handoff] SmartRouter: 主代理锁在场 → 直通主代理（跳过全部判向）"
            )
            return False
        # [T0 命令式触发 2026-09-08 用户指定] 以 / 开头的显式命令（如 /<名A>、/<名A>+<名B>、
        # /助手C+助手A+助手D、/主代理+助手A）→ 直接锁定目标，最高优先级短路，
        # 彻底跳过 T1 关键词猜测 / T0.5 粘滞 / T2 小模型。命令持续生效（写粘滞锁）。
        cmd_agents, cmd_main = self._parse_agent_command(raw_message)
        if cmd_agents or cmd_main:
            if cmd_agents and not cmd_main:
                # 纯子代理命令：短路调度（单人或多人 chained），并写命令强制锁。
                # 命令锁后续消息永远锁定这组，对话里出现其他子代理名也不切换（治本）。
                logger.info(
                    f"[parallel_handoff] SmartRouter: T0 命令式 {cmd_agents} → "
                    f"锁定并短路（跳过 T1/T0.5/T2）"
                )
                calls = [{"agent_name": a, "input": message} for a in cmd_agents]
                try:
                    await self.parallel_handoff(
                        event,
                        calls=calls,
                        call_mode="chained" if len(calls) > 1 else "direct",
                        route_mode="direct",
                        mode="affection",
                    )
                except Exception as e:
                    logger.error(
                        f"[parallel_handoff] SmartRouter: T0 命令式调用失败 {e}; release to main"
                    )
                    return False
                # 写命令强制锁（会覆盖该会话旧命令锁）→ 此后 messages 按锁组强制路由
                self._cmd_lock = {}
                self._record_cmd_lock(event, cmd_agents)
                event.stop_event()
                return True
            # 含主代理（/主代理 或 /主代理+助手A）：主代理在场。
            #   - agents 空 → 纯主代理：建立主代理锁（会话直通主代理）
            #   - agents 非空 → 主代理调度子代理，放行主代理 + 暂存判向目标
            if getattr(self, "_cmd_lock", None):
                self._cmd_lock.pop(event.unified_msg_origin, None)
            if cmd_agents:
                # 含子代理：不建主代理锁（子代理优先），主代理本轮可 relay 调度
                self._clear_main_lock(event)
                logger.info(
                    f"[parallel_handoff] SmartRouter: T0 命令式含主代理令牌 + {cmd_agents} → "
                    f"放行主代理并暂存候选子代理"
                )
                # 写粘滞锁：主代理+子代理共在场，后续无命令续接时子代理按锁调度
                self._record_route_hits(event, cmd_agents)
                self._record_route_suggestions(cmd_agents)
            else:
                # [主代理锁 2026-09-12 用户指定；回执强化 20:57 顾主要求]
                # 纯 /主代理（/主代理）→ 建立会话级主代理锁 + 回执并短路，
                # 与 /谁在 同款柜台行为：明确回报「已锁定」，不再默默放行让消息
                # 落进主代理 LLM（顾主原话：要报出已锁定 xxx 的标识消息）。
                logger.info(
                    "[parallel_handoff] SmartRouter: T0 命令式（纯主代理）→ "
                    "建立主代理锁，回执并短路"
                )
                self._record_main_lock(event)
                await self._send_admin_reply(event, "lock_main")
                event.stop_event()
                return True
        # [最高优先级 2026-09-03 用户指定] 含连续「主代理」四字 → 无条件放行主代理（=主代理）。
        # 跳过 T1/T1.5/T2 全部判向，任何子代理都不得接管。返回 False 表示不短路、不 stop_event，
        # 消息自然落回主代理路径。登记路由历史防止 T1.5 后续承接接到子代理。
        if self._MAIN_TOKEN_RE and self._MAIN_TOKEN_RE.search(message):
            logger.info(
                "[parallel_handoff] SmartRouter: 消息含「主代理令牌」→ 最高优先级放行主代理（不路由子代理）"
            )
            # 清掉该会话的续接记忆，避免后续承接句被 T1.5 续给错误子代理。
            # [2026-09-11 用户口径修正] 这里**不再**清命令锁：过去对话里只要提到
            # 「主代理」四个字就把命令强锁拆了，是「锁莫名其妙失效」的元凶之一。
            # 现在令牌只放行本轮；要解绑请发 /主代理（走 T0 命令路径显式解锁）。
            last, _ = self._route_mem()
            last.pop(event.unified_msg_origin, None)
            return False
        t0 = time.perf_counter()
        # [T0 命令式强制锁定 2026-09-08 用户指定] 命令锁在场者组存在 → 该会话所有后续
        # 消息直接按锁组路由（多人 chained / 单人 direct），T1 名字判定/T0.5 粘滞/T2
        # 全部跳过——即使对话里点了其他子代理名（如「你对某人的看法」），也只是
        # 对话内容，绝不切换到该子代理。只有新命令或「主代理」文本能解除（上层已判）。
        cmd_locked = self._cmd_locked_group(event)
        if cmd_locked:
            logger.info(
                f"[parallel_handoff] SmartRouter: T0 命令强制锁在场组 {cmd_locked} → "
                f"短路路由（对话提及他名不切换，治本）"
            )
            calls = [{"agent_name": a, "input": message} for a in cmd_locked]
            try:
                await self.parallel_handoff(
                    event,
                    calls=calls,
                    call_mode="chained" if len(calls) > 1 else "direct",
                    route_mode="direct",
                    mode="affection",
                )
            except Exception as e:
                logger.error(
                    f"[parallel_handoff] SmartRouter: T0 命令锁调用失败 {e}; release to main"
                )
                return False
            event.stop_event()
            return True
        # [多点名强呼叫短路 2026-09-07 方案A] 一次明确点名多个子代理
        #（如「助手A，助手D，我们一起来玩」）→ 短路走 chained 接龙，
        # 主代理完全不下场（用户记忆#4：点几个名就几个延续、不插嘴）。
        # 放在 T1 判定之前，覆盖粘滞（新点名优先于旧锁）。
        multi = self._t1_route_multi(message)
        if multi and len(multi) >= 2:
            logger.info(
                f"[parallel_handoff] SmartRouter: 多点名强呼叫 {multi} → "
                f"短路 chained 接龙（主代理不下场）"
            )
            calls = [{"agent_name": a, "input": message} for a in multi]
            try:
                await self.parallel_handoff(
                    event,
                    calls=calls,
                    call_mode="chained",
                    route_mode="direct",
                    mode="affection",
                )
            except Exception as e:
                logger.error(
                    f"[parallel_handoff] SmartRouter: 多点名接龙调用失败 {e}; release to main"
                )
                return False
            # [方案① 2026-09-07] 多P场次：把整组写入粘滞记忆，供后续无点名消息按组续接。
            self._record_route_hits(event, multi)
            event.stop_event()
            return True
        # T1 点名 / 领域词
        route = self._t1_route(message)
        # [锁仲裁 2026-09-11 用户口径] 粘滞在册组存在时，T1 只有提名权、没有转移权：
        # 命中项在组外且不构成显式转移 → 判为叙述提及，丢弃 T1 结果，落回 T0.5 按原组
        # 续接。效果：锁定了就一路是 TA，除非你明确喊人（呼叫词/前缀命令/短名消息）。
        if route:
            _locked = self._sticky_group_of(event)
            if _locked and route not in _locked and not self._is_explicit_transfer(message):
                logger.info(
                    f"[parallel_handoff] SmartRouter: 锁仲裁 → T1 提名 {route} 属叙述提及，"
                    f"保持粘滞组 {_locked}（T1 不得夺锁）"
                )
                route = None
        conf = 1.0 if route else 0.0
        source = "T1"
        # T0.5 会话粘滞锁定（零成本，纯规则；2026-09-07 方案A 取代旧 T1.5 弱续接）
        if not route:
            sticky = self._t1_sticky_route(event, message)
            if isinstance(sticky, list) and len(sticky) >= 2:
                # [方案① 2026-09-07] 粘滞命中多人组 → 按整组 chained 接龙，不掉队。
                # 与上方多点名短路同构：主代理不下场，多P场次后续轮次按组延续。
                logger.info(
                    f"[parallel_handoff] SmartRouter: T0.5 粘滞多人组 {sticky} → "
                    f"短路 chained 接龙（多P场次续接，主代理不下场）"
                )
                calls = [{"agent_name": a, "input": message} for a in sticky]
                try:
                    await self.parallel_handoff(
                        event,
                        calls=calls,
                        call_mode="chained",
                        route_mode="direct",
                        mode="affection",
                    )
                except Exception as e:
                    logger.error(
                        f"[parallel_handoff] SmartRouter: 粘滞多人接龙调用失败 {e}; release to main"
                    )
                    return False
                event.stop_event()
                return True
            route = sticky if isinstance(sticky, str) else None
            conf = 1.0 if route else 0.0
            source = "T0.5"
        # T2 小模型（带最近会话上下文）
        if not route:
            route, conf = await self._t2_route(event, message)
            source = "T2"
        if not route or conf < self._router_threshold():
            return False
        # [模式兼容 2026-08-31] T1/T2 命中后按用户模式配置裁决：
        # 技术干活任务（tech 特征）→ 放行主代理走 tech 模式统帅收卷（relay+parallel），
        # 避免 T1/T2 短路把任务变成 direct 单发、绕过用户模式配置；
        # 贴贴任务 → 按 affection_mode_config.route_mode：direct 短路直发，relay 放行主代理。
        if not self._mode_shortcut_decision(event, message, route):
            logger.info(
                f"[parallel_handoff] SmartRouter: {source} route -> {route} "
                f"但模式配置要求放行主代理（统帅收卷/relay），不短路"
            )
            # [判向传递 2026-08-31] 放行时把 T1/T2 判向目标暂存，供 directive 注入附加
            self._record_route_suggestion(route)
            return False
        self._record_route_hit(event, route)
        # [读空气·段二·低侵入 2026-09-03] 自动路由要短路抢派子代理前，请读空气看一眼
        # 是否倾向"主代理自然接住"（宁静权）。段二仅输出观察日志，绝不实际拦截路由；
        # 等三期随机性日常补齐后再实验性开启真实克制。总开关默认关，此调用零开销。
        try:
            if hasattr(self, "_arbitrate_directive") and self._read_air_enabled():
                _ra_verdict = self._arbitrate_directive(event, message, route, True)
                if _ra_verdict == "main":
                    # [段五 2026-09-10] 宁静权【真实拦截】入口（仅 read_air_enforce=True 时可达）：
                    # 读空气倾向克制 → 放行主代理自然接住，不短路抢派子代理。
                    # R6 约定（docs/read_air_arbitrate_plan.md）：拦截必清承接记忆，否则下一轮
                    # T0.5 粘滞路由会按旧在场者组把同一组又续上，拦截形同虚设。
                    # 注意 _record_route_hit 已在上面执行，此处清理是配套动作、不可省。
                    _ra_last, _ = self._route_mem()
                    _ra_last.pop(event.unified_msg_origin, None)
                    logger.info(
                        f"[read_air][enforce] 宁静权拦截：本应短路 {route}，"
                        f"改放行主代理自然接住（已清承接记忆）"
                    )
                    return False
        except Exception as _arb_e:
            logger.warning(f"[read_air] arbitrate skipped (non-fatal): {_arb_e}")
        logger.info(
            f"[parallel_handoff] SmartRouter: {source} route -> {route} "
            f"(conf={conf:.2f}, thr={self._router_threshold()}, "
            f"cost={int((time.perf_counter() - t0) * 1000)}ms)"
        )
        try:
            await self.call_subagent(event, agent_name=route, input=message)
        except Exception as e:
            logger.error(f"[parallel_handoff] SmartRouter direct call failed: {e}; release to main")
            return False
        event.stop_event()
        return True

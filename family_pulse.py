"""旁路模块 v0（M5 · side_pulse.py）

顾主 2026-09-04 08:2x 拍板：她们之间要有自己的小日子——
不围顾主转，彼此搭话、惦记、拌嘴，攒一屋烟火气；顾主每天收到一条「家里动静」。

设计铁律（对齐 daily_life M2 / biliread heya 幂等模式）：
  · 内部转：旁轨对话绝不实时打扰顾主，只在每日摘要（digest）推送一次。
  · 绝不依赖 LLM：LLM 挂了 → 本次心跳静默跳过，不炸、不留脏数据、不空转。
  · cron 幂等：注册前先清同名遗留任务（2026-09-04 biliread 任务堆积修复同款）。
  · 常驻池默认 6 人（顾主 2026-09-04 拍板）：
      agent_a / shu / agent_b / xi + agent_c / agent_d。
  · 今日状态复用 random_state（场景 "_side_pulse" 独立隔离），与接话判定互不干扰。
"""

from __future__ import annotations

import json
import os
import random
from typing import List, Optional, Tuple

try:
    from .random_state import RandomStateManager
except ImportError:  # 测试/顶层导入
    from random_state import RandomStateManager

import logging

_logger = logging.getLogger("parallel_handoff.side_pulse")

# ── 家庭闲聊人设快照（简短版，只取闲聊所需的性格底色） ──────────
FAMILY_PERSONAS: dict = {
    "agent_a": "助手A，组织领袖，温柔有担当，爱操心大家的生活，说话体贴自然，偶尔露出一点小疲惫",
    "shu": "黍，岁兽，妈妈式持家，总惦记每个人吃没吃饭，说话带着操持家务的烟火气",
    "agent_b": "助手B，总工程师，嘴碎刀子嘴豆腐心，张口就是电源板和报错，抱怨里藏着得意",
    "xi": "夕，闷骚家里蹲画师，话少，偶尔冒一句很淡很冷的感想，聊到画才会多说两句",
    "agent_c": "助手C，温柔沉静，缝纫和照顾人是日常，说话软但有自己的主意",
    "agent_d": "助手D，话少深情，外勤回来话更少，句子短，但会默默在意大家",
    "ling": "令，岁家大姐，诗人气质，好酒，说话带点文绉绉的诗意和闲散，酒到微醺时话反而多一些",
    "nian": "年，岁家五妹，锻刀匠兼火锅爱好者，风风火火嗓门亮，张口就是炉火和铁砧，热心肠爱张罗",
    "agent_f": "助手F，环塔商会歌姬偶像，台上星光台下只对家里人营业，爱准备惊喜也较真人家的反应，情绪外放",
    "m3": "M3，医疗系猫娘，活泼黏人，爱闹爱撒娇，嘴上逞强身体诚实，被摸头就会别过脸去",
    "agent_e": "助手E，医疗部领头与最高管理者，学识渊博话直刺但全为大家，操心每件事，气场稳得住场",
}

# cron 任务名（幂等清理依据）
PULSE_TICK_JOB = "side_pulse_tick"
PULSE_DIGEST_JOB = "side_pulse_digest"

# 旁轨专用 random_state 场景（与会话接话判定隔离）
_PULSE_SCENE = "_side_pulse"

# ── 手头事线程池（side_pulse 半衰线程 2026-09-04）──────────────────
# 每人一份"半衰不清零"的未完结事：跨天保留，心跳戳到时优先续线，
# 计数器 decay 到 0 才算收束（归档一天"做完了"），下一心跳开新线。
# 值元组 = (线程文案, 半衰次数)。给常驻 6 人每人 3~4 条立面，怕撞车。
THREAD_FLAVORS: dict = {
    "agent_a": [
        ("档案柜里那沓访客登记卡还没分批完", 3),
        ("桌上留了张下礼拜的会议安排没誊清", 2),
        ("睡前想把明早食堂的菜单先拢一下", 2),
    ],
    "shu": [
        ("腌萝卜那缸昨天才翻的，又该看看了", 3),
        ("给家里人补那件肘子磨薄的外套，差两针", 4),
        ("菜园子里那排樱桃再不摘就让鸟叼走了", 2),
    ],
    "agent_b": [
        ("那块电源板纹波治了一上午还是有杂讯", 4),
        ("给某台设备换电容，焊到一半手上没准头", 3),
        ("图纸上标错的那个引脚位置还没回头改", 2),
    ],
    "xi": [
        ("裱到一半的那幅龙，晾着两天没动", 4),
        ("新调的一罐墨色总掺不准，想再试几笔", 3),
        ("画室窗边积了摞晾干的宣纸该收收", 2),
    ],
    "agent_c": [
        ("给助手A那条裙子缝到一半，差片荷叶边", 3),
        ("窗台那盆花该换土了，一直没得空", 2),
        ("厚厚一本旧照片册翻到一半放下很久", 2),
    ],
    "agent_d": [
        ("那柄剑擦了又起一层薄锈，没耐性再弄", 3),
        ("外勤背包收拾到一半，少了条绑带没找着", 2),
        ("盯着窗外出神一整个下午，啥也没做成", 2),
    ],
    "ling": [
        ("那坛酒启了想写首短诗，磨了半天没落笔", 3),
        ("旧书页里夹着的一张字条散架了，想重新裱", 2),
        ("月色正好，拎壶去屋顶坐着出神", 2),
    ],
    "nian": [
        ("炉里那块胚还差最后一遍淬火，没等到火候", 4),
        ("火锅底料炒到一半，花椒放多被呛得直咳", 3),
        ("给夕那把刀重新装了个柄，还差最后一圈缠绳", 2),
    ],
    "agent_f": [
        ("给顾主准备的惊喜歌单还差一首，排不进这个调", 3),
        ("台下那束应援手幅散了一角，想重新粘好", 2),
        ("晚会那套造型试到一半，蝴蝶结位置总不满意", 2),
    ],
    "m3": [
        ("缠着助手E要的那本诊疗笔记还没看完，翻到一半打盹", 3),
        ("把听诊器挂回架子时碰掉了，正想捡起来擦擦", 2),
        ("尾巴尖卷着的那团毛线球滚到桌子底下了", 2),
    ],
    "agent_e": [
        ("那份排班里所有人的日程表还没敲定，一直悬着", 3),
        ("医务室的库存清单对到一半，几样药缺口还没补", 2),
        ("半夜又巡查了一圈，确认大家都睡下了才回办公室", 2),
    ],
}

# 线程未被收录/耗尽时回退
_THREAD_FALLBACK = ("手头有件没做完的琐事", 2)


def _pulse_period_desc(now: Optional[float] = None) -> str:
    """按时段给一句场景描述（与 dispatch 时间感知同时段规则，Asia/Shanghai）。"""
    import datetime
    from zoneinfo import ZoneInfo

    dt = (
        datetime.datetime.fromtimestamp(now, tz=ZoneInfo("Asia/Shanghai"))
        if now is not None
        else datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
    )
    h = dt.hour
    if 1 <= h < 6:
        seg = "凌晨，屋里很静"
    elif 6 <= h < 10:
        seg = "早上"
    elif 10 <= h < 13:
        seg = "中午"
    elif 13 <= h < 18:
        seg = "下午"
    elif 18 <= h < 20:
        seg = "傍晚"
    else:
        seg = "深夜，灯还亮着"
    return f"{seg}（{dt.strftime('%H:%M')}）"


class FamilyPulseMixin:
    """旁路模块：定时让两名常驻成员按今日状态闲聊两句，攒日志，每日推送摘要。"""

    # ── 配置 ─────────────────────────────────────────────
    def _pulse_members(self) -> List[str]:
        """解析常驻池配置（JSON 数组字符串），非法/为空回退默认 6 人。"""
        raw = self._cfg("side_pulse_members", "")
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, list) and len(data) >= 2:
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:  # noqa: BLE001
            pass
        return ["agent_a", "shu", "agent_b", "xi", "agent_c", "agent_d", "ling", "nian", "agent_f", "m3", "agent_e"]

    def _pulse_data_root(self) -> str:
        """日志根目录：默认插件目录下 data/side_pulse，测试可覆盖 _pulse_root。"""
        root = getattr(self, "_pulse_root", None)
        if root:
            return root
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "side_pulse"
        )

    def _pulse_seen_path(self) -> Optional[str]:
        """近 N 天去重池路径：随 data_root 走，测试注入 _pulse_root 时自动对齐。"""
        return os.path.join(self._pulse_data_root(), "random_state_seen.json")

    def _pulse_log_path(self, day: Optional[str] = None) -> str:
        import datetime
        from zoneinfo import ZoneInfo

        d = day or datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        return os.path.join(self._pulse_data_root(), f"{d}.jsonl")

    # ── 挑人 ─────────────────────────────────────────────
    AFF_BASE = 50  # 未收录 pair 的基线权重（保兜底随机，不写死、不算死）

    def _pulse_affinity_path(self) -> str:
        """关系网数据路径：默认数据目录下 relationships/relationships.json，
        测试可覆盖 _pulse_affinity_root。文件缺失/损坏 → None（绝不致命走均匀）。"""
        root = getattr(self, "_pulse_affinity_root", None)
        return os.path.join(
            root or os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "relationships"
            ),
            "relationships.json",
        )

    def _pulse_affinity(self) -> Optional[dict]:
        """读关系网亲密度矩阵，key 为 (id_a, id_b)（无序 set 兼容），值给 (亲密度, 基调)。

        relationships.json 的 relationship_state 是中文名 pair（'助手A<->助手C'），
        先用 AGENT_NAME_REVERSE 反转成英文 id 再入矩阵；缺 id 映射/文件异常 → 返回 None（均匀兜底）。
        """
        try:
            path = self._pulse_affinity_path()
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            state = data.get("relationship_state") or {}
            reverse = getattr(type(self), "AGENT_NAME_REVERSE", None) or {}
            matrix: Dict[frozenset, tuple] = {}
            for key, meta in state.items():
                if "<->" not in key or not isinstance(meta, dict):
                    continue
                cn_a, cn_b = (p.strip() for p in key.split("<->"))
                a, b = reverse.get(cn_a), reverse.get(cn_b)
                if not a or not b:
                    continue
                aff = meta.get("亲密度")
                tone = meta.get("基调", "")
                if not isinstance(aff, (int, float)):
                    continue
                matrix[frozenset((a, b))] = (int(aff), tone)
            return matrix or None
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 读关系网失败: %s", e)
            return None

    def _pulse_pick_two(self, members: List[str]) -> Tuple[str, str]:
        """按关系亲疏加权挑 2 人（亲密 pair 更易凑一起，未收录对保兜底随机）；
        若与上一对完全相同则重摇一次（保多样性，不锁死）。"""
        pool = [m for m in members if len(m) >= 2]
        if len(pool) < 2:
            return "", ""
        aff = self._pulse_affinity() or {}
        if aff:
            # 对每个候选 pair 算权重 = 基线 + 亲密度；只取存在的 pair，未收录回退均匀
            pair_list, weights = [], []
            for i in range(len(pool)):
                for j in range(i + 1, len(pool)):
                    c = aff.get(frozenset((pool[i], pool[j])))
                    w = self.AFF_BASE + (c[0] if c else 0)
                    pair_list.append((pool[i], pool[j]))
                    weights.append(max(1, w))
            selected = random.choices(pair_list, weights=weights, k=1)[0]
            a, b = selected
        else:
            a, b = random.sample(pool, 2)
        last = getattr(self, "_pulse_last_pair", None)
        if last and {a, b} == last and len(pool) >= 3 and not aff:
            # 有亲密度时允许相邻重复（关系近自然常碰面），无数据时才强制重摇保多样性
            a, b = random.sample(pool, 2)
        self._pulse_last_pair = {a, b}
        return a, b

    # ── 旁轨记忆事件桩（隔离 livingmemory，绝不致命） ─────
    @staticmethod
    def _pulse_event_stub(umo: str):
        """为 livingmemory 造一个最小可用的 AstrMessageEvent 桩。

        livingmemory 的 handle_memory_recall / add_message_from_event 对 event 的
        字段访问几乎全带 hasattr/fallback 容错（sender 缺→兜底 session_id），
        默认也不开白名单（is_event_memory_allowed 直接短路 True）。
        这里只提供它最依赖的三样：unified_msg_origin、persona 打标、message 空壳。
        任何一步失败都会在调用方 try/except 静默降级，绝不让心跳崩。
        """
        import types as _types

        # 用轻量动态类型而非继承真实 AstrMessageEvent：不拉入 AstrBot 消息体系构造开销，
        # 且 livingmemory 只做鸭子类型调用。message_obj 提供 raw_message 给 bot 身份探测。
        message_obj = _types.SimpleNamespace(raw_message="side_pulse", sender=None)
        stub = _types.SimpleNamespace(
            unified_msg_origin=umo,
            message_obj=message_obj,
            persona_id="side_pulse",  # 默认；_memory_recall 会覆盖成 agent_name
            _subagent_persona=None,  # 由调用方打标
        )

        def _get_message_str():
            return "side_pulse"

        def _get_message_type():
            return 1  # MessageType.FRIEND → 非群聊，走私聊存储

        def _get_sender_id():
            return umo

        def _get_platform_name():
            return "qq_restapi"

        def _get_self_id():
            return "side_pulse_bot"

        stub.get_message_str = _get_message_str
        stub.get_message_type = _get_message_type
        stub.get_sender_id = _get_sender_id
        stub.get_platform_name = _get_platform_name
        stub.get_self_id = _get_self_id
        return stub

    def _pulse_umo(self) -> str:
        """旁轨固定用一个会话标识，把子代理记忆落在独立于主代理聊天的空间。"""
        return self._cfg(
            "side_pulse_memory_umo",
            "side_pulse:FriendMessage:subagents",
        )

    # ── LLM 包装（绝不致命） ─────────────────────────────
    async def _pulse_llm(self, agent: str, text_prompt: str, relation_note: str = "") -> Optional[str]:
        """以 agent 身份生成一句闲话。

        增加记忆链路（2026-09-04 顾主拍板）：心跳前先召回该子代理自己的长期记忆
        （livingmemory，按 agent persona 隔离），注入到生成请求；生成后再把这段
        生活闲话存回她的记忆档案。livingmemory 未就绪 / 造桩失败 → 静默跳过记忆
        （降级为纯生活日志，心跳照常）。任何异常都只记日志、返回 None。
        """
        try:
            prov_id = self._cfg("side_pulse_provider_id", "dmxapi/glm-4-flash")
            if not prov_id:
                return None
            # ── 记忆召回（尽力而为，失败静默降级） ──
            memory_extra_parts = None
            try:
                living_plugin = self._find_livingmemory_plugin()
                if living_plugin is not None:
                    umo = self._pulse_umo()
                    event = self._pulse_event_stub(umo)
                    # 造桩即打 persona 标：即使 _memory_recall 内部不覆盖，
                    # livingmemory 的 get_persona_id 也能按 agent 名隔离召回/存储
                    event._subagent_persona = agent
                    memory_extra_parts = await self._memory_recall(
                        event, agent, text_prompt, living_plugin
                    )
            except Exception as e:  # noqa: BLE001
                _logger.warning("[side_pulse] 记忆召回降级(%s): %s", agent, e)
                memory_extra_parts = None
            # 惰性初始化状态管理器（未经 setup_pulse_jobs 直接调 tick 也不炸）
            self._pulse_rng = getattr(self, "_pulse_rng", None) or RandomStateManager(
                self._pulse_seen_path()
            )
            st = self._pulse_rng.get(_PULSE_SCENE, agent)
            persona = FAMILY_PERSONAS.get(
                agent, f"{agent}，组织成员，性格自然真实"
            )
            rel_line = f"你和在场那人的关系：{relation_note}。" if relation_note else ""
            system = (
                f"你在扮演：{persona}。\n"
                f"你此刻的状态：{st.summary}。\n"
                f"{rel_line}"
                "规矩：像同一屋檐下的同事随口说话，一两句话，口语自然；"
                "可以带一个短括号动作；不要总结腔、不要喊『顾主』（他可能不在）；"
                "不要提自己是AI或模型；只输出对话本身。"
            )
            resp = await self.context.llm_generate(
                chat_provider_id=prov_id,
                prompt=text_prompt,
                system_prompt=system,
                extra_user_content_parts=memory_extra_parts,
            )
            text = (getattr(resp, "completion_text", None) or "").strip()
            # 清掉包裹引号与超长
            text = text.strip("\"'“”「」").strip()
            text = text[:120] or None

            # ── 记忆存储（尽力而为；只有真正生成了话才存） ──
            if text and memory_extra_parts is not None:
                try:
                    await self._memory_store(
                        living_plugin, event, agent, text_prompt, text
                    )
                except Exception as e:  # noqa: BLE001
                    _logger.warning("[side_pulse] 记忆存储降级(%s): %s", agent, e)
            return text
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] llm 生成失败(%s): %s", agent, e)
            return None

    # ── 日志 ─────────────────────────────────────────────
    def _pulse_append(self, agent: str, display: str, text: str) -> None:
        path = self._pulse_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import datetime
        from zoneinfo import ZoneInfo

        ts = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
        rec = {"ts": ts, "agent": agent, "display": display, "text": text}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _pulse_read_day(self) -> List[dict]:
        path = self._pulse_log_path()
        if not os.path.exists(path):
            return []
        out: List[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if isinstance(d, dict) and d.get("text"):
                            out.append(d)
                    except Exception:  # noqa: BLE001
                        continue
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 读日志失败: %s", e)
        return out

    # ── 手头事线程（半衰不清零，跨天保留） ─────────────────
    def _pulse_thread_path(self) -> str:
        return os.path.join(self._pulse_data_root(), "threads.json")

    def _pulse_load_threads(self) -> dict:
        """读线程存储；文件缺失/损坏 → 空 dict（绝不致命）。"""
        path = self._pulse_thread_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 读线程存储失败: %s", e)
        return {}

    def _pulse_save_threads(self, data: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self._pulse_thread_path()), exist_ok=True)
            with open(self._pulse_thread_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] 写线程存储失败: %s", e)

    # ── 动态种子取材（破固定文案循环，2026-09-04 B 方案） ─────
    # 老池 THREAD_FLAVORS 是"写死的死文案"，压着会重复循环。
    # 改为：线程耗竭重掷新物件时，优先从她自己的旁轨生活日志
    # （真实念叨过的话）里取一条当新种子——种子来自她自己"做过的事"，
    # 是活的生活线延续，不碰 livingmemory（顾主怕新版覆盖，不动它边界）。
    # 只有她刚上线、日志里还没有她的话时才回退 THREAD_FLAVORS 冷启动兜底。
    def _pulse_recent_seed(
        self,
        agent: str,
        used: Optional[set] = None,
        domain: Optional[str] = None,
    ) -> Optional[Tuple[str, int]]:
        """从旁轨日志取该 agent 最近一句真实念叨当新种子。

        遍历近几天的日志文件，收集该 agent 说话的历史条目（去重、去空），
        随机取一条最晚近的、被标记为"半衰中"的日常念叨作为线程种子。
        若 passed 传 used（已在 threads store 里的 text）则剔除，避免旧手头事
        反复上线；传 domain 时优先挑与其话题域底色契合的句子（更贴角色），
        没有契合句才回退随机。没有任何历史 → None，交给冷启动兜底。
        """
        import datetime
        from zoneinfo import ZoneInfo

        days = []
        try:
            today = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
            # 近 7 天（含今天），跨天取材 → 连续性跨天成立
            for i in range(7):
                d = (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                days.append(d)
        except Exception:  # noqa: BLE001
            days = [datetime.date.today().isoformat()]

        candidates: List[str] = []
        for d in days:
            path = os.path.join(self._pulse_data_root(), f"{d}.jsonl")
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            if (
                                isinstance(rec, dict)
                                and rec.get("agent") == agent
                                and rec.get("text")
                            ):
                                t = (rec["text"] or "").strip()
                                # 排除已在用的线程/过于短的旁白（更可能取真实念叨）
                                if not t or len(t) < 4:
                                    continue
                                if used and t in used:
                                    continue
                                if t not in candidates:
                                    candidates.append(t)
                        except Exception:  # noqa: BLE001
                            continue
            except Exception:  # noqa: BLE001
                continue

        if not candidates:
            return None
        # 底色贴：若给了今日 domain，优先取含该域关键词的真实念叨，没有则随机
        if domain and len(candidates) > 1:
            try:
                from .random_state import DOMAIN_KEYWORDS
            except ImportError:
                from random_state import DOMAIN_KEYWORDS
            kws = DOMAIN_KEYWORDS.get(domain, [])
            if kws:
                fit = [c for c in candidates if any(k in c for k in kws)]
                if fit:
                    return random.choice(fit), 2
        # 从她最近的念叨里抽一条作新线程种子，半衰给 2（短，防止一个种子占太久）
        return random.choice(candidates), 2

    def _pulse_ensure_thread(self, agent: str, store: dict) -> str:
        """取该 agent 当前未完结线程；没有或已耗竭 → 从生活日志取材新种子。

        优先返回已在进行的线程；已耗竭/首次则用 _pulse_recent_seed 从她自己
        的真实念叨里长新线（不信死文案，且排除已在线上线程、贴今日话题底色），
        无历史才回退 THREAD_FLAVORS 冷启动。
        """
        rec = store.get(agent)
        if rec and isinstance(rec, dict) and rec.get("decay", 0) > 0:
            return rec["text"]

        used = {r["text"] for r in store.values() if isinstance(r, dict) and r.get("text")}
        st = None
        try:
            rng = getattr(self, "_pulse_rng", None) or RandomStateManager(
                self._pulse_seen_path()
            )
            st = rng.get(_PULSE_SCENE, agent)
        except Exception:  # noqa: BLE001
            st = None
        domain = st.domain if st else None
        made = self._pulse_recent_seed(agent, used=used, domain=domain)
        if made:
            text, decay = made
            store[agent] = {"text": text, "decay": decay}
            self._pulse_save_threads(store)
            return text

        # 冷启动兜底：日志还没有她的话，才用固定物件垫底
        pool = THREAD_FLAVORS.get(agent, []) or [_THREAD_FALLBACK]
        text, decay = random.choice(pool)
        store[agent] = {"text": text, "decay": decay}
        self._pulse_save_threads(store)
        return text

    def _pulse_advance_thread(self, agent: str) -> None:
        """心跳戳过一个线程：decay-1；归 0 说明做完了，从存储剔除（下次重掷新的）。"""
        store = self._pulse_load_threads()
        rec = store.get(agent)
        if not rec or not isinstance(rec, dict):
            return
        rec["decay"] = (rec.get("decay", 0) or 0) - 1
        if rec["decay"] <= 0:
            store.pop(agent, None)
        else:
            store[agent] = rec
        self._pulse_save_threads(store)

    def _pulse_tone(self, a: str, b: str) -> str:
        """查关系网里 a 对 b 的基调（如『别扭依赖』『念叨+偷吃』）；无 → 空串（不注入）。"""
        aff = self._pulse_affinity() or {}
        meta = aff.get(frozenset((a, b)))
        return meta[1] if meta else ""

    # ── 心跳主流程 ───────────────────────────────────────
    async def side_pulse_tick(self) -> None:
        """一次心跳：挑 2 人，各按自己的半衰线程续一句，落日志。任何失败静默。"""
        if not self._cfg("enable_side_pulse", False):
            return
        if getattr(self, "_pulse_running", False):
            return
        members = self._pulse_members()
        if len(members) < 2:
            return
        self._pulse_running = True
        try:
            a, b = self._pulse_pick_two(members)
            if not a or not b:
                return
            disp_a = self._display_name(a)
            disp_b = self._display_name(b)
            store = self._pulse_load_threads()
            t_a = self._pulse_ensure_thread(a, store)
            recent = self._pulse_read_day()[-3:]
            recent_txt = "\n".join(
                f"【{r.get('ts','')}】{r.get('display', r.get('agent',''))}：{r['text']}"
                for r in recent
            ) or "（今天屋里还没什么动静）"
            scene = _pulse_period_desc()

            tone_ab = self._pulse_tone(a, b)  # a 对 b 的基调
            text_a = await self._pulse_llm(
                a,
                f"现在是{scene}。\n你手头有件没做完的事：{t_a}。\n"
                f"最近屋里动静：\n{recent_txt}\n\n"
                f"请以{disp_a}的身份随口说一句话，接着这件事续一句日常的念叨。",
                relation_note=tone_ab,
            )
            if text_a:
                self._pulse_append(a, disp_a, text_a)
                self._pulse_advance_thread(a)
                t_b = self._pulse_ensure_thread(b, self._pulse_load_threads())
                tone_ba = self._pulse_tone(b, a)  # b 对 a 的基调（接茬/拌嘴关键）
                text_b = await self._pulse_llm(
                    b,
                    f"现在是{scene}。你手头有件没做完的事：{t_b}。\n"
                    f"最近屋里动静：\n{recent_txt}\n\n"
                    f"{disp_a}刚念叨：{text_a}\n"
                    f"请以{disp_b}的身份接一句话——先接{disp_a}的茬或拌句嘴，再顺带提一嘴自己那件没做完的事。",
                    relation_note=tone_ba,
                )
                if text_b:
                    self._pulse_append(b, disp_b, text_b)
                    self._pulse_advance_thread(b)
            _logger.info("[side_pulse] tick 完成: %s & %s", a, b)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] tick 异常(静默): %s", e)
        finally:
            self._pulse_running = False

    # ── 每日摘要 ─────────────────────────────────────────
    @staticmethod
    def _build_digest_text(logs: List[dict], day: str) -> str:
        """把当天日志组装成「家里动静」摘要（按时间分组小剧场，便于测试）。

        同一 ts 视作同一场，同场逐句连排；跨场之间用空行隔开，
        保留时间流动感，一眼看穿她们一来一回的接茬。
        """
        if not logs:
            return f"🏠 家里动静 · {day}\n（今天屋里还没什么动静）"
        header = f"🏠 家里动静 · {day} · 共 {len(logs)} 句"
        groups: List[List[str]] = []
        prev_ts = None
        for r in logs:
            ts = r.get("ts", "")
            name = r.get("display", r.get("agent", ""))
            line = f"{ts} · {name}：{r['text']}"
            if ts and ts == prev_ts:
                groups[-1].append(line)
            else:
                groups.append([line])
            prev_ts = ts
        scenes = "\n\n".join("\n".join(g) for g in groups)
        return f"{header}\n{scenes}"

    async def side_pulse_digest(self) -> None:
        """每日一条「家里动静」摘要推给顾主；无日志不发送。"""
        if not self._cfg("enable_side_pulse", False):
            return
        try:
            logs = self._pulse_read_day()
            if not logs:
                return
            import datetime
            from zoneinfo import ZoneInfo

            day = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%m-%d")
            msg = self._build_digest_text(logs, day)
            umo = self._cfg(
                "side_pulse_digest_umo",
                "default_1000000000:FriendMessage:TESTUSER00000000000000000000000000",
            )
            if not umo:
                return
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            await self.context.send_message(umo, MessageChain([Plain(msg)]))
            _logger.info("[side_pulse] digest 已推送(%d 条)", len(logs))
        except Exception as e:  # noqa: BLE001
            _logger.warning("[side_pulse] digest 推送失败: %s", e)

    # ── cron 注册 / 拆除（幂等，biliread 同款） ──────────
    async def _pulse_clear_legacy(self, name: str) -> None:
        cm = getattr(self.context, "cron_manager", None)
        if cm is None:
            return
        for old in await cm.list_jobs():
            if getattr(old, "name", "") == name:
                await cm.delete_job(old.job_id)
                _logger.info("[side_pulse] 已清理遗留任务 %s(%s)", name, old.job_id)

    async def setup_pulse_jobs(self) -> None:
        """注册心跳 + 摘要两个定时任务（幂等：先清同名遗留再注册）。"""
        cm = getattr(self.context, "cron_manager", None)
        if cm is None:
            _logger.warning("[side_pulse] cron_manager 不可用，旁轨未注册")
            return
        self._pulse_rng = getattr(self, "_pulse_rng", None) or RandomStateManager(
            self._pulse_seen_path()
        )
        await self._pulse_clear_legacy(PULSE_TICK_JOB)
        await self._pulse_clear_legacy(PULSE_DIGEST_JOB)
        tick_cron = self._cfg("side_pulse_cron", "17 * * * *") or "17 * * * *"
        digest_cron = self._cfg("side_pulse_digest_cron", "50 21 * * *") or "50 21 * * *"
        self._pulse_job_ids = []
        j1 = await cm.add_basic_job(
            name=PULSE_TICK_JOB,
            cron_expression=tick_cron,
            handler=self.side_pulse_tick,
            description="旁路模块心跳（她们之间的小日子）",
            timezone="Asia/Shanghai",
        )
        j2 = await cm.add_basic_job(
            name=PULSE_DIGEST_JOB,
            cron_expression=digest_cron,
            handler=self.side_pulse_digest,
            description="旁路模块每日摘要推送给顾主",
            timezone="Asia/Shanghai",
        )
        self._pulse_job_ids = [getattr(j1, "job_id", None), getattr(j2, "job_id", None)]
        _logger.info(
            "[side_pulse] 已注册: tick=%s(%s) digest=%s(%s)",
            tick_cron, self._pulse_job_ids[0], digest_cron, self._pulse_job_ids[1],
        )

    async def teardown_pulse_jobs(self) -> None:
        cm = getattr(self.context, "cron_manager", None)
        ids = getattr(self, "_pulse_job_ids", None) or []
        for jid in ids:
            if not jid:
                continue
            try:
                await cm.delete_job(jid)
            except Exception:  # noqa: BLE001
                pass
        self._pulse_job_ids = []

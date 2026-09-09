"""2026-08-22 新增：T1.5 响应式情话承接 + T2 剧情参照注入 回归测试

背景：博士发"好，让我舒服起来，爱你"承接特蕾西娅，因 T1.5 只认纯承接词、
T2 无剧情参照，两人话被腰斩到主代理（00:12:01 事件）。本用例锁定两类修复：
1. T1.5 词表扩充：情话承接类短句剥离后无残留 → 时间窗内直接续上次对象
2. T2 参照注入：_record_direct_reply 记录直发回复尾部，_last_direct_reply 可回读

2026-09-10 维护：T1.5 已于 2026-09-07 方案A 升级为 T0.5 会话级硬锁定，
入口方法更名 _t15_continue_route → _t1_sticky_route（本文件同步对齐，见下方注释）。
"""
import asyncio
import re
import time
import unittest
from unittest.mock import MagicMock, patch

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用 test_plugin.py 的 astrbot 导入桩（sys.modules fake 注入）
import test_plugin  # noqa: F401

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


PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


class TestT15LoveWords(unittest.TestCase):
    """T1.5 情话承接：句式应直接续上次路由对象，不落 T2/主代理"""

    def _fresh_router(self):
        p = _load_plugin_class()(
            context=MagicMock(),
            config={
                "enable_smart_router": True,
                "router_continue_window_sec": 300,
            },
        )
        return p

    def _route_once_to_theresia(self, p, sid="sess-love"):
        p._record_route_hit(
            type("E", (), {"unified_msg_origin": sid})(), "theresia"
        )

    def _t15(self, msg, sid="sess-love"):
        p = self._fresh_router()
        self._route_once_to_theresia(p, sid)
        ev = MagicMock()
        ev.unified_msg_origin = sid
        return p._t1_sticky_route(ev, msg)

    def test_let_me_feel_good(self):
        """'让我舒服起来' → 续 theresia"""
        self.assertEqual(self._t15("让我舒服起来"), "theresia")

    def test_want_you(self):
        """'想要你' → 续 theresia"""
        self.assertEqual(self._t15("想要你"), "theresia")

    def test_love_you(self):
        """'爱你' → 续 theresia"""
        self.assertEqual(self._t15("爱你"), "theresia")

    def test_harder(self):
        """'用力' → 续 theresia（单字情话动词）"""
        self.assertEqual(self._t15("用力"), "theresia")

    def test_original_broken_sentence(self):
        """'好，让我舒服起来，爱你'（原故障句）→ 续 theresia"""
        self.assertEqual(self._t15("好，让我舒服起来，爱你"), "theresia")

    def test_love_you_tail_particle(self):
        """'爱你呀' → 续 theresia（尾字语气词不影响剥离）"""
        self.assertEqual(self._t15("爱你呀"), "theresia")

    # ── 以下 3 例原为旧 T1.5「词表剥离 + 长度门槛 + 实词残留拒绝」契约。
    # 2026-09-07 方案A 已把 T1.5 升级为 T0.5 会话级硬锁定（_t1_sticky_route），
    # 博士明确指定「点名后一路粘着，久聊不释放」——续接不再按消息内容判定，
    # 故契约随设计变更：锁定态下这些消息仍续接（真技术请求由 directive 层
    # _classify_directive_task 的 tech 分类先行兜底）。测试同步锁定新语义。

    def test_long_sentence_still_sticky(self):
        """硬锁定下超长消息不再按长度释放（旧 40 字门槛已废）"""
        long_msg = "好，让我舒服起来，爱你，然后我们一起去吃火锅，再去看电影"
        self.assertEqual(self._t15(long_msg), "theresia")

    def test_plain_statement_still_sticky(self):
        """硬锁定下含实词不释放续接（旧实词残留判定已废）"""
        self.assertEqual(self._t15("爱你是我的自由"), "theresia")

    def test_business_sentence_still_sticky(self):
        """硬锁定下正事句也续接（真技术请求由 directive 层 tech 分类兜底）"""
        self.assertEqual(self._t15("想要那份报表"), "theresia")

    def test_no_lock_returns_none(self):
        """未锁定会话时普通句不续接（防误接覆盖，替代旧词表剥离防线）"""
        p = self._fresh_router()
        ev = MagicMock()
        ev.unified_msg_origin = "sess-unlocked"
        self.assertIsNone(p._t1_sticky_route(ev, "想要那份报表"))


class TestT2ReplyRef(unittest.TestCase):
    """T2 剧情参照：直发回复尾部落库后可回读、prompt 注入串含参照行"""
    def _fresh_router(self):
        p = _load_plugin_class()(
            context=MagicMock(),
            config={"enable_smart_router": True},
        )
        return p

    def test_record_and_read_back(self):
        p = self._fresh_router()
        sid = "sess-replyref"
        p._record_direct_reply(sid, "theresia", "好，博士，那我们继续吧，轻一点就好")
        agent, tail = p._last_direct_reply(sid)
        self.assertEqual(agent, "theresia")
        self.assertIn("轻一点就好", tail)

    def test_tail_truncated_to_300(self):
        p = self._fresh_router()
        sid = "sess-trunc"
        long_reply = "好，" + ("啊" * 500) + "，轻一点就好"
        p._record_direct_reply(sid, "theresia", long_reply)
        agent, tail = p._last_direct_reply(sid)
        self.assertEqual(agent, "theresia")
        self.assertLessEqual(len(tail), 300)
        self.assertTrue(tail.startswith("啊"))

    def test_stale_reply_not_injected(self):
        """超过有效期（600s）的旧回复不再注入参照，防陈旧剧情误导"""
        import time as _t
        p = self._fresh_router()
        sid = "sess-stale"
        # 直接注入过期记录（先初始化惰性记忆再篡改时间戳）
        p._route_mem()
        p._record_direct_reply(sid, "theresia", "很久以前的回复")
        p._route_reply[sid] = ("theresia", _t.time() - 3600, "很久以前的回复")
        _agent, _tail = p._last_direct_reply(sid)
        self.assertIsNone(_agent)
        self.assertIsNone(_tail)
        p._record_direct_reply(sid, "theresia", "刚才的回复")
        agent, tail = p._last_direct_reply(sid)
        self.assertEqual(agent, "theresia")
        self.assertIn("刚才", tail)

    def test_prompt_contains_reply_ref_line(self):
        p = self._fresh_router()
        sid = "sess-prompt"
        p._record_direct_reply(sid, "theresia", "好，博士，那我们继续吧，轻一点就好")
        _, _, _, sys_prompt, _ = self._capture_t2_setup(p, sid)
        self.assertIn("最近一次子代理直发回复尾部", sys_prompt)
        self.assertIn("theresia", sys_prompt)
        self.assertIn("轻一点就好", sys_prompt)

    def _capture_t2_setup(self, p, sid):
        """复刻 _t2_route 的注入构造，供断言 prompt 内容（不真发模型）"""
        pool = p._router_agent_pool()
        brief_lines = "\n".join(pool.values())
        _, msgs = p._route_mem()
        buf = msgs.get(sid)
        recent_lines = "\n".join(f"- {m[:120]}" for m in (list(buf)[-4:] if buf else []))
        reply_agent, reply_tail = p._last_direct_reply(sid)
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
        return pool, brief_lines, recent_lines, sys_prompt, reply_tail


class TestT15WordTableSanity(unittest.TestCase):
    """词表正则健全性：每个新增词自身必须能匹配（防手误打错字）"""
    def setUp(self):
        from router import RouterMixin
        words = RouterMixin.T1_CONTINUE_WORDS
        self.re = re.compile("|".join(words))

    def assert_matches(self, s):
        self.assertTrue(self.re.search(s), f"词条未能匹配输入: {s!r}")

    def test_new_words_all_match(self):
        for s in ["让我舒服起来", "想要你", "爱你", "用力", "继续动", "再来一遍",
                  "顶进去", "快点动", "深点", "爽死了", "别停", "想死你了", "舒服吗"]:
            self.assert_matches(s)


if __name__ == "__main__":
    unittest.main(verbosity=2)

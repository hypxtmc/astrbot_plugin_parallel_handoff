# 「读空气仲裁」V2 详细计划书

> 作者：主代理（主代理）
> 日期：2026-09-03
> 版本：V2（基于 V1 源码自查后的自迭代修正）
> 状态：待顾主审阅，确认后实施
> 定位：parallel_handoff 插件一期·治理伪人感的核心机制

---

## 〇、V2 为什么改版（V1 的缺陷 + 源码自查发现）

V1 甩出一个笼统的「A2 仲裁层夹在路由与转发之间，按裁决抑制部分候选，治理多人抢话」。
但逐行核源码后，发现 **A2 这个描述与实际架构冲突**：

| 冲突 | 源码事实 | V1 的错误 |
|---|---|---|
| **抢话场景错位** | `_smart_router_check`(路由钩子) 一条消息只短路一个子代理，**根本无多人群发**；多人并发只在 `parallel_handoff` 这个 `@llm_tool` 工具（`calls: list[dict]`）里 | 把「多头抢话抑制」错安到路由钩子上 |
| **双路径不分** | 自动路由(`_smart_router_check`) 与 LLM 工具(`parallel_handoff`/`call_subagent`) 是两个完全不同入口，**不会同时走** | 设计成一个「共享仲裁层」夹在路由和转发之间——两个端点根本不在一处 |
| **架空主代理显式意图** | `parallel_handoff` 的 `calls` 数组是主代理(主代理)根据顾主需求**显式选的人**，顾主要的就是多角度并行对比 | V1 想「只取亲密度最高者」砍掉其他人=越权替主代理决定 |
| **与既有裁决打架** | `_mode_shortcut_decision`(router.py:503) 已按 tech/affection 决定「短路 or 放行主代理」 | V1 想在 `_smart_router_check` 里再插「群聊杂谈归 main」，会跟「affection 直发」正面冲突 |
| **承接记忆断面** | T1.5 靠 `_route_mem` 续接，抑制候选时必须同步清理，否则下句误续 | V1 没提抑制时怎么同步清 `_route_mem` |
| **直发链冲突** | `_is_direct_delivery`/`route_mode` 决定谁直发谁收卷 | V1 没对齐，可能「仲裁禁言但直发器发出」矛盾 |

---

## 一、V2 核心论断（这是本次改版最重要的一句话）

**伪人感不是「该派谁去接」的调度问题，而是「逢消息就抢着派子代理」的克制问题。**

现有体系（T1点名/T1.5承接/T2分类 + `_mode_shortcut_decision`）在**谁能接**上已经足够——它缺的是**什么时候该让主代理自己接、什么时候该克制不派**。所以「读空气仲裁」的价值，是在**自动路由这条路径**上加一道「宁静权」判据，克制过度派生。

而 **`parallel_handoff`（LLM 工具）不做仲裁砍人**——那是主代理在显式调度，顾主要的就是多人并行，不该被抢。

---

## 二、双路径 + 共享状态（V2 架构）

```
【共享】ConversationPresence 在场状态模块（新文件 arbitrate.py，按 unified_msg_origin 隔离）
  - recent: deque 最近 N 条发言 {agent, action, text, ts}
  - last_speaker / active_chain / pending_batch
  - 在 _call_one 转发后 / 主代理回复后更新；纯内存，不持久化

【路径 A · 自动路由】（router.py _smart_router_check 内，T1/T1.5/T2 命中后、`_mode_shortcut_decision` 前后）
  仲裁做「宁静权」克制，不做「核心裁定」
    R1 主代理令牌        → main（已有，保持）
    R2 T1 强点名 call_hits → 直发（绝不影响，延续 09-03 点名修复）
    R3 T1.5承接 && active_chain → 续接 last_speaker
    R4 群聊杂谈/无关提问(?broadcast 但 无点名无领域) → 若 `_mode_shortcut_decision` 本要放行主代理，
       则仲裁不做额外动作（不抢、不追加短路）——「读空气」只在**犹豫点**介入
    R5 弱路由(T2低置信) + 无在场连续 + 主代理活跃 → 建议 main（安全克制，不强派）

【路径 B · LLM 工具】（parallel_handoff / call_subagent，调用前）
  不做砍人，只做两件事：
  - 更新在场状态（记录本批 candidate，供路径 A 下轮「读空气」参考）
  - 温和收敛建议：当顾主 UI 只点名一人、但 calls 误带多人时，日志提示主代理「本批是否只需某人」
    （仅建议，不强制过滤 calls）

【关键约束】仲裁与 `_mode_shortcut_decision` 的关系：
  仲裁**只在 `_mode_shortcut_decision` 已决定放行主代理(return False) 的犹豫分支**上追加「读空气」判断，
  不做与 `_mode_shortcut_decision` 相悖的「反放行强行短路」。
  即：仲裁永远比 `_mode_shortcut_decision` 更「克制」，绝不更「激进」。
  这样二者绝不可能打架：一个裁决是否放行主代理，仲裁只决定「放行后是否要在后续自然处理中更克制派生」。
```

---

## 三、在场状态模块（arbitrate.py）

```python
class ConversationPresence:
    def __init__(self, window=6):
        self.window = window
        self.recent: deque = deque(maxlen=window)
        self.last_speaker: str | None = None
        self.active_chain: bool = False
        self.pending_batch: list[str] = []

class ArbitrationMixin:
    """读空气仲裁 mixin，混入主壳类（main.py 壳类 MRO 加一层）"""
    def _presence_get(self, event) -> ConversationPresence:
        """按 event.unified_msg_origin 惰性初始化"""
    def _presence_update(self, event, agent, text):
        """转发后/主代理回复后更新 last_speaker/recent/active_chain"""
    def _arbitrate_directive(self, event, message, route, mode_decision) -> str | None:
        """路径A：返回 None=不介入(保持原行为) / 'main'=建议克制落主代理。只在犹豫分支生效"""
    def _arbitrate_tool(self, event, calls) -> list[dict]:
        """路径B：返回原 calls（不过滤），仅在日志给出收敛建议"""
```

---

## 四、V2 改动清单（已对齐到行号）

| # | 位置 | 改动 | 是否新建 |
|---|---|---|---|
| 1 | `arbitrate.py` 新建 | `ConversationPresence` + `ArbitrationMixin` + 3 个方法 | ✅ 新建 |
| 2 | `main.py:56-65` 类定义 MRO | 混入 `_arbitrate_mod.ArbitrationMixin` | 改 |
| 3 | `main.py` import 段(31-47) | 加 `_arbitrate_mod` 导入（含 try/except 双路径） | 改 |
| 4 | `router.py:_smart_router_check`(604-686) | 在 `_mode_shortcut_decision` 之后、短路/放行之前，插 `_arbitrate_directive` 宁静权调 | 改 |
| 5 | `dispatch.py:_call_one`(221-462) 内转发成功处 | `_presence_update` 记录实际发言 | 改 |
| 6 | `dispatch.py:parallel_handoff`(462-776) 调用前 | `_arbitrate_tool` 收敛建议 + 更新 pending_batch | 改 |
| 7 | `config.py` | 加 `enable_read_air_arbitrate`(默认False)、`read_air_window=6` | 改 |

---

## 五、安全与回滚（质量和安全优先，比 V1 更严）

1. **总开关默认 False**：`enable_read_air_arbitrate=False`，不开等同零行为变化
2. **宁可不介入，不可误伤**：仲裁只做「克制」从不「抢派」。凡不确定是否介入 → 一律不介入
3. **点名神圣不可侵**：T1 call_hits 强点名（含助手E/M3 爱称）仲裁绝不拦（延续 09-03 修复）
4. **不与 `_mode_shortcut_decision` 相悖**：只在其「已放行主代理」的犹豫分支追加克制，绝无反向强行短路
5. **不砍 `calls`**：parallel_handoff 的多人并行是主代理显式意图，仲裁只建议不强制
6. **抑制必清承接记忆**：仲裁命中 main 时同步 pop `_route_mem` 该会话，防 T1.5 误续
7. **纯规则零 LLM 成本**：不回调 LLM，毫秒级，可单测
8. **失败不阻断**：仲裁异常 → warning → 按原行为走，绝不吃消息
9. **测试门禁**：新增 `test_arbitrate.py` 覆盖 6 个判定 + 状态窗口滚动 + 与 mode_decision 的兼容矩阵 + 全量 `test_plugin.py` 回归

---

## 六、实施步骤（四段，每段独立验收）

**段一·状态机骨架**：arbitrate.py + ConversationPresence + 挂进壳类 + `_presence_update` 记录。不碰任何路由行为。验收：日志可见状态滚动，行为不变。
> ✅ 已落地（2026-09-03 段一完成）：`ConversationPresence` 状态机 + `ArbitrationMixin` 已混入壳类 MRO，`_presence_get/_presence_update` 在场记录可用。

**段二·宁静权仲裁**：`_arbitrate_directive` 接入路径 A，默认关。验收：单测 6 判定 + 与 mode_decision 兼容矩阵全过；关闭时零行为变化。
> 🔧 段二·低侵入版已落地（2026-09-03 12:3x）：`_arbitrate_directive` 已接入 `_smart_router_check` 短路分支（observe-only），
> 规则 `_read_air_wants_quiet`（R1/R2/R4）纯规则零 LLM。当前【只输出 Read-Air 观察日志，绝不实际拦截路由】。
> 真实克制干预（返回 'main' 拦截短路）按顾主 2026-09-03 12:31 拍板：**等三期随机性日常补齐后，再一起实验性开启测试**。
> 开关 `enable_read_air_arbitrate` 默认 False，开启也仅观察不拦截。5 个读取记测试全过 + 全量 122/0 绿。

**段三·工具侧收敛**：`_arbitrate_tool` 接入路径 B + pending_batch 更新。验收：parallel_handoff 调用时日志给出收敛建议，不砍 calls。
> 🔧 段三已落地（2026-09-03 13:5x）：`_arbitrate_tool` 已实现并在 dispatch.py parallel_handoff 真正调度前接入。
> 1) 更新 pending_batch（本批候选名单，供路径 A 读空气参考）；2) 温和收敛建议——顾主只点名一人(call_t1_mentions 复用 RouterMixin)
> 却误带多人时打日志提示「本批是否只需某人」，绝不砍 calls（V2 关键约束）。4 个收敛测试全过 + 全量 126/0 绿。

**段四·回归+部署**：全量 test_arbitrate.py + test_plugin.py 过，`py_compile` 三查，`hot_reload_plugin` 上线。顾主验收四类场景：点名、群聊杂谈、承接句、多人并行。
> 🔧 段四回归+部署已完成（2026-09-03 14:3x）：py_compile 三查通过；test_plugin.py 全量 **112 passed**（含 9 个读空气测试）；total 126=112+旧归档 test_plugin_20260822(14，非本次验收)；hot_reload 生效、插件 v2.3.0 正常加载、日志无异常。

> 🔧 **段五·宁静权落地 + 两条死规则修复**（2026-09-10）：深挖发现段二落地时 R1/R4 两条规则**从未生效过**——
> ① R1 依赖 `last_speaker == MAIN_SPEAKER`，而 `_presence_update` 全库只在 dispatch 子代理转发后被调用一次，**主代理回复从不入册**（`MAIN_SPEAKER` 一次都没写入过）；
> ② R4 嵌在 R2 的无条件 `return True` 之前，两条 return 结果相同、判定白算，是死代码；且判据键名 `main` 与主代理真实记录键 `MAIN_SPEAKER("__main__")` 不一致，`c_main` 恒为 0。
> 修复：**a.** forward.py 的 `on_decorating_result` 出口新增 `_presence_mark_main`，补齐主代理发言入册（R1/R4 唯一数据来源；主代理静默场景不入册，避免污染 `last_speaker`）；**b.** R4 提到 R2 之前独立判定 + 键名对齐 `MAIN_SPEAKER`；**c.** 新增二级闸门 `read_air_enforce`（默认 false）——false=段二 observe-only、行为零变化，true=宁静权**真实拦截**：`_arbitrate_directive` 返 `"main"` → `_smart_router_check` 清承接记忆 + 放行主代理自然接住（R6 约定：拦截必清 `_route_mem`，否则下一轮 T0.5 粘滞按旧在场者组又把同组续上，拦截形同虚设）。回退只需置 false，无需改码。
> 验证：`test_plugin.py` 全量 **278 passed**（基线 272，零回归；新增 6 个读空气测试）。顺手治本一处**测试隔离缺陷**：`_presences` 是 mixin 类属性、跨测试实例共享，而全类测试共用同一 session key `"sess-readair"`，导致断言结果依赖执行顺序（新增测试改变字母序后即暴露）——现于 `_fresh_plugin` 内 `clear()`，每个测试从干净状态起步。
> ⚠️ 旧测试 `test_read_air_quiet_on_old_rivalry` 用手工 `record("main", ...)` 造数据，**测试绿但生产从不写入该键** —— 属"假测试掩盖真实缺陷"，已由 `test_read_air_r4_fires_with_real_main_key`（用真实键 `MAIN_SPEAKER`）覆盖补齐。
> 顾主四类场景验收需真实环境触发（点名/群聊杂谈/承接句/多人并行）→ 开关默认 False，三期补齐后一起实验性开启。

---

## 七、验收标准（顾主视角）

1. 点名助手E/M3 ← 依然准确直达（回归 09-03 修复）
2. 群聊杂谈/无关提问 ← 不再逢消息就抢派，主代理自然接住
3. 承接句 ← 平滑续给刚才的人，不断线
4. 多人并行 ← 顾主显式要的并行照常，不因仲裁被砍
5. 出岔子 ← 关 `enable_read_air_arbitrate` 即恢复，零风险

---

## 八、范围边界（本期不做）

- ❌ 不做 LLM 情感仲裁（吃 token 逆零成本原则）
- ❌ 不改 T1/T2/T3 与 `_mode_shortcut_decision` 现有规则
- ❌ 不砍 `parallel_handoff` 的 calls（主代理显式意图）
- ❌ 不做跨会话/全局在场（只按 unified_msg_origin 隔离）
- ❌ 不做心跳/主动唤醒（另一遗留项，不混入）

### 顾主 2026-09-03 定性修正（补进范围，强约束）

**① 随机性核心原则——三期任务，一期不越界**
- 接话人**不能被算死**、不能固定套路；每个人开启的话题也不能固定套路，应有**自己随机演化出一天日常生活**，天天过着不一样的生活
- 但这是**三期**任务，**一期只做「宁静权」克制，绝不引入随机生活演化**——免得一期方案跑到三期的地盘上
- V2 阶段仲裁只负责「该不该派 / 该让谁别抢」的克制判断，**不负责决定子代理每天聊什么、怎么演化**

**② 旧怨的表现形式——贫嘴，不针锋相对**
- 关系网里的旧怨（如主代理×助手E）只能体现在**贫嘴**上——刀子嘴豆腐心那种，嘴上贫、心里暖
- **不能针锋相对**，不做棱角对撞、不因旧怨产生对抗性仲裁裁决
- 读空气仲裁在裁决「谁更该接」时，关系网的旧怨**只作贫嘴基调与亲密度参考**，**绝不作「谁敌视谁」的对抗判据**

---

## 八·补 仲裁规则对顾主定性修正的落地

| 顾主定性 | V2 落地 |
|---|---|
| 随机性（一期不做） | 仲裁不做话题生成/演化，只做克制判断；三期另行实现 |
| 穷人日子贫嘴不针锋 | R4/R5 裁决旧怨组成员时，只按亲密度加权，不引入对抗性；子代理间旧怨默认为贫嘴语气 |

---

*主代理 V2 定稿，基于源码逐行核实的自迭代修正。关键改进：把「笼统仲裁层」拆成「双路径 + 共享状态」，抢话治理只放自动路由、绝不越权砍主代理显式调度，对齐 `_mode_shortcut_decision` 与 `_route_mem`。待顾主确认。*
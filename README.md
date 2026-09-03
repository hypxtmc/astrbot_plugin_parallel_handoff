# parallel_handoff — 并行子代理调用插件

并行路由插件，主代理协调中枢。支持主代理通过 **parallel_handoff / call_subagent** 统一入口同时调用多个子代理（如助手A、助手C、助手B、夕等），并行获取回复并汇总输出。

## 功能特性

- 统一入口：`parallel_handoff`（批量并行调用）与 `call_subagent`（单个子代理调用）两个工具共用同一套调度、路由、记忆链路
- 多路由模式：direct 直发 / relay 中转 / chained 接龙，WebUI 切换即时生效
- 场景与基线注入：自动携带当前对话场景、共用剧情基线，子代理不脱戏
- 长期记忆集成：接入 livingmemory 的召回/存储，支持排除名单与召回条数上限
- 跨轮上下文：子代理记住本轮会话历史，超限自动压缩
- 分段转发：子代理回复按括号/换行拆分逐条发送，阅读体验友好

## 路由模式

| 模式 | 说明 |
|------|------|
| `direct` | 子代理回复**直接分段转发到用户端**，不经主代理转述（`route_mode=direct` 时 `call_subagent` 路由） |
| `relay` | 子代理回复**返回主代理**，由主代理转述/加工后发出（`transfer_to_*` 路由） |
| `chained` | **接龙式串行调用**（`call_mode=chained`）：前一个子代理的回复自动传给下一个作为上下文，剧情连贯；接龙模式下 direct/relay 均正常工作 |
| 强制直连黑名单 | `handoff_blacklist_agents` 中的子代理（默认 `tech,技术Agent`）**永远走 `transfer_to_xxx` 直连**，不受 route_mode / direct_delivery_agents 影响；通过 parallel_handoff / call_subagent 调用它们会被拦截并提示改用 transfer_to_* |

路由优先级：**强制直连黑名单 > route_mode（direct/relay）> call_mode（parallel/chained）**。黑名单中的子代理绕开插件中转，主代理必须直接调用 `transfer_to_xxx`。

**动态模式覆盖（v2.2.0）**：`parallel_handoff` 工具新增 `route_mode` / `call_mode` 两个可选参数，主代理可在调用时显式覆盖配置默认值，按任务类型动态切换：
- **技术干活任务**：传 `route_mode="relay"`（子代理回复全部返回主代理汇总，不直发）+ `call_mode="parallel"`（并行调度）→ 主代理当统帅收卷
- **日常贴贴**：不传参数 → 走配置默认（direct 直发名单内子代理），主代理隐身
- **流水线任务**：传 `call_mode="chained"` 串行接龙（relay 语义仍生效）

**双模式可选设置（v2.3.0，顾主指定 2026-08-31）**：在 WebUI 配置两个 JSON 字符串块，自行决定每个模式的调度方式（数值随意填，改动即时生效，无需改代码）：

```json
"tech_mode_config": "{\"route_mode\": \"relay\", \"call_mode\": \"parallel\", \"timeout\": 120}",
"affection_mode_config": "{\"route_mode\": \"direct\", \"call_mode\": \"chained\", \"timeout\": 120}"
```

| 配置块 | 含义 | 默认调度 |
|:---|:---|:---|
| `tech_mode_config` | 技术干活模式（编码/检索/分析/资料整理/查证） | relay（回复返回主代理汇总）+ parallel（并行收卷）+ timeout 120s |
| `affection_mode_config` | 后宫贴贴模式（点名子代理/撒娇/聊天） | direct（直接分段转发用户端）+ chained（串行接龙）+ timeout 120s |

调用 `parallel_handoff` 时传 `mode="tech"` 或 `mode="affection"` 即按对应配置块调度；不传 mode 回落全局 `route_mode`/`call_mode`；显式传 `route_mode`/`call_mode` 参数优先于模式配置。与 T1/T2 小模型路由层的关系见「小模型路由层」章节——T1/T2 已由顾主关闭（`enable_smart_router: false`），本模式配置走主代理路径，不受短路影响。

**指令注入智能规则（v2.3.0）**：`directive_inject_mode=smart` 时的注入预判已从"一刀切"升级为**任务分类注入**：
- 消息命中技术特征关键词（代码/报错/查证/分析/配置/部署等）→ 注入 `tech_mode_config` 对应的技术干活指令（relay+parallel 统帅收卷）
- 消息命中点名/领域词/会话续接 → 注入 `affection_mode_config` 对应的后宫贴贴指令（direct 直发）
- 纯闲聊 → 不注入，省 token 不污染上下文

## 小模型路由层（SmartRouter）

在 `OnWaitingLLMRequestEvent`（主代理 LLM 调用前最早停点）判断本条消息该找谁。命中直接子代理直发 + `stop_event()` 短路，整个主代理流程（记忆召回/req 构建/LLM 调用）跳过；未命中一律落回主代理原路径，行为零变化。

**层级设计（三层降级）：**

| 层 | 机制 | 成本 | 命中条件 |
|:---|:---|:---|:---|
| **T1 规则层** | 点名（中文名/英文 id + 词边界）、爱称别名（`T1_ALIASES`）、强领域词（`T1_KEYWORDS`，如"工程部"→agent_b、"画室"→xi） | 0 LLM 成本，毫秒级 | 单点名 + 非叙述语境 + 非技术文本 |
| **T1.5 会话续接** | 纯承接短句（"继续""再来""嗯"等，`T1_CONTINUE_WORDS`）+ 时间窗内（默认 300s）上次路由目标 + 无新点名 | 0 LLM 成本 | 三者同时满足 |
| **T2 小模型路由** | glm-4-flash（默认 `dmxapi/glm-4-flash`）单消息判向，JSON 输出 `{route, confidence}`，注入最近 4 条会话上下文 + 最近直发回复尾部作剧情参照 | 1 次小模型调用，超时 5s | `confidence >= 阈值`（默认 0.8） |
| **T3 兜底层** | 任何异常/超时/低置信 → 不拦截，主代理原路径全量接管 | 0 | 无条件兜底 |

**内置保护（防误抢）：**
- **技术文本拦截**（T1）：报错/日志/代码块特征（traceback/exception/```/`code` 等）出现子代理名不视为点名，放行 main/T2——防 `module 'agent_c' not found` 这类把日志里的名字当呼叫乱路由
- **叙述语境识别**（T1）："某人说过""昨天和某人聊了"是提及非呼叫；多点名歧义交 T2/main 仲裁
- **T2 默认归 main**：判向 prompt 明确"技术任务（默认）→ main"，无强相关给低分（0.1-0.3）
- **T1.5 三重约束**：纯承接句 + 时间窗 + 无新点名，宁可不接也不误路由

**与动态模式覆盖的分工**：T1/T2 命中 = 点名直达通道（direct 直发，主代理隐身，后宫贴贴场景）；未命中落主代理 = 主代理可用 `route_mode="relay"` 收卷汇总（技术干活场景）。边界：技术任务中若**主动点名**子代理（"让助手A整理这份资料"），T1 会短路直发——如需让此类消息落主代理收卷，可考虑 `router_tech_override`（规划中，未实现）。

## 模块结构（v2.0.0 重构后）

```
main.py        入口壳：插件注册 + 事件/工具装饰器（on_llm_request / on_decorating_result / llm_tool），实现经 super() 转发到各 mixin
├── config.py      ConfigMixin    配置读取 / 迁移 / 保存 / 前缀开关动态切换
├── directive.py   DirectiveMixin 路由强制指令注入（OnLLMRequestEvent）+ 强制直连黑名单映射
├── scene.py       SceneMixin     场景上下文构建 + 共用剧情基线注入（仅对 relay 子代理生效）
├── memory.py      MemoryMixin    livingmemory 集成：接龙注入剥离 / 召回 / 存储 / 记忆工具过滤
├── dispatch.py    DispatchMixin  核心调度：去重守卫 / 单子代理调用 / 并行调度 / 跨轮上下文
└── forward.py     ForwardMixin   分段转发 / 主代理前缀注入 / 姓名前缀
```

装饰器方法必须留在 `main.py` 壳上（保证 `handler_module_path` 与插件主模块一致，AstrBot star_manager 按 module_path 绑定插件实例），业务实现全部在六个 mixin 模块中，职责单一、便于单测。

## 关键配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enable_scene_inject` | `true` | 调用子代理时自动注入当前场景信息（群聊/私聊、对话对象等） |
| `enable_baseline_inject` | `true` | 是否向子代理注入共用剧情基线 `shared_scene_baseline`（仅对 relay 子代理生效，direct 子代理回复不经主代理、注入无意义） |
| `shared_scene_baseline` | `""` | 共用剧情场景基线（多行文本），只放静态设定锚点，动态剧情由主代理动态传达 |
| `handoff_blacklist_agents` | `tech,技术Agent` | 强制直连黑名单：列出的子代理永远走 `transfer_to_*` 直连，不走插件 relay 中转；parallel_handoff/call_subagent 调用会被拦截 |
| `exclude_agents` | `tech,技术Agent` | 子代理记忆排除名单：列出的子代理不启用长期记忆（默认硬编码，配置只能追加不能删除） |
| `recall_enabled` | `true` | 子代理长期记忆开关（主代理记忆参数不受影响，独立设置） |
| `recall_default_k` | `5` | 子代理默认记忆召回条数 |
| `recall_max_k` | `10` | 召回条数硬上限 |
| `route_mode` | `direct` | 子代理路由模式：direct=直发用户端 / relay=返回主代理转述 |
| `call_mode` | `parallel` | 调用模式：parallel=并行 / chained=接龙串行 |
| `direct_delivery_agents` | `agent_a,agent_b,agent_c,xi` | 直接发送到用户的子代理列表（黑名单代理不受此配置影响） |
| `enable_route_directive` | `true` | 向主代理 LLM 请求注入路由强制指令（extra_user_content_parts），主代理必须照走 |
| `enable_smart_router` | `false` | 小模型路由层总开关（T1 规则 / T1.5 续接 / T2 小模型 / T3 兜底），显式开启才生效，默认关闭防误伤现网 |
| `router_confidence_threshold` | `0.8` | T2 小模型路由置信度阈值，`confidence >= 阈值` 才直连子代理，其余落 main |
| `router_provider_id` | `dmxapi/glm-4-flash` | T2 判向用的小模型 provider id |
| `router_timeout` | `5` | T2 小模型调用超时（秒），超时直接放行主代理 |
| `router_continue_window_sec` | `300` | T1.5 会话续接时间窗（秒），窗内纯承接短句直接续接上次路由对象 |
| `enable_segmented_forward` | `true` | 子代理回复分段转发开关 |
| `enable_subagent_name_prefix` | `true` | 子代理消息加【名字】前缀 |
| `enable_mainagent_name_prefix` | `false` | 主代理回复自动加【名字】前缀 |
| `subagent_context_enabled` | `true` | 启用跨轮对话上下文（tech/技术Agent 始终不启用） |
| `chain_summary_enabled` | `true` | 接龙长回复自动精简开关（超阈值压缩为摘要+首尾） |

## 版本历史

### v2.3.0 — 双模式可选设置 + 指令注入智能规则（当前）

- **双模式可选设置**：新增 `tech_mode_config` / `affection_mode_config` 两个顾主可填配置块（JSON 字符串），各自独立定义 route_mode / call_mode / timeout，顾主自行决定每个模式的调度方式；`parallel_handoff` 新增 `mode` 参数（tech/affection）按对应配置块调度，显式传参优先
- **指令注入智能规则**：`directive_inject_mode=smart` 预判升级为任务分类注入——技术特征关键词 → 注入 tech 模式指令（relay+parallel 统帅收卷）；点名/领域词/会话续接 → 注入 affection 模式指令（direct 直发）；纯闲聊不注入
- **兼容保留**：`_need_route_directive` 委托新 `_classify_directive_task`，不传 mode / always 模式走全局配置，旧行为不变

### v2.2.0 — 动态模式覆盖 + 路由层文档化

- **`parallel_handoff` 新增 `route_mode` / `call_mode` 可选参数**：主代理按任务类型动态覆盖配置默认值——技术干活传 `route_mode="relay"+call_mode="parallel"`（统帅收卷）、日常贴贴不传走默认直发、流水线传 `call_mode="chained"`
- **新增 `_is_direct_delivery` 统一判定**：relay 全员返回主代理，direct/auto 按直发名单，chained 流式转发与统一转发共用
- **路由强制指令（directive.py）补充"动态模式覆盖"规范**：明确技术任务/日常贴贴/流水线的传参方式
- **README 文档化小模型路由层**：T1 规则 / T1.5 续接 / T2 小模型 / T3 兜底完整说明 + 内置保护（技术文本拦截、叙述语境、T2 默认归 main）+ 与动态模式的分工边界
- **配置项表补齐 smart_router 字段**：`enable_smart_router` / `router_confidence_threshold` / `router_provider_id` / `router_timeout` / `router_continue_window_sec`

### v2.0.0 — 重构版

- **拆模块**：`main.py` 瘦身为入口壳（仅保留插件注册与事件/工具装饰器），业务实现拆分为 config / directive / scene / memory / dispatch / forward 六个 mixin 模块，装饰器方法留在主模块保证 handler_module_path 匹配
- **删旧配置兼容层**：移除旧版双层记忆排除逻辑，收敛为单一 `exclude_agents` 集合（tech/技术Agent 默认硬编码，配置只能追加不能删除）；旧配置项（如 `enable_name_prefix`）由新配置项取代，兼容层清理
- **黑名单拦截与基线隔离补测试**：强制直连黑名单（`handoff_blacklist_agents`）拦截逻辑、剧情基线注入隔离语义（仅 relay 生效）补充单测覆盖（test_plugin.py）

### v1.1.0 — 基线版

初始功能基线：parallel_handoff 并行调用、分段转发、姓名前缀、场景注入、热重载、防重守卫、livingmemory 记忆集成等

## 开发

- 依赖：AstrBot `>= 4.26.0`
- 测试：`test_plugin.py` 覆盖调度去重、路由指令、黑名单拦截、基线隔离、接龙精简等场景

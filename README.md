# parallel_handoff — 并行子代理调用插件

并行路由插件，主代理协调中枢。支持主代理通过 **parallel_handoff / call_subagent** 统一入口同时调用多个子代理（如阿米娅、特蕾西娅、可露希尔、夕等），并行获取回复并汇总输出。

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
| `direct_delivery_agents` | `amiya,closure,theresia,xi` | 直接发送到用户的子代理列表（黑名单代理不受此配置影响） |
| `enable_route_directive` | `true` | 向主代理 LLM 请求注入路由强制指令（extra_user_content_parts），主代理必须照走 |
| `enable_segmented_forward` | `true` | 子代理回复分段转发开关 |
| `enable_subagent_name_prefix` | `true` | 子代理消息加【名字】前缀 |
| `enable_mainagent_name_prefix` | `false` | 主代理回复自动加【名字】前缀 |
| `subagent_context_enabled` | `true` | 启用跨轮对话上下文（tech/技术Agent 始终不启用） |
| `chain_summary_enabled` | `true` | 接龙长回复自动精简开关（超阈值压缩为摘要+首尾） |

## 版本历史

### v2.0.0 — 重构版（当前）

- **拆模块**：`main.py` 瘦身为入口壳（仅保留插件注册与事件/工具装饰器），业务实现拆分为 config / directive / scene / memory / dispatch / forward 六个 mixin 模块，装饰器方法留在主模块保证 handler_module_path 匹配
- **删旧配置兼容层**：移除旧版双层记忆排除逻辑，收敛为单一 `exclude_agents` 集合（tech/技术Agent 默认硬编码，配置只能追加不能删除）；旧配置项（如 `enable_name_prefix`）由新配置项取代，兼容层清理
- **黑名单拦截与基线隔离补测试**：强制直连黑名单（`handoff_blacklist_agents`）拦截逻辑、剧情基线注入隔离语义（仅 relay 生效）补充单测覆盖（test_plugin.py）

### v1.1.0 — 基线版

初始功能基线：parallel_handoff 并行调用、分段转发、姓名前缀、场景注入、热重载、防重守卫、livingmemory 记忆集成等

## 开发

- 依赖：AstrBot `>= 4.26.0`
- 测试：`test_plugin.py` 覆盖调度去重、路由指令、黑名单拦截、基线隔离、接龙精简等场景

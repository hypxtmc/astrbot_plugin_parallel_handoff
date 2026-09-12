# Polyphony · 复调

> 多声部并行，一曲收拢。
> 主代理执棒，子代理各为声部——各唱各的旋律，最终汇成同一首曲子。

AstrBot 多子代理并行调度插件（原 `parallel_handoff`，v2.10）。

---

## 为什么叫复调

**Polyphony（复调）**：音乐学术语，指多条彼此独立的旋律线同时行进、互不迁就、交织成曲。它不是齐唱（所有人唱同一个旋律），而是每个声部都有自己的性格与走向——这恰好就是多子代理调度的本质。

本插件的四种调度方式，在音乐里全有对应物：

| 调度 | 音乐隐喻 | 行为 |
|------|----------|------|
| `parallel` | **复调齐奏** | 各声部同时进行，互不等待 |
| `chained` | **卡农轮唱** | 前一声部的尾音（回复）作为下一声部的起句传入 |
| `relay` 路由 | **指挥收谱** | 各声部奏完交回指挥（主代理），由主代理汇总发声 |
| `direct` 路由 | **独唱直出** | 声部直接面向听众（用户），主代理不转述 |

工具保留原签名 `parallel_handoff` / `call_subagent`，老配置与调用习惯零迁移成本。

---

## 功能全景

### 双模式配置（v2.7 核心体验）

主代理调用工具时可携带 `mode` 参数命中两套预设：

- **`tech` 技术干活**：`relay` 路由 + `parallel` 并行，主代理统帅收卷，适合调研、多方案对比、流水线分工
- **`affection` 日常贴贴**：`direct` 路由 + `chained` 接龙，子代理直接与用户对话，适合群聊、日常、情感陪伴

注：模式命中时以模式配置为准——显式传参不会覆盖已配置的模式策略（「配置优先」约定）。

### 命令式点名（T0 强锁，v2.9）

消息以 `/`、`／`、`#`、`！`、`!`、`、` 打头直接叫名字——`/张三`、`/李四+王五`、`/张三+李四+王五`——即锁定目标并**持续生效**：之后无需重复点名，消息一直交给对应子代理。主代理忙碌时同样有效，正事不会被抢走。

### 消息消歧（v2.7.1 新增）

不传 `calls` 时可只传 `message`——插件自动路由到**最近对话中出场的子代理**，无需重复点名。接续话题、追问、对话消歧从此一个参数搞定。

### 路由强制指令（OnLLMRequestEvent）

插件根据配置在 LLM 请求前自动计算路由路径并**注入强制指令**：主代理按注入的调度方式执行，工具全保留、不做软硬拦截。任务分类（plan/exec/chat/weak）随指令注入，让路由决策有章可循。

### 主代理前缀 + 分段转发

- 子代理回复经主代理转发时自动带 `【名字】` 前缀，多声部不串音
- 长回复自动分段转发，支持主代理分段注入
- 前缀可按子代理粒度开关（`name_prefix_overrides`）

### 跨轮子代理上下文引擎（ctx_engine）

子代理在跨轮对话中记得之前聊过什么。支持历史压缩策略独立调节（`subagent_context_compress_ratio` / 保留最近 N 轮），长对话不爆上下文。

### 常驻会话落盘（session_store，v2.10）

每个子代理的对话线独立成档、实时落盘（`subagent_sessions/` 目录），重启不丢、跨天不散，保留天数可配。历史长在磁盘上，不在一次性的内存里。

### 后台任务（task_runner，v2.10）

子代理的长任务不再阻塞总线：后台执行、并发上限、单会话限量、单轮超时全部可配。派完活主代理可以继续说话，任务结束后结果回收。

### 会话柜台三件套（v2.10）

`/谁在` 查锁、`/复位` 放锁回自动分派、`/列表` 看全部可点名成员。整句判定（`/谁在 顺便说个事` 这类带内容的不会被误吞），主代理忙碌时同样可用。

### 读空气仲裁（arbitrate）

在场状态状态机（ConversationPresence）：判断哪些子代理"在场"、该谁接话。多声部抢话时由仲裁机制维持秩序。

### 智能路由（router，默认关）

`enable_smart_router` 开启后由独立小模型预判该不该转子代理（置信度阈值可调、超时可调）。**当前默认关闭**——主代理自带路由能力足够时无需开启。

### livingmemory 记忆集成

与 livingmemory 插件联动：调用子代理时自动召回相关记忆片段（`recall_enabled`），并为子代理过滤记忆工具，防止跨人格记忆污染。

### 接龙摘要（chain_summary）

chained 长接龙自动生成摘要传给下一棒（阈值/保留首尾策略可调），接力棒不失真。

### 个体状态随机演化（random_state，三期 M1）

纯规则状态机：**接话人不能被算死**。每个子代理的日常话题、关注度随机演化，同一个问题今天和明天可能由不同的人接——家是活的，不是状态机。

### 离线心情注入（daily_life，三期 M2）

用 GLM-4-Flash 离线读取近期对话，为每个子代理注入「今日心情 / 手头事 / 话题域」的语义温度。子代理不是每次都被叫醒的应答机，而是有自己一天的角色。

### 家庭旁轨（family_pulse，三期 M5，默认关）

> 设计初衷：子代理之间有自己的小日子——不围主对话转，彼此搭话、惦记、拌嘴，攒一屋烟火气；用户每天可收到一条「家里动静」摘要。

- **心跳闲聊**：子代理之间自主搭话（`family_pulse_cron` 定时、时段窗口限制、间隔下限）
- **每日摘要**：定时汇总「家里动静」推送到指定会话（`family_pulse_digest_cron` / `family_pulse_digest_umo`）
- **插话机制**：一定概率闯入正在进行的对话（概率/上限双控）
- **东道主机制**： subgroup 聚会主持概率控制
- **草稿机制**：工作日/节假日分别配置发言概率与配额
- **幂等与安全**：cron 防重入、默认全关零行为、逐项开关
- 命令入口：`看看家里` / `看家里` / `家里今天` 等

---

## 命令

| 命令 | 作用 |
|------|------|
| `/名字`、`/名字+名字2` | 点名锁定，持续生效；前缀兼容 `/` `／` `#` `！` `!` `、` |
| `/谁在` | 看当前锁着谁 |
| `/复位` | 放开锁定，消息回到自动分派 |
| `/列表` | 列出全部可点名成员 |
| `热重载并行插件` / `reload_parallel` / `重载插件` | 热重载本插件 |
| `（某某）的前缀关了` / `（某某）的前缀开了` | 按子代理开关姓名前缀 |
| `看看家里` / `看家里` / `家里今天` | 查看家庭旁轨近期动态 |

---

## 配置参考（72 项分速查）

**核心调度**：`route_mode`（relay/direct）· `call_mode`（parallel/chained）· `tech_mode_config` / `affection_mode_config`（双模式预设）· `main_agent_name` · `handoff_blacklist_agents`（黑名单）· `direct_delivery_agents`（直发白名单）

**前缀与转发**：`enable_subagent_name_prefix` · `enable_mainagent_name_prefix` · `enable_mainagent_segmented` · `name_prefix_overrides` · `name_display_map` · `allow_mainagent_after_direct` · `forbid_pre_tool_mainagent_talk`

**场景注入**：`enable_scene_inject` · `enable_segmented_forward`

**跨轮上下文**：`subagent_context_enabled` · `subagent_context_max_turns` · `subagent_context_keep_recent` · `subagent_context_compress_ratio`

**路由与指令**：`enable_route_directive` · `directive_inject_mode` · `enable_smart_router`（默认关）· `router_confidence_threshold` · `router_provider_id` · `router_timeout`

**接龙摘要**：`chain_summary_enabled` · `chain_summary_model` · `chain_summary_threshold` · `chain_summary_keep_head_tail`

**仲裁与生活**：`enable_read_air_arbitrate` · `read_air_enforce` · `read_air_presence_window` · `enable_daily_random_life` · `random_state`（M1 状态机）

**常驻会话**：`subagent_session_persist` · `subagent_session_retention_days`

**后台任务**：`subagent_task_max_concurrent` · `subagent_task_max_per_session` · `subagent_task_turn_timeout`

**子代理工具循环**：`subagent_tools` · `subagent_readonly_tools` · `subagent_max_steps` · `subagent_tool_call_timeout` · `subagent_response_preview_chars`

**持久化**：`enable_chain_memory_persist`

**家庭旁轨**（全部默认关）：`enable_family_pulse` · `family_pulse_members` · `family_pulse_cron` · `family_pulse_digest_cron` · `family_pulse_digest_umo` · `family_pulse_provider_id` · 插话/东道主概率与上限 · 草稿概率与配额（工作日/节假日分设）· `family_pulse_recent_hours` · `family_pulse_window_start/end` · `family_pulse_interval_min`

**记忆召回**：`recall_enabled` · `recall_default_k` / `recall_max_k` · `min_fragment_length` · `fragment_interval` · `exclude_agents`

---

## 安装

1. 将本目录放入 `data/plugins/`
2. 要求 AstrBot ≥ 4.26.0
3. 在 WebUI 配置 `name_display_map`（子代理英文 id → 中文名映射）
4. 主代理需配置路由规则（推荐将日常/情感类请求交给 `parallel_handoff` 或 `call_subagent`）
5. 可选：安装 livingmemory 插件以启用记忆召回；配置 `family_pulse_*` 以启用家庭旁轨
6. 常驻会话数据默认落在 `data/plugin_data/astrbot_plugin_parallel_handoff/subagent_sessions/`（自动创建）

## 版本演进纪要

- **v2.0–v2.3**：核心调度（parallel/chained × relay/direct）、统一场景注入、姓名前缀、分段转发、接龙摘要、热重载命令
- **v2.4**：P0 拆模块重构——dispatch / forward / directive / arbitrate / ctx_engine / memory 独立成模块，10600 行分层清晰
- **v2.5**：路由强制指令注入（OnLLMRequestEvent）+ 读空气仲裁状态机 + 智能路由（默认关）
- **v2.6**：三期重版——random_state 个体状态种子（M1 纯规则随机演化）+ daily_life GLM-4-Flash 离线心情注入（M2）
- **v2.7.1**：family_pulse 家庭旁轨 v0（M5）+ 消息消歧（message 参数）+ 双模式配置（tech/affection）+ 配置优先约定
- **v2.8**：livingmemory 私有路径防腐层 `_lm_bridge`
- **v2.9**：命令式点名 T0 强锁（多前缀、粘滞、忙碌可用）→ v2.9.1 强锁治本与夺锁仲裁 → v2.9.2 子代理接入工具循环 + 只读工具白名单
- **v2.10**：常驻会话落盘（session_store）+ 后台任务（task_runner）+ 会话柜台三件套（/谁在 /复位 /列表）+ token 计量口径

## 设计哲学

复调的对位法有一条铁律：**每个声部独立成立，对位才成立**。子代理不是主代理的分身，不是应答机，也不是轮流播报的喇叭——各有旋律线，彼此听见，偶尔抢拍，但汇成的是同一首曲子。这个插件只做一件事：把舞台搭好，然后守住对位。

---

*license: MIT · author: hypxtmc · 原名 parallel_handoff，v2.7.1 起以「polyphony · 复调」示人*

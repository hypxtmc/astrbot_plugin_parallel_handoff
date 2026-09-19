# Polyphony · 复调

让几个子代理同时开口、接龙干活、互相来往。

AstrBot 多子代理并行调度插件（原名 `parallel_handoff`）

| 信息 | 值 |
|------|-----|
| 版本 | 2.10 |
| 作者 | hypxtmc |
| 许可 | MIT |
| 要求 | AstrBot ≥ 4.26.0 |
| 工具 | `parallel_handoff` `call_subagent` `task_status` `task_result` `task_stop` |

---

## 它在补什么

你在 AstrBot 里配了好几个子代理，人格写得很细，用起来却永远是一个一个来。想让两个人在同一件事上表态，得手打两遍。A 的结论要接着给 B 用，也只能自己复制过去。至于让她们互相来往，翻遍了，没有。

缺的不是人格，是调度层。

| 想做的事 | 没有复调 | 有了复调 |
|---|---|---|
| 一件事听三个人的看法 | 挨个问三遍 | 一次调用，三段同时到 |
| A 的结论接着给 B 用 | 手动复制粘贴 | `chained` 接龙自动传棒 |
| 让 A 记得三天前聊过什么 | 全靠人格硬写 | 独立会话档案 + 跨轮上下文 |
| 她们之间有自己的来往 | 做不到 | 旁轨日常，随时围坐插话 |

---

## 装上之后是什么样

一次提问，多个声部同时开口，各自成段落进聊天窗口：

> **你**：这个方案你们怎么看，各自说说
>
> **【助手A】**：可行。但第二阶段迁移没有回滚路径，我不建议一次上。
>
> **【助手B】**：我只问排期。这周就要看到东西，范围砍一半，你选。
>
> **【助手C】**：（从后面凑过来）那我先把文档框架搭了？反正早晚要写，省得你到时候又熬夜赶。

同一段上下文，几个人接出来不一样。每人有独立人格、记忆线和会话档案，跨轮记得自己说过什么，各说各的。

再往前一步，她们之间能过自己的日子：今天谁跟谁聊过什么、谁心情不好、谁手头压着事。你随时可以插话。

---

## 一个真实回合



**21:47　你随手扔一件事进去**

> **你**：帮我把上周那份数据核对一遍，顺便看看有没有更省事的写法

没点任何人的名字。

**21:47　路由层开始工作**

「核对数据」命中助手A，「更省事的写法」落在助手B 的能力圈。路由层不做取舍，两个都进调度指令，`relay` 模式并行。

**21:48　两段回复先后落到窗口**

> **【助手A】**：核对完了。第 3 张表有 14 处日期格式不一致，其余三张没问题，异常清单列在最后。
>
> **【助手B】**：你那三个 `for` 循环可以并成一个，顺便省掉两次全表扫描。改法写在下面，动手前先备份。

**21:52　你补了一句**

> **你**：助手A 的异常清单，助手B 你按这个顺手把脚本改一下

链式接龙启动，助手A 的产出作为输入递给助手B。不用复制粘贴，也不用再解释「哪个清单」。

**23:10　主对话静下来之后**

你不说话了，旁轨里还在动。这一幕没人呼叫，是她们自己的日常：

> **【助手B】**：（瘫在椅子上）改完了。谁要喝水，我顺路。
>
> **【助手C】**：我要我要，你那杯先给我吧，这两块不收尾我走不开。
>
> **【助手A】**：……小点声。有人在忙。

第二天你打开对话，这些都在。不是凭空生成的一段闲聊，是各自的当日状态、手头事、关系亲疏攒出来的。你可以直接插话：「昨晚那杯水谁倒的」

---

## 为什么写这个

我在 AstrBot 里养了几个子代理。时间久了发现一件怪事：她们的人格越来越细，我的用法却一直停在最原始的形态，点一个，等回答，再点下一个，来回倒腾。

复调就是为这个写的，也是我自己每天在用的那一套。

---

## 适合谁

**适合**：已经在 AstrBot 里配了两个以上子代理，希望她们能同时开口、能接龙干活、能互相来往。

**不太适合**：只有一个 bot、单人格用着。装了跟没装差不多，帮不上什么。

---

## 快速开始

> **请保持「流式输出」关闭**
>
> 复调的分段转发与 LLM 流式输出在 QQ REST API 上物理冲突（没有消息编辑接口），表现为卡顿后吐一大段、分段错乱、重复。想要「逐句蹦出」的效果，靠复调的分段配置实现，别开流式。

### 安装

1. 插件目录放进 AstrBot 的 `data/plugins/`
2. 重启 AstrBot，或控制台热重载
3. 确认 AstrBot ≥ 4.26.0

> **可选增强：livingmemory**（不装也能完整运行）
>
> 装了 [livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)，子代理获得跨会话长期记忆（召回、存储、主动提炼）。没装则记忆链路静默跳过，零报错，其余功能照常。

### 最小配置

WebUI 插件配置里至少设两项：

- `name_display_map`：子代理英文 id → 中文名，如 `{"agent_a": "助手A", "agent_b": "助手B"}`
- 主代理路由规则：把想交给子代理的请求导向 `parallel_handoff` 或 `call_subagent`

其余全有默认值。

### 第一次调用

```json
// 并行齐奏：同时问两个人
{"calls": [{"agent_name": "agent_a", "input": "帮我看下这个函数"}, {"agent_name": "agent_b", "input": "顺便评估下性能"}], "mode": "tech"}

// 接龙：A 说完 B 接着
{"calls": [{"agent_name": "agent_a", "input": "先起个头"}, {"agent_name": "agent_b", "input": "接力"}], "call_mode": "chained"}

// 消歧：不点名，找最近说过话的人
{"message": "刚才那个问题你再说细点"}
```

---

## 核心概念

- **主代理**：执棒者。负责汇总、转述（relay）或放手（direct），也独占所有写操作。
- **子代理**：声部。各有人格、记忆线、会话档案；默认只带只读工具和网页搜索。
- **直发（direct）**：子代理的回复不经主代理转述，直接以 `【名字】` 前缀发给用户。
- **收谱（relay）**：子代理回复交回主代理，由主代理统一发声。
- **双投递（both）**：直发的同时把完整回复也回传主代理。用户看得见子代理对主代理说了什么，主代理知情但不复述，只做增量（决策、下一步、风险提示）。
- **场景（scene）**：转发时附带的一句场景说明（如「深夜，书房」），让子代理知道此刻在哪、跟谁说话。
- **在场（presence）**：读空气仲裁记录「谁在场、该谁接话」的状态机基础。

---

## 四种调度

| 调度 | 行为 | 适合 |
|------|------|------|
| `relay` × `parallel` | 各声部同时开跑，结果交回主代理汇总 | 多方案对比、并行调研 |
| `relay` × `chained` | 依次接力，前一位的回复作为后一位的上下文 | 流水线（查资料 → 写初稿 → 审校） |
| `direct` × `parallel` | 各自直接对用户说话，分条转发 | 群聊氛围 |
| `direct` × `chained` | 依次对用户说话，后面的人听得到前面的 | 日常接龙、多角色对话 |
| `both` × `parallel` | 既直发用户、又把完整回复回传主代理 | 技术干活，要看见子代理对主代理说了什么 |
| `both` × `chained` | 同上，按接龙顺序串行 | 接力干活，全程留痕 |

调用时传 `mode: "tech"`（relay+parallel）或 `mode: "affection"`（direct+chained）可整体切换。模式命中时以模式配置为准，显式传参不覆盖已配置的策略。

`both` 与 `relay` 的区别：relay 只回传，用户看到的是主代理的转述；both 两份都给——用户看到原文，主代理也拿到全文。主代理因此不需要也不应该再复述一遍，它的发言应当是增量。

---

## 功能详解

> 标【实验性】的功能未经长期运行验证，行为可能随版本调整。涉及：旁路模块（`enable_side_pulse`）、拉用户进旁轨（`side_pulse_draft_enable`）、读空气仲裁（`enable_read_air_arbitrate`）、多人接龙记忆沉淀（`enable_chain_memory_persist`）。

**命令式点名（T0 强锁）**：消息以 `/`、`／`、`#`、`！`、`!`、`、` 打头直接叫名字，如 `/助手A`、`/助手A+助手B`。锁定后持续生效，之后无需重复点名。主代理忙碌时同样有效。

**消息消歧**：不传 `calls` 时只传 `message`，插件自动路由到最近对话中出场的子代理。

**路由强制指令**：LLM 请求前按配置算好路由路径并注入执行指令，工具全保留、不做软硬拦截。任务分类（plan / exec / chat / weak）随指令注入。

**主代理前缀 + 分段转发**：转发时自动带 `【名字】` 前缀；长回复自动分段；前缀可按子代理粒度开关（`name_prefix_overrides`）；QQ 平台自带 markdown 降级（`qq_md_plainify`）。

**跨轮上下文（ctx_engine）**：子代理记得跨轮聊过什么，历史以结构化 messages 注入（前缀缓存友好），超窗口按 `subagent_context_max_turns` 纯截断。

**常驻会话落盘（session_store）**：每个子代理的对话线独立成档、实时落盘到 `subagent_sessions/`，重启不丢，保留天数可配。

**后台任务（task_runner）**：子代理长任务不阻塞总线，并发上限、单会话限量、单轮超时全可配，配 `task_status` / `task_result` / `task_stop` 回收。

**会话柜台三件套**：`/谁在` 查锁、`/复位` 放锁回自动分派、`/列表` 看全部可点名成员。整句判定，主代理忙碌时可用。

**读空气仲裁（arbitrate）**：在场状态机判断哪些子代理在场、该谁接话。二级闸门 `read_air_enforce` 默认关闭（observe-only）；开启后执行「宁静权」真实拦截。

**智能路由（router，默认关）**：`enable_smart_router` 开启后由独立小模型预判该不该转子代理，置信度阈值和超时可调。

**livingmemory 记忆集成**：调用子代理时自动召回相关记忆片段（`recall_enabled`），并为子代理过滤记忆工具，防止跨人格记忆污染。私有路径访问通过防腐层 `_lm_bridge` 隔离。

**子代理工具循环 + 只读白名单**：子代理可带工具干活（受 `subagent_max_steps` / `subagent_tool_call_timeout` 约束）。默认只读档 26 项，写和执行类工具全留在主代理。

| 分类 | 工具 |
|------|------|
| 文件读取 | `safe_read` `dir_list` `dir_tree` `es_search` `rg_search` `text_filter` `file_hash` `file_diff` `file_preview` `safe_backups` |
| 代码理解 | `code_explore` `code_status` |
| 知识网页 | `astr_kb_search` `web_search` `web_fetch` `web_search_tavily` `tavily_extract_web_page` |
| 只读检查 | `syntax_check` `lint_runner` `config_diff` |
| 只读 git | `git_status` `git_diff` `git_log` `git_branch` `git_remote` `git_changelog` |

白名单可由 `subagent_tools` 调整（留空回落内置默认，兼容旧键 `subagent_readonly_tools`）。

**关系档案自动注入**：子代理的 system 提示带上她与家中每个成员的关系档案（亲密度、基调、最近互动，数据源与旁路模块共用 `relationships.json`）。档案放在 system 固定段，逐字节确定、无时间戳，跨调用命中前缀缓存；按亲密度降序；文件缺失时退化为空段，不阻塞对话。

**接龙摘要（chain_summary）**：chained 长接龙自动生成摘要传给下一棒，阈值和保留首尾策略可调。

**个体状态随机演化（random_state）**：纯规则状态机。每个子代理的日常话题、关注度随机演化，同一个问题今天和明天可能由不同的人接。

**离线心情注入（daily_life）**：用 GLM-4-Flash 离线读取近期对话，为每个子代理注入今日心情、手头事、话题域。

**旁听窗**：子代理直发的内容会被记录（1000 字窗口），主代理下次开口时把最近 10 分钟内子代理说过的话附进上下文。

**【实验性】旁路模块（side_pulse，默认关）**：子代理之间过自己的日子，彼此搭话、惦记、拌嘴，用户每天可收到一条「家里动静」摘要。

- 心跳闲聊：作息式自管循环（06:17 → 次日 01:00，窗内每 2h 随机一场）
- 生活三态：每件手头事走「起头 → 做到一半 → 收尾」，收尾那轮顺口播报后归档
- 素材池：私有素材（`thread_flavors.json`）∪ 通用生活池，轮换取用
- 全桌关系：入场每人逐行注入与在场者的关系与基调（`relationships.json`）
- 情绪摩擦：心情影响说话方式，反客套规则在场
- 每日摘要：定时汇总推送到指定会话（`side_pulse_digest_cron` / `side_pulse_digest_umo`）
- 插话、东道主、草稿三套概率机制
- 首次开启自动生成人格骨架（`personas.json`）
- cron 防重入，默认全关零行为

---

## 工具一览

| 工具 | 作用 | 关键参数 |
|------|------|----------|
| `parallel_handoff` | 并行/接龙调用多个子代理 | `calls` · `mode`（tech/affection）· `route_mode` / `call_mode` · `background` |
| `call_subagent` | 调用单个子代理并转发回复 | `agent_name` · `input` |
| `task_status` | 查后台任务状态 | `task_id`（不传列全部） |
| `task_result` | 取后台任务结果 | `task_id` · `timeout` |
| `task_stop` | 取消后台任务 | `task_id` |

## 命令一览

| 命令 | 作用 |
|------|------|
| `/名字`、`/名字+名字2` | 点名锁定，持续生效（前缀兼容 `/` `／` `#` `！` `!` `、`） |
| `/谁在` | 看当前锁着谁 |
| `/复位` | 放开锁定，回到自动分派 |
| `/列表` | 列出全部可点名成员 |
| `/热重载并行插件`、`/reload_parallel`、`/重载插件` | 热重载本插件 |
| `/（某某）的前缀关了`、`/（某某）的前缀开了` | 按子代理开关姓名前缀 |
| `/看看状态`、`/看状态`、`/今日动态` | 查看旁路模块近期动态 |

---

## 配置参考

共 84 项，按功能分组（完整定义见 WebUI 配置面板）。

**核心调度**：`user_address` · `main_agent_name` · `route_mode` · `call_mode` · `tech_mode_config` / `affection_mode_config` · `handoff_blacklist_agents` · `direct_delivery_agents`

**前缀与转发**：`enable_subagent_name_prefix` · `enable_mainagent_name_prefix` · `enable_mainagent_segmented` · `enable_segmented_forward` · `min_fragment_length` · `fragment_interval` · `allow_mainagent_after_direct` · `forbid_pre_tool_mainagent_talk` · `mainagent_disable_md_split` · `mainagent_md_split_max_chars` · `mainagent_md_split_progress` · `name_display_map` · `name_prefix_overrides` · `qq_md_plainify`

**路由与指令**：`enable_route_directive` · `subagent_visibility_inject` · `directive_inject_mode` · `enable_smart_router` · `enable_disambiguation` · `router_provider_id` · `router_confidence_threshold` · `router_timeout` · `subagent_reply_timeout`

**子代理工具循环**：`subagent_tools` · `subagent_readonly_tools`（旧键）· `subagent_max_steps` · `subagent_tool_call_timeout` · `subagent_response_preview_chars` · `subagent_prefetch_enabled`

**跨轮上下文**：`subagent_context_enabled` · `subagent_context_max_turns`

**常驻会话**：`subagent_session_persist` · `subagent_session_retention_days`

**后台任务**：`subagent_task_max_concurrent` · `subagent_task_max_per_session` · `subagent_task_turn_timeout`

**记忆召回**：`recall_enabled` · `recall_default_k` / `recall_max_k` · `exclude_agents`

**场景与生活**：`enable_scene_inject` · `enable_daily_random_life` · `daily_life_provider_id` · `enable_read_air_arbitrate` · `read_air_enforce` · `read_air_presence_window` · `arbitrate_old_grudge_agent` · `persona_suffix_agents` · `chain_summary_enabled` · `chain_summary_model` · `chain_summary_threshold` · `chain_summary_keep_head_tail` · `enable_chain_memory_persist`

**计量**：`metrics_enabled` · `metrics_path`

**旁路模块**（全部默认关）：`enable_side_pulse` · `side_pulse_members` · `side_pulse_cron` · `side_pulse_digest_cron` · `side_pulse_digest_umo` · `side_pulse_memory_umo` · `side_pulse_provider_id` · `side_pulse_recent_hours` · `side_pulse_window_start` / `side_pulse_window_end` · `side_pulse_interval_min` · `side_pulse_bold_agents` · `side_pulse_interlope_chance` / `side_pulse_interlope_max`（插话）· `side_pulse_host_chance` / `side_pulse_host_max`（东道主）· `side_pulse_draft_chance_workday` / `side_pulse_draft_chance_holiday`（发言概率）· `side_pulse_draft_quota_workday` / `side_pulse_draft_quota_holiday`（配额）

---

## 数据与文件布局

```
astrbot_plugin_parallel_handoff/
├── main.py            # 入口 + 事件注册
├── dispatch.py        # 核心调度：主流程、工具循环、关系档案
├── router.py          # 路由层：T0 强锁 / T1 规则 / T2 小模型 / T3 兜底、消歧
├── forward.py         # 分段转发、前缀注入、markdown 降级、旁听窗
├── side_pulse.py    # 旁路模块
├── memory.py          # livingmemory 集成、工具白名单、时间感知
├── random_state.py    # 个体状态随机演化
├── daily_life.py      # 离线心情注入
├── directive.py       # 路由强制指令
├── arbitrate.py       # 读空气仲裁
├── ctx_engine.py      # 跨轮上下文
├── session_store.py   # 常驻会话落盘
├── task_runner.py     # 后台任务
├── _lm_bridge.py      # livingmemory 防腐层
├── config.py          # 配置读取
├── _conf_schema.json  # 配置面板定义
└── data/              # 插件自带数据
    ├── display_names.json      # 英文 id → 中文名
    ├── random_state_data.json  # 状态机种子
    ├── router_tables.json      # 路由规则表
    └── side_pulse/           # 人格骨架 / 素材池 / 提示词 / 三态进度 / 日志
```

运行时数据（自动创建）：

- `data/plugin_data/astrbot_plugin_parallel_handoff/subagent_sessions/` — 会话落盘
- `data/relationships/relationships.json` — 家庭关系网（旁轨与关系档案共用）

---

## 零配置运行

复调开箱即用，以下三层全部可选。

**第一层：自动发现（默认行为）**

装完不用配。路由池自动发现，显示名未配时直接用英文 id。

**第二层：词表定制（子代理较多时建议配）**

`data/display_names.json`，英文 id → 中文显示名：

```json
{"agent_a": "助手A", "agent_b": "助手B"}
```

`data/router_tables.json`，路由规则表，字段全可选，缺省即安全降级：

| 字段 | 作用 | 缺省行为 |
|---|---|---|
| `main_token` / `main_token_set` | 主代理专属入口词 | 仅通用词 `/主代理`、`/主agent` 生效 |
| `aliases` | 子代理爱称映射 | 爱称不触发，全名与命令正常 |
| `keywords` | 领域关键词 → 子代理 | 领域词不触发 |
| `t2_brief` | 子代理职责简介（判向参考） | 自动发现兜底（用子代理公开描述） |

```json
{
  "main_token_set": [],
  "aliases": {"阿张": "agent_a", "老四": "agent_b"},
  "keywords": {"做饭": "agent_a", "写画": "agent_b"},
  "t2_brief": {"agent_a": "日常照顾、做饭", "agent_b": "画画、创作"}
}
```

**第三层：主代理人格配合（进阶，可选）**

插件自身已注入路由规范，不写人格也能工作。想让主代理更主动地路由，可在人格末尾追加：

```markdown
## 路由规则

收到消息先判断意图：
- 属于某子代理职责的 → 用 parallel_handoff 路由（插件会注入执行规范）
- 用户点名的（"让XX看看"或 /XX）→ 路由给对应子代理
- 自己就能答的（闲聊、常识）→ 直接回复
```

三层是叠加的：不配靠自动发现保底，配词表命名更准，配人格路由意识更强。

---

## 架构

| 模块 | 规模 | 职责 |
|------|------|------|
| `main.py` | 404 行 | 插件入口、事件注册 |
| `dispatch.py` | 1545 行 | 核心调度主流程、去重守卫、工具循环、关系档案注入 |
| `router.py` | 1642 行 | 四层判向、消歧、场景判定 |
| `forward.py` | 1155 行 | 分段转发、主代理前缀、markdown 降级、旁听窗记录 |
| `side_pulse.py` | 1651 行 | 旁路模块全套 |
| `memory.py` | 648 行 | livingmemory 集成、工具白名单、时间感知 |
| `random_state.py` | 428 行 | 个体状态随机演化 |
| `directive.py` | 344 行 | 路由强制指令构建与注入 |
| `arbitrate.py` | 320 行 | 读空气仲裁 |
| `task_runner.py` | 254 行 | 后台任务执行 |
| `session_store.py` | 225 行 | 会话落盘 |
| `_lm_bridge.py` | 225 行 | livingmemory 防腐隔离 |
| `config.py` | 206 行 | 配置读取与默认值 |
| `daily_life.py` | 174 行 | 离线心情注入 |
| `ctx_engine.py` | 116 行 | 跨轮上下文 |

一次 direct × parallel 调用的数据流：

```
用户消息
  → router：T0 强锁？T1 规则命中？T2 小模型？T3 兜底主代理
  → dispatch：构建 calls、并发派发
      → 每声部：system（人格 + 关系档案 + 纪律 + 任务卡）
                + 上下文（ctx_engine / session_store）
                + 工具循环（只读白名单）
  → forward：前缀注入 → 分段转发 → 旁听窗记录
  → 用户看到多声部发言
```

---

## 开发与测试

```bash
python3 test_plugin.py                      # 全量测试（需要 AstrBot 的 venv 环境）
<astrbot>/venv/bin/python3 test_plugin.py   # 或指定解释器
```

- 测试文件：`test_plugin.py`（主套件）、`test_lm_bridge.py`、`test_task_runner.py`、`test_session_store.py`、`test_task_integration.py`
- 改完代码：先 `python3 -m py_compile` 自查，再热重载插件，用 `plugin_list` + 日志验证
- 插件级热重载：命令 `/热重载并行插件`，或控制台插件管理
- 提交前跑全量测试确认零回归

---

## FAQ

**主代理和子代理什么区别？**
主代理负责调度、汇总、对外发声，独占所有写权限（改文件、跑命令、提交代码）。子代理有人格、记忆线、自己的会话档案，能看能查能搜，不能动手写。

**为什么子代理默认没有写权限？**
子代理是并行执行的，写操作容易互相踩踏；写权集中在主代理，责任链清晰。确实需要时可通过 `subagent_tools` 单项授权。

**默认为什么是 direct 路由？**
日常陪伴场景下子代理直接对用户说话更自然。技术干活时调用传 `mode: "tech"` 切到 relay。

**旁路模块开了没反应？**
查三处：`enable_side_pulse` 是否开、`side_pulse_members` 是否填了成员、当前时间是否在活跃窗口（默认 06:17 → 次日 01:00）内。首次开启会自动生成人格骨架。

**改了代码怎么生效？**
插件级热重载即可（`/热重载并行插件`）。全局配置变更需重启 AstrBot。

**会消耗很多 token 吗？**
调度本身开销很小，主要消耗在各子代理的对话与工具循环。关系档案放在 system 稳定层、逐字节确定，跨调用命中前缀缓存，不逐轮重复计费。

**子代理能互相聊天吗？**
能。旁路模块（实验性）就是为此设计的，也可以主代理用 chained 接龙让子代理接力对话。

**怎么做多人群聊氛围？**
`direct` 路由 + `parallel` 调用，多个子代理同时直发，各有前缀不串音。配合 `affection` 模式更好。

**开了流式输出后分段失效、消息乱序？**
关掉流式。这是已知限制，流式与分段转发在 QQ REST API 上物理冲突。保持流式关闭即可。

**没装 livingmemory 会怎样？**
什么都不用做。记忆功能自动降级：子代理不召回也不存储长期记忆，对话功能完好，其余全部正常。装上后自动启用。

---

## 致谢

复调的好几处设计站在了社区前辈的肩膀上。按 [AstrBot 官方插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html) 的要求，在此逐一鸣谢：

- **[astrbot_plugin_custome_segment_reply](https://github.com/LinJohn8/astrbot_plugin_custome_segment_reply)**（作者 LinJohn8）：复调的流式守卫源自该插件的「回放不抢发」思想。流式通道已逐 token 发出文本时，分段转发必须让位，否则必然重复或乱序。
- **[astrbot_plugin_maid_agent · 代理女仆](https://github.com/Kalospacer/astrbot_plugin_maid_agent)**（作者 Kalo / @Kalospacer）：复调的后台任务体系借鉴了它的「前台阈值 → 超时原地转后台」非阻塞派活模型。`steer`（运行中追加要求）因复调一次性工具循环的架构限制暂未实现，此处如实注明差异。
- **[astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)**（作者 lxfight）：复调与它深度集成（记忆召回、存储、工具过滤、私有路径防腐层 `_lm_bridge`）。
- **[AstrBot](https://github.com/AstrBotDevs/AstrBot)**（AstrBotDevs 团队与全体贡献者）：复调首先是 AstrBot 的插件，感谢框架、官方文档与社区生态。

若清单有所疏漏，或某处借鉴的边界描述不够准确，欢迎联系作者补充修正。

---

*license: MIT · author: hypxtmc · 原名 parallel_handoff，v2.7.1 起以「polyphony · 复调」示人*

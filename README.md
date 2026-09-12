# Polyphony · 复调

> 多声部并行，一曲收拢。
> 主代理执棒，子代理各为声部——各唱各的旋律，最终汇成同一首曲子。

**AstrBot 多子代理并行调度插件**（原名 `parallel_handoff`）

| 信息 | 值 |
|------|-----|
| 版本 | 2.10 |
| 作者 | hypxtmc |
| 许可 | MIT |
| 要求 | AstrBot ≥ 4.26.0 |
| 工具名 | `parallel_handoff` / `call_subagent` / `task_status` / `task_result` / `task_stop` |

---

## 你的子代理是不是只会排队说话

你已经配了好几个子代理。性格各异，设定写得很细，你也清楚谁适合聊什么。

但她们**永远是一个一个来**：

- 想让两个人在同一件事上各自表态，你得手打两遍
- 想让 A 说完 B 接着往下做，得自己把 A 的输出复制过去
- 想让她们之间也有来往、有自己的小日子——插件翻遍了，没有

问题不在人格设定，在于**调度层是空的**。复调补的就是这一层。

| 你想做的事 | 没有复调 | 有了复调 |
|---|---|---|
| 一件事听三个人的看法 | 挨个问三遍 | 一次调用，三段同时到 |
| 让 A 的结论接着给 B 用 | 手动复制粘贴 | `chained` 接龙自动传棒 |
| 让 A 记得三天前聊过什么 | 全靠人格设定硬写 | 独立会话档案 + 跨轮上下文 |
| 让她们之间有自己的来往 | 做不到 | 旁轨日常，随时围坐插话 |

---

## 装上之后是什么样

**一次提问，多个声部同时开口**，各自成段落到聊天窗口：

> **你**：这个方案你们怎么看，各自说说
>
> **【助手A】**：可行。但第二阶段迁移没有回滚路径——我不建议一次上。
>
> **【助手B】**：我只问排期。这周就要看到东西，范围砍一半，你选。
>
> **【助手C】**：（从后面凑过来）那我先把文档框架搭了？反正早晚要写——省得你到时候又熬夜赶。

她们读到的**是同一段上下文**，说出的是**各自的判断**——不是一段话复制三遍，也不是一个模型换三副口吻。每个人都有独立的人格、记忆线和会话档案，跨轮记得自己说过什么。

再往前一步：她们之间可以有**自己的日常**——今天谁跟谁聊过什么、谁心情不好、谁手头压着事。你随时能围坐插话。

---

## 一个真实回合

上面那三句你大概会想“演示而已”。下面这一段是我自己实际跑过的——**流程照实写，名字和内容做了泛化**。

**21:47　你随手扔一件事进去**

> **你**：帮我把上周那份数据核对一遍，顺便看看有没有更省事的写法

这句话里没点任何人的名字。

**21:47　路由层开始工作**

- 关键词命中「核对数据」→ 指向助手A
- 同一句里「更省事的写法」落在助手B 的能力圈
- 路由层不做取舍，两个都进调度指令 → `relay` 模式，两人并行

**21:48　两段回复先后落到窗口**

> **【助手A】**：核对完了。第 3 张表有 14 处日期格式不一致，其余三张没问题，异常清单列在最后。
>
> **【助手B】**：你那三个 `for` 循环可以并成一个，顺便省掉两次全表扫描——改法写在下面，动手前先备份。

**21:52　你补了一句**

> **你**：助手A 的异常清单，助手B 你按这个顺手把脚本改一下

链式接龙在这里启动——助手A 的产出**作为输入**递给助手B。不用你复制粘贴，也不用你再解释一遍“哪个清单”。

**23:10　主对话静下来之后**

你不说话了，旁轨里还在动。这一幕没有任何人呼叫，是她们自己的日常：

> **【助手B】**：（瘫在椅子上）改完了。谁要喝水，我顺路。
>
> **【助手C】**：我要我要——你那杯先给我吧，这两块不收尾我走不开。
>
> **【助手A】**：……小点声。有人在忙。

第二天你打开对话，这些都在。不是凭空生成的一段闲聊——是各自的当日状态、手头事、关系亲疏攒出来的。你可以直接插话：“昨晚那杯水谁倒的”

---

**三样东西合起来才是这套插件**：一句话扔进去自动找对人、产出能顺着往下传、她们之间有自己的日子。

---

## 为什么写这个

我在 AstrBot 里养了几个子代理。时间久了发现一件怪事：她们的人格越来越细，我的用法却一直停在最原始的形态——点一个，说一句，等回答，再点下一个。

差的不是人格，是一层能把她们放到同一张桌子上说话的东西。

复调补的就是这一层。也是我自己每天在用的那一套。

---

## 这插件适合谁

**适合**：你已经在 AstrBot 里配了两个以上子代理，并且希望她们能同时开口、能接龙干活、能互相来往。

**不太适合**：只有一个 bot、单人格的用法——装上不会有变化，先不用装。

---

## 目录

- [你的子代理是不是只会排队说话](#你的子代理是不是只会排队说话)
- [装上之后是什么样](#装上之后是什么样)
- [一个真实回合](#一个真实回合)
- [为什么写这个](#为什么写这个)
- [这插件适合谁](#这插件适合谁)
- [为什么叫复调](#为什么叫复调)
- [特性总览](#特性总览)
- [快速开始](#快速开始)
- [核心概念](#核心概念)
- [调度四式](#调度四式)
- [功能详解](#功能详解)
- [工具一览](#工具一览)
- [命令一览](#命令一览)
- [配置参考](#配置参考)
- [数据与文件布局](#数据与文件布局)
- [零配置运行与词表定制](#零配置运行与词表定制可选进阶)
- [路由规则](#路由规则)
- [架构概览](#架构概览)
- [开发与测试](#开发与测试)
- [版本演进纪要](#版本演进纪要)
- [设计哲学](#设计哲学)
- [FAQ](#faq)
- [致谢与参考](#致谢与参考)

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

## 特性总览

**调度与路由**

- **四种调度**：parallel 齐奏 / chained 接龙 × relay 收谱 / direct 直发，任意组合
- **双模式配置**：`tech`（技术干活）与 `affection`（日常贴贴）两套预设，一个参数切换
- **命令式点名 T0 强锁**：`/名字` 锁定目标并持续生效，主代理忙碌时也不被抢
- **消息消歧**：只传 `message`，自动路由到最近对话中出场的子代理
- **路由强制指令**：LLM 请求前自动计算路由路径并注入执行指令，任务分类随行
- **读空气仲裁**：在场状态机（ConversationPresence）维持多声部秩序【实验性】
- **会话柜台三件套**：`/谁在`、`/复位`、`/列表`

**上下文与记忆**

- **跨轮上下文引擎**：子代理记得跨轮聊过什么，窗口轮数独立可调
- **常驻会话落盘**：每个子代理的对话线独立成档、实时落盘，重启不丢
- **livingmemory 记忆集成**：自动召回相关记忆片段，防止跨人格记忆污染
- **接龙摘要**：chained 长接龙自动摘要传给下一棒，接力棒不失真
- **旁听窗**：主代理能接上子代理直发的内容，会话里自然往返

**生活感**

- **个体状态随机演化**：纯规则状态机，每天谁来接话不固定，日子是活的
- **离线心情注入**：小模型离线读取近期对话，为子代理注入「今日心情 / 手头事」
- **关系档案自动注入**：子代理 system 提示携带彼此的关系档案（稳定层·缓存友好）
- **家庭旁轨**：子代理之间有自己的小日子，攒一屋烟火气【实验性·默认关】

**工程与安全**

- **主代理前缀 + 分段转发**：多声部不串音，长回复自动分段
- **后台任务**：子代理长任务不阻塞总线，并发/时限全可配，结果随时回收
- **子代理工具循环**：子代理可带工具干活——默认**只读档**，写权留在主代理

---

## 快速开始

> ⚠️ **重要：请保持"流式输出"关闭（不要开启）**
>
> 复调的分段转发与 LLM 流式输出不兼容：QQ REST API 没有消息编辑接口，流式与分段重发互相打架，
> 表现为"卡顿后吐一大段、分段错乱、重复"。**复调的分段转发本身就是这条平台物理限制下的替代方案**——
> 想要"逐句蹦出"的效果，请靠复调的分段配置实现，不要开流式。

### 安装

1. 将本插件目录放入 AstrBot 的 `data/plugins/` 下
2. 重启 AstrBot（或控制台热重载插件）
3. 确认 AstrBot 版本 ≥ 4.26.0

> 💡 **可选增强：livingmemory**（不装也能完整运行）
>
> 复调不强制依赖任何外部插件。装了 [livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)，
> 子代理会获得**跨会话长期记忆**（召回 / 存储 / 主动提炼）；**没装则记忆链路自动静默跳过**——
> 零报错、零残留日志，其余全部功能（路由 / 接龙 / 旁轨 / 后台任务）照常可用。

### 最小配置

在 WebUI 插件配置中至少设置：

- **`name_display_map`**：子代理英文 id → 中文名映射，例如 `{"agent_a": "张三", "agent_b": "李四"}`
- 主代理路由规则：把想交给子代理的请求导向 `parallel_handoff` 或 `call_subagent`（推荐覆盖日常/情感类）

其余全部有默认值，直接可用。

### 第一次调用

主代理（或你在支持工具调用的大模型对话中）调用：

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

- **主代理**：执棒者。负责汇总、转述（relay）或放手（direct），也负责所有「写」的操作。
- **子代理**：声部。各自有人格、记忆线、会话档案；默认只带**只读工具 + 网页搜索**，看、查、搜可以，动手改东西不行。
- **直发（direct）**：子代理的回复不经主代理转述，直接以 `【名字】` 前缀发给用户。
- **收谱（relay）**：子代理回复交回主代理，由主代理统一对外发声。
- **场景（scene）**：转发时附带的一句场景说明（如「深夜，书房」），让子代理知道此刻在哪、跟谁说话。
- **在场（presence）**：读空气仲裁记录「谁在场、该谁接话」的状态机基础。

---

## 调度四式

### relay × parallel（指挥收谱 · 复调齐奏）

所有子代理同时开跑，各自把结果交回主代理，主代理汇总后统一回复。适合：多方案对比、并行调研、任务分派后收卷。

### relay × chained（指挥收谱 · 卡农轮唱）

子代理依次接力，前一位的回复作为后一位的上下文传入。适合：流水线（先查资料 → 再写初稿 → 最后审校）、有依赖关系的多步任务。

### direct × parallel（独唱直出 · 复调齐奏）

所有子代理直接对用户说话（分条转发）。适合：群聊氛围、想让每个人自己发声。

### direct × chained（独唱直出 · 卡农轮唱）

子代理依次对用户说话，后面的人听得到前面的人说了什么。适合：日常接龙、多角色对话感。

> **双模式预设**：调用时传 `mode: "tech"`（relay+parallel）或 `mode: "affection"`（direct+chained）即可整体切换策略。
> **配置优先约定**：模式命中时以模式配置为准——显式传参不会覆盖已配置的策略。

---

## 功能详解

> ⚠️ **实验性功能提示**：标注【实验性】的功能未经长期运行验证，行为可能随版本演进调整，请谨慎开启并留意日志。涉及：家庭旁轨（`enable_family_pulse`）、拉用户进旁轨（`family_pulse_draft_enable`）、读空气仲裁（`enable_read_air_arbitrate`）、多人接龙记忆沉淀（`enable_chain_memory_persist`）。

### 命令式点名（T0 强锁）

消息以 `/`、`／`、`#`、`！`、`!`、`、` 打头直接叫名字——`/张三`、`/李四+王五`、`/张三+李四+王五`——即锁定目标并**持续生效**：之后无需重复点名，消息一直交给对应子代理。主代理忙碌时同样有效，正事不会被抢走。

### 消息消歧

不传 `calls` 时可只传 `message`——插件自动路由到**最近对话中出场的子代理**，无需重复点名。接续话题、追问、对话消歧从此一个参数搞定。

### 路由强制指令（OnLLMRequestEvent）

插件根据配置在 LLM 请求前自动计算路由路径并**注入强制指令**：主代理按注入的调度方式执行，工具全保留、不做软硬拦截。任务分类（plan / exec / chat / weak）随指令注入，让路由决策有章可循。

### 主代理前缀 + 分段转发

- 子代理回复经主代理转发时自动带 `【名字】` 前缀，多声部不串音
- 长回复自动分段转发，支持主代理分段注入
- 前缀可按子代理粒度开关（`name_prefix_overrides`）
- QQ 平台自带 markdown 降级（`qq_md_plainify`），裸符号观感修复

### 跨轮子代理上下文引擎（ctx_engine）

子代理在跨轮对话中记得之前聊过什么。历史以结构化 messages 注入（前缀缓存友好），超窗口按 `subagent_context_max_turns` 纯截断——保留最近 N 轮，长对话不爆上下文。

### 常驻会话落盘（session_store）

每个子代理的对话线独立成档、实时落盘（`subagent_sessions/` 目录），重启不丢、跨天不散，保留天数可配。历史长在磁盘上，不在一次性的内存里。

### 后台任务（task_runner）

子代理的长任务不再阻塞总线：后台执行、并发上限、单会话限量、单轮超时全部可配。派完活主代理可以继续说话，任务结束后结果回收——配套 `task_status` / `task_result` / `task_stop` 三个工具。

### 会话柜台三件套

`/谁在` 查锁、`/复位` 放锁回自动分派、`/列表` 看全部可点名成员。整句判定（`/谁在 顺便说个事` 这类带内容的不会被误吞），主代理忙碌时同样可用。

### 读空气仲裁（arbitrate）

在场状态机（ConversationPresence）：判断哪些子代理"在场"、该谁接话。多声部抢话时由仲裁机制维持秩序。二级闸门 `read_air_enforce` 默认关闭（observe-only，行为零变化）；开启后执行「宁静权」真实拦截。

### 智能路由（router，默认关）

`enable_smart_router` 开启后由独立小模型预判该不该转子代理（置信度阈值可调、超时可调）。**当前默认关闭**——主代理自带路由能力足够时无需开启。

### livingmemory 记忆集成

与 livingmemory 插件联动：调用子代理时自动召回相关记忆片段（`recall_enabled`），并为子代理过滤记忆工具，防止跨人格记忆污染。私有路径访问通过防腐层 `_lm_bridge` 隔离。

### 子代理工具循环 + 只读白名单

子代理可带工具干活（受 `subagent_max_steps` / `subagent_tool_call_timeout` 约束），工具循环支持多步调用。

**默认只读档（26 项）**——子代理只能看、查、搜，不能动手：

| 分类 | 工具 |
|------|------|
| 文件读取 | `safe_read` `dir_list` `dir_tree` `es_search` `rg_search` `text_filter` `file_hash` `file_diff` `file_preview` `safe_backups` |
| 代码理解 | `code_explore` `code_status` |
| 知识·网页 | `astr_kb_search` `web_search` `web_fetch` `web_search_tavily` `tavily_extract_web_page` |
| 只读检查 | `syntax_check` `lint_runner` `config_diff` |
| 只读 git | `git_status` `git_diff` `git_log` `git_branch` `git_remote` `git_changelog` |

写/执行类工具（文件编辑、删除、移动、压缩、重命名、测试运行、git 提交等）**全部留在主代理**。白名单可由 `subagent_tools` 调整（留空回落内置默认；兼容旧键 `subagent_readonly_tools`）。

### 关系档案自动注入

子代理的 system 提示里会带上她与家中每个成员的「关系档案」——亲密度、基调、最近互动（数据源与家庭旁轨共用 `relationships.json`）。

- **稳定层注入**：档案放在 system 提示的固定段（人格 → 关系档案 → 检索纪律 → 任务卡），逐字节确定、无时间戳——**跨调用命中前缀缓存，不逐轮打断**；
- **亲密度降序**：近况按亲密度稳定排序，最亲的先说；
- **失败退化**：关系文件缺失/读取失败时自动退化为空段，绝不阻塞对话。

### 接龙摘要（chain_summary）

chained 长接龙自动生成摘要传给下一棒（阈值/保留首尾策略可调），接力棒不失真。

### 个体状态随机演化（random_state）

纯规则状态机：**接话人不能被算死**。每个子代理的日常话题、关注度随机演化，同一个问题今天和明天可能由不同的人接——家是活的，不是状态机。

### 离线心情注入（daily_life）

用 GLM-4-Flash 离线读取近期对话，为每个子代理注入「今日心情 / 手头事 / 话题域」的语义温度。子代理不是每次都被叫醒的应答机，而是有自己一天的角色。

### 旁听窗

子代理直发（direct）的内容会被记录（1000 字窗口），主代理下次开口时系统把最近 10 分钟内子代理说过的话附进上下文——主代理接得上，会话自然往返，像真人 QQ 对话。

### 【实验性】家庭旁轨（family_pulse，默认关）

> 设计初衷：子代理之间有自己的小日子——不围主对话转，彼此搭话、惦记、拌嘴，攒一屋烟火气；用户每天可收到一条「家里动静」摘要。

- **心跳闲聊**：子代理之间自主搭话（作息式自管循环 06:17→次日 01:00、窗内每 2h 随机一场；`family_pulse_window_start/end`、`family_pulse_interval_min` 可调）
- **生活三态**：每件手头事走「起头 → 做到一半 → 收尾」，收尾那轮顺口播报后归档，再开新事——不拖不弃
- **素材池**：私有素材（`thread_flavors.json`）∪ 通用生活池，轮换取用、一圈不重复；首次开启自动生成人格骨架（`personas.json`），照骨架写你自己的家人们
- **全桌关系**：入场每人逐行注入与在场者的关系与基调（`relationships.json`；未收录 =「不太熟，别热络」）
- **情绪摩擦**：心情影响说话方式，反客套规则在场——不寒暄敷衍
- **每日摘要**：定时汇总「家里动静」推送到指定会话（`family_pulse_digest_cron` / `family_pulse_digest_umo`）
- **插话机制**：一定概率闯入正在进行的对话（概率/上限双控）
- **东道主机制**：围坐主持概率控制
- **草稿机制**：工作日/节假日分别配置发言概率与配额
- **幂等与安全**：cron 防重入、默认全关零行为、逐项开关
- 命令入口：`/看看家里`、`/看家里`、`/家里今天` 等

---

## 工具一览

| 工具 | 作用 | 关键参数 |
|------|------|----------|
| `parallel_handoff` | 并行/接龙调用多个子代理 | `calls`（列表）· `mode`（tech/affection）· `route_mode` / `call_mode`（覆盖）· `background`（后台执行） |
| `call_subagent` | 调用单个子代理并转发回复 | `agent_name` · `input` |
| `task_status` | 查后台任务状态 | `task_id`（可选，不传列全部） |
| `task_result` | 取后台任务结果 | `task_id` · `timeout` |
| `task_stop` | 取消后台任务 | `task_id` |

---

## 命令一览

| 命令 | 作用 |
|------|------|
| `/名字`、`/名字+名字2` | 点名锁定，持续生效；前缀兼容 `/` `／` `#` `！` `!` `、` |
| `/谁在` | 看当前锁着谁 |
| `/复位` | 放开锁定，消息回到自动分派 |
| `/列表` | 列出全部可点名成员 |
| `/热重载并行插件`、`/reload_parallel`、`/重载插件` | 热重载本插件 |
| `/（某某）的前缀关了`、`/（某某）的前缀开了` | 按子代理开关姓名前缀 |
| `/看看家里`、`/看家里`、`/家里今天` | 查看家庭旁轨近期动态 |

---

## 配置参考

共 **78** 项，按功能分组速查（完整定义见插件 WebUI 配置面板）：

**核心调度**
`user_address` · `main_agent_name` · `route_mode`（relay/direct）· `call_mode`（parallel/chained）· `tech_mode_config` / `affection_mode_config`（双模式预设）· `handoff_blacklist_agents`（黑名单）· `direct_delivery_agents`（直发白名单）

**前缀与转发**
`enable_subagent_name_prefix` · `enable_mainagent_name_prefix` · `enable_mainagent_segmented` · `enable_segmented_forward` · `min_fragment_length` · `fragment_interval` · `allow_mainagent_after_direct` · `forbid_pre_tool_mainagent_talk` · `mainagent_disable_md_split` · `mainagent_md_split_max_chars` · `mainagent_md_split_progress` · `name_display_map` · `name_prefix_overrides` · `qq_md_plainify`

**路由与指令**
`enable_route_directive` · `subagent_visibility_inject` · `directive_inject_mode` · `enable_smart_router`（默认关）· `enable_disambiguation` · `router_provider_id` · `router_confidence_threshold` · `router_timeout` · `subagent_reply_timeout`

**子代理工具循环**
`subagent_tools`（白名单·默认只读档）· `subagent_readonly_tools`（旧键兼容）· `subagent_max_steps` · `subagent_tool_call_timeout` · `subagent_response_preview_chars`

**跨轮上下文**
`subagent_context_enabled` · `subagent_context_max_turns`

**常驻会话**
`subagent_session_persist` · `subagent_session_retention_days`

**后台任务**
`subagent_task_max_concurrent` · `subagent_task_max_per_session` · `subagent_task_turn_timeout`

**记忆召回**
`recall_enabled` · `recall_default_k` / `recall_max_k` · `exclude_agents`

**场景与生活**
`enable_scene_inject` · `enable_daily_random_life` · `daily_life_provider_id` · `enable_read_air_arbitrate`（实验性）· `read_air_enforce` · `read_air_presence_window` · `chain_summary_enabled` · `chain_summary_model` · `chain_summary_threshold` · `chain_summary_keep_head_tail` · `enable_chain_memory_persist`（实验性）

**家庭旁轨**（实验性，全部默认关）
`enable_family_pulse` · `family_pulse_members` · `family_pulse_cron` · `family_pulse_digest_cron` · `family_pulse_digest_umo` · `family_pulse_memory_umo` · `family_pulse_provider_id` · `family_pulse_recent_hours` · `family_pulse_window_start` / `family_pulse_window_end` · `family_pulse_interval_min` · 插话/东道主概率与上限 · 草稿概率与配额（工作日/节假日分设）

---

## 数据与文件布局

```
astrbot_plugin_parallel_handoff/
├── main.py            # 入口 + 事件注册（工具/命令/装饰器）
├── dispatch.py        # 核心调度：parallel_handoff 主流程、工具循环、关系档案
├── router.py          # 路由层：T0 强锁 / T1 规则 / T2 小模型 / T3 兜底、消歧
├── forward.py         # 分段转发、前缀注入、markdown 降级、直发记录
├── family_pulse.py    # 家庭旁轨（心跳/三态/素材/关系/摘要）
├── memory.py          # livingmemory 集成、子代理工具白名单、时间感知
├── random_state.py    # 个体状态随机演化（M1）
├── daily_life.py      # 离线心情注入（M2）
├── directive.py       # 路由强制指令注入
├── arbitrate.py       # 读空气仲裁（在场状态机）
├── ctx_engine.py      # 跨轮上下文引擎
├── session_store.py   # 常驻会话落盘
├── task_runner.py     # 后台任务
├── _lm_bridge.py      # livingmemory 防腐层
├── config.py          # 配置读取
├── _conf_schema.json  # 配置面板定义（78 项）
└── data/              # 插件自带数据
    ├── display_names.json      # 英文 id → 中文名
    ├── random_state_data.json  # 状态机种子数据
    ├── router_tables.json      # 路由规则表
    └── family_pulse/           # 家庭旁轨数据
        ├── personas.json          # 人格骨架（首开自动生成）
        ├── thread_flavors.json    # 私有素材池
        ├── prompts.json           # 提示词
        ├── threads.json           # 生活三态进度
        └── *.jsonl                # 每日活动日志
```

**运行时数据**（自动创建）：

- `data/plugin_data/astrbot_plugin_parallel_handoff/subagent_sessions/` —— 会话落盘
- `data/relationships/relationships.json` —— 家庭关系网（旁轨与关系档案共用）

---

## 零配置运行与词表定制（可选进阶）

**复调开箱即用，零配置不裸奔。** 以下三层配置全部可选，按需逐层叠加：

### 第一层：自动发现（默认行为，什么都不用配）

安装后无需任何配置：

- **路由池自动发现**——插件从 AstrBot 的 subagent_orchestrator 读取已注册的子代理清单，自动生成路由目标池（T0 命令点名 / T1 规则 / T2 判向开箱可用）
- **路由指令自动注入**——主代理请求自动携带完整路由规范（用什么工具、什么模式、怎么调度），不依赖人格设定里写任何内容
- **显示名自动回退**——未配置中文名时直接使用英文 id

### 第二层：词表定制（`data/` 数据文件，子代理较多时建议配置）

**`data/display_names.json`** —— 英文 id → 中文显示名：

```json
{
  "agent_a": "张三",
  "agent_b": "李四"
}
```

**`data/router_tables.json`** —— 路由规则表（全字段可选，缺省即安全降级）：

| 字段 | 作用 | 缺省行为 |
|---|---|---|
| `main_token` / `main_token_set` | 主代理专属入口词（可多写法） | 仅通用词 `/主代理`、`/主agent` 生效 |
| `aliases` | 子代理爱称映射 | 爱称不触发，全名与命令正常 |
| `keywords` | 领域关键词 → 子代理 | 领域词不触发，命令与点名正常 |
| `t2_brief` | 子代理职责简介（判向参考） | 自动发现兜底（用子代理公开描述） |

示例：

```json
{
  "main_token_set": [],
  "aliases": {"阿张": "agent_a", "老四": "agent_b"},
  "keywords": {"做饭": "agent_a", "写画": "agent_b"},
  "t2_brief": {"agent_a": "日常照顾、做饭", "agent_b": "画画、创作"}
}
```

### 第三层：主代理人格配合（可选，进阶）

插件自身已注入路由规范，**不写人格也能工作**。若想让主代理更主动地路由，可在主代理人格设定末尾追加：

```markdown
## 路由规则

收到消息先判断意图，决定自己回复或路由给子代理：
- 请求明显属于某子代理的职责 → 用 parallel_handoff 工具路由（插件会注入执行规范）
- 用户点名某子代理（"让XX看看"或 /XX 命令）→ 路由给对应子代理
- 自己就能答的（闲聊、常识）→ 直接回复，不必路由
```

> 三层是叠加关系：不配 → 自动发现保底；配词表 → 命名与领域词更精准；配人格 → 主代理路由意识更强。

---

## 架构概览

| 模块 | 规模 | 职责 |
|------|------|------|
| `main.py` | 403 行 | 插件入口、事件注册（`@llm_tool` / `@filter.command` / 装饰器壳方法） |
| `dispatch.py` | 1545 行 | 核心调度主流程、去重守卫、工具循环、关系档案注入 |
| `router.py` | 1642 行 | 四层判向（T0 强锁 → T1 规则 → T2 小模型 → T3 兜底）、消歧、场景判定 |
| `forward.py` | 1155 行 | 分段转发、主代理前缀、markdown 降级、旁听窗记录 |
| `family_pulse.py` | 1651 行 | 家庭旁轨全套（心跳/三态/素材/骨架/关系/摘要/插话/草稿） |
| `memory.py` | 648 行 | livingmemory 集成、工具白名单、时间感知与状态注入 |
| `random_state.py` | 428 行 | 个体状态随机演化（M1 纯规则状态机） |
| `directive.py` | 344 行 | 路由强制指令构建与注入 |
| `arbitrate.py` | 320 行 | 读空气仲裁（ConversationPresence 状态机） |
| `task_runner.py` | 254 行 | 后台任务执行（并发/超时/回收） |
| `session_store.py` | 225 行 | 会话落盘（JSONL，磁盘为真源） |
| `_lm_bridge.py` | 225 行 | livingmemory 私有路径防腐隔离 |
| `config.py` | 206 行 | 配置读取与默认值 |
| `daily_life.py` | 174 行 | 离线心情注入（M2） |
| `ctx_engine.py` | 116 行 | 跨轮上下文：历史存储 / 窗口截断 / 上限裁剪 |

**数据流（一次 direct × parallel 调用）**：

```
用户消息
  → router：T0 强锁？/ T1 规则命中？/ T2 小模型？/ T3 兜底主代理
  → dispatch：构建 calls、并发派发子代理
      → 每声部：system（人格 + 关系档案 + 纪律 + 任务卡）
                + 上下文（ctx_engine / session_store）
                + 工具循环（只读白名单）
  → forward：前缀注入 → 分段转发 → 旁听窗记录
  → 用户看到多声部发言
```

---

## 开发与测试

```bash
# 全量测试（需要 AstrBot 的 venv 环境）
python3 test_plugin.py

# 或使用 AstrBot 自带的解释器（路径按你的部署调整）
<astrbot>/venv/bin/python3 test_plugin.py
```

- 测试文件：`test_plugin.py`（主套件）、`test_lm_bridge.py`、`test_task_runner.py`、`test_session_store.py`、`test_task_integration.py`
- 修改插件代码后：先 `python3 -m py_compile` 自查，再热重载插件，用 `plugin_list` + 日志验证
- 插件级热重载：命令 `/热重载并行插件` 或控制台插件管理
- 提交前建议全量测试零回归

---

## 版本演进纪要

- **v2.0–v2.3**：核心调度（parallel/chained × relay/direct）、统一场景注入、姓名前缀、分段转发、接龙摘要、热重载命令
- **v2.4**：P0 拆模块重构——dispatch / forward / directive / arbitrate / ctx_engine / memory 独立成模块，分层清晰
- **v2.5**：路由强制指令注入（OnLLMRequestEvent）+ 读空气仲裁状态机 + 智能路由（默认关）
- **v2.6**：三期重版——random_state 个体状态种子（M1 纯规则随机演化）+ daily_life 离线心情注入（M2）
- **v2.7.1**：family_pulse 家庭旁轨 v0（M5）+ 消息消歧（message 参数）+ 双模式配置（tech/affection）+ 配置优先约定
- **v2.8**：livingmemory 私有路径防腐层 `_lm_bridge`
- **v2.9**：命令式点名 T0 强锁（多前缀、粘滞、忙碌可用）→ v2.9.1 强锁治本与夺锁仲裁 → v2.9.2 子代理接入工具循环 + 只读工具白名单
- **v2.10**：常驻会话落盘（session_store）+ 后台任务（task_runner）+ 会话柜台三件套（/谁在 /复位 /列表）+ token 计量口径
- **v2.10 系列（最新）**：家庭旁轨四缺陷修复（全桌关系矩阵 / 情绪摩擦 / 骨架引导 / 事件三态机）· 关系档案自动注入迁入 system 稳定层（缓存友好）· 子代理工具收权为只读档（26 项）· 旁听窗（主代理↔子代理自然对话）

---

## 设计哲学

复调的对位法有一条铁律：**每个声部独立成立，对位才成立**。子代理不是主代理的分身，不是应答机，也不是轮流播报的喇叭——各有旋律线，彼此听见，偶尔抢拍，但汇成的是同一首曲子。这个插件只做一件事：把舞台搭好，然后守住对位。

---

## FAQ

**Q：主代理和子代理有什么区别？**
A：主代理是执棒者——负责调度、汇总、对外发声，并且**独占所有写权限**（改文件、跑命令、提交代码）。子代理是各声部——有人格、有记忆线、有自己的会话档案，能看、能查、能搜，不能动手写。

**Q：为什么子代理默认没有写权限？**
A：安全第一。子代理是多声部并行执行的，写操作容易互相踩踏；写权集中在主代理，责任链清晰。确实需要时可通过 `subagent_tools` 白名单单项授权。

**Q：默认为什么是 `direct` 路由？**
A：日常陪伴场景下，子代理直接对用户说话更自然（各唱各的，用户直接听到多个声部）。技术干活场景调用时传 `mode: "tech"` 即可切换为 relay 收谱。

**Q：家庭旁轨开了没反应？**
A：检查三处：① `enable_family_pulse` 是否开启；② `family_pulse_members` 是否填了成员；③ 当前时间是否在活跃窗口（默认 06:17 → 次日 01:00）内。首次开启会自动生成人格骨架，照骨架写你自己的家人们。

**Q：改了代码怎么生效？**
A：插件级热重载即可（命令 `/热重载并行插件`）。全局配置变更需重启 AstrBot。

**Q：会消耗很多 token 吗？**
A：调度本身开销很小；主要消耗在各子代理的对话与工具循环上。关系档案放在 system 稳定层、内容逐字节确定——**跨调用命中前缀缓存**，不会逐轮重复计费。

**Q：子代理能互相聊天吗？**
A：可以——家庭旁轨（实验性）就是为此设计的：心跳闲聊、生活三态、全桌关系。主代理也可以通过 chained 接龙让子代理接力对话。

**Q：怎么做多人群聊氛围？**
A：`direct` 路由 + `parallel` 调用——多个子代理同时直发，各有前缀不串音；配合 `affection` 模式预设食用更佳。

**Q：开了流式输出后分段失效/消息乱序？**
A：请关闭流式输出——已知限制。流式与复调的分段转发在 QQ REST API 上物理冲突（没有消息编辑接口），表现为卡顿后吐一大段、分段错乱、重复。复调的分段方案就是为绕开这条限制而生的替代路线；保持流式关闭即可获得最佳效果。

**Q：没装 livingmemory 插件会怎样？**
A：什么都不用做——记忆功能会自动优雅降级：子代理不召回/不存储长期记忆（对话功能完好，只是每轮都“新认识”），其余全部功能正常。装上 livingmemory 后自动启用，无需任何配置。

---

## 致谢与参考

复调不是凭空长出来的——它的好几处设计，站在了社区前辈的肩膀上。遵照 [AstrBot 官方插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html) 的要求（借鉴其他项目的设计、功能创意或实现方式时，应在 README 中明确致谢来源并链接相关项目），在此逐一鸣谢：

- **[astrbot_plugin_custome_segment_reply](https://github.com/LinJohn8/astrbot_plugin_custome_segment_reply)**（作者：**LinJohn8**）——复调的**流式守卫**源自该插件的「**回放不抢发**」思想：流式通道已逐 token 发出文本时，分段转发必须主动让位，否则必然重复/乱序。这份"知道什么时候不该说话"的经验，是复调流式稳定性的基石。
- **[astrbot_plugin_maid_agent · 代理女仆](https://github.com/Kalospacer/astrbot_plugin_maid_agent)**（作者：**Kalo（@Kalospacer）**）——复调的**后台任务体系**（task_runner）借鉴了它的「**前台阈值 → 超时原地转后台**」非阻塞派活模型。`steer`（运行中追加要求）因复调一次性工具循环的架构限制暂未实现，此处刻意保留差异、如实注明。
- **[astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)**（作者：**lxfight**）——复调与 livingmemory 深度集成（记忆召回 / 存储 / 工具过滤 / 私有路径防腐层 `_lm_bridge`）。感谢 lxfight 提供了稳定可靠的记忆底座，让子代理记得住来路。
- **[AstrBot](https://github.com/AstrBotDevs/AstrBot)**（AstrBotDevs 团队与全体贡献者）——复调首先是 AstrBot 的插件。感谢框架、官方文档与社区生态，让这一切得以成立。

若以上清单有所疏漏，或某处借鉴的边界描述不够准确，欢迎联系作者补充与修正——**尊重每一位开源作者的劳动，是复调对位法的一部分**。

---

*license: MIT · author: hypxtmc · 原名 parallel_handoff，v2.7.1 起以「polyphony · 复调」示人*

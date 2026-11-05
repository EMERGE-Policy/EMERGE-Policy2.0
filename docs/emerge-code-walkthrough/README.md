# EMERGE-Policy 2.0 代码讲解（阶段 0–8）

代码：`/data/yuqingchi/Code/EMERGE-Policy2.0/`。位置一律相对仓库根（`emerge/...:行号`），行号按 2026-10-30 的代码。阶段 9（application、cli、tui）没有单独成篇，见第 4 节。2026-11-04 改成"一个对话一个文件夹"（1.4）：04 整篇、07 的会话与记忆与工具、本文涉及的地方按改后的代码重写，其余引用的行号随改动平移过。

## 怎么读

| 文件 | 内容 | 阶段 |
|---|---|---|
| 本文 | 全貌、依赖方向、全局规则落在哪、跨模块流程、核实过的疑点 | — |
| [parts/01-foundation-models.md](parts/01-foundation-models.md) | 配置、异常、事件、callbacks、模型层（三种 API 模式对照、流式与降级、断线判断、重试） | 0–2 |
| [parts/02-agent-core.md](parts/02-agent-core.md) | Agent 通用模块：消息历史、`@tool`、middleware、预算、上下文与压缩、循环、skills | 3 |
| [parts/03-graph.md](parts/03-graph.md) | 图：节点与 ctx 协议、端口、启动边与 `start_status`、注册、校验错误码、patch、替换、序列化、图文件（含示例文件 50 多种编辑的实测） | 4 |
| [parts/04-storage.md](parts/04-storage.md) | SQLite 表、接口、事务与取号、记忆检索、删除会话、产物路径 | 5 |
| [parts/05-runtime-core.md](parts/05-runtime-core.md) | 生命周期状态机、节点进程、消息总线与盖章、节点内 ctx、supervisor | 6 |
| [parts/06-runtime-service.md](parts/06-runtime-service.md) | RuntimeService、运行状态、改图提交、关闭与接着跑、反馈、stream | 7 |
| [parts/07-main-agent.md](parts/07-main-agent.md) | 主 Agent：一轮的流程、inbox、过期校验、控制通道、节点提问分叉、会话、记忆、20 个工具 | 8 |

建议顺序：本文 → 03 → 05 → 06 → 07；01、02、04 按需查。各部分末尾有"疑点"一节，本文第 3 节只收我对照代码核实过的。

## 1. 全貌

### 1.1 进程与组件

```text
主进程（一个 asyncio 事件循环）
  用户 ──submit / control──▶ MainAgent ──20 个工具──▶ RuntimeService ──▶ RunHandle（每个运行一个）
    │                         │  ▲                                          │
    │                   AgentLoop  inbox ◀── on_feedback ◀── FeedbackHandler │
    │                         │                                  ▲           ▼
    │                   ModelClient ──model.*──┐                 │      Supervisor ──▶ MessageBus
    │                                          ▼                 │                     │ ▲
    └──────── 事件 ─────────────────▶ CallbackManager ───────────┘                     │ │ 每个节点进程
                                      ├─ JsonlTracer → <对话>/runs/<run>/trace.jsonl   │ │ 一条 Pipe
                                      ├─ FeedbackHandler（推给主 Agent）               │ │ + 两端各一对
                                      └─ StreamHandler（外部订阅，阶段 9 的 TUI）      │ │ 读写线程
  Storage（SQLite <对话>/.emerge/state.db）◀── Session / bus / mutations 写入          ▼ │
                                                                                       │ │
节点子进程（每个节点实例一个，spawn）                                                  │ │
  node_process_main：加载类 → setup → READY → run(ctx) → teardown → EXIT 报告 ◀────────┘ │
  RuntimeNodeContext（ctx）── publish / report / ask / emit / snapshot / fetch ──────────┘
```

- 图中 `<对话>` 指对话文件夹 `<工作区>/sessions/<session_id>/`：每个对话一个库，trace、运行目录也都在里面，见 1.4。
- 节点之间不直接通信：所有消息经主进程的 bus 中转、盖章、写库，再推给订阅方。
- 改图只有一个入口：主 Agent 编辑 `runs/<run_id>/graph.json`，调用 `commit_graph`；节点的 ctx 没有任何改图接口。
- 主 Agent 用的事件发射器默认就是 Runtime 的 `CallbackManager`，所以 trace 文件里既有节点事件，也有主 Agent 的 `model.*`、`tool.*`、`agent.*`。

### 1.2 依赖方向（按 import 统计）

| 包 | 依赖 | 说明 |
|---|---|---|
| `events`、`exceptions` | 无 | |
| `config` | exceptions | |
| `callbacks` | events | |
| `models` | config、callbacks、events、exceptions | |
| `graph` | exceptions、events、config；`models` 只在 `TYPE_CHECKING` 下 | 图层不依赖执行 |
| `storage` | graph、models、exceptions | |
| `runtime` | graph、storage、callbacks、events、config、models、exceptions | |
| `agent` 通用模块（messages、tool、middleware、state、context、loop、skills） | models、callbacks、events、exceptions | 不依赖 runtime、storage、graph，也不依赖 inbox、control、session，Agent 节点可单独复用 |
| `agent` 主 Agent（agent、session、inbox、control、memory、tools） | 以上全部 | `agent/__init__.py` 延迟导入这些名字，导入通用模块时不会拖进 runtime |

### 1.3 设计规则落在代码哪里

| 规则 | 代码怎么保证 | 位置 |
|---|---|---|
| 图只往下续写，已结束的节点连同结局永远留在图里 | patch 对已结束节点的删除、改参数、改描述、改输入、改启动边、替换一律拒绝 | `emerge/graph/patch.py`（03「各种 op」的矩阵） |
| 每个节点最多启动一次，运行中不自动重启 | supervisor 只对当前实例为 `pending` 的节点 `_launch`；代码里没有重启路径。要重做，只能由主 Agent 替换成新实例或新加节点 | `emerge/runtime/supervisor.py:751-770` |
| 启动边只认节点自己返回的、已声明的结局 | 结束状态由主进程定：发过停止 → `stopped`；返回已声明的 `Outcome` → `finished`；其余 → `failed`。只有 `finished` 带结局，`start_status` 对没有结局的来源一律判"不满足" | `emerge/runtime/supervisor.py:850-874`；`emerge/graph/graph.py:375-377` |
| 被要求停止过的实例一律记 `stopped` | `_final_state` 先看 `process.stop_requested`，即使 `run()` 返回了结局也只写日志 | `emerge/runtime/supervisor.py:854-866` |
| 启动边不能成环，订阅可以成环 | 校验错误码 `START_CYCLE`；订阅不查环 | `emerge/graph/graph.py`、`validation.py`（03） |
| 全图一条时间线，只由 Runtime 盖章 | bus 是唯一的盖章者：在 `_timeline` 锁内先向存储取号（`counters` 表的 `timeline:<run>`），再写记录。取号和写记录是两个事务（用户 10-28 同意），只会出现空号，不会重号 | `emerge/runtime/bus.py:171,251-319,382-458` |
| 订阅方读最新值 | 库里每个输出端口保留最近 `port_buffer` 条；节点进程里每路输入同样保留这么多条 | `emerge/storage/sqlite.py:724-738`；`emerge/runtime/context.py` |
| 改图只碰 diff 里出现的节点 | `apply_patch` 只处理 diff 的删除、替换、新增，以及来源变了的输入；运行中、没被点名的节点保留进程和实例 | `emerge/runtime/supervisor.py:437-484` |
| 替换出的新实例从头跑，不接旧实例的工作 | 新实例 id 为 `<id>@v<图版本>`；旧实例成为陈旧实例，它发来的消息被丢弃，带请求的消息回"已停止" | `emerge/runtime/bus.py:369-373` |
| 用户发了新消息后，旧轮次的改图类调用要重新判断 | 用户消息和成功的控制命令让意图计数 +1；`StaleCallGuard` 拦下意图已变的受管调用（提交图、改 graph.json、启停运行与节点、发消息给节点） | `emerge/agent/agent.py:100-124` |
| 暂停、停止不经过模型 | `Controller` 直接调 `RuntimeService`，事后在 inbox 留一条记录给模型看 | `emerge/agent/control.py:103-125` |
| 请求前缀稳定 | system 快照存在库里，打开会话时原样使用；工具列表在 `attach` 时固定；运行信息作为 user 消息追加，不改 system；只有压缩会重建 system | `emerge/agent/agent.py:283-307`；`emerge/agent/context.py` |
| 存储只为"退出后打开接着跑"服务 | 关闭时 `shutdown(closing=True)` 不写任何状态；打开时已结束的保留、正在跑的换新实例重跑、`pending` 照常等、没回答的提问作废 | `emerge/runtime/checkpoint.py`；`supervisor.py:212-325` |
| `acts_on_environment` 只是标记 | 只进事件和报告；状态规则、重开时是否重跑都和其他节点相同 | `emerge/runtime/supervisor.py:850-852` |

### 1.4 工作区与对话文件夹（2026-11-04）

一个对话的全部东西放在它自己的文件夹里，工作区只放所有对话共用的设置：

```text
<工作区>/
  config.json  AGENTS.md  USER.md  skills/      设置
  sessions/<session_id>/                         一个对话
    .emerge/state.db                             它的库：会话、消息、运行、节点状态、端口消息、提问、记忆索引
    trace.jsonl                                  它在运行之外的事件
    memory/MEMORY.md、memory/asset/              它的记忆（只有这一份）
    code/<节点名>/v<版本>.py                     它生成的节点代码
    runs/<run_id>/                               trace.jsonl、graph.json、nodes/<节点>/<实例>/...
```

| 谁 | 怎么做到 | 位置 |
|---|---|---|
| 应用 | 每打开一个对话，就在它的文件夹上依次建存储、注册表（`config.nodes` 的模块 + 这个对话 `code/` 下的全部版本）、`RuntimeService`、`MainAgent`，再 `Session.create/open`；切换或退出时按相反顺序拆掉。要切到的对话读不出来时，先报错，当前对话不动 | `emerge/application.py:159-245` |
| 存储 | 一个库只装一个对话，接口不带会话 id；列出对话是只读地扫 `sessions/` 下的文件夹，删除对话是删整个文件夹（04「列出与删除对话」） | `emerge/storage/sqlite.py`；`emerge/agent/session.py:222-241` |
| Runtime | 运行目录、trace、节点产物都在对话文件夹下；节点进程拿到的 `ctx.session_dir` 就是它 | `emerge/runtime/service.py:122`；`emerge/runtime/context.py:128-132` |
| 主 Agent | 文件工具以对话文件夹为根，碰不到工作区的设置和别的对话；记忆、生成代码都写进对话文件夹；模型请求里的相对图片路径也按它读 | `emerge/agent/tools/files.py:31-42`；`emerge/agent/agent.py:196-222` |

## 2. 跨模块流程

每步只写到"哪个函数做了什么"，细节见对应部分。

### 2.1 用户发消息 → 主 Agent 一轮（细节：07「一条用户消息的处理流程」）

| 步 | 做什么 | 位置 |
|---|---|---|
| 1 | `MainAgent.submit`：先写 `pending_inputs`（立即 await），再放进 inbox，意图 +1 | `agent/agent.py:229-235` |
| 2 | `_serve` 睡在 `inbox.wait()`，醒来取走全部待处理项，一起开新一轮 | `agent/agent.py:404-414`；`agent/loop.py:88-102` |
| 3 | `_run_turn` 把 `AgentLoop.run_turn` 放进单独的 task（这样能被中断），会话状态写 `turn_open` | `agent/agent.py:416-427` |
| 4 | 每次调用模型前，`_at_boundary` 把新到的消息并进当前轮；若某条回复的工具调用还没全部有结果，不插入 | `agent/agent.py:449-460` |
| 5 | 运行信息（运行摘要 + graph.json 状态）有变化才作为 user 消息追加 | `agent/agent.py:531-553` |
| 6 | 模型调用经 middleware：`TurnControl` → `StaleCallGuard` → `PromptCacheKey` → `CompactionMiddleware`（超阈值时先压缩） | `agent/agent.py:328-347` |
| 7 | 回复里的工具调用按顺序逐个执行；受管调用若意图已变，直接返回"not executed"，不执行 | `agent/loop.py:182-196`；`agent/agent.py:120-124` |
| 8 | 回复不再含工具调用时本轮结束；会话写回 `idle`，主 Agent 回到睡眠 | `agent/agent.py:411-431` |

History 每变一次，`Session` 把对应的写库排进一个后台队列，一条消息一行（`agent/session.py:139-207`）。

### 2.2 改图：编辑 graph.json → commit_graph → 节点启停

| 步 | 做什么 | 位置 |
|---|---|---|
| 1 | 主 Agent 用 `read_file` / `edit_file`（精确替换）/ `write_file` 改 `runs/<run_id>/graph.json`（路径相对对话文件夹）。这个文件只是草稿，运行中的图在 Storage 和 supervisor 里 | `agent/tools/files.py` |
| 2 | `commit_graph`：同一份被拒过的文件原样再交 → 直接拒绝；先 dry run，新增或替换的节点 `description` 为空 → 拒绝；然后真提交 | `agent/tools/graph.py:37-82` |
| 3 | `mutations.commit_graph_file`（持有运行的 `commit_lock`）：读文件 → 三步格式检查 → `patch_from_graph_spec`（文件里的 `graph_version` 过期 → 拒绝，不合并）→ 文件与当前图一致 → 不提交 | `runtime/mutations.py:320-357` |
| 4 | `supervisor.apply_patch`（持有 supervisor 的 `_ops` 锁）：用此刻的节点状态 `apply_graph_patch` 并整体校验，任一处不合法 → 整份不生效；通过 → `on_commit` 存图、发 `graph.patched` | `runtime/supervisor.py:437-458`；`runtime/mutations.py:169-185` |
| 5 | 换上新图；在任何 await 之前先通知来源变了的输入（换线或关闭）；删除的节点记 `skipped`；替换的节点：旧进程还在就请求停止，新实例等旧进程退出后再启动（提交不等）；新增的节点登记为 `pending` | `runtime/supervisor.py:459-475,663-723` |
| 6 | `_advance`：按 `start_status` 启动 READY 的节点，把 NEVER 的记 `skipped` | `runtime/supervisor.py:751-770` |
| 7 | 提交成功后用新版本重写 graph.json；返回 `GraphCommitResult`（diff、每个未启动节点的去向、影响说明），`describe()` 的文字就是工具结果 | `runtime/mutations.py:202-215,354-357` |

"在文件里做某种编辑会得到什么"的完整对照见 03「在文件里做某种编辑会得到什么」，"图的变化 → 运行时动作"见 06 同名表。

### 2.3 节点运行：启动 → 收发 → 结束 → 下游启动

| 步 | 做什么 | 位置 |
|---|---|---|
| 1 | `_launch`：spawn 子进程，传 `NodeLaunch`（类型定位、参数、各输入的来源、是否以暂停状态启动）；`bus.attach` 交出排队给它的主 Agent 消息；状态记 `starting` | `runtime/supervisor.py:772-807` |
| 2 | 子进程：加载节点类（生成代码先核对文件摘要）→ `setup` → 发 READY（主进程记 `running`）→ `run(ctx)` | `runtime/process.py:157-201` |
| 3 | `publish_output_message`：bus 先做陈旧检查，再在 `_timeline` 锁内取号、分配端口版本、写 `port_messages`，回复盖好章的 `Message`，推给当前订阅方的进程，发 `port.published` | `runtime/bus.py:382-414` |
| 4 | 订阅方在 ctx 里读：`get_latest_input_message`、`wait_for_input_message`（可带条件）、`wait_for_input_messages`（多路汇合）、`iter_input_messages`；来源结束 → 输入关闭 → `InputClosed` | `runtime/context.py:190-287` |
| 5 | `report_to_main_agent` → 盖章、发 `node.report` → FeedbackHandler 按窗口合并推送 | `runtime/bus.py:416-426` |
| 6 | 节点在循环里调用 `wait_for_run_permission()`：暂停时在这里阻塞，被要求停止后返回 `False` | `runtime/context.py:358-363` |
| 7 | `run()` 返回 → teardown → EXIT 报告 → `_final_state` 定状态 → 写库、发 `node.state` → 作废它未回答的提问 → 关闭它供给的输入 → `_advance` | `runtime/supervisor.py:837-848,876-885` |
| 8 | 所有节点的当前实例都结束、进程都退出 → `run.idle` → 立即推 `graph_idle` 给主 Agent。运行不会自己结束，只有 `end_run` 或停止整个运行 | `runtime/supervisor.py:887-903`；`runtime/run.py:135-140` |

### 2.4 Runtime 通知 → 主 Agent 被唤醒

| 事件 | 推给主 Agent 的方式 | 到了主 Agent 以后 |
|---|---|---|
| 节点结束（finished / failed / stopped）、跳过、图空闲 | 立即推；推之前先把窗口里攒的批次发出去 | 进 inbox（notice），唤醒或在下个边界合并 |
| 节点非结束的状态变化、节点汇报 | 1 秒窗口内按（节点，种类）合并成一条 `batch` | 同上 |
| 节点提问 | 立即推 `ask` | 不进 inbox，另开一个分叉回答（2.5） |
| 端口新消息、`node.custom`、`graph.patched`、`run.status` | 不推 | 只进运行摘要，或者都不进；主 Agent 需要时用工具查 |

位置：`emerge/runtime/feedback.py:139-198`；`emerge/agent/agent.py:250-259`。所以每次提交或重开会话、有节点开始跑时，主 Agent 都会被唤醒一次。

### 2.5 节点提问 → 分叉回答

| 步 | 做什么 | 位置 |
|---|---|---|
| 1 | 节点 `ask_main_agent(question)` → bus 盖章、`add_ask` 入库、发 `node.ask`；节点阻塞，等 reply | `runtime/bus.py:446-458` |
| 2 | FeedbackHandler 立即推 `ask`；`on_feedback` 为它开一个独立 task，不打断当前轮 | `agent/agent.py:254-257` |
| 3 | `fork_request`：用与主请求相同的 system、工具定义和历史（去掉还没拿到全部结果的最后一条回复），末尾加上问题，`tool_choice="none"` | `agent/agent.py:489-498,587-596` |
| 4 | 模型回答 → `runtime.answer` → bus 盖章、写库、回复节点。模型出错时，回一句"按你自己的判断继续" | `agent/agent.py:470-480`；`runtime/bus.py:301-319` |
| 5 | 问答记录作为 qa 项进 inbox（不计意图），主 Agent 之后能看到自己答过什么 | `agent/agent.py:481-486` |

不会转给用户。会话关闭时分叉被取消，提问在重开时作废。

### 2.6 用户控制命令

| 命令 | 走的路 | 结果 |
|---|---|---|
| `interrupt` | `TurnControl.interrupt`：在等模型 → 立即取消；在执行工具 → 等工具做完，下个边界结束 | 整轮丢弃（History 截断，库里标 discarded），不计意图 |
| `pause` / `resume` / `stop`（带节点 id） | `Controller` → `RuntimeService` → supervisor | 成功后 inbox 留一条 control 记录，意图 +1，旧的受管调用随之被拒 |
| `pause` / `resume`（不带节点 id） | `pause_all` + `runs.paused`；之后启动的节点也以暂停状态启动 | 同上 |
| `stop`（不带节点 id） | 停下全部节点，运行以 `stopped` 结束 | 同上 |

位置：`emerge/agent/control.py:69-125`。注意未知 action 会落进 stop 分支（第 3 节 A6）。

### 2.7 关闭会话 → 打开接着跑

| 时机 | 主 Agent 层 | 运行层 |
|---|---|---|
| 关闭 `Session.close` | `detach`：正在输出的回复取消、不写；正在执行的工具做完，结果照写；同一回复里还没执行的调用补 `not executed: session closed`；分叉被取消；会话保持 `turn_open` | `close_run` → `shutdown(closing=True)`：进程都停下（超时强杀），但不写任何状态、不触发启动边、不关闭输入；运行记 `closed`，`runs.paused` 保留 |
| 打开 `Session.open` | 从 `messages` 表重建 History；照用存下的 system 快照；`turn_open` 的会话在同一轮里接着做，末尾追加"会话重新打开"和恢复报告；`pending_inputs` 放回 inbox | 每个 `closed` 的运行 `resume_run`：已结束的节点保留；`starting`/`running`/`paused` 的用一个 `replace_node` patch 换成新实例重跑（作用于环境的节点也重跑）；`stopping` 的记 `stopped`；`pending` 的照常等；没回答的提问全部作废；时间线编号和端口版本号接着往下编 |

位置：`emerge/agent/session.py:95-135`；`emerge/agent/agent.py:337-382`；`emerge/runtime/checkpoint.py:111-177`；`emerge/runtime/supervisor.py:212-325`。各状态的处理见 06「关闭时节点状态 → 打开后」。

## 3. 核实过的疑点

只收我对照代码确认过的。"确认"列：代码 = 我读代码确认；实验 = 子任务在临时环境跑脚本复现，我核对过相关代码。各部分末尾还有更多边角条目。

### A. 影响功能或设计核心

| # | 问题 | 后果 | 位置 | 确认 |
|---|---|---|---|---|
| A1 | `wait_for_run_permission()` 没暂停时直接返回 `True`，不让出事件循环；而暂停、停止指令要等事件循环运行 `receive` 才生效 | 循环体只做同步工作（比如阻塞的 IK 调用、`time.sleep`）、只靠这个调用当检查点的节点，永远收不到暂停和停止：`stop_timeout` 后被强杀，teardown 不跑。在开头加 `await asyncio.sleep(0)` 即可 | `emerge/runtime/context.py:358-362`；`emerge/runtime/bus.py:136` | 代码 + 实验 |
| A2 | 重试只按 HTTP 状态码判断。Anthropic 在流中途用 `event: error` 发来的错误（如 `overloaded_error`），SDK 按原响应的 200 构造异常 | Claude 负载高时常见的过载错误不重试，一次失败就报 `ModelError` | `emerge/models/retry.py:60-64` | 代码 + 实验 |
| A3 | 图片在消息里只存路径，每次构造请求时才读文件 | 历史里引用过的图片一旦被删或移走，之后每次请求都失败，直到压缩把那一轮去掉。记忆刷新会删掉 MEMORY.md 不再引用的图片（`read_file` 读过的记忆图片就在历史里）；节点覆盖或删除自己的产物也会触发 | `emerge/models/base.py:180-189`；`emerge/agent/memory.py:148-152` | 代码 + 实验 |
| A4 | 一轮开始时记下的 `turn_index` 是绝对下标；同一轮里压缩把 ≥2 轮并成 1 轮后，轮数变少 | 这时中断或取消，`truncate_to_turn_count(turn_index)` 什么都不删：被取消的半轮留在 History 和库里，可能留下没有结果的 tool_call，之后每次请求都带着它 | `emerge/agent/loop.py:93,136,142`；`emerge/agent/messages.py:77-82` | 代码 + 实验 |
| A5 | 记忆检索切词的正则 `[CJK]+\|[^\s]+` 会把紧跟在字母、数字、标点后面的汉字并进同一个词，而索引里汉字是逐字分开的 | "UR5机械臂"、"v2发布"、"偏好，颜色"查不到；查询里有单独的符号（"偏好。"）时整个查询落空 | `emerge/storage/sqlite.py:69,1034-1045` | 代码 + 实验 |
| A6 | 控制通道的 `else` 分支接住 interrupt、pause、resume 以外的任何字符串，按 stop 执行；`ControlAction` 只是类型注解 | 阶段 9 接 TUI 时，一个拼错的命令（如 `"Pause"`）会停掉整个运行。要在入口校验 | `emerge/agent/control.py:113-121` | 代码 + 实验 |
| A7 | 打开会话时，`load_pinned_code` 只在"注册表里已有来自文件的当前实现"时才把它还原 | 全新进程里生成类型都还没注册，加载的固定版本就成了当前实现：只固定 v1 时，即使 v2 存在，目录也显示 v1，之后不写 `code` 的新节点会固定到 v1。应用打开对话时先用 `load_registry` 把对话文件夹 `code/` 下各类型的全部版本按顺序注册（最新的成为当前实现），再打开会话，所以经应用打开时不会发生（`emerge/application.py:247-263`） | `emerge/runtime/checkpoint.py:183-193`；`emerge/graph/registry.py:203` | 代码 + 实验 |

### B. 罕见竞态与边角（知道即可）

| # | 问题 | 位置 | 确认 |
|---|---|---|---|
| B1 | `void_asks` 不持锁，逐条 await；`answer_ask` 同时 pop 同一实例的另一条提问 → `KeyError`，`_finish` 跳过 `_on_current_ended`：下游输入不关闭、启动边不推进。前提是同一实例有 ≥2 条待答提问 | `emerge/runtime/bus.py:321-326`；`supervisor.py:846-848` | 代码 + 实验（假存储） |
| B2 | `end_run` 时被停节点的下游 `pending` 节点会被记成 `skipped`；决策记录写的是"保持 pending"。关闭会话（closing=True）不受影响 | `emerge/runtime/supervisor.py:751-758,839-840` | 代码 + 实验 |
| B3 | `ctx.aclose()` 抛错时没有 EXIT 报告，结局丢失，记 `failed` | `emerge/runtime/process.py:195-201` | 实验 |
| B4 | 节点 fork 出的子进程拿着管道副本时，主进程一直等它退出，强杀后的等待没有上限 | `emerge/runtime/supervisor.py:834-835` | 实验 |
| B5 | `starting` 时暂停再恢复，`setup` 还没完成就记成 `running` | `emerge/runtime/supervisor.py:344-353` | 实验 |
| B6 | 模型层抛出非 `ModelError` 的异常（如回包 `choices` 为空时的 `IndexError`）时，不发 `model.finished`，也不重试 | `emerge/models/client.py:59` | 实验 |
| B7 | 主 Agent：`attach` 中途出错后无法再挂会话；分叉回答只捕获 `ModelError` 和 `RuntimeControlError`；模型调用失败的那一轮不回滚，也不告诉模型 | `emerge/agent/agent.py:276-278,361-362,470-480,417-427` | 实验 |
| B8 | 存储：`open()` 版本检查失败时原来不关连接（进程退出时可能卡住），2026-11-04 起先关再抛；删除对话（现在是 `delete_session` 按文件夹删）仍是先只读查一次运行状态再删，中间没有锁 | `emerge/storage/sqlite.py:240-246`；`emerge/agent/session.py:231-241` | 代码 + 实验 |
| B9 | stream：`Subscription` 丢弃后不退订（强引用、队列无上限）；`aclose()` 唤不醒正在等待的迭代。阶段 9 的 TUI 订阅时要显式退订 | `emerge/runtime/stream.py:86-119` | 实验 |

### C. 给模型的文字或报告不准

| # | 问题 | 位置 | 确认 |
|---|---|---|---|
| C1 | 提示里出现不存在的工具名 `reset_graph_file`（工具叫 `reset_graph`）；过期拒绝的同一条回复里两种名字都有 | `emerge/runtime/mutations.py:335,347`；`emerge/runtime/checkpoint.py:104`；`emerge/agent/tools/graph.py:76` | 代码 |
| C2 | graph.json 被删时：`commit_graph` 说"文件按你写的原样保留"；`run_status`、`show_graph` 和运行信息说"与运行中的图一致" | `emerge/agent/tools/graph.py:77`；`emerge/runtime/mutations.py:366-371`；`emerge/agent/agent.py:553` | 代码 |
| C3 | dry run 把正在等旧实例退出的节点报成 `started`，真提交时报 `after_old_instance` | `emerge/runtime/mutations.py:146-155` | 实验 |
| C4 | 实际什么都没改的 patch（如描述设成原值）也让版本 +1，并报 `committed=True`。图文件这条路径不会产生这种 patch | `emerge/graph/patch.py:156-162` | 实验 |
| C5 | 图文件里把参数 `1` 改成 `1.0` 或 `true` 被当成没改（Python 里 `1 == 1.0 == True`） | `emerge/graph/spec.py`（03「在文件里做某种编辑」） | 实验 |
| C6 | 图文件的格式检查遇到第一个错误就停，不是"一步的错误一次报全"；`all_of` 出错时还会多报一条说 `any_of` 为空的错误 | `emerge/graph/spec.py:45-87,270-272` | 实验 |
| C7 | 几个启动边环共用一个节点时只报出一个环 | `emerge/graph/graph.py:279` | 实验 |
| C8 | 生成代码文件里放一个没写 `name` 的公共基类，整个文件都注册失败 | `emerge/graph/registry.py:254-256` | 实验 |

### D. 注释或计划文字过时（不影响行为）

| # | 内容 | 位置 |
|---|---|---|
| D1 | "取号和写盖章的消息在同一个事务里"。实际是两个事务，用户 10-28 同意保持 | `emerge/storage/sqlite.py:1-2`；`emerge/storage/base.py:423`；HANDOFF.md:270 |
| D2 | 阶段 9 计划写的 `ModelClient.check()` 已改名为 `probe_api_compatibility()` | `phase-09-app-cli-tui.md:36`；`emerge/models/client.py:63` |
| D3 | phase-02-models.md:134-201 是旧版"要求 / 完成标准"的重复段落（还有 `previous_response_id`、`request_timeout`）；阶段 5 计划有三处与代码不同；HANDOFF 若干早期条目没有标注已被后来的决定取代 | 01、04 各自的疑点一节 |

## 4. 阶段 9 要接的口

| 要做的事 | 现成的入口 | 注意 |
|---|---|---|
| 读配置、建工作区 | `load_config`、`init_workspace` | 非 UTF-8、路径是目录等情况抛原始 OSError，不是 `ConfigError`（01 疑点 11） |
| 节点注册 | `load_registry(config, session_dir)`：`config.nodes` 的模块，加上这个对话 `code/` 下的全部版本 | 每个对话一个注册表；先注册再打开会话（A7） |
| 存储 | `open_storage(session_dir)`，一个对话一个库；列出对话用 `list_sessions(workspace)`（内部是只读的 `read_session`） | 版本不对时会关掉连接再抛（B8） |
| 模型 | `create_client(config, image_root=对话文件夹, provider=...)`、`probe_api_compatibility()`；启动时的检查是 `check_model_api(config, provider)` | 计划里的名字是 `check()`（D2） |
| Runtime 与主 Agent | `RuntimeService.open(storage, config, registry, callbacks=..., on_feedback=lambda item: agent.on_feedback(item))`，`MainAgent(config, storage, runtime, ...)` | 两者互相引用，先建 service 再建 agent，回调里用闭包 |
| 会话 | `Session.create(agent, session_id)` / `Session.open(agent)` / `Session.close`；`list_sessions(workspace)`、`delete_session(workspace, session_id)` | `open` 出错时主 Agent 已 attach 未 start（B7）；应用的 `_open` 会把这个对话的整套都关掉 |
| 用户输入与命令 | `MainAgent.submit`、`MainAgent.control` | 入口校验 action（A6） |
| TUI 数据 | 自己的 `CallbackHandler` + `RuntimeService.stream(run_id, modes)` | 显式退订（B9）；`graph_file_status` 给 `/graph` 用 |

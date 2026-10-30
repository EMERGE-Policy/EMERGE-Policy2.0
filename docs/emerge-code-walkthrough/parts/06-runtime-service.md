# Runtime 服务层（阶段 7）

## 概览

| 文件 | 职责 | 主要调用方 |
|---|---|---|
| `service.py` | `RuntimeService`：运行时唯一对外入口；持有本进程打开的运行（每个一个 `Supervisor`）和三个共享回调 handler（`JsonlTracer`、`FeedbackHandler`、`StreamHandler`） | 主 Agent 的工具与控制通道、`Session.open/close`、以后的 TUI/脚本 |
| `run.py` | `Run`（对外快照）、`RunHandle`（服务里一个打开的运行：supervisor、提交锁、运行状态及其写库）、事件常量 `run.status` / `run.idle` | `service.py`、`mutations.py`、`checkpoint.py` |
| `mutations.py` | 运行中改图：patch 提交、`graph.json` 草稿的提交/重置/状态、dry run、提交结果与影响说明 | `service.py`；`checkpoint.py`（写图文件、`commit_recorder`） |
| `checkpoint.py` | 会话关闭时收尾（`close`），打开时接着跑（`resume`），恢复报告 | `RuntimeService.close_run` / `resume_run` |
| `feedback.py` | 把运行事件变成给主 Agent 的 `Feedback`（立即或按窗口合并）；运行摘要 | 回调管理器；`RuntimeService.summary`；`MainAgent.on_feedback` |
| `stream.py` | 按模式把事件放进外部订阅者的队列 | `RuntimeService.stream` |

位置列的写法：`emerge/runtime/` 下的文件只写文件名（如 `service.py:154`），其他文件写 `emerge/` 起的路径。文中"实验确认"指在 `/data/yuqingchi/tmp/emerge-code-walkthrough/scratch/service/exp*.py` 里实际跑过。

## RuntimeService：`service.py`

运行时唯一对外入口。它在本进程里持有打开的运行（`_runs: run_id → RunHandle`）：改图转给 `mutations`，关闭/打开转给 `checkpoint`，暂停、停止、发消息、回答直接转给该运行的 `Supervisor`；三个回调 handler 由所有运行共享。所有事件经 `CallbackManager.emit_nowait` 只入队，每个 handler 在自己的 task 里按顺序处理（`emerge/callbacks/manager.py:25-43,110-116`），所以 handler 看到事件总比发出时晚。

### 创建与关闭

`await RuntimeService.open(storage, config, registry=None, **options)` 的参数：

| 参数 | 含义 | 默认 |
|---|---|---|
| `storage` | 已打开的 `Storage`；服务不负责关闭它 | 必填 |
| `config` | `WorkspaceConfig`；工作区路径取自它 | 必填 |
| `registry` | 所有运行共用的节点注册表；接着跑时会往里注册固定版本的生成代码 | `get_active_registry()` |
| `callbacks` | 外部 `CallbackManager`；不给时自建，`aclose` 时关闭 | 自建 |
| `trace` | 是否挂 `JsonlTracer(runs/)`：全部运行的全部事件（含 `model.token`）写 `runs/<run_id>/trace.jsonl`，无 run_id 的写 `runs/_session/trace.jsonl`；收到 `run.finished` 关该运行的文件 | `True` |
| `on_feedback` | 给主 Agent 的回调（同步或异步）；None 时 `Feedback` 进 `service.feedback.queue` | None |
| `feedback_interval` | 合并推送的窗口（秒） | 1.0 |
| `summary_chars` | `summary()` 的字符上限 | 4000 |
| `policy` | 传给每个 `Supervisor` 的 `StopPolicy`；None 时各自按 `config.runtime.stop_timeout` | None |

| 情况 | 处理 | 位置 |
|---|---|---|
| `open()` | 构造后把 tracer（若有）、feedback、streams 加到回调管理器；handler 要绑定正在运行的事件循环，所以放在 `open()` 而不是 `__init__`（代码注释） | `service.py:125-138` |
| 不经 `open()` 直接构造 | handler 没挂上：没有 trace、反馈、stream；自建的管理器未绑定事件循环，第一次 emit 抛 `RuntimeError` | `service.py:98-123`；`emerge/callbacks/manager.py:71-73` |
| 外部管理器已绑定别的事件循环 | `add_handler` 抛 `RuntimeError` | `emerge/callbacks/manager.py:99-104` |
| `aclose()` | 对 `_runs` 里每个未结束的运行调 `close_run`（等同会话关闭，状态 `closed`）；已 `finished`/`stopped` 的留在 `_runs` 里 | `service.py:140-144` |
| `aclose()`，管理器是自建的 | `callbacks.aclose()`：每个 handler 处理完队列里剩下的事件后调 `close()`（feedback 立即发出未满窗口的 batch；stream 给每个订阅者放结束标记；tracer 关文件）；同一管理器上别人挂的 handler 也一起关 | `service.py:145-146`；`emerge/callbacks/manager.py:90-97` |
| `aclose()`，管理器是外部的 | 只 `remove_handler` 自己的三个：各自排空队列后同样调 `close()`，但不等它们完成就返回 | `service.py:147-150`；`emerge/callbacks/manager.py:61-64` |
| `aclose()` 中某个 `close_run` 抛错 | 异常直接抛出，后面的运行不再关闭，handler 不释放 | `service.py:142-150` |
| 重复 `aclose()` | 没有打开的运行；关闭已关闭的管理器、移除不存在的 handler 都是空操作 | 同上 |

### 接口总表

"持有"指运行在本服务的 `_runs` 里（含已 `finished`/`stopped`，不含已 `close` 的）；"未结束"指状态不是 `finished`/`stopped`/`closed`。不满足时一律 `RuntimeControlError`（`service.py:323-333`）。

| 方法 | 作用 | 前置条件 | 报错 / 拒绝 | 写存储 / 发事件 |
|---|---|---|---|---|
| `start_run(graph, session_id) -> Run` | 建运行并启动 `graph`（可为空图） | 会话在库里 | 会话不存在：`StorageError`（`create_run`） | `runs` 新行、`sessions.current_run_id`、`graphs`、节点状态、`graph.json` 与 `runs.graph_file_digest`；`run.started`、`node.state`…、`run.status` |
| `end_run(run_id) -> Run` | 明确结束运行，`finished` | 持有 | 已结束：`"run X is already finished"` | 节点 `stopping`→`stopped`、不可能再启动的 `skipped`；`runs.status`、`finished_at`；`node.state`…、`run.status`、`run.finished` |
| `close_run(run_id) -> Run` | 会话关闭：停进程、保留节点状态，`closed` | 持有且未结束 | 已结束：`"run X is finished"` | 只写 `runs.status=closed`（`finished_at` 仍空）；`run.status`；不发 `run.finished` |
| `resume_run(run_id) -> ResumeReport` | 打开会话时接着跑 `closed` 的运行 | 未持有；库里有；状态 `closed` | 已持有：`"already open"`；库里没有：`RuntimeControlError`；非 closed：`CheckpointError`；固定版本代码文件缺失：`RegistryError`；重跑补丁校验失败：`GraphValidationError` | 提问作废；旧实例补记 `stopped`、新实例登记；有重跑时 `graphs` 新版本；无草稿时 `graph.json` 与摘要；`runs.status`；`graph.patched`(source=resume)、`node.stale`、`node.state`、`run.status` |
| `runs() -> tuple[Run, ...]` | 持有的运行的快照 | — | — | — |
| `apply_patch(run_id, patch) -> GraphCommitResult` | 提交一个 patch | 未结束 | 运行正在 shutdown：`RuntimeControlError`；patch 不合法不抛，返回 `error_kind="rejected"` | 见 mutations |
| `commit_graph_file(run_id, *, replace=(), dry_run=False) -> GraphCommitResult` | 提交 `graph.json` | 未结束 | 同上；文件问题返回 `missing_file`/`format`/`stale`/`rejected` | 见 mutations；`dry_run` 什么都不写 |
| `reset_graph_file(run_id) -> GraphFileStatus` | 丢弃草稿，按当前图重写文件 | 持有（已结束也可） | — | `graph.json`、`runs.graph_file_digest` |
| `graph_file_status(run_id) -> GraphFileStatus` | 文件与运行中图的对比 | 持有 | — | 只读文件 |
| `pause(run_id, node_id=None)` | 暂停一个节点（返回 `NodeRuntimeState`）或整个运行（返回 `Run`） | 未结束；节点见下表 | 节点不存在、状态不对：`RuntimeControlError` | 节点 `paused`；整个运行另写 `runs.paused=1`、状态 `paused`；`node.state`、`run.status` |
| `resume(run_id, node_id=None)` | 恢复 | 同上 | 同上 | 节点 `running`；整个运行另写 `runs.paused=0`、状态重算 |
| `stop(run_id, node_id=None)` | 停一个节点并等它退出 / 结束整个运行为 `stopped` | 同上 | 节点 `pending`：`"... remove it instead of stopping it"` | 节点 `stopping`→`stopped`；整个运行同 `end_run`，但 `stopped` |
| `answer(ask_id, text) -> AskRecord` | 把回答交给提问的实例 | 某个未结束运行的 bus 里有这条在等的提问 | 找不到：`"ask X is not waiting for an answer"` | `asks` 记 answered；`node.answer`（盖时间线编号） |
| `send_to_node(run_id, node_id, *, text, data, refs) -> NodeDelivery` | 给节点当前实例发主 Agent 消息 | 未结束；节点存在且未结束 | 节点已结束：`"node X is <state>; the message was not delivered"` | `node.main_agent_message`（盖编号）；实例未启动时写 `queued_node_messages` |
| `graph(run_id) -> Graph` | 当前图 | 持有 | — | — |
| `status(run_id) -> RunStatusReport` | 运行、各节点、待答提问、图文件状态 | 持有 | — | — |
| `node_detail(run_id, node_id) -> NodeDetail` | 单节点详情 | 持有；节点在当前图中 | 无此节点：`"no node X in run Y"` | — |
| `summary(run_id) -> str` | 运行摘要（`FeedbackHandler.summary`） | 不检查 | 不报错；没见过的 run_id 返回 `"run <id>: running"` | — |
| `stream(run_id, modes) -> Subscription` | 订阅该运行的输出 | 持有 | 模式为空/未知：`RuntimeControlError` | — |

### 运行级操作

| 情况 | 处理 | 位置 |
|---|---|---|
| `start_run` 正常 | ① `Storage.create_run` 发 run id（uuid hex），`ensure_run_dir` 建 `runs/<run_id>/` ② 建 `Supervisor` + `RunHandle`，登记进 `_runs` ③ `save_graph` ④ 发 `run.started{session_id, graph_version}` ⑤ `supervisor.start`：全部节点登记 `pending`，可启动的立即启动 ⑥ 强制写 `graph.json` ⑦ `refresh_status` | `service.py:154-164`；`run.py:51-55` |
| `start_run` 用空图 | 状态 running→`idle`（发 `run.status`），不发 `run.idle`：空图的 supervisor 永远不判定空闲 | `run.py:132-134`；`supervisor.py:891-895` |
| `start_run` 图里有未注册类型 / 进程起不来 | 不报错：该节点记 `starting`→`failed`（"cannot start: ..."），再按启动边推进 | `supervisor.py:796-801` |
| `start_run` 在登记 handle 之后抛错 | 不回滚：handle 留在 `_runs` 里（见疑点） | `service.py:157-163` |
| `end_run` / `stop(run_id)` 正常 | `_end`：`supervisor.shutdown()`（对每个未退出的进程发停止，等退出，超 `stop_timeout` 强杀；当前实例 `stopping`→`stopped`；启动边再也满足不了的 `pending` 记 `skipped`；不再启动任何节点；关 bus）→ `set_status(finished 或 stopped)` → 发 `run.finished{status, graph_version}` | `service.py:335-341`；`supervisor.py:416-433,751-769` |
| `end_run` 时有替换在等旧进程 | 旧进程按停止流程结束；新实例因 shutdown 不再启动，留在 `pending`（实验确认） | `supervisor.py:704-712,759` |
| 对已结束运行 `end_run` | `"run X is already finished"`（`_end` 的检查） | `service.py:336-337` |
| 对已结束运行 `stop(run_id)` | `_open_handle` 先拒：`"run X is finished"` | `service.py:329-333` |
| 对已 close 的运行做任何操作（除 `resume_run`、`summary`） | 不在 `_runs`：`"run X is not open in this service"` | `service.py:323-327` |
| `close_run` 正常 | `checkpoint.close`（见 checkpoint 一节）→ 从 `_runs` 删除 → `feedback.forget(run_id)` | `service.py:170-178` |
| 对已结束运行 `close_run` | `"run X is finished"` | `service.py:173-174` |
| 重复 `close_run` | 第二次已不在 `_runs`：`RuntimeControlError` | `service.py:172` |
| `resume_run` 正常 | `checkpoint.resume` → 登记进 `_runs` → 用当前各节点状态和库里此刻 `pending` 的提问 `feedback.seed` | `service.py:180-199` |
| `resume_run` 的运行已被持有（包括已 finished/stopped） | `"run X is already open"` | `service.py:182-183` |
| `resume_run` 库里没有该运行 | `"no run X"` | `service.py:184-186` |
| `resume_run` 运行不是 `closed` | `CheckpointError`（不是 `RuntimeControlError`） | `checkpoint.py:123-124` |

### 节点控制

| 情况 | 处理 | 位置 |
|---|---|---|
| `pause(run_id)`（整个运行） | `pause_all`：设 `_run_paused`，`starting`/`running` 的当前实例记 `paused` 并发 `control pause`；之后被启动边、提交、替换启动的实例都以暂停状态启动（`starting → paused`）→ `set_paused(True)`：有变化才写 `runs.paused`，再重算状态为 `paused` | `service.py:230-232`；`supervisor.py:360-366,776`；`run.py:112-116` |
| 已整体暂停时再 `pause(run_id)` | 无报错、无新事件 | `run.py:113` |
| 运行 `idle` 时 `pause(run_id)` | 状态变 `paused`；之后新加的节点以暂停状态启动 | `run.py:130-131` |
| `resume(run_id)` | `resume_all`：清 `_run_paused` 和 `_start_paused`；所有 `paused` 的当前实例转 `running` 并发 `control resume`（包括单独暂停的节点）；以暂停状态启动、尚未 ready 的发 `resume` → `set_paused(False)`，状态重算为 `running` 或 `idle` | `service.py:238-240`；`supervisor.py:368-376` |
| 运行没整体暂停时 `resume(run_id)` | 照样执行 `resume_all`：单独暂停的节点、替换/重跑时继承了暂停的待启动实例都被放开 | `supervisor.py:370-376` |
| `pause(run_id, node)`：节点 `starting`/`running` | 记 `paused`，发 `control pause`；运行状态不变 | `supervisor.py:339-347` |
| `pause(run_id, node)`：已 `paused` | 原样返回记录 | `supervisor.py:343` |
| `pause(run_id, node)`：`pending`/`stopping`/已结束 | `"node X is <state>; only a starting or running node can be paused"` | `supervisor.py:344-345` |
| `resume(run_id, node)`：`paused` | 记 `running`，发 `control resume`；整个运行仍暂停时也照样放开这一个，运行状态仍 `paused` | `supervisor.py:352-353` |
| `resume(run_id, node)`：以暂停状态启动、还在 `starting` | 移出 `_launch_paused`，发 `resume`，ready 后转 `running` | `supervisor.py:356-357` |
| `resume(run_id, node)`：普通 `starting`/`running` | 原样返回 | `supervisor.py:349-358` |
| `resume(run_id, node)`：`pending`/`stopping`/已结束 | `"node X is <state>; only a paused node can be resumed"` | `supervisor.py:354-355` |
| `stop(run_id, node)`：`starting`/`running`/`paused`/`stopping` | 发停止（已在 `stopping` 的再发一次），在锁外等进程退出，超 `stop_timeout` 强杀，都记 `stopped`；调用阻塞到退出为止；下游按"无结局"处理 | `supervisor.py:326-337` |
| `stop(run_id, node)`：已结束 | 原样返回记录 | `supervisor.py:330-331` |
| `stop(run_id, node)`：`pending`（包括替换后等旧实例退出的新实例） | `"node X has not started; remove it instead of stopping it"` | `supervisor.py:332-333` |
| 节点不在当前图 | `"no node X in the graph"` | `supervisor.py:636-640` |

### 消息与提问

| 情况 | 处理 | 位置 |
|---|---|---|
| `send_to_node`，当前实例进程已在 | 在 bus 的时间线锁内盖编号，`seq` 按实例 +1，发 `node.main_agent_message`，直接写入管道；`queued=False` | `supervisor.py:394-411`；`bus.py:269-299` |
| 当前实例还没启动（`pending`，包括等旧实例退出的新实例） | 同样盖编号、发事件，另写 `queued_node_messages`；`queued=True`；实例启动 `attach` 时按 `seq` 交出并标记送达 | `bus.py:294-296,204-211` |
| 节点已结束 | 拒绝并说明状态 | `supervisor.py:405-406` |
| 消息排队后节点被删除，或 `pending` 时被替换 | 消息绑定原实例 id，永远不会送达，行留在库里 | `bus.py:296` |
| `answer`：依次查每个未结束运行的 bus 内存，找到在等的 `ask_id` | 写 answered，发 `node.answer`，回复提问的进程 | `service.py:249-253`；`bus.py:301-319` |
| 提问的实例已结束、运行已 close、已回答过、关闭前的旧提问 | `has_ask` 为假：`"ask X is not waiting for an answer"`；`has_ask` 之后、拿到时间线锁之前被作废的，bus 抛同样的错 | `service.py:253`；`bus.py:304-306` |

### 查询

| 情况 | 处理 | 位置 |
|---|---|---|
| `status` | `run` 快照；按图中顺序每个节点一个 `NodeView`；库里 `pending` 的提问；`graph_file_status` | `service.py:276-283` |
| `NodeView.state` 找不到记录 | 填 `"pending"`（防御分支：节点登记时就有记录） | `service.py:353` |
| `NodeView.irreversible` | 按 `(type, code)` 查注册表；查不到为 False | `service.py:347,357` |
| `node_detail` | 当前实例的参数、`code`、状态快照（库）；该逻辑节点每个输出端口的最新消息（可能来自被替换的旧实例：版本号跨实例连续）；当前实例产物目录（相对工作区）；替换历史；所有实例的内存记录（含旧实例）；该节点所有实例的待答提问 | `service.py:285-306` |
| 查询已 `finished`/`stopped` 的运行 | 允许 | `service.py:323-327` |

### 返回类型

| 类型 | 字段 | 约束/说明 |
|---|---|---|
| `NodeView` | `node_id, instance_id, type, description, state, outcome, reason, error, irreversible` | frozen |
| `RunStatusReport` | `run: Run, nodes, pending_asks, graph_file: GraphFileStatus` | frozen |
| `NodeDetail` | `node, params, code, snapshot, outputs: dict[端口, Message], artifacts_dir, replacements, instances, pending_asks` | `artifacts_dir` 为 `runs/<run>/nodes/<node>/<instance>` |
| `NodeDelivery` | `node_id, instance_id, seq, content_time, queued` | `seq` 按实例编号 |

### 锁与并发

| 锁 | 谁拿 | 作用 |
|---|---|---|
| `RunHandle.commit_lock`（每个运行一把） | `mutations.apply`（只包住 `_apply`）、`commit_graph_file`（全过程，含写文件）、`reset_graph_file` | 同一运行的提交先后执行；不同运行互不阻塞 |
| `Supervisor._ops` | supervisor 内部：`apply_patch`、`continue_run`、暂停/恢复/停止、`deliver`、`shutdown` 等 | 校验与执行用同一时刻的节点状态；`pause`/`stop`/`send_to_node` 不拿提交锁，在这里与提交串行 |
| `RunHandle._status_lock` | `set_status`、`refresh_status`、`reopen` | 运行状态的写库与事件不交错 |
| `FeedbackHandler._push_lock` | `_send` | 所有运行的推送一条条按产生顺序交出 |

加锁顺序固定：提交锁 → `_ops`（`on_commit` 在 `_ops` 内）→ 释放后再拿 `_status_lock`；`on_idle` 只拿 `_status_lock`。

### 调用方

| 调用方 | 调用 | 位置 |
|---|---|---|
| `Session.open` | 对该会话库里每个 `closed` 的运行 `resume_run`，报告交给主 Agent | `emerge/agent/session.py:96-101` |
| `Session.close` | 先 `agent.detach()`，再对本会话每个未结束的运行 `close_run` | `emerge/agent/session.py:109-118` |
| 工具 `start_run` | 当前运行已结束（或没有）时用空图 `start_run`，再取 `graph_file_status().path` | `emerge/agent/tools/execution.py:28-43` |
| 工具 `run_status`、`node_detail`、`show_graph` | `status`、`node_detail`、`graph` | `emerge/agent/tools/execution.py:46-82`；`emerge/agent/tools/graph.py:96-113` |
| 工具 `pause`/`resume`/`stop_node`；用户控制通道 | `pause`/`resume`/`stop`，`RuntimeControlError` 转成工具错误或控制结果 | `emerge/agent/tools/execution.py:148-159`；`emerge/agent/control.py:103-125` |
| 工具 `end_run(stopped)` | `stopped` 为真调 `stop(run_id)`，否则 `end_run` | `emerge/agent/tools/execution.py:113-124` |
| 工具 `send_to_node` | `send_to_node`，按 `queued` 回复"已排队"或"已送达第 N 条" | `emerge/agent/tools/execution.py:127-143` |
| 工具 `commit_graph` | 先 `commit_graph_file(dry_run=True)`（检查新增/替换的节点必须写 description），通过后再真正提交；同一份被拒的文件在同一版本下不再重交 | `emerge/agent/tools/graph.py:37-81` |
| 工具 `reset_graph` | `reset_graph_file` | `emerge/agent/tools/graph.py:84-93` |
| `MainAgent.on_feedback` | 未挂会话或正在关闭时丢弃；`ask` 另起分叉回答、经 `answer` 交回；其余作为通知进 inbox | `emerge/agent/agent.py:242-251,468-472` |
| `MainAgent._run_info` | `runs()`、`summary()`、`graph_file_status()` | `emerge/agent/agent.py:535-547` |
| `ToolContext.held_run_id` / `open_run_id` | 用 `runs()` 判断当前运行是否仍被持有、是否已结束 | `emerge/agent/tools/__init__.py:54-66` |

`apply_patch` 没有主 Agent 调用方：patch 工具不在主 Agent 的默认工具表里（决策记录［图文件：主 Agent 直接改磁盘上的文件］，HANDOFF.md:255），留给 TUI 和程序。包内没有调用 `RuntimeService.open` 的地方（阶段 9 的应用未实现）；`MainAgent` 的说明要求传 `on_feedback=lambda item: agent.on_feedback(item)`（`emerge/agent/agent.py:166-168`）。

## 运行记录：`run.py`

`Run` 是调用方看到的运行快照；`RunHandle` 是服务里一个打开的运行，持有它的 `Supervisor`、提交锁和运行状态，状态每次变化都先写库再发 `run.status`。由 `RuntimeService._make_handle` 创建（`service.py:319-321`），`mutations`、`checkpoint` 通过它改状态。

### `Run` 与 `RunHandle`

| 字段/方法 | 含义 | 约束/默认 |
|---|---|---|
| `Run.run_id` | `Storage.create_run` 生成的 uuid hex | `Run` 为 frozen 模型 |
| `Run.session_id` | 所属会话 | |
| `Run.graph_version` | 当前图版本（取自 supervisor） | |
| `Run.status` | `running`/`idle`/`paused`/`closed`/`finished`/`stopped` | |
| `Run.paused` | 整个运行暂停中 | False |
| `Run.started_at` / `finished_at` | `finished_at` 只在 `finished`/`stopped` 时由存储填，`closed` 和其余状态为 None | `emerge/storage/sqlite.py:683-699` |
| `create_run(storage, workspace, session_id)` | `storage.create_run` + `ensure_run_dir` | 库里初值 `status=running, paused=0` |
| `RunHandle.commit_lock` | 每个运行一把提交锁 | |
| `RunHandle.graph_file_digest` | 运行时上次写 `graph.json` 的 sha256 | 初值取库里的 |
| `RunHandle.ended` | `status ∈ ENDED_STATUSES = {finished, stopped, closed}` | `run.py:33` |
| `snapshot()` | 生成 `Run` | |
| `set_status(s)` | 拿 `_status_lock` 后 `_set_status`，不论是否已结束 | 只有 `_end` 和 `checkpoint.close` 用 |
| `refresh_status()` | 未结束时按 `_live_status()` 重算；已结束不动 | `start_run` 末尾、提交后、暂停/恢复后、空闲回调里调用 |
| `reopen()` | 不管是否 `closed`，按 `_live_status()` 设置 | 只在 `checkpoint.resume` 末尾用 |
| `set_paused(p)` | 值有变化才写 `runs.paused`，然后 `refresh_status` | `runs.paused` 与状态分开存，因为关闭会话时状态会变成 `closed`（`emerge/storage/base.py:102-103`） |
| `on_idle()` | supervisor 判定空闲时调度 | 构造时设 `supervisor.on_idle = self.on_idle`（`run.py:83`） |
| `_set_status(s)` | 与当前相同直接返回；否则 `storage.set_run_status` → 更新内存与 `finished_at` → 发 `run.status{status, previous}` | `run.py:147-154` |

`run.status`、`run.idle` 是模块常量，不在 `events.EventType`（阶段 0 的接口）里，handler 经 `on_event` 收到（决策记录［阶段 7］`run.py`，HANDOFF.md:747）。

### 状态取值（`_live_status`）

| 条件（按顺序） | 结果 | 位置 |
|---|---|---|
| `paused` 为真 | `paused` | `run.py:130-131` |
| 图里没有节点，或 `supervisor.idle` | `idle` | `run.py:132-134` |
| 其余 | `running` | `run.py:135` |

### 状态转换

运行不会自己结束：`idle` 不是终点，主 Agent 可以接着往图里加节点；只有 `end_run`、`stop(run_id)` 进入 `finished`/`stopped`，`close_run` 进入 `closed`。

| 当前状态 | 触发 | 新状态 | 副作用 |
|---|---|---|---|
| （无） | `start_run` → `Storage.create_run` | `running` | 库里新行；不发 `run.status` |
| `running` | `start_run` 末尾 `refresh_status`，图为空 | `idle` | 写库，`run.status`；不发 `run.idle` |
| `running` | supervisor 判定空闲 → `on_idle` | `idle` | 先发 `run.idle{graph_version}`，再写库、发 `run.status` |
| `idle` | 提交登记了新节点（`_apply` 末尾 `refresh_status`），运行未整体暂停 | `running` | 写库，`run.status` |
| `idle` | 提交新增的节点全部立即 `skipped` | `idle`（不变） | supervisor 再次判定空闲 → 再发一次 `run.idle` |
| `running`/`idle` | `pause(run_id)` | `paused` | `runs.paused=1`，写状态，`run.status` |
| `paused` | `resume(run_id)` | `running` 或 `idle` | `runs.paused=0`，写状态，`run.status` |
| `paused` | 暂停期间所有节点都结束（不调用 `wait_for_run_permission()` 的节点照样跑完） | `paused`（不变） | 照发 `run.idle`，主 Agent 收到 graph_idle（实验确认） |
| `running`/`idle`/`paused` | `end_run` / `stop(run_id)` | `finished` / `stopped` | 先 shutdown（节点状态先写库）；写 `finished_at`；`run.status`；`run.finished` |
| `running`/`idle`/`paused` | `close_run` / `aclose()` | `closed` | 先 shutdown(closing)；`finished_at` 仍为空；`run.status`；不发 `run.finished`；`runs.paused` 保留 |
| `closed` | `resume_run` → `reopen` | `paused`（`runs.paused` 为真）/ `idle` / `running` | `run.status{previous: closed}`；不发 `run.idle`（见下） |
| `finished`/`stopped`/`closed` | `refresh_status`（提交、暂停、空闲回调） | 不变 | — |
| 任意 | 单个节点暂停/恢复/停止 | 不变 | 运行状态只随整个运行暂停、空闲回调、提交重算 |

### 图空闲的判定与通知

1. supervisor 在每次 `_advance` 末尾、每次回收进程后调用 `_check_idle`（`supervisor.py:770,908`）。
2. 空闲的条件：不在 shutdown；当前图至少一个节点；所有节点的当前实例都已结束；所有进程都已退出，包括还在停的被替换旧实例（`supervisor.py:886-895`）。
3. 只在"非空闲 → 空闲"时调用一次 `on_idle`，以后台任务调度（`supervisor.py:896-902`）；登记新节点会清掉空闲标记（`supervisor.py:736`）。
4. `RunHandle.on_idle` 执行时再确认：运行未结束（`closed` 也算结束）且 supervisor 仍空闲，否则直接返回（`run.py:139-140`）。
5. 发 `run.idle{graph_version}`，再 `refresh_status`（`run.py:141-142`）。
6. `FeedbackHandler` 收到 `run.idle` 立即推 `graph_idle` 给主 Agent（`feedback.py:161-172`）；stream 里只有 `debug` 模式能看到。

| 情况 | 处理 | 位置 |
|---|---|---|
| 空图 | 永不判定空闲、不发 `run.idle`；状态靠"图为空"成为 `idle` | `supervisor.py:893-895`；`run.py:132-133` |
| `on_idle` 执行前已有提交启动了新节点 | 早返回 | `run.py:139` |
| 打开会话时所有节点都已结束 | `on_idle` 在 `continue_run` 里被调度，执行时 handle 状态仍是 `closed`，早返回：状态变 `idle`，不发 `run.idle`、不推 graph_idle（实验确认，见疑点） | `run.py:139`；`checkpoint.py:150` |
| `end_run` / `close_run` 期间 | shutdown 中 `_check_idle` 直接返回 | `supervisor.py:889-890` |

## 运行中改图：`mutations.py`

把一次改动整份交给 `Supervisor.apply_patch`（它在锁内用同一时刻的节点状态校验并执行），并负责主 Agent 的草稿文件 `runs/<run_id>/graph.json`。改动来源记在 `graph.patched.source`：调用方给的 patch（`patch`）、从 `graph.json` 算出的 patch（`file`）、接着跑时的重跑补丁（`resume`，由 checkpoint 经 `commit_recorder` 记录）。

### 一次 patch 提交（`apply_patch`）

1. `RuntimeService.apply_patch`：`_open_handle`，未持有或已结束抛 `RuntimeControlError`（`service.py:207-208`）。
2. `mutations.apply` 拿该运行的 `commit_lock`（`mutations.py:126`）。
3. `_apply`：再查一次 `handle.ended`，已结束抛 `RuntimeControlError`（`mutations.py:189-190`）。
4. `Supervisor.apply_patch(patch, on_commit=commit_recorder(handle, source))`，进入 `_ops` 锁（`supervisor.py:448`）：
   1. 正在 shutdown（`end_run`/`close_run` 进行中）：抛 `RuntimeControlError`，`_apply` 不捕获（`supervisor.py:449-450`）。
   2. 取各节点当前实例的状态，`apply_graph_patch(当前图, patch, registry, states=...)`，不合法抛 `GraphValidationError`（`supervisor.py:452-453`）。
   3. patch 没有编辑：`new is old`，原样返回（`supervisor.py:454-455`）。
   4. `diff_graphs`；`on_commit`：`save_graph` 覆盖库里的图，发 `graph.patched{version, previous_version, source, patch, diff}`，都在执行之前、锁内（`supervisor.py:456-458`；`mutations.py:172-183`）。
   5. 切换到新图；不经 await 给每个来源有变化的输入发一次 `input` 消息（`supervisor.py:459-462`）。
   6. 删除的节点在自己的实例下记 `skipped`；替换的节点换实例；新增的节点全部登记（`supervisor.py:463-475`）。
   7. 记下此刻所有 `pending` 节点，统一判断一次启动边（`_advance`），返回 `PatchExecution(old, new, diff, fates, held)`（`supervisor.py:476-484`）。
5. `_apply`：捕获 `GraphValidationError` → `error_kind="rejected"`；`new is old` → `committed=False, changed=False`；否则 `refresh_status` 后组装结果（`mutations.py:192-199`）。
6. 释放 `commit_lock`；提交成功则 `write_graph_file(force=False)`：文件不含草稿才重写（`mutations.py:128-129`）。

### 提交 `graph.json`（`commit_graph_file`）

1. `_open_handle`（`service.py:213`）。
2. 拿 `commit_lock`，全过程（含最后写文件）都在锁内（`mutations.py:330`）。
3. 文件不存在 → `missing_file`，提示用 `reset_graph_file`（`mutations.py:333-336`）。
4. `loads_graph_spec` 失败 → `format`；它分三步检查（JSON 语法与重复键 → 字段 → 引用），前一步有错不做后一步（`mutations.py:337-340`；`emerge/graph/spec.py:242-259`）。
5. `patch_from_graph_spec(当前图, spec, replace)` 失败：文件 `graph_version` 非空且 ≠ 当前版本 → `stale`，错误末尾加 "Call reset_graph_file, read the file again and redo the edit."；`replace` 里有不同时在图和文件里的节点 → `rejected`（`mutations.py:341-349`；`emerge/graph/spec.py:306-318`）。不合并：基于旧状态的判断不能套到新状态上（`mutations.py:326-327`）。
6. patch 为空 → "没有改动"：`committed=False, changed=False`，不产生版本（`mutations.py:350-351`）。
7. `dry_run` → `preview` 后返回（`mutations.py:352-353`）。
8. `_apply(handle, patch, "file")`，与上面第 3–5 步相同（`mutations.py:354`）。
9. 提交成功 → `write_graph_file(force=True)`，文件成为新版本；被拒时文件原样不动，主 Agent 可就地修改再提交（`mutations.py:355-356`）。

### 图的变化 → 运行时动作

状态要求不满足时，阶段 4 抛 `GraphValidationError`，整份 patch 不生效（`error_kind="rejected"`）。"有进程"指订阅方的当前实例已有进程。

| 变化（patch 操作 / 文件里的改法） | 状态要求 | 运行时动作 | 结果中的体现 |
|---|---|---|---|
| 新增节点（`add_node` / 文件加节点） | id 不存在，也不是本 patch 刚删掉的；整图校验通过 | 登记 `pending`（发 `node.state`）；全部新增登记完后统一判断：无启动边或某条边已满足 → 启动（整个运行暂停时以暂停状态启动）；再也满足不了 → `skipped` 并关闭它供给的输入；否则等待 | `diff.nodes.added`；`fates` 为 `started`/`never`/`waiting`；impact `added X[; starts after A, B]` |
| 新增节点，启动边指向已结束的节点 | 同上 | 用各节点当前实例已有的结局判断：结局符合（`*` 即任意已声明结局）→ 立即启动；以别的结局结束、或没有结局（`failed`/`stopped`/`skipped`）→ 立即 `skipped` | `fates[X]` 为 `started` 或 `never` |
| 改启动边（`set_start_edges` / 文件 `start_when`） | 只能是 `pending`；文件里只调换条件顺序不算改动（`emerge/graph/spec.py:388-395`） | 原地更新，立即重判：满足 → 启动；不可能 → `skipped`；否则等 | `modified` 字段 `start_edges`；impact `modified X: start_edges`；`fates[X]` |
| 删除节点（`remove_node` / 文件删节点） | 只能是 `pending`（开始过的提示改用停止，已结束的提示它留在图里）；不能同时被本 patch 替换；不能出现在非 `pending` 节点的启动边里；仍引用它的 `pending` 节点须在同一 patch 改启动边，否则校验失败 | 旧实例记 `skipped`（已非当前实例，只发 `node.stale`，不推给主 Agent）；它的订阅（两个方向）一并删除；它供给、且本 patch 没换到新来源的输入记为孤立，有进程的订阅方收到"关闭"；发给它的排队消息不再送达 | `diff.nodes.removed`；`removed_downstream[X]` = 旧图里的下游闭包（订阅 + 启动边，含环）；impact `removed X; inputs left without it: ...; start edges naming it: ...`（按旧图算，同一 patch 换了来源的输入也会列出） |
| 删除 pending 节点，同时把它的订阅方换到别的来源 | 同上 | 每个输入只通知一次、以新图为准：订阅方只收到"改读新来源"，不会先收到"关闭" | `removed` + `subscriptions.rewired` |
| 替换（`replace_node` / 文件改 `type`、改 `code`、或列在 `replace` 里） | 只能是 `pending`/`starting`/`running`/`paused`（`stopping` 和已结束拒绝）；一个节点一个 patch 只能换一次；保留的订阅两端端口类型原来一致、替换后不一致时，须在同一 patch 换线 | 新实例 `<id>@v<新版本>` 登记为当前实例。旧进程不存在或已退出：旧实例记 `skipped`，新实例按启动边判断。旧进程还在：旧实例 `paused`（或以暂停状态启动、尚未 ready）则新实例以暂停状态启动；节点加入 `_holding`，旧实例 `stopping`（`node.stale`）并发停止；后台等它退出（超 `stop_timeout` 强杀），然后在锁内放行并判断启动边。提交不等旧进程 | `diff.nodes.replaced`（新旧实例、类型、代码版本）；`fates[X]=after_old_instance`，`waiting_for_old_instance` 含 X；impact `replaced X: old -> new; A, B read the new instance from now on; start edges on it wait for the new instance: ...` |
| 改参数（`update_params` / 文件改 `params`） | 已结束拒绝 | `pending`：原地改，启动时用新参数。`starting`/`running`/`paused`：按替换处理，`code` 沿用原版本。`stopping`：替换的状态检查拒绝 | `pending`：`modified` 字段 `params`；其余：`replaced` |
| 改描述（`set_description` / 文件改 `description`） | 已结束拒绝 | 只换图里的 spec；已在跑的进程不受影响（它启动时拿到的描述不变） | `modified` 字段 `description`；impact `modified X: description` |
| 新增订阅（`connect` / 文件给输入加来源） | 订阅方已结束拒绝；已存在的订阅忽略 | 有进程：发 `input{source, closed=来源已结束, start=来源此刻的最新版本}`，只读之后发布的；订阅方未启动：不发，启动时从来源现存最早一条读起 | `subscriptions.added`；impact `connected A.x -> T.q` |
| 删除订阅（`disconnect` / 文件删输入） | 订阅不存在拒绝；订阅方已结束拒绝 | 有进程：发 `input{source=None, closed=False}`（只是断开，不算关闭） | `subscriptions.removed`；impact `disconnected ...`（两端节点都没被删时才写） |
| 换线（同一输入换来源：文件改输入的端点 / patch 先 disconnect 再 connect） | 同上 | 有进程：一条 `input{新来源, closed=新来源已结束, start=新来源此刻的最新版本}`；进行中的读取和等待不中断，从这一刻往后读；不发来源变更通知 | `subscriptions.rewired`（旧, 新）；impact `rewired T.q: A.x -> B.y, read from now on` |
| patch 没有编辑 / 文件与当前图一致 | — | 无 | `committed=False, changed=False`；版本不变，不发事件，不写文件 |
| 编辑实际没改变什么（如描述设成原值），经 `apply_patch` | — | 仍产生新版本、写库、发 `graph.patched`（diff 为空）（实验确认，见疑点） | `committed=True, changed=True`，impact 为空 |
| 任意一处不合法 | — | 整份不生效：版本、节点、进程都不变 | `committed=False`，`error` 为阶段 4 原文 |

### 拒绝与异常

| 情况 | 处理 | 位置 |
|---|---|---|
| 运行未持有 / 已结束 | `RuntimeControlError`（服务层，`_apply` 里还有一次） | `service.py:329-333`；`mutations.py:189-190` |
| 提交时运行正在 shutdown | supervisor 抛 `RuntimeControlError`，原样抛给调用方，不是 `GraphCommitResult` | `supervisor.py:449-450` |
| 阶段 4 的状态检查或整图校验不通过 | `GraphCommitResult(committed=False, error=原文, error_kind="rejected")`，什么都没变 | `mutations.py:194-195` |
| `save_graph` 抛错 | 异常向外抛；此时还没切换图、没发 `graph.patched`，进程不受影响 | `supervisor.py:457-459`；`mutations.py:173` |
| 同一运行两次提交同时到达 | `commit_lock` 让它们先后执行；第二次看到第一次提交后的图和重写过的文件 | `mutations.py:126,330` |
| 一次替换还在等旧进程时又提交 | 新提交立即生效，不等前一次；那个节点在新结果的 `fates` 里仍是 `after_old_instance`，但不出现在新结果的 `waiting_for_old_instance` 里（实验确认） | `supervisor.py:717-723` |
| 新节点启动立即失败（进程起不来） | 记 `failed`；`fates` 里仍是 `started`（`_fate` 只区分 pending/skipped/其余） | `supervisor.py:717-723,796-801` |

### `GraphCommitResult`

| 字段 | 含义 | 约束/默认 |
|---|---|---|
| `committed` | 是否提交 | |
| `dry_run` | 是否只是预览 | False |
| `changed` | 有无改动 | False；真实提交成功一律 True（`mutations.py:206`） |
| `previous_version` / `version` | 提交前/后版本；未提交时都等于当前版本；dry run 时 `version` 是"将会是"的版本 | |
| `diff` | `GraphDiff`：`nodes.added/removed/modified/replaced`，`subscriptions.added/removed/rewired` | 空 |
| `fates` | 提交生效时所有 `pending` 节点（不只是本次涉及的）→ `started`/`waiting`/`never`/`after_old_instance` | `{}` |
| `waiting_for_old_instance` | 本次提交里被替换、旧进程尚未退出的节点 | `()` |
| `removed_downstream` | 每个被删节点 → 旧图里的下游闭包（不含自己） | `{}` |
| `impact` | 每处改动一行（格式见上表），由 `Graph.affected_by` 等查询算出 | `()` |
| `error` / `error_kind` | 拒绝原因原文；`format`/`stale`/`rejected`/`missing_file` | None |
| `describe()` | 有错：`not committed (<kind>): <error>`；无改动：`no changes; the graph stays at version N`；否则首行 `committed: version a -> b` 或 `dry run, nothing committed`，接 impact 各行，再按去向分组：`started`（dry run 写 `starts now`）、`waiting for start edges`、`can never start (skipped)`、`starts once the old instance has stopped` | `mutations.py:79-99` |

### dry run（`preview`）与真实提交的差别

| 方面 | dry run | 真实提交 | 位置 |
|---|---|---|---|
| 写库 / 发事件 / 写文件 | 都不做 | 都做 | `mutations.py:133-166` |
| 锁 | 在 `commit_graph_file` 的提交锁内，不进 supervisor 锁，用调用时刻的节点状态 | supervisor 锁内 | |
| 校验失败 | `rejected`，`dry_run=True` | `rejected` | `mutations.py:138-141` |
| 去向的依据 | `start_status(新图, 已结束的结局)`；被本次替换且旧实例在 `starting`/`running`/`paused`/`stopping` 的 → `after_old_instance` | 执行后各节点的实际状态（`_fate`） | `mutations.py:145-155`；`supervisor.py:717-723` |
| 前一次提交的替换还在等旧实例的节点 | 判定 READY 时报 `started`（实验确认，见疑点） | `after_old_instance` | |
| 整个运行暂停中 | 报 `starts now` | 以暂停状态启动，报 `started` | `supervisor.py:776` |
| `waiting_for_old_instance` | 空 | 本次替换中旧进程未退出的节点 | |

### `graph.json`：主 Agent 的草稿

格式 `emerge.graph-spec/v1`。运行时从不读它来驱动执行；"文件是不是运行时上次写的样子"用 `runs.graph_file_digest`（UTF-8 文本的 sha256）判断，每次写文件都同时更新库和 `handle.graph_file_digest`（`mutations.py:298-317`）。框架不覆盖可能有未提交改动的文件（计划"磁盘上的图文件"一节）。

| 情况 | 处理 | 位置 |
|---|---|---|
| `start_run` | 强制写一次（空图写 `"nodes": {}`） | `service.py:162` |
| `apply_patch` 提交成功 | 释放提交锁后按 `force=False` 写：文件不存在或摘要等于上次所写 → 重写为新版本；否则视为草稿，不动 | `mutations.py:128-129,309-310` |
| `commit_graph_file` 提交成功 | 锁内强制重写为新版本 | `mutations.py:355-356` |
| `commit_graph_file` 被拒 / 无改动 / dry run | 文件原样不动 | `mutations.py:333-353` |
| `reset_graph_file` | 锁内强制重写；已结束的运行也可以 | `mutations.py:360-363`；`service.py:215-218` |
| `resume_run` | `force=False`：无草稿则按（可能因重跑加一的）当前版本重写；有草稿则保留，报告里说明 | `checkpoint.py:140-149` |
| 节点状态变化、运行结束、`close_run` | 不写文件（文件只描述结构） | — |
| 程序 `apply_patch` 改了图时文件里有草稿 | 草稿保留；之后提交它因版本过期被拒（`stale`） | `mutations.py:309-310`；`emerge/graph/spec.py:306-311` |
| 文件被删 | `graph_file_status.exists=False`；提交得 `missing_file`；下一次非强制写会重新生成（不存在不算草稿） | `mutations.py:300-301,333-336` |
| 文件不写 `graph_version` | 不做过期检查，直接与当前图比较 | `emerge/graph/spec.py:306` |

`graph_file_status` 返回 `GraphFileStatus(path, exists, current_version, file_version, has_changes, stale, diff, error)`：

| 文件情况 | 返回 | 位置 |
|---|---|---|
| 不存在 | `exists=False`，`has_changes=False` | `mutations.py:370-371` |
| 摘要等于上次所写 | `file_version=当前版本`，`has_changes=False` | `mutations.py:372-373` |
| 解析失败 | `has_changes=True`，`error=解析错误`，`file_version=None` | `mutations.py:374-377` |
| 能解析 | `file_version`=文件里的版本；`stale`=版本非空且 ≠ 当前；去掉版本号后与当前图做纯结构比较，不传节点状态（所以运行中节点的参数改动在这里是 `modified params`，真正提交时是 `replaced`）；比较失败（如未知类型）→ `error`，`diff=None` | `mutations.py:378-387` |
| `has_changes` | `diff is None` 或 diff 非空 或 `stale`（只改了空白等、内容等价时为 False） | `mutations.py:393` |

## 关闭与接着跑：`checkpoint.py`

只服务一种情况：用户中途关掉会话，之后打开这个会话接着跑；不考虑崩溃，不留历史、不回档、不重放（决策记录［存储：退出后打开会话接着跑］，HANDOFF.md:284-290）。需要的东西在事情发生时已写进存储；`close` 只停进程，`resume` 读回来接着跑。由 `RuntimeService.close_run` / `resume_run` 调用。

### 会话关闭（`close`）

| 情况 | 处理 | 位置 |
|---|---|---|
| 主 Agent 一侧（阶段 8，先于 `close_run`） | `agent.detach()`：正在输出的回复取消且不写；正在执行的工具做完、结果照写；同一回复中未执行的工具调用补 `not executed: session closed`；分叉回答节点提问的任务取消 | `emerge/agent/session.py:14-18,109-118`；`emerge/agent/agent.py:350-374` |
| `close(handle)` | `supervisor.shutdown(closing=True)`，然后 `set_status("closed")` | `checkpoint.py:111-114` |
| 节点状态 | `_closing` 为真：`_set_state`、`_finish` 直接返回。库里每个节点保持关闭前的状态（`running` 仍是 `running`），不记 `stopped`、不触发启动边、不关闭输入、不发 `node.state`、不作废提问 | `supervisor.py:426-427,739-740,838-839` |
| 进程 | 每个未退出的进程收到 stop；等退出，超 `stop_timeout` 强杀；等后台任务（替换的 `_start_after_exit` 此时不再启动新实例）；关 bus | `supervisor.py:428-433` |
| 运行状态 | `closed`，`finished_at` 保持空，发 `run.status`；不发 `run.finished`，tracer 的文件保持打开到服务关闭 | `run.py:147-154` |
| 整个运行暂停中关闭 | `runs.paused` 保留为 1，打开后回到 `paused` | `run.py:112-116` |
| `graph.json` 草稿、排队消息、未答提问、端口消息、产物 | 原样留在磁盘和库里 | — |
| 之后（服务层） | 从 `_runs` 删除；`feedback.forget` 取消该运行的窗口，未发出的 batch 丢弃 | `service.py:176-177`；`feedback.py:132-135` |

### 打开会话（`resume`）流程

1. `resume_run`：已持有 → `RuntimeControlError`；`get_run` 为空 → `RuntimeControlError`（`service.py:182-186`）。
2. 状态不是 `closed` → `CheckpointError`（`checkpoint.py:123-124`）。
3. `make_handle(record)`：新 `Supervisor` + `RunHandle`；handle 状态仍是 `closed`，所以此后到第 9 步之间 `refresh_status`、`on_idle` 都不起作用（`checkpoint.py:125`）。
4. `load_run_state`：运行记录、最新的图（没有则用空图）、所有实例的节点状态、各端口保留的消息、`pending` 提问、未送达的排队消息（`checkpoint.py:127-129`；`emerge/storage/sqlite.py:990-1004`）。
5. `load_pinned_code`：图中每个带 `code` 且注册表里还没有该版本的节点，从 `code/<type>/v<N>.py` 注册；注册前该类型的当前实现若来自文件，再注册它一次，恢复为当前实现（`checkpoint.py:180-192`）。
6. 库里全部 `pending` 提问 `void_ask`（`checkpoint.py:132`）。
7. `Supervisor.continue_run(graph, run_id, 节点状态, paused=runs.paused, on_commit=commit_recorder(handle, "resume"))`（`checkpoint.py:133-139`，见下表）。
8. `write_graph_file(force=False)`；没写（有草稿）时读文件判断 `draft_stale`（`checkpoint.py:140-149`）。
9. `handle.reopen()`：`closed` → `paused`/`idle`/`running`（`checkpoint.py:150`）。
10. 组装 `ResumeReport`（`checkpoint.py:151-177`）。
11. 服务层登记 handle，`feedback.seed`（运行状态、各节点当前状态、库里此刻 `pending` 的提问，即新实例已经重新提出的）（`service.py:188-198`）。

关闭时正在跑的节点重跑而不是记失败、旧提问作废而不是重新送达，原因是用户会接入仿真并自己负责保存和恢复环境状态（决策记录［检查点：只做接着跑］"规则的变化"，HANDOFF.md:276-277）。

### 关闭时节点状态 → 打开后

| 关闭时（库里的记录） | 打开后 | 写库 / 事件 | 报告字段 | 位置 |
|---|---|---|---|---|
| 当前实例已结束（`finished`/`failed`/`stopped`/`skipped`） | 原样保留，不再跑，记录不改写；结局照样参与启动边（包括之后新加的边）；以它为来源的输入对新启动的订阅方是关闭的 | 无 | `kept` | `supervisor.py:271-272,659` |
| 当前实例 `starting`/`running`/`paused` | 全部收集后用一个只含 `replace_node`（`code` 沿用原固定版本）的 patch 得到新版本，新实例 id `<id>@v<新版本>`，替换历史有记录；`on_commit` 存图、发 `graph.patched{source: "resume"}`；旧实例补记 `stopped`（错误 "the session was closed while this instance was <状态>; a new instance runs instead"；已非当前实例 → 只发 `node.stale`，不触发启动边、不关输入）；新实例登记后在统一判断时启动；原为 `paused` 的、或整个运行暂停的，以暂停状态启动（`starting → paused`）；不可逆节点同样重跑，不询问用户 | 旧实例 `stopped`；新实例 `pending`→`starting`… | `rerun`（`was`、`paused`） | `supervisor.py:278-315` |
| 当前实例 `stopping` | 记 `stopped`（错误 "the session was closed while the node was stopping"），不再启动；它是当前实例，发 `node.state`，主 Agent 立即收到 `node_ended` | `stopped` | `stopped` | `supervisor.py:273-277` |
| 当前实例 `pending` | 照常等，`start_status` 重新判断：满足 → 启动（整个运行暂停则暂停启动），启动时排队消息按 `seq` 交给它；再也满足不了 → `skipped`；否则等待 | 视结果 | `started`/`never`/`waiting` | `supervisor.py:316-317,323` |
| 当前节点在库里没有记录 | 按新节点登记 `pending`，同上 | `pending` | 同上 | `supervisor.py:269-270` |
| 被替换的旧实例仍是 `starting`/`running`/`paused`/`stopping`（关闭时替换还在等旧进程） | 补记 `stopped`（错误 "the session was closed while this replaced instance was <状态>"），只发 `node.stale`；它的新实例按 `pending` 处理 | `stopped` | 不出现 | `supervisor.py:258-263` |
| 关闭前整个运行暂停 | `_run_paused` 恢复为真，本次启动的节点都以暂停状态启动；运行回到 `paused` | | `status="paused"`；`rerun[].paused=True` | `supervisor.py:240`；`run.py:130-131` |

### 接续的内容

| 内容 | 怎么接续 | 位置 |
|---|---|---|
| 时间线编号 | 存储每个运行一个计数器（`counters` 表的 `timeline:<run_id>`）；取号是单独一个事务，盖了这个号的记录随后另一个事务写，两步都在 bus 的 `_timeline` 锁内（见 05「盖章」）。重开后接着往上加，不重复、不倒退，可能有空号 | `emerge/storage/sqlite.py:981-986` |
| 端口版本号 | 新 bus 启动时取库里每个端口最新一条的版本号，继续 +1 | `bus.py:181-184` |
| 端口消息 | 留在库里（每端口最近若干条）；新实例的输入从来源现存最早一条读起 | `bus.py:464-480` |
| 主 Agent 消息的 `seq` | 新 bus 从排队表（含已送达的行）取每个实例的最大 `seq` 续编；重跑的新实例从 1 开始 | `bus.py:185-188` |
| 排队给节点的消息 | 仍在排队表里；实例启动 `attach` 时按 `seq` 交出、标记送达，重开前后是同一机制 | `bus.py:204-211` |
| 未回答的提问 | 全部作废并列入 `voided_asks`；新实例需要会重新问，旧 ask id 再回答报 "not waiting" | `checkpoint.py:132` |
| 状态快照 | 按实例存；新实例没有 | — |
| 产物与引用 | 文件留在原处，旧引用仍归属旧实例；对每条保留的端口消息的每个引用检查 `runs/<run>/nodes/<producer>/<producer_instance>/<port>/<key>`，不在就记入 `unavailable_refs`，不改库、不找替代 | `checkpoint.py:195-213` |
| env 恢复钩子 | 没有。框架不记录环境动作、不预留仿真状态的保存/恢复接口（决策记录［检查点：只做接着跑］"不预留仿真环境的接口"，HANDOFF.md:262；计划 checkpoint 一节"不记录环境动作"） | — |

### `ResumeReport`

| 字段 | 含义 |
|---|---|
| `run_id`, `status`, `graph_version` | 打开后的状态与版本（有重跑时比关闭前大 1） |
| `kept` | 关闭前已结束、原样保留的节点 |
| `rerun` | `RerunEntry(node_id, old_instance_id, new_instance_id, was, paused)` |
| `stopped` | 关闭时在 `stopping`、现记 `stopped` 的节点 |
| `started` / `waiting` / `never` | 关闭前没开始的节点此刻的去向（不含重跑的节点） |
| `voided_asks` | 作废的提问（`AskRecord`） |
| `queued_messages` | 库里未送达的排队消息条数（统计口径见疑点） |
| `draft_kept` | `graph.json` 里有未提交的草稿、被保留 |
| `draft_stale` | 草稿的 `graph_version` 非空且 ≠ 当前版本；有节点重跑（版本加一）时必然为真；文件解析失败时为 False |
| `unavailable_refs` | `UnavailableRef(node, port, version, producer, producer_instance, key, path)` |
| `describe()` | 首行 `run X reopened at graph version N, now <status>`，再逐项列 kept、每个 rerun、stopped、started、waiting、never、作废的提问、排队消息条数、草稿及是否过期、不可用的引用文件；阶段 8 把它追加到对话里 |

### 异常情况

| 情况 | 处理 | 位置 |
|---|---|---|
| 固定版本的代码文件缺失 / 文件里没有节点类 | `register_from_file` 抛 `RegistryError`，向外抛；此时库没动，运行仍 `closed`，修好后可重试 | `checkpoint.py:190`；`emerge/graph/registry.py:230-231,265-266` |
| 重跑补丁校验失败（如库里图中某个节点类型在本进程没注册） | `GraphValidationError` 向外抛；提问已作废，新 bus 的分发任务没人关闭，handle 未登记，运行仍 `closed`（实验确认，见疑点） | `supervisor.py:291`；`checkpoint.py:132-139` |
| 库里没有图 | 用空图接着跑 | `checkpoint.py:129` |
| 没有需要重跑的节点 | 不做补丁，版本不变，不发 `graph.patched` | `supervisor.py:281` |
| 草稿文件解析失败 | `draft_kept=True`，`draft_stale=False` | `checkpoint.py:148-149` |

## 反馈：`feedback.py`

`FeedbackHandler` 挂在回调管理器上，看运行事件，把主 Agent 需要知道的事包装成 `Feedback`，经注入的回调交出（不 import 主 Agent），没有回调时放进 `handler.queue`。它只决定多快推，不改写节点汇报的内容。每个 `RuntimeService` 一个，所有运行共用；每个运行在里面有一个视图（状态、节点、待答提问、最近事件、当前窗口）。

### 参数

| 参数 | 含义 | 默认 |
|---|---|---|
| `deliver` | 回调，可同步可异步；None 时 `put_nowait` 进 `queue`（无上限） | None（服务传 `on_feedback`） |
| `interval` | 合并窗口（秒） | 1.0（服务传 `feedback_interval`） |
| `recent` | 摘要里"最近的重要事件"保留条数 | 20（服务不传） |
| `max_chars` | 摘要字符上限 | 4000（服务传 `summary_chars`） |

### 事件 → 推送

| 事件 | 推给主 Agent | 合并 / 节流 | 推送内容（`kind`：`text`） | 对视图 / 摘要的影响 | 位置 |
|---|---|---|---|---|---|
| `node.state` → `finished`（当前实例） | 立即 | 先发出窗口里已攒的 batch | `node_ended`：`<id>[ [irreversible]] finished: <outcome>[ (<reason>)]`；`data={state, outcome, reason, error}` | 更新节点视图；删除该实例的提问；记入 recent | `feedback.py:187-192,281-285` |
| `node.state` → `failed`/`stopped`（当前实例） | 立即 | 同上 | `node_ended`：`<id>[ [irreversible]] <state>[: <error>]` | 同上 | `feedback.py:187-192,288-289` |
| `node.state` → `skipped`（当前实例） | 立即 | 同上 | `node_skipped`：`<id> can never start: none of its start edges can fire any more (skipped)` | 更新节点视图；记入 recent | `feedback.py:193-196` |
| `node.state` → `pending`/`starting`/`running`/`paused`/`stopping` | 合并 | 按（节点, `state`）计数，留最后一个状态 | 窗口到时一条 `batch`，每行 `<id>: [N x ]state, latest: <state>` | 更新节点视图 | `feedback.py:197-198,240-261` |
| `node.report` | 合并 | 按（节点, `report`）计数，留最后一条文字 | 同一条 `batch` 里 `<id>: [N x ]report, latest: <text>` | 不进摘要 | `feedback.py:177-178` |
| `node.ask` | 立即 | 先发 batch | `ask`：`<id> asks (<ask_id>): <question>`；`data={ask_id, question}` | 记入待答提问和 recent | `feedback.py:151-158` |
| `node.answer` | 否 | — | — | 从待答提问删除 | `feedback.py:159-160` |
| `node.stale`，`kind=state` 且状态为结束或 `skipped`（被替换的旧实例、被删除的节点） | 否 | — | — | 删除该实例的提问；节点视图不动（见疑点） | `feedback.py:148-150` |
| `run.idle` | 立即 | 先发 batch | `graph_idle`：`the graph is idle: every node has ended (graph version N); add nodes to go on or end the run` | 记入 recent | `feedback.py:161-172` |
| `run.status` | 否 | — | — | 视图状态（摘要首行） | `feedback.py:173-174` |
| `graph.patched` | 否 | — | — | recent 记 `graph changed: version a -> b` | `feedback.py:175-176` |
| `port.published`、`node.custom`、`model.*`、`tool.*`、`run.started`、`run.finished`、`node.main_agent_message`、`agent.*` | 否 | — | — | 无（但会为该运行建视图） | `feedback.py:139-178` |
| `run_id` 为 None 的事件 | 忽略 | — | — | — | `feedback.py:140-141` |

阶段 8 去掉了 `node.custom`、`port.published` 的合并推送；非结束的状态变化照旧按窗口合并推送，这是用户 2026-10-30 的更正（决策记录［阶段 8］，HANDOFF.md:782-783）。

### 窗口与顺序

| 情况 | 处理 | 位置 |
|---|---|---|
| 窗口里第一条可合并事件到达 | 为该运行起一个后台任务：睡 `interval` 秒后把攒下的全部合并成一条 `batch` | `feedback.py:240-250` |
| 窗口内同一（节点, 种类）多次 | 只计数并保留最后的文字/状态；`data["<node>/<kind>"] = {count, latest}` | `feedback.py:241-243,260` |
| 窗口没到就来了立即推送的事件 | 取消窗口任务，先发已攒的 batch，再发这条 | `feedback.py:263-269` |
| 多个运行同时推送 | `_push_lock` 串行，一次交出一条；异步回调要等它完成才发下一条 | `feedback.py:271-278` |
| 回调为 None | 放进 `queue` | `feedback.py:273-275` |
| 回调抛错 | 经事件处理路径时由回调管理器记日志并继续处理后续事件；经窗口任务时异常留在任务里无人取，该 batch 丢失 | `emerge/callbacks/manager.py:36-39`；`feedback.py:247-250` |
| handler 关闭（管理器关闭或 `remove_handler`） | 取消所有窗口，立即发出各运行攒下的 batch | `feedback.py:223-228` |
| `forget(run_id)`（`close_run` 时） | 删除视图、取消窗口，未发出的 batch 丢弃；之后到达的该运行事件（如还在排队的 `run.status closed`）会重新建视图（实验确认，见疑点） | `feedback.py:132-135,230-234` |
| `seed(...)`（`resume_run` 时） | 设视图状态、各节点（state/instance/outcome/reason/error）、待答提问；不清 recent 和窗口 | `feedback.py:106-130` |
| `end_run` / `stop(run_id)` | 每个被停的节点照常立即推 `node_ended`（stopped），启动边再也满足不了的推 `node_skipped` | `supervisor.py:416-433,751-758` |
| 打开会话时有节点重跑 | 新实例的 `pending`/`starting`/`running` 进 batch，主 Agent 被叫醒一次；`stopping` 改记 `stopped` 的立即推 | `supervisor.py:273-315` |

### `summary(run_id, *, max_chars=None)`

| 部分 | 内容 | 位置 |
|---|---|---|
| 首行 | `run <id>: <status>`（最近一次 `run.status`；没有视图时默认 `running`） | `feedback.py:205-206` |
| 待答提问 | `questions waiting for an answer:`，每条 `  <ask_id> from <node>: <question>` | `feedback.py:207-209` |
| 节点 | `nodes:`，先列未结束的 `  <id>: <state>`，再列已结束的 `  <id> finished: <outcome> (<reason>)` / `  <id> <failed 或 stopped>: <error>` / `  <id>: skipped`（不标 irreversible）；顺序是节点第一次出现在事件里的顺序；只来自事件和 `seed`，不读图 | `feedback.py:210-215` |
| 最近事件 | `recent:`，最近 20 条：节点结束/跳过、提问、图已空闲、图版本变化；不含 report 和状态 batch | `feedback.py:216-218` |
| 超长 | 按整行截断，末尾加 `... N more line(s) left out`，再按字符硬截到上限 | `feedback.py:292-305` |

### `Feedback`

| 字段 | 含义 | 约束/默认 |
|---|---|---|
| `run_id` | 运行 | |
| `kind` | `node_ended`/`node_skipped`/`graph_idle`/`ask`/`batch` | |
| `text` | 一到几行文字，可直接放进上下文 | |
| `node_id` | `batch`、`graph_idle` 为 None | None |
| `data` | 见上表 | `{}` |
| `timestamp` | 生成时间 | `time.time()` |

## 输出订阅：`stream.py`

`StreamHandler` 是另一个回调 handler，给外部调用方（TUI、headless 脚本）按自己的节奏读运行输出：每个订阅者一个队列，事件按模式放入。`RuntimeService.stream(run_id, modes)` 先确认运行被持有，再 `subscribe(modes, run_id=run_id)`；`values` 快照由服务的 `_values` 提供。

### 模式

| 模式 | 包含的事件 | 何时生效 | 过滤 |
|---|---|---|---|
| `messages` | `model.token`（payload 带 `kind`、`part`） | `subscribe()` 调用时就开始入队，不是第一次迭代时 | 给了 `run_id` 只收该运行的事件；`None` 收所有运行及无 run_id 的事件（服务的 `stream()` 总给 run_id） |
| `updates` | `port.published`、`node.state` | 同上 | 同上 |
| `values` | 只有 `stream.values` 快照，不含原始事件：第一次迭代时现算一份；之后每个 `port.published`/`node.state`/`graph.patched` 之后算一份，放给该运行所有 `values` 订阅者 | 同上；第一份在第一次迭代时 | 必须给 `run_id` |
| `custom` | `node.custom`（节点 `ctx.emit_event`） | 同上 | 同上 |
| `debug` | 全部原始事件（`run.*`、`node.report`/`ask`/`answer`/`stale`/`main_agent_message`、`graph.patched`、`model.*`、`tool.*` 等），不含 `stream.values` | 同上 | 同上 |

`stream.values` 的 payload：`graph_version`；`graph`（图文件形式，去掉 None）；`nodes`（每个节点的 `instance_id, state, outcome, reason, error`）；`ports`（`"<node>.<port>"` → 最新一条消息）。运行已不被持有时 payload 为 `{}`（`service.py:360-378`）。`node.report`、`node.ask`、`run.idle`、`run.status` 只在 `debug` 里出现。

### 订阅的情况

| 情况 | 处理 | 位置 |
|---|---|---|
| 模式为空或含未知模式 | `RuntimeControlError`（列出允许的模式）；`modes` 传成字符串会被拆成字符，同样报错 | `stream.py:112-115` |
| `values` 不给 run_id | `RuntimeControlError` | `stream.py:116-117` |
| 运行未被持有（含已 close） | `RuntimeControlError`（服务层） | `service.py:314` |
| 订阅了多个模式，一个事件符合其中几个 | 只入队一次 | `stream.py:132-135` |
| 多个订阅者 | 各自一个队列；快照每个触发事件只算一次，放给该运行所有 `values` 订阅者 | `stream.py:129-143` |
| 消费者读得慢 | 队列无上限，事件一直累积 | `stream.py:51,82-84` |
| 第一次迭代前已有变化 | 第一次迭代先返回现算的快照，之后才交出队列里更早的快照（实验确认，见疑点） | `stream.py:60-66` |
| `aclose()` 或 `async with` 退出 | 从订阅者列表移除、丢弃队列；之后的迭代立即结束 | `stream.py:76-90` |
| 另一个 task 正阻塞在 `async for` 时 `aclose()` | 那次等待不会被唤醒（实验确认，见疑点） | `stream.py:67,86-90` |
| 不 `aclose()` 就丢弃对象（如 `break` 出循环） | `__del__` 想移除，但 handler 的列表持有强引用，对象不会被回收，订阅一直有效（实验确认，见疑点） | `stream.py:92-96,104,119` |
| handler 关闭（服务 `aclose`） | 给每个订阅者放结束标记：先读完已入队的事件，再结束迭代并释放 | `stream.py:145-147,68-70` |
| 运行 `close_run` 之后 | 订阅仍在，只是不再有该运行的事件，直到服务关闭才结束 | — |

## 导出：`__init__.py`

| 名字 | 来源 | 阶段 |
|---|---|---|
| `NODE_EVENT_TYPES`, `NODE_STALE`, `Channel`, `Kind`, `MessageBus` | `bus.py` | 6 |
| `RerunEntry`, `ResumeReport`, `UnavailableRef` | `checkpoint.py` | 7 |
| `RuntimeNodeContext` | `context.py` | 6 |
| `Feedback`, `FeedbackHandler`, `FeedbackKind` | `feedback.py` | 7 |
| `GraphCommitResult`, `GraphFileStatus` | `mutations.py` | 7 |
| `StopPolicy` | `policies.py` | 6 |
| `LOG_FILE`, `NodeLaunch`, `NodeProcess`, `load_node_class`, `node_process_main` | `process.py` | 6 |
| `RUN_IDLE`, `RUN_STATUS`, `Run`, `RunHandle` | `run.py` | 7 |
| `NodeDelivery`, `NodeDetail`, `NodeView`, `RunStatusReport`, `RuntimeService` | `service.py` | 7 |
| `REPLACEABLE_STATES`, `TERMINAL_STATES`, `LifecycleState`, `NodeRuntimeState`, `can_transition`, `check_transition` | `state.py` | 6 |
| `STREAM_MODES`, `VALUES_EVENT`, `StreamHandler`, `StreamMode`, `Subscription` | `stream.py` | 7 |
| `ContinueExecution`, `NodeFate`, `PatchExecution`, `RerunNode`, `Supervisor` | `supervisor.py` | 6（前四个是阶段 7 加的） |

未导出：`run.ENDED_STATUSES`、`run.create_run`（主 Agent 直接 `from emerge.runtime.run import ENDED_STATUSES`）；`mutations` 的函数（`apply`、`preview`、`commit_graph_file`、`write_graph_file`、`graph_file_status`、`reset_graph_file`、`render_graph_file`、`commit_recorder`、`holds_draft`）；`checkpoint.close`、`resume`、`load_pinned_code`；`supervisor.OnCommit`；`stream.ValuesProvider`。`mutations.__all__` 另外重导出 `NodeFate`（`mutations.py:36-47`）。

## 疑点（待核实）

1. **订阅对象丢弃后不会退订**：`Subscription.__del__` 想在对象被丢弃时退订（`stream.py:92-96`），但 `StreamHandler._subscribers` 是强引用列表（`stream.py:104,119`），对象不会被回收。实验：`del sub; gc.collect()` 后 `subscriber_count` 仍为 1；队列无上限，会一直累积。与决策记录（HANDOFF.md:760"对象被丢弃时也会从订阅者列表里去掉"）不符。
2. **另一个 task 调 `aclose()` 唤不醒等待中的迭代**：`aclose()` 只把 `_queue` 置空、不放结束标记（`stream.py:86-90`），已在 `await self._queue.get()`（`stream.py:67`）的那次等待一直挂着。实验：`aclose()` 后 0.5 秒消费 task 仍未结束；调用方只能取消 task。
3. **`values` 的第一份快照可能比后面几份新**：第一次迭代时现算快照（`stream.py:60-66`），订阅后、第一次迭代前的变化已经把当时的快照放进队列（`stream.py:136-143`），消费者先看到新状态、后看到旧状态。实验：连续三项里 `t.out` 的版本依次为 4、无、1。
4. **dry run 把还在等旧实例的节点报成 `started`**：`preview` 只认本次 patch 里的替换（`mutations.py:146,150-155`），不看 supervisor 的 `_holding`；真实提交用 `_fate`，报 `after_old_instance`（`supervisor.py:717-723`）。实验：替换一个 stubborn 节点后再提交一个无关的新增，dry run 得 `{'x': 'started'}`，真实提交得 `{'x': 'after_old_instance'}`。主 Agent 调 `commit_graph(dry_run=True)` 时看到的就是前者。
5. **没改变任何东西的编辑也产生新版本**：`apply_graph_patch` 只对"没有编辑"的 patch 返回原图（`emerge/graph/patch.py:156-157`），其余一律版本加一（`emerge/graph/patch.py:161-162`）；`_committed` 固定 `changed=True`（`mutations.py:206`）。实验：把 running 节点的描述设成原值，结果 `committed=True, changed=True`，版本 0→1，diff 为空，发了 `graph.patched`。与 `changed` 的字段说明（`mutations.py:60`）不符；只影响 `apply_patch`，文件提交算出的 patch 只含真实改动。
6. **恢复报告的排队消息条数口径不对**：`queued_messages=len(state.queued_messages)`（`checkpoint.py:172`）取整个运行所有未送达的行（`emerge/storage/sqlite.py:1003`），包括永远不会启动的实例（被删除的 `pending` 节点、`pending` 时被替换的旧实例）的消息，也包括本次打开时实例一启动就已交出的消息（`continue_run` 已先 `attach`）。实验：给 `pending` 节点 p 发消息、删掉 p、关闭再打开，报告仍写 "1 queued message(s) wait for nodes that have not started"。
7. **被删除的 `pending` 节点在运行摘要里一直是 `pending`**：删除时在非当前实例下记 `skipped`，bus 只发 `node.stale`（`supervisor.py:463-467`；`bus.py:246-249`），feedback 对 `node.stale` 只删提问（`feedback.py:148-150`），节点视图既不更新也不删除。实验：删除 p 后图里只剩 v，摘要仍列 `p: pending`。
8. **全新进程里打开会话后，生成类型的"当前实现"可能退回旧版本**：`register` 总把新注册的版本设为当前实现（`emerge/graph/registry.py:203`），`load_pinned_code` 只在注册前已有来自文件的当前实现时才还原（`checkpoint.py:189-192`）。全新进程里生成类型都还没注册：只有节点固定 v1、v2 文件已存在时，当前实现变成 v1（v2 不会被载入）；两个节点分别固定 v1、v2 时，还原步骤又把先载入的 v1 设回当前，结果取决于节点在图中的顺序。实验（图中依次是固定 v1 的 x、固定 v2 的 y）：结束后 `find_entry("gen_echo")` 是 v1，`pin("gen_echo")` 返回 "1"，之后不写 `code` 的新节点会固定到 v1。与函数说明"目录显示的当前实现不变"（`checkpoint.py:183-184`）不符。
9. **重新打开一个已全部结束的运行，不推"图已空闲"**：`continue_run` 判定空闲时调度 `on_idle`，执行时 handle 状态仍是 `closed`，早返回（`run.py:139`），之后 `reopen()` 只改状态（`run.py:124-127`）。实验：重开后状态 `idle`，`run.idle` 0 条、反馈 0 条。计划 run.py 一条要求所有节点终止时立即通知主 Agent；这里只有恢复报告首行写了 `now idle`。
10. **`forget` 被还在排队的事件撤销；没见过的 run 摘要写 `running`**：`close_run` 在 feedback 处理 `run.status closed` 之前就 `forget`（`service.py:175-177`），事件随后经 `_view` 重建视图，状态 `closed`、没有节点（`feedback.py:142,230-234`）。`summary()` 对没有视图的 run_id 返回 `run <id>: running`（`feedback.py:205`）。实验均确认；主 Agent 只对持有的运行取摘要，影响小。
11. **中途失败不回滚**：`resume_run` 中 `continue_run` 抛错时，提问已作废（`checkpoint.py:132`），新 bus 的分发任务已启动且无人关闭（`supervisor.py:235-238`），handle 未登记，运行仍 `closed`。实验：删掉一个类型的注册后 `resume_run` 抛 `GraphValidationError`，库里 `pending` 提问 0 条，`bus:<run_id>` 任务仍在。`start_run` 在登记 handle（`service.py:158`）之后任一步抛错，handle 同样留在 `_runs` 里。
12. **`end_run` 之后留下 `pending` 的新实例**：替换还在等旧进程时结束运行，`_start_after_exit` 的 `_advance` 因 shutdown 不启动新实例，新实例也不是 NEVER，于是库里一直是 `pending`（`supervisor.py:704-712,759-769`）。实验：`finished` 的运行里 `x@v1` 为 `pending`。

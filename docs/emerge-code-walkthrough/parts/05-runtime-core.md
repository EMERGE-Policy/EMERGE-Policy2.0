# Runtime 核心（阶段 6）

## 概览

| 文件 | 职责 |
|---|---|
| `state.py` | 9 个生命周期状态、合法转换表、每个实例一条的 `NodeRuntimeState` |
| `policies.py` | `StopPolicy`：停止超时 |
| `process.py` | `NodeLaunch`（启动参数）、`NodeProcess`（主进程里的进程句柄）、子进程入口（setup → run → teardown → 结束报告） |
| `bus.py` | 管道两端共用的 `Channel`；主进程里的 `MessageBus`：盖章、写库、路由、过滤陈旧实例 |
| `context.py` | 子进程里的 `RuntimeNodeContext`，即节点拿到的 `ctx` |
| `supervisor.py` | 按启动边启动、监控、停止、暂停、替换节点进程；整份改图 `apply_patch`；接着跑 `continue_run` |

主进程跑 `Supervisor` 和 `MessageBus`（一个分发任务，外加每个节点进程一对管道读写线程）；每个节点实例一个 spawn 子进程，跑 `node_process_main`：`RuntimeNodeContext` 加节点的 `setup/run/teardown`，同样一对管道读写线程。下文"位置"里不带目录的文件都在 `emerge/runtime/` 下。

## 生命周期状态：`state.py`

每个节点实例一条状态记录。只有 supervisor 通过 `NodeRuntimeState.transition` 改状态，非法转换抛 `RuntimeControlError`。

### 状态与集合

| 名称 | 内容 | 位置 |
|---|---|---|
| `LifecycleState` | pending、starting、running、paused、stopping、finished、failed、stopped、skipped | state.py:20-30 |
| `TERMINAL_STATES` | finished、failed、stopped、skipped；取自 `emerge/graph/patch.py:35` | state.py:35 |
| `REPLACEABLE_STATES` | pending、starting、running、paused；取自 `emerge/graph/patch.py:31` | state.py:36 |
| 导入时核对 | patch 里的名字不在枚举中 → `ValueError`；`storage.base.NodeStateName` 与枚举不一致 → `RuntimeError` | state.py:35-38 |
| supervisor 用的集合 | `_PAUSABLE` = {starting, running}；`_STOPPABLE` = {starting, running, paused}；`_INTERRUPTED` = {starting, running, paused} | supervisor.py:46-49 |

### 合法转换（state.py:41-51）

| 当前 | 可以转到 |
|---|---|
| pending | starting、skipped |
| starting | running、paused、stopping、failed、stopped |
| running | paused、stopping、finished、failed、stopped |
| paused | running、stopping、finished、failed、stopped |
| stopping | stopped |
| finished / failed / stopped / skipped | 无 |

### 转换由谁触发、有什么副作用

| 当前状态 | 触发 | 新状态 | 副作用 | 位置 |
|---|---|---|---|---|
| （无记录） | `_register`：start、continue_run（库里没有这一行）、apply_patch 新增或替换、add_node、replace_instance | pending | 写库；当前实例盖章发 `node.state`（previous=None）；清掉空闲标记 | supervisor.py:725-736 |
| pending | `_advance` 判 READY，且不在 `_holding`、不在关闭中，`_launch` 成功 | starting（带 pid） | 起进程、`bus.attach`（交出排队消息）、写库、`node.state` | supervisor.py:759-769, 803-807 |
| pending | `_launch` 抛 `Exception`（类型找不到、spawn 失败等） | starting，紧接 failed | 两条 `node.state`，error=`cannot start: <类型>: <信息>`；关闭它喂的输入；嵌套一次 `_advance` | supervisor.py:797-802 |
| pending | `_advance` 判 NEVER | skipped | `node.state`；给订阅它的在跑进程发关闭 | supervisor.py:754-758 |
| pending | apply_patch 删除节点、单项 `remove_node` | skipped | 此时已不在图中，只写库 + `node.stale` | supervisor.py:463-467, 511-513 |
| pending | 被替换（`_swap_instance`、`replace_instance`） | skipped | 同上（旧实例已不是当前实例） | supervisor.py:692-696, 589-590 |
| starting | READY，实例不在 `_launch_paused` | running | `node.state` | supervisor.py:604-610 |
| starting | READY，实例在 `_launch_paused`（以暂停状态启动） | paused | 移出 `_launch_paused`；`node.state` | supervisor.py:605-608 |
| starting / running | `pause`、`pause_all` | paused | 移出 `_launch_paused`；`node.state`；发 `control pause` | supervisor.py:809-812 |
| paused | `resume`、`resume_all` | running | `node.state`；发 `control resume` | supervisor.py:819-821 |
| starting / running / paused | `stop`、`shutdown`、被替换（都走 `_request_stop`） | stopping | `stop_requested=True`；发 `control stop`；被替换时这已是陈旧记录，只发 `node.stale` | supervisor.py:823-827 |
| starting / running / paused | 进程结束，没被要求停止 | finished / failed / stopped（见 process 一节的结束表） | 写库、`node.state`、作废提问、关闭它喂的输入、`_advance` | supervisor.py:837-848 |
| stopping | 进程结束（任何方式，含强杀） | stopped | 同上；陈旧实例只写库 + `node.stale`，不关输入、不推进 | supervisor.py:854-867 |
| starting / running / paused / stopping，库里读出，已不是当前实例 | `continue_run` | stopped | error=`the session was closed while this replaced instance was <state>`；`node.stale` | supervisor.py:258-263 |
| stopping，库里读出，当前实例 | `continue_run` | stopped | error=`the session was closed while the node was stopping`；`node.state` | supervisor.py:273-277 |
| starting / running / paused，库里读出，当前实例 | `continue_run`，先替换再记 | stopped | error=`... was <state>; a new instance runs instead`；此时已是陈旧实例，只发 `node.stale`；新实例 pending | supervisor.py:296-306 |
| 任意 | 会话关闭中（`_closing`） | 不变 | `_set_state` 直接返回：不写库、不发事件、内存记录也不改 | supervisor.py:739-740 |

### `NodeRuntimeState`（state.py:63-105）

| 字段 | 含义 | 约束/默认 |
|---|---|---|
| node_id、instance_id | 逻辑 id、实例 id | 必填 |
| state | 当前状态 | 默认 pending |
| pid | 进程号 | 转换时不给就沿用；不写库（`set_node_state` 没有这个参数），只出现在 `node.state` 里 |
| outcome | 结局名 | 只有 finished 有 |
| reason | 结局理由 | 只在转到 finished 时保留，其余转换都清成 "" |
| error | 最近一次错误 | 转换时给 None 就沿用旧值 |
| changed_at | 最近转换时刻 | `time.time()`；continue_run 装入时用库里的 `updated_at` |
| terminal（属性） | 是否终止 | state ∈ `TERMINAL_STATES` |

`frozen=True, extra="forbid"`：每次转换返回新对象，supervisor 用它替换 `_records[instance_id]`。

### 非法转换与字段规则

| 情况 | 处理 | 位置 |
|---|---|---|
| 目标不在转换表里（含从终止状态出发） | `check_transition` 抛 `RuntimeControlError("illegal node state transition a -> b")`；`_set_state` 不捕获，抛给调用方；若发生在 bus 分发任务里，只记一条日志 | state.py:58-60, 93; bus.py:347-348 |
| 带 outcome 但目标不是 finished | `RuntimeControlError` | state.py:94-95 |
| 正常路径会不会撞上 | 不会：READY 总在 EXIT 之前到（同一管道、同一分发任务），所以没有 starting→finished；被要求停止后一律记 stopped，所以没有 stopping→finished/failed | process.py:183; supervisor.py:854 |

## 停止策略：`policies.py`

只有一个值：从发出停止到强杀的秒数。节点结束后不再启动，所以这里没有重启策略（policies.py:1-5）。

| 项 | 内容 | 位置 |
|---|---|---|
| `StopPolicy.stop_timeout` | 发停止后等进程退出的上限，超过就强杀并记 stopped | policies.py:14-20 |
| 来源 | `StopPolicy.from_config(config.runtime)` → `RuntimeConfig.stop_timeout`，默认 30，须 > 0；`Supervisor(policy=...)` 可覆盖，服务层把自己的 `policy`（默认 None）透传 | policies.py:22-24; `emerge/config.py:121`; supervisor.py:126; service.py:322 |
| 用在哪 | `_await_exit` 的等待上限；强杀后的错误文字 | supervisor.py:831, 866 |
| 不在这里的时间值 | 没收到 EXIT 就断开时等退出码的 1 s 是写死的；`wait_idle` 和 ctx 各等待的 timeout 由调用方给 | supervisor.py:620 |

## 节点进程：`process.py`

supervisor 为每个要启动的实例建一个 `NodeProcess`，用 spawn 起子进程；子进程入口 `node_process_main` 加载节点类、依次跑 setup/run/teardown、发结束报告。报告怎么变成状态由 supervisor 的 `_final_state` 决定，因为只有主进程知道是否请求过停止（process.py:1-8）。

### `NodeLaunch`（process.py:39-69）

| 字段 | 含义 | 约束/默认 |
|---|---|---|
| run_id、node_id、instance_id、node_type | 身份与类型名 | |
| locator | `NodeLocator.to_dict()`：module 型给模块名；file 型给 path、version、digest | 实例固定在这个实现上 |
| params | `thaw(spec.params)` | 子进程里用 `cls.Params.model_validate` 校验；`Params` 为 None 时忽略 |
| description | 主 Agent 写的实例说明 | |
| inputs | 每个声明的输入 → `{source: "node.port" 或 None, closed, start}`，由 `_input_state(from_now=False)` 算，start 总是 None | 之后的变化走 INPUT 消息 |
| workspace、session_dir、config | 工作区路径；运行所属对话的文件夹 `<工作区>/sessions/<id>/`（取自 `storage.session_dir`，supervisor.py:789-790）；`WorkspaceConfig.model_dump(mode="json")` | 子进程用 workspace 还原 `config.workspace`，用 session_dir 定产物目录和图片根目录 |
| port_buffer | 每路输入在进程内保留的条数 | = `config.runtime.port_buffer` |
| paused | 以暂停状态启动 | 默认 False |

整个对象是 JSON-ready dict（`to_dict` / `from_dict`）；跨进程只传它和管道的一端。

### `NodeProcess`（主进程里的句柄，process.py:72-143）

| 属性/方法 | 含义 | 约束/默认 |
|---|---|---|
| acts_on_environment | 来自 registry entry | 只用于事件标记，不影响状态 |
| stop_requested | supervisor 发过停止 | `_request_stop` 设 True |
| forced | 被强杀过 | `kill()` 设 True |
| exit_report | 子进程 EXIT 的内容 | 没发就是 None |
| exited | `asyncio.Event`：进程已回收、管道已关 | 只在 `reap()` 末尾设 |
| `start(on_message)` | spawn 上下文建双向 Pipe；在线程里 `process.start()`；关掉父进程手里的子端；建 Channel 并启动 | `process.start()` 失败：关父端，异常抛出 |
| `send(message)` | 交给 Channel 排队 | Channel 不存在或 `exited` 已设 → 静默丢弃 |
| `control(op)` | 发 `{"kind": "control", "op": op}` | |
| `kill()` | forced=True，SIGKILL | 进程对象不存在时只设标记 |
| `join(timeout)` | 在线程里 join | |
| `reap()` | join（无超时）→ 线程里关 Channel → 设 `exited` | supervisor 的 `_reap` 调用 |

### 子进程里的执行顺序

1. `node_process_main`：忽略 SIGINT（终端 Ctrl+C 发给整个进程组，退出由主进程经 bus 决定），`asyncio.run(_serve(...))`（process.py:149-154）。
2. `_serve`：建 Channel（收到的每条交给 `ctx.receive`）→ 建 `RuntimeNodeContext`（校验 config、创建 `artifacts_dir`）→ `_capture_output` 把输出改到 `node.log` → 启动管道线程（process.py:157-162）。
3. `_run`：`load_node_class` → `ctx.bind(cls)`（记下结局与输出端口）→ `cls(Params.model_validate(params))`（process.py:173-176）。
4. `await node.setup(ctx)`，然后发 READY（process.py:182-183）。
5. `value = await node.run(ctx)`，`_classify` 归类（process.py:184-188）。
6. finally：`await node.teardown(ctx)`（出错只记日志）→ `await ctx.aclose()`（关模型客户端）（process.py:195-201）。
7. ctx 收到过停止且 run() 返回了结局：记一条 INFO（process.py:202-203）。
8. 发 EXIT `{result, outcome, reason, error, returned}` → 线程里 `channel.close()`（把排队的发完再关）→ `logging.shutdown()` → `os._exit(0)`，节点留下的线程或执行器任务不会把进程挂住（process.py:163-169）。

### 各种结束方式 → 报告 → 最终状态

"报告"是子进程的 EXIT（process.py:219-223）；"最终状态"由 `_final_state`（supervisor.py:850-874）定：只要主进程发过停止（`process.stop_requested`），一律 stopped（决策记录［阶段 6］停止与"迭代正常结束"的兜底规则）。

| 情况 | 子进程报告 | 没被要求停止 → 状态 | 被要求停止过 → 状态 | 位置 |
|---|---|---|---|---|
| 加载类失败（import 出错、摘要不符、找不到类名），构造节点或 Params 校验失败 | result=failed，error=`<类型>: <信息>`；不发 READY，不跑 setup/teardown，不调 `ctx.aclose` | failed（starting→failed） | stopped，error 取报告 | process.py:173-179 |
| setup() 抛异常 | failed，error | failed；teardown 照跑 | stopped | process.py:182, 191-194 |
| setup() 抛 `StopRequested` | stopped | stopped | stopped | process.py:189-190 |
| run() 返回已声明的 `Outcome` | finished，outcome、reason | finished，记结局与理由 | stopped，结局不记，只写日志（子进程、主进程各一条 INFO） | process.py:209-210, 202-203; supervisor.py:857-864 |
| run() 返回未声明的 `Outcome` | failed，error=`run() returned outcome 'x', which 'T' does not declare [...]`，returned='x'，子进程 WARNING | failed | stopped，error 取报告 | process.py:211-213 |
| run() 返回 None | failed，error=`run() returned without an outcome` | failed | stopped | process.py:214-215 |
| run() 返回其他类型 | failed，error=`run() returned <类型>, not an Outcome` | failed | stopped | process.py:216 |
| run() 抛 `StopRequested`（ctx 阻塞处抛出，或节点自己抛） | stopped，error=None | stopped（error 沿用之前的） | stopped | process.py:189-190; supervisor.py:872-873 |
| run() 抛其他异常，含 `SystemExit`、`KeyboardInterrupt`、`CancelledError`、没接住的 `InputClosed` / `WaitTimeout` | failed，error=`<类型>: <信息>`；traceback 写 node.log 并经 bus 进主进程日志 | failed | stopped，error 照记 | process.py:191-194 |
| teardown() 抛异常（含 BaseException） | 不改 result，`logger.exception` | 不变 | 不变 | process.py:196-200 |
| teardown() 卡住 | EXIT 发不出；没有单独超时 | 一直停在原状态，没有任何计时器 | 到 stop_timeout 强杀，stopped | supervisor.py:829-835 |
| `ctx.aclose()` 抛异常 | 异常逃出 `_run`，没有 EXIT，退出码 1 | failed，`the node process exited unexpectedly (exit code 1)`，原结局丢失（实验 E6，见疑点） | stopped，`... without reporting how` | process.py:201, 163 |
| 建 ctx 或打开 node.log 失败 | 没有 EXIT，退出码 1（traceback 打到原来的 stderr） | failed，带退出码 | stopped | process.py:159-162 |
| 进程崩溃、被外部杀死，没有 EXIT | — | failed，`the node process exited unexpectedly (exit code N)`（SIGKILL 为 -9） | stopped，`the process ended after the stop request without reporting how` | supervisor.py:867-869 |
| 停止超时被强杀 | — | — | stopped，`ended by force <stop_timeout> s after the stop request`；teardown 不跑 | supervisor.py:865-866 |
| 循环节点：`while await ctx.wait_for_run_permission()` 收到停止后返回结局 | finished | — | stopped，结局只写日志 | context.py:358-363 |
| 循环节点：`iter_input_messages` 因输入关闭正常结束，之后返回结局 | finished | finished | — | context.py:279, 283-284 |
| 循环节点：等待时输入关闭，`InputClosed` 没接住 | failed | failed | stopped | context.py:213-214 |
| 循环节点：等待超时，`WaitTimeout` 没接住 | failed | failed | stopped | context.py:416-418 |
| 循环节点：循环体只有同步工作加 `wait_for_run_permission()` | 收不到 control（见疑点） | — | 到 stop_timeout 强杀，stopped（实验 E7） | context.py:358-361 |

### `load_node_class`（process.py:226-249）

| 情况 | 处理 |
|---|---|
| file 型，文件当前内容的 sha256 ≠ locator.digest | 执行文件前抛 `NodeError(... no longer holds version ...)` |
| file 型，摘要相符 | 在子进程里新建 `NodeRegistry` 执行 `register_from_file(path, version)` |
| 文件里没有该类型名 | `NodeError(... defines no node class named ...)` |
| 执行后实际内容的摘要 ≠ locator.digest（加载期间文件被改写） | `NodeError(... changed while it was being loaded)` |
| module 型 | `importlib.import_module`，在模块变量里找 `Node` 子类且 `name == node_type`；找不到 → `NodeError` |
| 以上任何异常 | `_run` 报 failed（上表第一行） |

### 输出与日志

| 情况 | 处理 | 位置 |
|---|---|---|
| print、C 层直接写 fd 1/2 | `os.dup2` 到实例目录的 `node.log`（追加、行缓冲），`sys.stdout = sys.stderr = 该文件` | process.py:267-275 |
| logging | 根 logger 清空原 handler，INFO 及以上写 node.log；WARNING 及以上另经 bus 发 LOG | process.py:276-282, 252-264 |
| LOG 到了主进程 | 记到 logger `emerge.node.<node_id>`，消息前缀 `[instance_id]`，等级不变；陈旧实例的 LOG 也照转 | bus.py:352-356 |
| 格式化日志记录出错 | 退回 `record.getMessage()` | process.py:260-263 |

## 消息总线：`bus.py`

`MessageBus` 在主进程里，由 supervisor 在 `start` / `continue_run` 时创建，并通过 `bus.lifecycle = supervisor` 把 READY / EXIT / 断开交回 supervisor。它是唯一的盖章者：给消息分版本号、盖 `content_time`、写库，然后才推送和发事件。`Channel` 是管道两端共用的收发器，主进程和子进程各有一个。

### 消息种类（`Kind`，bus.py:59-79）

| Kind | 方向 | 内容 | 处理 | 位置 |
|---|---|---|---|---|
| ready | 节点→bus | — | 交 `on_node_ready`（陈旧实例也交） | bus.py:360-362 |
| publish | 节点→bus | req, port, text, data, refs | 盖章、分版本、写库、回 reply（盖好章的 Message）、推送、发 `port.published` | bus.py:382-414 |
| report | 节点→bus | text | 盖章、发 `node.report`；不回复 | bus.py:416-426 |
| event | 节点→bus | event（`Event.to_dict()`） | 覆盖 run_id、node_id、instance_id、acts_on_environment；`node.custom` 盖章，其余不盖章原样发 | bus.py:428-444 |
| ask | 节点→bus | req, question | 盖章、`add_ask` 入库、记 `_asks`、发 `node.ask`；答案到了才回 reply | bus.py:446-458 |
| snapshot | 节点→bus | data | `set_node_snapshot` 覆盖写；不盖章、不发事件 | bus.py:460-462 |
| fetch | 节点→bus | req, port | 按 bus 当前图找这个输入的来源，读库里来源端口现存的消息，回 `{source, messages}`；不盖章 | bus.py:464-480 |
| log | 节点→bus | level, name, message | 写主进程日志；不做陈旧检查 | bus.py:352-356 |
| exit | 节点→bus | result, outcome, reason, error, returned | 交 `on_node_exit`（陈旧实例也交） | bus.py:363-365 |
| control | bus→节点 | op：pause / resume / stop | ctx 改 `_paused` / `_stop_requested`；别的 op 忽略 | context.py:175-182 |
| port_message | bus→节点 | port, message | 加进这个输入的缓冲 | context.py:169-170 |
| input | bus→节点 | port, source, closed, start | 换源或关闭 | context.py:171-172 |
| reply | bus→节点 | req, ok, result, error, stop | 完成对应的请求；stop=True 时节点抛 `StopRequested` | context.py:165-168, 432-437 |
| main_agent_message | bus→节点 | message（`MainAgentMessage` 的 JSON） | 追加到队列 | context.py:173-174 |
| disconnected | 读线程自己投递 | — | 主进程：交 `on_node_disconnected`；子进程：ctx 不认，只唤醒等待者（不处理主进程断开，决策记录［阶段 6］其他实现细节） | bus.py:129-131; context.py:183 |

辅助函数：`reply(req, result, error, stop)`，ok = error is None（bus.py:82-84）；`input_message(port, source, closed, start=None)`（bus.py:87-90）。

### 管道与读写线程（`Channel`，bus.py:93-145）

| 情况 | 处理 | 位置 |
|---|---|---|
| `send` | 放进无界 `SimpleQueue`，从不阻塞；写线程按调用顺序发出 | bus.py:115-117, 140-145 |
| 写失败（对端已关：`OSError` / `ValueError`） | 写线程退出，之后排队的都丢弃 | bus.py:143-145 |
| 读到 EOF / `OSError` | 投递一条 disconnected，读线程退出 | bus.py:126-131 |
| 投递时事件循环已关 | 吞掉 `RuntimeError` | bus.py:134-138 |
| `close()` | 放入哨兵 None，等写线程发完，再关连接；会阻塞，调用方都放进 `to_thread` | bus.py:119-123; process.py:142, 165 |
| 读写线程 | daemon 线程；读线程用 `call_soon_threadsafe` 把消息交给事件循环，事件循环从不阻塞在管道上 | bus.py:108-109, 136 |
| EOF 的前提 | 子端的所有副本都关闭：父进程在 spawn 后立即关自己手里的那份子端 | process.py:112-115 |

### 分发与陈旧实例过滤（bus.py:342-380）

| 情况 | 处理 | 位置 |
|---|---|---|
| 入站顺序 | 读线程 → `post` → `_inbox`；一个分发任务按到达顺序逐条 `await _handle` | bus.py:200-202, 342-346 |
| `_handle` 抛 `Exception` | 记日志 `bus could not handle ...`，继续下一条 | bus.py:347-348 |
| log | 写日志后返回 | bus.py:352-356 |
| ready / exit / disconnected | 交 supervisor，陈旧实例也交：旧实例自己的记录还得结束，由 supervisor 保证不影响下游 | bus.py:358-368 |
| 其余种类，发送方不是当前实例（`graph.is_current`） | 发 `node.stale`（payload `{kind, port}`）；带 req 的（publish / ask / fetch）回 `reply(stop=True, error="this instance has been replaced and is stopping")`，节点里抛 `StopRequested`；report / event / snapshot 直接丢 | bus.py:369-373 |
| 处理函数抛异常，消息带 req | 回 `reply(error="<类型>: <信息>")`，节点里抛 `NodeError` | bus.py:375-380 |
| 处理函数抛异常，消息不带 req | 再抛给分发任务，记日志 | bus.py:378-379 |
| 未知 kind | `_handlers[kind]` 的 KeyError 在 try 之外 → 只记日志；若带 req，节点等不到回复（ctx 不会发未知种类） | bus.py:374 |
| 陈旧检查只在开始处理时做一次 | publish 过了检查、正在写库时替换生效：这条照常入库、推送、发事件；它的 content_time 早于新实例的 pending 记录 | bus.py:369, 385-414 |
| 生命周期回调要拿 supervisor 锁 | supervisor 持锁期间（如 `apply_patch` 里 await `on_commit`、`_launch` 起进程），队头若是 ready/exit/disconnected，整个 bus 的入站处理都在后面排队 | bus.py:360-368; supervisor.py:603, 614, 621 |

### 盖章：`content_time`

| 项 | 内容 | 位置 |
|---|---|---|
| 取号 | `storage.next_content_time(run_id)`：库里计数器加一并写回，一个事务 | bus.py:251, 285, 308, 386, 418, 443, 448 |
| 锁 | bus 的 `_timeline`（`asyncio.Lock`）：取号 → 写记录（第二个事务）→ 回复、推送 → 发事件，都在锁内；所以编号按拿到锁的顺序递增，回调收到事件的顺序与编号一致 | bus.py:171 |
| 为什么是两个事务 | 阶段 5 的 Storage 没有"取号并写入"的合并接口；停在两步之间只会空出一个编号，10-28 用户同意不合并（决策记录［阶段 6］盖章与写存储的事务） | |
| 盖章的 | 当前实例的 `node.state`、`port.published`、`node.report`、`node.custom`、`node.ask`、`node.answer`、`node.main_agent_message` | |
| 不盖章的 | 陈旧实例的状态记录与 `node.stale`、非 custom 的节点事件（`model.*`、`tool.*`、`agent.message`）、snapshot、fetch、log | bus.py:246-249, 438-441 |
| 处理途中出错（如 Message 校验失败） | 号已取，空出一个编号；节点收到 `NodeError` | bus.py:386-398 |
| 谁会调用 | 节点来的消息走分发任务；supervisor 的状态记录（`record_state`）、主 Agent 的消息与回答由主进程直接调用，同样拿 `_timeline` | bus.py:233-319 |
| 会话重开 | 计数器在库里接着编；端口版本号由 `start()` 从 `latest_port_messages` 接着编 | bus.py:180-184 |

### 端口消息：版本、缓冲、推送、补齐

| 情况 | 处理 | 位置 |
|---|---|---|
| 版本号 | 按逻辑端口 `(node_id, port)` 递增；替换后接着编，订阅方的游标仍然有效 | bus.py:172-174, 387, 400 |
| Message 字段 | bus 填 source_node、source_instance、source_port、version、timestamp、content_time；节点只给 text、data、refs | bus.py:388-398 |
| 写库 | `put_port_message`：库里每个端口只留最近 `port_buffer` 条 | bus.py:399; `emerge/storage/base.py:367-372` |
| 先后顺序 | 写库 → 更新版本 → 回 reply（节点的 `await publish` 在这里返回）→ 推送 → 发 `port.published` | bus.py:399-414 |
| 推送给谁 | 当前图里订阅这个端口的每个输入，目标节点**当前实例**的已 attach 进程；还没进程的不推，它第一次读时从库里补齐 | bus.py:403-406, 220-222 |
| 被替换节点的旧进程 | 收不到推送（`current_process` 只认当前实例） | bus.py:220-222 |
| fetch | 按 bus 当前图 `source_of(node, port)`：没有来源 → `{source: None, messages: []}`；有来源 → 来源端口在库里现存的全部消息，版本升序 | bus.py:464-480 |
| 两处缓冲上限 | 库的上限是打开 storage 时给的值，进程内是 `config.runtime.port_buffer`；supervisor 要求两者相同，代码不核对 | supervisor.py:107-108; context.py:80-82 |

### 主 Agent 消息与提问

| 情况 | 处理 | 位置 |
|---|---|---|
| deliver 给有进程的实例 | 锁内盖章；seq = 该实例上一条 + 1；发 `node.main_agent_message`（payload 是整条 `MainAgentMessage`）；经管道送达 | bus.py:284-298 |
| deliver 给还没进程的实例（pending，含等旧进程退出的新实例） | 同样盖章、发事件，然后 `queue_node_message` 落库排队 | bus.py:292-296 |
| 实例 attach | 读出该实例没送达的排队消息，按 seq 依次发出，然后全部标记已送达（发出即算） | bus.py:204-211 |
| seq 续编 | `start()` 用库里每个实例的最大 seq（含已送达的） | bus.py:185-188 |
| 发给运行中实例的消息 | 不写库（只在事件 / trace 里） | bus.py:297-298 |
| ask | 锁内盖章、`add_ask` 入库、`_asks[ask_id] = (进程, req)`、发 `node.ask`；节点一直等 reply | bus.py:446-458 |
| answer_ask，提问在等 | 锁内 pop、盖章、`storage.answer_ask`、发 `node.answer`、回 reply `{answer}` | bus.py:301-319 |
| answer_ask，提问不在等（已答、已作废、不存在） | `RuntimeControlError("ask ... is not waiting for an answer")`；服务层先用 `has_ask` 找运行 | bus.py:305-306; service.py:253 |
| 被替换、还在停的旧实例的提问 | 结束前仍可回答（不看是否当前实例），答案送回旧进程 | bus.py:301-319 |
| 实例结束（会话关闭除外） | `void_asks`：删掉 `_asks` 里这个实例的提问，逐条 `void_ask` 入库；不持锁（并发问题见疑点） | bus.py:321-326; supervisor.py:846 |
| 停止请求之后节点仍调 `ask_main_agent` | ask 照发：入库、发 `node.ask`；节点随即抛 `StopRequested`，结束时作废（实验 E3） | context.py:339-340 |

### 引用（Ref）出处核对

核对在节点进程里的 `publish_output_message` 做，bus 不改写引用，只做 `Ref.model_validate`（决策记录［阶段 6］引用的出处）。

| 情况 | 处理 | 位置 |
|---|---|---|
| refs 里有不是 `Ref` 的对象 | `NodeError` | context.py:302-304 |
| producer 是本节点，producer_instance 是本实例，port 是本节点的某个输出端口 | 放行；不要求等于这次发布的端口 | context.py:305-307 |
| producer 是本节点，但实例不是本实例（例如替换前旧实例产出的引用），或端口不是输出端口 | `NodeError` | context.py:307-311 |
| producer 是别的节点 | 原样放行 | context.py:305-306 |
| key 为 `.`、`..` 或含 `/`、`\` | 构造 `Ref` 时就被拒绝 | `emerge/graph/ports.py:90-96` |

### 输入关闭（`InputClosed` 的来源）

关闭标记是一条 INPUT 消息（`closed: true`），不是 `Message`：不占版本、不盖章、不进缓冲；节点启动时的关闭状态放在 `NodeLaunch.inputs` 里（决策记录［阶段 6］输入关闭与 InputClosed）。

| 情况 | 是否关闭 | 怎么做 | 位置 |
|---|---|---|---|
| 来源节点当前实例结束（finished / failed / stopped，含启动失败） | 关闭 | `_finish` → `_on_current_ended` → `_close_inputs_fed_by`：给每个订阅方的当前进程发 INPUT（source 不变，closed=True） | supervisor.py:847-848, 876-885, 801 |
| 来源节点被 `_advance` 记 skipped | 关闭 | 同上 | supervisor.py:756-758 |
| 来源节点被删除（apply_patch），这个输入没有新来源 | 关闭，source=None | 记入 `_orphaned`，发 INPUT(None, closed=True) | supervisor.py:677-679, 682-684 |
| 来源节点被删除（单项 `remove_node`） | 关闭，source=None | 同上 | supervisor.py:514-518 |
| 同一次提交删掉来源 P、把输入换到 Q | 不关闭 | 只发一次"改读 Q" | supervisor.py:667-673 |
| 来源节点被替换 | 不关闭 | 订阅没变，不发 INPUT；旧实例结束时已不是当前实例，不走 `_on_current_ended` | supervisor.py:847 |
| 换线到开着的来源 | 不关闭 | INPUT(新来源, closed=False, start=新来源当时最新版本) | supervisor.py:657-661 |
| 换线到已结束的来源 | 关闭 | `_input_state` 的 closed = 来源当前记录已终止 | supervisor.py:659 |
| 换线到本次提交刚加的来源 | 不关闭 | 它还没有记录，按开着处理 | supervisor.py:655-656 |
| 只断开（来源节点还在） | 不关闭，source=None | 移出 `_orphaned`；之后的等待一直等，直到超时、停止或再接上 | supervisor.py:680-681, 653-654 |
| 运行结束（`shutdown(closing=False)`） | 没有单独的"运行结束"标记 | 所有进程先收到 stop；被停的来源结束时照常发关闭，但订阅方已被要求停止，阻塞处先抛 `StopRequested` | supervisor.py:428-430; context.py:411-412 |
| 会话关闭（`shutdown(closing=True)`） | 不关闭 | `_finish` 直接返回 | supervisor.py:839-840 |
| 节点启动时来源已结束 | 启动参数 closed=True | | supervisor.py:788, 659 |
| 节点启动时输入在 `_orphaned` 里（来源曾被删） | 启动参数 closed=True、source=None；节点被替换后新实例也一样 | | supervisor.py:654 |
| 关闭后再打开 | 不会 | 节点不重启；同一来源的 INPUT 只会带 closed=True | context.py:94 |

关闭在 ctx 里的表现：

| 调用 | 输入关闭后 | 位置 |
|---|---|---|
| `get_latest_input_message` | 照常返回缓冲里最新一条；来源被删时缓冲已清空 → None | context.py:187-188, 90 |
| `wait_for_input_message` | 没有可交的消息 → `InputClosed` | context.py:213-214 |
| `wait_for_input_messages`（无 match） | 某一路已关闭且没有比游标新的 → `InputClosed`（报第一个这样的端口） | context.py:246-248 |
| `wait_for_input_messages`（有 match） | 已关闭各路的取值交集为空 → `InputClosed`；开着的路不参与判断 | context.py:264-267 |
| `iter_input_messages` | 交完剩余消息后正常结束，不抛异常 | context.py:279, 283-284 |

### 换线（rewire）后从哪读

每个输入有一个起点 `start`：版本 ≤ start 的消息不会由等待和迭代交出，只有 `get_latest_input_message` 看得到（决策记录［阶段 7］换线从换线那一刻往后读）。

| 情况 | 处理 | 位置 |
|---|---|---|
| 节点启动 | 每个输入 start=None：从来源现存最早一条读起，后加的节点能读到已发布的消息 | supervisor.py:788, 660 |
| 运行中换线（apply_patch 里订阅的增、删、改，单项 `rewire`） | INPUT 带 start = 新来源输出端口当时的最新版本（没发过为 0）；版本号是每个端口各自编的，旧游标的数字不能沿用 | supervisor.py:536, 684, 660 |
| 节点收到不同 source 的 INPUT | 换 source 和 start、清空缓冲、`synced` = (source is None)、游标清空、generation + 1 | context.py:84-93 |
| 节点收到同一 source 的 INPUT | 只更新 closed，start 不变 | context.py:85, 94 |
| 正在进行的 `wait_for_input_message` | 发现 generation 变了：`newer_than` 作废、`where` 的已看记录清空，落到 start 之后 | context.py:202-203 |
| 正在进行的 `iter_input_messages` | 游标已被清空，从 start 之后接着交 | context.py:92, 276 |
| 正在进行的 `wait_for_input_messages` | 只有这一路的游标和 match 缓存清空 | context.py:237-241 |
| fetch 在途时换线 | 回复的 source 与发出时、与当前都相同才收下，否则丢弃重发 | context.py:388-395 |
| INPUT 与推送的先后 | supervisor 切图后、任何 await 之前就发 INPUT，按新图推送的消息一定排在 INPUT 后面 | supervisor.py:459-462, 530-536 |
| 换线后的 `get_latest_input_message` | 返回新来源最新一条（版本可能 ≤ start） | context.py:100-101 |

## 节点进程内的 ctx：`context.py`

`RuntimeNodeContext` 是节点 `setup/run/teardown` 收到的 `ctx`，由 `process._serve` 创建。每个调用都变成发给 bus 的一条消息，或在本地缓冲上等待；所有等待都在子进程的事件循环上，任何消息到达都唤醒全部等待者，各自重查条件。

### 内部状态

| 字段 | 含义 | 约束/默认 |
|---|---|---|
| `_inputs[port]`（`_Input`） | 每个声明的输入：source、closed、start、messages（版本 → Message）、synced、generation、cursor | 来自 `NodeLaunch.inputs`（context.py:140-142） |
| `_Input.synced` | 来源端口在库里现存的消息已补进来 | 初值 = (source is None) |
| `_Input.cursor` | `iter_input_messages` 已交出的最后一个版本；按输入保存，多个迭代器共用 | None 表示从最早现存 |
| `_Input.add` | 来源不符（`"node.port"` ≠ source）直接丢；超过 port_buffer 删最旧的 | context.py:76-82 |
| `_Input.after(v)` | 下限 = v 与 start 中较大的（一个为 None 就取另一个）；返回大于下限的版本，升序 | context.py:96-98 |
| `_main_agent_messages` | deque，按到达顺序 | |
| `_paused`、`_stop_requested` | 由 control 消息改；`_paused` 初值 = `launch.paused` | context.py:144-145 |
| `_requests` | req 号 → future | context.py:147-148 |
| `_signal` | 唤醒全部等待者 | context.py:37-55 |
| `emitter` | `_BusEmitter`，给模型客户端、agent 循环用；不在 `NodeContext` 协议里 | context.py:104-118 |
| `session_dir` | 所属对话的文件夹，来自 `launch.session_dir`；产物目录、引用目录、模型客户端的图片根目录都从它算 | context.py:128 |
| `artifacts_dir` | 构造时就创建 `<对话文件夹>/runs/<run>/nodes/<node>/<instance>/` | context.py:132 |
| `outcomes` | `bind(cls)` 之后才有值，之前是 () | context.py:151-154 |

### 收到 bus 消息（`receive`，context.py:162-183）

| 消息 | 处理 |
|---|---|
| reply | 按 req 取出 future 并设结果；req 不存在或 future 已完成就忽略 |
| port_message | 校验成 `Message` 后加进该输入；端口不存在会抛 KeyError，只在事件循环的回调里报错 |
| input | `_Input.update(source, closed, start)` |
| main_agent_message | 校验后追加到队列 |
| control | pause → `_paused=True`；resume → `_paused=False`；stop → `_stop_requested=True`；其他 op 忽略 |
| disconnected 或其他 | 不处理 |
| 每条消息之后 | `_signal.notify()` 唤醒所有等待者 |

### 等待的通用规则（`_wait`，context.py:398-419）

每一轮按顺序：

1. 把这次等待涉及的输入补齐：未 synced 的发 fetch，等回复。
2. `_stop_requested` 为真 → 抛 `StopRequested`，即使已经有可交的消息。
3. 查条件：有结果就返回；条件里抛出的 `InputClosed` 等直接抛给节点。
4. 到期 → 抛 `WaitTimeout`（timeout 为 0 或负数：只查一次）。
5. 否则在 `_signal` 上等，最多等到剩余时间。

| 情况 | 结果 |
|---|---|
| 没给 timeout | 一直等，直到条件满足、停止或输入关闭 |
| 消息在"查条件"和"开始等"之间到达 | 不会漏：两步之间没有 await，回调只能在开始等之后运行 |
| 节点自己用 `asyncio.wait_for` 取消了等待 | finally 移除等待者（context.py:54-55） |
| fetch 回复 stop=True（实例已被替换） | `StopRequested` |
| fetch 回复错误 | `NodeError` |
| bus 一直不回复 fetch | 一直等；停止也打断不了这次 fetch，回复到了才轮到第 2 步 |

`StopRequested` 是 `BaseException`，节点里的 `except Exception` 接不住它，不会把停止变成普通错误继续跑（`emerge/exceptions.py:45-51`）。

### 方法一览

| 方法 | 行为 | 异常/返回 | 停止请求之后 |
|---|---|---|---|
| `get_latest_input_message(port)` | 必要时先补齐，返回缓冲里版本最大的一条；不看 start | 没有 → None；不是输入端口 → `NodeError` | 不检查停止，照常返回 |
| `wait_for_input_message(port, newer_than, timeout, where)` | 见"条件等待" | `WaitTimeout` / `InputClosed` / `StopRequested` / `NodeError` | 抛 `StopRequested` |
| `wait_for_input_messages(ports, *, newer_than, match, timeout)` | 见"多输入汇合" | 同上；ports 为空或有重复 → `NodeError`；match 的值不可哈希 → `TypeError` | 抛 `StopRequested` |
| `iter_input_messages(port)` | 见下表 | 异步生成器：端口不对的 `NodeError` 在第一次取值时才抛 | 阻塞处抛 `StopRequested` |
| `publish_output_message(port, *, text, data, refs)` | 核对端口、引用出处、data 可 JSON 化，发 publish 等回复，返回盖好章的 `Message` | 不是输出端口、引用不合规、data 不可序列化、bus 回错 → `NodeError`；实例已被替换 → `StopRequested` | 照常可用，实例仍是当前的就入库、推送 |
| `report_to_main_agent(text)` | 发 report（`str(text)`），不等回复 | 无 | 照常发 |
| `emit_event(event)` | dict → `node.custom`，payload 就是这个 dict；`Event` → 只接受 `NODE_EVENT_TYPES`，重建为只有 type 和 payload 的 Event | 类型不允许或 payload 不可序列化 → `NodeError` | 照常发 |
| `ask_main_agent(question)` | 发 ask，等 reply，返回答案字符串 | 被替换 → `StopRequested`；bus 回错 → `NodeError` | ask 先发出，再抛 `StopRequested` |
| `take_main_agent_messages()` | 取走队列里全部，返回元组 | 空就是 () | 照常 |
| `wait_for_main_agent_message(timeout)` | 取下一条；与 take 共用队列，每条只交一次 | `WaitTimeout` | 抛 `StopRequested`（队列里有消息也一样） |
| `set_status_snapshot(data)` | 发 snapshot，库里覆盖写 | 不可序列化 → `NodeError` | 照常 |
| `ref_dir(ref)` | 算出 `runs/<run>/nodes/<producer>/<producer_instance>/<port>/<key>/` | 只算路径，不创建目录（`emerge/storage/artifacts.py:85-90`） | — |
| `wait_for_run_permission()` | 没暂停：立即返回 True，不让出事件循环；暂停中：在 `_signal` 上等 | 不抛异常 | 返回 False |
| `get_model_client()` | 第一次调用时用 `ctx.emitter` 建客户端（延迟导入 provider SDK） | | |
| `aclose()` | 关模型客户端；由 process 在 teardown 之后调用 | 抛错的后果见疑点 | |

### 条件等待：`wait_for_input_message`（context.py:190-217）

| 情况 | 处理 |
|---|---|
| `newer_than=None` | 从现存最早一条（或 start 之后）看起，返回第一条满足的，不是最新一条（决策记录［阶段 6］条件等待与多输入汇合） |
| 给了 `newer_than` | 只看版本 > max(newer_than, start) 的 |
| 有 `where` | 按版本升序逐条调用；每条只调一次（记住看过的最大版本）；`where` 抛的异常直接抛给节点 |
| 没有满足的，输入开着 | 继续等 |
| 没有满足的，输入已关闭 | `InputClosed` |
| 等待中换线 | 见"换线后从哪读" |

### 多输入汇合：`wait_for_input_messages`（context.py:219-270）

| 情况 | 处理 |
|---|---|
| 游标 | 每路取 `newer_than.get(port)`，没给的为 None；不移动 `iter` 的游标，调用方拿返回的版本自己往后接 |
| 无 `match`，每路都有比游标新的 | 每路返回最早的一条 |
| 无 `match`，某路没有且已关闭 | `InputClosed`（第一个这样的端口） |
| 无 `match`，其余情况 | 继续等 |
| 有 `match` | 每路每个取值保留最早一条；`match` 每条只算一次（按 (端口, 版本) 缓存） |
| 有共同取值 | 多组都能配上时，取"组内 content_time 最大值最小"的一组，即最后一条最早到达 bus 的那组 |
| 没有共同取值，已关闭各路的取值交集为空 | `InputClosed` |
| 没有共同取值，其余情况 | 继续等：开着的路以后什么值都可能来 |

### `iter_input_messages`（context.py:272-287）

| 情况 | 处理 |
|---|---|
| 有比游标（与 start）新的 | 交出最早一条；先把输入的游标设为它的版本，再 yield |
| 没有，输入开着 | 等 |
| 没有，输入已关闭 | 正常结束 |
| 慢消费者，缓冲已淘汰中间版本 | 从现存最早的接着交，出现缺口，不报错 |
| 同一输入上新开一个迭代器 | 接着上一个的游标 |
| 换线 | 游标清空，从新来源 start 之后交 |

### 停止请求时阻塞中的调用怎么返回

| 阻塞位置 | 收到 control stop 之后 | 位置 |
|---|---|---|
| `wait_for_input_message(s)`、`iter_input_messages`、`wait_for_main_agent_message`、`ask_main_agent` | 被唤醒，下一轮第 2 步抛 `StopRequested` | context.py:411-412 |
| 正在等 fetch 回复（首次读、换线后） | 回复到了才抛；陈旧实例的 fetch 回 stop=True，直接抛 | context.py:390, 435-436 |
| `wait_for_run_permission`（暂停中） | 返回 False | context.py:358-363 |
| `publish_output_message` 在等回复 | 不被打断，照常完成 | context.py:312-321 |
| 节点自己的 `asyncio.sleep`、外部 I/O | 不被打断；下次调用 ctx 的阻塞方法或 `wait_for_run_permission` 才生效；否则到 stop_timeout 被强杀 | |
| 节点在事件循环里做同步工作 | `receive` 根本没机会运行，标志不会被设，到 stop_timeout 被强杀（实验 E7） | |

## 监管者：`supervisor.py`

`Supervisor` 是服务层使用的执行入口：`service.start_run` 调 `start`，`mutations.apply` 调 `apply_patch`，`checkpoint.close` / `resume` 调 `shutdown(closing=True)` / `continue_run`，结束运行（`end_run`、不带节点的 stop）调 `shutdown()`，节点级控制调 `stop` / `pause` / `resume` / `deliver` / `answer_ask`。它同时实现 bus 的 `LifecycleHandler`（bus.py:148-155）。

### 内部状态（supervisor.py:112-151）

| 字段 | 含义 |
|---|---|
| 构造参数 | storage（打开时的 port_buffer 要与配置一致，运行记录须已存在）、callbacks、config、registry（默认当前活动 registry）、policy（默认来自配置）、on_idle |
| `_graph` | 当前图；`_set_graph` 同时改 `bus.graph`，路由与陈旧判断都看它 |
| `_records` | instance_id → `NodeRuntimeState`；本次运行所有实例，旧实例保留 |
| `_acts_on_environment` | instance_id → 是否作用于环境；找不到 registry entry 记 False |
| `_processes` | 已启动、还没回收的进程，instance_id → `NodeProcess` |
| `_orphaned` | (node, port)：来源被删、又没有新来源的输入，关闭，直到再接上 |
| `_holding` | 新实例要等旧进程退出才能启动的节点 id |
| `_run_paused` | 整个运行暂停中 |
| `_start_paused` | 运行没暂停、但这些实例要以暂停状态启动 |
| `_launch_paused` | 以暂停状态启动、还没被放行的实例 |
| `_ops` | supervisor 锁（`asyncio.Lock`） |
| `_idle` | 空闲标记（`asyncio.Event`） |
| `_shutting_down`、`_closing` | 关闭中；会话关闭（什么都不记） |
| `_tasks` | 后台任务：回收进程、等旧进程退出、协程形式的 on_idle |

### 返回结构

| 结构 | 字段 | 含义 |
|---|---|---|
| `NodeFate` | started / waiting / never / after_old_instance | 提交前还没启动的节点现在的去向；启动失败（failed）也算 started（supervisor.py:717-723） |
| `PatchExecution` | old, new, diff, fates, held | `new is old` 表示什么都没变；held = 在等旧进程退出的节点 |
| `RerunNode` | node_id, old_instance_id, new_instance_id, was, started_paused | started_paused = 运行暂停，或关闭前是 paused |
| `ContinueExecution` | graph, kept, rerun, stopped, fates | fates 不含重跑的节点 |

### 锁、任务与并发

| 机制 | 规则 | 位置 |
|---|---|---|
| `_ops` | 改状态的公共方法和 bus 的生命周期回调都在锁内；`stop`、`replace_instance`、`shutdown` 在等进程退出之前出锁，否则 EXIT 的处理拿不到锁 | supervisor.py:328-336, 591-595, 425-431 |
| 加锁顺序 | `_ops` → bus `_timeline`；bus 处理节点消息只拿 `_timeline`，没有反向的 | |
| 后台任务 | `_spawn` 用 `ensure_future` 放进 `_tasks`，完成后自动移除；`shutdown` 只 gather 调用那一刻的任务 | supervisor.py:911-914, 432 |
| on_idle | 同步函数在 `_check_idle` 里直接调用（经 `_advance` 时在锁内）；返回协程的放进后台任务，不在锁内 | supervisor.py:900-903 |
| 不加锁的查询 | `graph`、`state`、`states`、`instance_states`、`idle`、`run_paused`、`ended_outcomes` | supervisor.py:155-186 |
| `bus` 属性 | 还没 start → `RuntimeControlError("the supervisor has not started a run")` | supervisor.py:159-163 |

### 启动：`start` → `_advance` → `_launch`

| 情况 | 处理 | 位置 |
|---|---|---|
| 同一个 supervisor 第二次 start 或 continue_run | `RuntimeControlError("this supervisor has already started a run")` | supervisor.py:199-200, 232-233 |
| start 流程 | 建 bus、`bus.start()`（版本号与 seq 续编、起分发任务）；锁内切图、逐个登记 pending、`_advance` 一次 | supervisor.py:201-210 |
| 空图 | 没有记录，永远不算空闲，不发空闲通知 | supervisor.py:894 |
| `_advance` 第一轮 | `start_status(图, 已结束节点 → 结局)`；pending 且 NEVER 的 → skipped，并关闭它喂的输入；"不可能启动"沿启动边的传递在 `start_status` 内部完成 | supervisor.py:753-758; `emerge/graph/graph.py:319-357` |
| `_advance` 第二轮 | 不在关闭中时，对 pending、READY、不在 `_holding` 的逐个 `_launch`；每个都重新查状态，因为启动失败会嵌套推进 | supervisor.py:759-769 |
| 启动边规则 | 没有启动边 → READY；某条边上每个来源都以要求的结局结束（outcome=None 表示任意已声明结局）→ READY；来源以别的结局或无结局（failed / stopped / skipped）结束、来源不在图中或不可能启动 → 这条边不可能触发；所有边都不可能 → NEVER；其余 WAITING | `emerge/graph/graph.py:336-377` |
| 已经启动过的节点 | 不会再启动：只 launch pending 的 | supervisor.py:765 |
| `_launch` 判断暂停 | `_run_paused` 或实例在 `_start_paused` → paused=True，并从 `_start_paused` 移除 | supervisor.py:776-777 |
| `_launch` 正常 | `registry.require_entry` → `NodeLaunch`（输入状态 from_now=False）→ `NodeProcess.start(bus.post)` → 记入 `_processes`（暂停的另记 `_launch_paused`）→ `bus.attach` → starting(pid) | supervisor.py:778-807 |
| `_launch` 抛 `Exception` | starting → failed（`cannot start: ...`）→ `_on_current_ended` | supervisor.py:797-802 |
| 调用 `_advance` 的地方 | start、continue_run、apply_patch、add_node、remove_node、update_start_edges、update_graph、replace_instance、`_start_after_exit`、`_on_current_ended` | supervisor.py:210, 317, 477, 499, 519, 547, 554, 597, 712, 878 |

### 监控进程结束

| 情况 | 处理 | 位置 |
|---|---|---|
| READY，状态 starting | 在 `_launch_paused` → paused；否则 → running | supervisor.py:602-610 |
| READY，状态不是 starting（已被暂停、停止、替换） | 忽略 | supervisor.py:604 |
| EXIT | 锁外存 `exit_report`，锁内 `_finish` | supervisor.py:612-615 |
| disconnected，没收到 EXIT | 先 `join(1.0)` 拿退出码（这期间分发任务在等）；锁内 `bus.detach` + `_finish`；后台 `_reap` | supervisor.py:617-624 |
| disconnected，已收到 EXIT | detach；`_finish` 因记录已终止直接返回；后台 `_reap` | supervisor.py:621-624 |
| `_finish`，会话关闭中 | 什么都不记 | supervisor.py:839-840 |
| `_finish`，记录已终止 | 直接返回：每个实例只记一次 | supervisor.py:842-843 |
| `_finish`，其余 | `_final_state` → `_set_state` → `void_asks` → 若仍是当前实例：关闭它喂的输入、`_advance` | supervisor.py:844-848 |
| `_reap` | `process.reap()`（join、关管道、设 exited）→ 从 `_processes` 删（是同一对象才删）→ `_check_idle` | supervisor.py:905-909 |
| 节点进程 fork 出的子进程还拿着管道 | 主进程收不到 EOF，`exited` 要等那个子进程退出（实验 E5，见疑点） | |

### 停止流程：`stop(node_id)`

1. 锁内：节点不在图 → `RuntimeControlError`；当前实例已终止 → 直接返回记录；pending → `RuntimeControlError("... has not started; remove it instead of stopping it")`（supervisor.py:328-333）。
2. `_request_stop`：`stop_requested=True`；状态在 starting / running / paused → stopping（写库、`node.state`）；已是 stopping 不再转；发 `control stop`（supervisor.py:823-827）。
3. 出锁，`_await_exit`：等 `exited`，最多 `policy.stop_timeout` 秒（supervisor.py:829-831）。
4. 超时：`kill()`（forced=True，SIGKILL），然后不限时等 `exited`（supervisor.py:832-835）。
5. 子进程：阻塞处抛 `StopRequested`，或 `wait_for_run_permission()` 返回 False → run 结束 → teardown → EXIT → 退出；被强杀则什么都不发。
6. 主进程：EXIT → `_finish` 记 stopped；disconnected → `_reap` 设 `exited`，第 3 或第 4 步返回。
7. 返回这个实例的记录（此时已是 stopped）（supervisor.py:337）。

| 情况 | 处理 | 位置 |
|---|---|---|
| 对同一节点并发两次 stop | 第二次看到 stopping，只重发 `control stop`；两次各自等、各自可能强杀（kill 可重复） | supervisor.py:825-827 |
| stop 与节点自己结束赛跑：EXIT 已发出、还没处理时 stop 先拿到锁 | 记 stopped，返回的结局只写日志（兜底规则） | supervisor.py:854-864 |
| 刚提交替换，马上 stop 同一节点 | 当前实例是还没启动的新实例 → `RuntimeControlError`（pending） | supervisor.py:332-333 |
| `stop_all()` | 不在锁内取出 starting / running / paused 的节点，并发 `stop`；pending 节点不管；服务层不调用它 | supervisor.py:378-380 |

### 暂停与恢复

暂停只在节点的 `wait_for_run_permission()` 处生效，正在做的事不会被打断（supervisor.py:340；决策记录［阶段 7］整个运行暂停）。

| 调用 | 当前实例状态 | 处理 | 位置 |
|---|---|---|---|
| `pause(node)` | paused | 不做事，返回记录 | supervisor.py:343 |
| `pause(node)` | starting / running | `_pause`：移出 `_launch_paused`、→ paused、发 `control pause` | supervisor.py:344-346, 809-812 |
| `pause(node)` | pending / stopping / 终止 | `RuntimeControlError("... only a starting or running node can be paused")` | supervisor.py:344-345 |
| `resume(node)` | paused | `_resume`：→ running、发 `control resume`；不看 READY 到没到（见疑点） | supervisor.py:352-353, 819-821 |
| `resume(node)` | starting，且在 `_launch_paused` | `_release_paused_launch`：移出、发 `control resume`；状态保持 starting，READY 后 → running | supervisor.py:356-357, 814-817 |
| `resume(node)` | running，或 starting 但不在 `_launch_paused` | 不做事，返回记录 | supervisor.py:358 |
| `resume(node)` | pending / stopping / 终止 | `RuntimeControlError("... only a paused node can be resumed")` | supervisor.py:354-355 |
| `pause_all()` | — | `_run_paused=True`；starting / running 的逐个 `_pause`，其余跳过；之后启动的节点都以暂停状态启动 | supervisor.py:360-366 |
| `resume_all()` | — | `_run_paused=False`；清空 `_start_paused`；paused 的（含单独暂停的）→ `_resume`；`_launch_paused` 里的 → 放行；其余跳过 | supervisor.py:368-376 |
| 以暂停状态启动 | `_run_paused`，或实例在 `_start_paused` | `NodeLaunch.paused=True`，实例记入 `_launch_paused`；ctx 的 `_paused` 初值为 True，setup 照常做，`wait_for_run_permission` 第一次就停住 | supervisor.py:776-777, 793, 804-805; context.py:144 |
| 谁进 `_start_paused` | 被替换时旧实例 paused（或以暂停启动、还没 ready）；continue_run 时关闭前是 paused | | supervisor.py:697-698, 305-306 |
| 运行暂停写库 | — | 不在这里：服务层 `RunHandle.set_paused` 写 `runs.paused` | service.py:232-233 |

### 发消息、回答提问、查询

| 方法 | 情况 | 处理 | 位置 |
|---|---|---|---|
| `deliver` / `send_to_node` | 节点不在图 | `RuntimeControlError("no node ... in the graph")` | supervisor.py:636-640 |
| | 当前实例已终止 | `RuntimeControlError("node ... is <state>; the message was not delivered")` | supervisor.py:405-406 |
| | 当前实例有进程（含 stopping） | 经 bus 送达；queued=False | supervisor.py:407-410 |
| | 当前实例还没进程（pending、等旧进程退出） | 落库排队；queued=True | supervisor.py:407; bus.py:294-296 |
| | 与 `_launch` 里的 attach 同时发生 | 都在锁内，不会交错，排队消息与直发消息的顺序不乱 | supervisor.py:403, 806 |
| | 返回 | (当前实例记录, `MainAgentMessage`, 是否排队)；`send_to_node` 只返回消息 | supervisor.py:382-411 |
| `answer_ask` | — | 直接调 `bus.answer_ask`，不拿 supervisor 锁 | supervisor.py:413-414 |
| `status_snapshot(node)` | — | 读当前实例的快照；替换后新实例还没写过 → None | supervisor.py:191-193 |
| `wait_idle(timeout)` | 超时 | `TimeoutError` | supervisor.py:188-189 |
| `state(node)` | 节点不在图或没有记录 | None | supervisor.py:177-179 |

### 整份改图：`apply_patch(patch, *, on_commit=None)`

1. 拿锁；关闭中 → `RuntimeControlError("the run is shutting down; the graph can no longer change")`（supervisor.py:448-450）。
2. 用此刻各当前实例的状态调用 `apply_graph_patch(旧图, patch, registry, states=...)`；任何一处不合法抛 `GraphValidationError`，图、记录、进程都不变（supervisor.py:451-453）。
3. 空 patch → `new is old` → 返回 `PatchExecution(old, old, GraphDiff())`（supervisor.py:454-455; `emerge/graph/patch.py:156-157`）。
4. `diff_graphs`；给了 `on_commit` 就在锁内 await（mutations 用它存图、发 `graph.patched`，所以存图和事件都在执行之前）（supervisor.py:456-458）。
5. `_set_graph(new)`：从这一刻起，被替换的旧实例发来的都算陈旧（supervisor.py:459）。
6. 不经过 await，`_renotify_inputs`：每个来源有变化的输入发一次 INPUT（from_now=True）；来源被删又没有新来源的记入 `_orphaned` 并关闭，其余移出 `_orphaned`（supervisor.py:460-462, 663-684）。
7. 被删除的节点（只可能是 pending）在自己的实例名下记 skipped，是陈旧记录，只有 `node.stale`（supervisor.py:463-467）。
8. 每个被替换的节点走 `_swap_instance`（见下一节）；要等旧进程的记进 `held`（supervisor.py:468-471）。
9. 登记全部新增节点（supervisor.py:472-475）。
10. 记下此刻 pending 的节点，统一 `_advance` 一次，返回 `PatchExecution(old, new, diff, fates, held)`（supervisor.py:476-484）。

| 情况 | 处理 | 位置 |
|---|---|---|
| `on_commit` 抛异常 | 还没切图，图和进程不变；异常抛给调用方（mutations 只接 `GraphValidationError`）；on_commit 自己已写的内容不回滚 | supervisor.py:457-458; mutations.py:192-195 |
| 只改了 modified 字段（pending 节点的参数或启动边、描述） | 不单独处理：切图后 `_advance` 用新启动边判断；pending 节点启动时用新参数 | supervisor.py:459, 477 |
| 改运行中节点的参数 | 阶段 4 已把它变成替换 | `emerge/graph/patch.py:255-259` |
| 替换 stopping 或已结束的节点 | `apply_graph_patch` 拒绝 | `emerge/graph/patch.py:286-293` |
| 删除不是 pending 的节点 | `apply_graph_patch` 拒绝 | `emerge/graph/patch.py:214-221` |
| 给在跑节点原本没接的输入新接一个来源 | 同样按换线处理：start = 来源当时的最新版本 | supervisor.py:668-669, 684 |
| 有变化的输入，其节点本次被删 | 跳过 | supervisor.py:675-676 |
| 没涉及的节点 | 进程和实例都不动 | |

### 替换与陈旧实例

| 情况 | 处理 | 位置 |
|---|---|---|
| apply_patch 替换 pending 节点 | 登记新实例（pending）；旧实例记 skipped（陈旧，`node.stale`） | supervisor.py:691-696 |
| apply_patch 替换在跑的节点（starting / running / paused） | 登记新实例；旧实例 paused 或以暂停启动还没 ready → 新实例进 `_start_paused`；节点进 `_holding`；`_request_stop`（旧实例 stopping，陈旧记录）；起后台 `_start_after_exit`；进 `held` | supervisor.py:691, 697-702 |
| 旧进程已退出（`exited` 已设）时被替换 | 代码按 pending 处理记 skipped；实际不会出现：`exited` 只在 `_finish` 之后设，那时旧记录已终止，替换先被 `apply_graph_patch` 拒绝 | supervisor.py:693-696 |
| 旧进程退出（含超时强杀） | `_start_after_exit`：锁内移出 `_holding` 并 `_advance`，新实例按启动边启动 | supervisor.py:704-712 |
| 为什么要等 | 不让同一节点有两个进程同时作用于环境（决策记录［阶段 6］替换与陈旧实例） | |
| 旧进程还在停，又提交替换同一节点 | 当前实例是 pending 的新实例 → 只换登记，它记 skipped；`_holding` 保留到最早那个旧进程退出 | supervisor.py:692-696, 711 |
| 旧进程还在停，节点被删、之后又以同一 id 加回 | 新加的节点同样要等那个旧进程退出（`_holding` 按节点 id） | supervisor.py:767 |
| 旧实例发 publish / ask / fetch | bus 回 `reply(stop=True)` → 节点里 `StopRequested`；发 `node.stale` | bus.py:369-373 |
| 旧实例发 report / event / snapshot | 丢弃，发 `node.stale` | bus.py:369-370 |
| 旧实例发 log | 照常转进主进程日志 | bus.py:352-356 |
| 旧实例 READY | 状态已是 stopping，忽略 | supervisor.py:604 |
| 旧实例 EXIT / disconnected | `_finish` 记 stopped（`stop_requested`）；写库 + `node.stale(kind=state)`；作废提问；不关闭输入、不 `_advance` | supervisor.py:837-848; bus.py:246-249 |
| 订阅了被替换节点的输入 | 不关闭；新实例在同一逻辑端口上接着编版本 | bus.py:172-174 |
| 旧实例没取走的主 Agent 消息 | 不转给新实例 | |
| 替换后发给该节点的消息 | 新实例还没进程 → 落库排队，启动时交出 | bus.py:294-296 |
| 被替换节点的新实例启动时 | 输入状态 from_now=False，即从来源现存最早一条读起 | supervisor.py:788 |

### 接着跑：`continue_run(graph, run_id, stored, *, paused=False, on_commit=None)`

1. 已 start 过 → `RuntimeControlError`；建 bus 并启动（端口版本、排队消息的 seq 从库里续编）（supervisor.py:232-238）。
2. 锁内：`_run_paused = paused`；切到存储里的图（supervisor.py:239-241）。
3. 把 `stored` 里每个实例的行原样装进 `_records`，不写库；pid 不恢复，changed_at = updated_at；`acts_on_environment` 标记按"当前图的 spec + 替换历史里旧实例的 type/code"查 registry（supervisor.py:243-257）。
4. 不是当前实例、却还在 starting / running / paused / stopping 的（关闭时替换还在等旧进程）→ stopped（supervisor.py:258-263）。
5. 遍历当前图的节点：库里没有这一行 → 登记 pending；已终止 → 进 kept，记录不改写；stopping → stopped（当前实例，发 `node.state`），进 stopped 列表；starting / running / paused → 收进 interrupted；pending → 不动（supervisor.py:267-279）。
6. 有 interrupted：用只含 `replace_node`（code 沿用旧实例固定的版本）的 patch 得到新图（版本 +1，实例 id `<node>@v<新版本>`，替换历史有记录）→ `on_commit` → 切图 → 旧实例记 stopped（已是陈旧，只发 `node.stale`）→ 登记新实例 → 关闭前是 paused 的进 `_start_paused`（supervisor.py:281-315）。
7. 记下 pending 节点，`_advance` 一次，返回 `ContinueExecution`；fates 去掉重跑的节点（supervisor.py:316-324）。

| 情况 | 处理 | 位置 |
|---|---|---|
| 已结束节点的结局 | 照样参与启动边判断；它喂的输入在订阅方启动时就是关闭的 | supervisor.py:645-647, 659 |
| 重跑的节点 | 新实例从头跑；旧实例不触发启动边、不关闭订阅方的输入 | supervisor.py:847 |
| paused=True | 运行继续暂停，之后启动的节点都以暂停状态启动 | supervisor.py:240, 776 |
| 关闭时没答的提问 | 不在这里处理：`checkpoint.resume` 调用前已全部作废 | checkpoint.py:131-132 |
| `apply_graph_patch` 或 `on_commit` 抛错 | 第 4、5 步已写的 stopped 留在库里，bus 已启动；异常抛给调用方 | supervisor.py:258-277, 291-294 |

### 单项改图方法（阶段 6 保留，服务层不调用）

它们每次接收整张新图，只适合一次改一处；一次提交多处改动要走 `apply_patch`（决策记录［阶段 7］supervisor 的两个新入口）。

| 方法 | 前提（不满足抛 `RuntimeControlError`） | 做什么 | 与 apply_patch 的差别 | 位置 |
|---|---|---|---|---|
| `add_node(graph, node_id)` | node_id 在新图、不在旧图 | 切图、登记、返回当时的 `start_status`、`_advance` | 不通知任何输入 | supervisor.py:486-500 |
| `remove_node(graph, node_id)` | 当前实例 pending；新图不含它 | 切图、记 skipped、它喂的输入记 `_orphaned` 并关闭、`_advance` | | supervisor.py:502-519 |
| `rewire(graph, node_id, port)` | 新图有 node_id | 切图、移出 `_orphaned`、给在跑进程发 INPUT（from_now=True）；不 `_advance` | | supervisor.py:521-536 |
| `update_start_edges(graph, node_id)` | — | 切图、返回 verdict（节点已结束或不在图 → None）、`_advance` | | supervisor.py:538-548 |
| `update_graph(graph)` | — | 切图、`_advance` | | supervisor.py:550-554 |
| `replace_instance(node_id, new_spec, *, graph=None)` | 当前状态可替换；new_spec 同 id、新实例 id；给了 graph 就必须含 new_spec | 锁内切图（没给 graph 就在当前图里换掉这个 spec，版本和替换历史不变）、登记新实例；旧进程在 → `_holding` + 停止，否则旧实例记 skipped；出锁等旧进程退出（超时强杀）；再锁内 `_advance`，返回新实例记录 | 等旧进程退出才返回；新实例不继承暂停；不发输入通知 | supervisor.py:556-598 |

### 关闭：`shutdown(*, closing=False)`

| 步骤 / 情况 | closing=False（结束运行） | closing=True（关闭会话） | 位置 |
|---|---|---|---|
| 没 start 过 | 直接返回 | 同左 | supervisor.py:423-424 |
| 锁内 | `_shutting_down=True`；对每个未退出的进程 `_request_stop`（stopping，写库、发事件） | 同样发 stop，但 `_set_state` 什么都不记 | supervisor.py:425-430 |
| 出锁 | 并发等全部退出，超时强杀 | 同左 | supervisor.py:431 |
| 进程结束时 | 照常记 stopped、作废提问、关闭输入、`_advance`：不再启动任何节点，但启动边已不可能的 pending 节点照样记 skipped（实验 E1，见疑点） | 什么都不记：库里保持关闭前的状态，不触发启动边、不关输入、不作废提问 | supervisor.py:759, 754-758, 839-840 |
| 收尾 | gather 当时的后台任务，`bus.close()`（处理完已到达的消息再停） | 同左 | supervisor.py:432-433 |
| 之后 apply_patch | `RuntimeControlError` | 同左 | supervisor.py:449-450 |
| 空闲通知 | 关闭中不发 | 同左 | supervisor.py:890-891 |
| 再调一次 | 没有未退出的进程；gather 任务，bus 已关则 `close()` 无事 | 同左 | supervisor.py:428; bus.py:191-196 |

### 空闲（`_check_idle`，supervisor.py:887-903）

| 情况 | 处理 | 位置 |
|---|---|---|
| 关闭中 | 不改空闲标记 | supervisor.py:890-891 |
| 图为空；或有当前实例没终止；或 `_processes` 里有进程没退出（含还在停的旧实例） | 清空闲标记 | supervisor.py:892-896 |
| 条件满足，原本不空闲 | 设空闲，调 `on_idle`；之后每次重新进入空闲各调一次 | supervisor.py:897-903 |
| 已经空闲 | 不重复调用 | supervisor.py:897-898 |
| 登记任何新实例 | 清空闲标记 | supervisor.py:736 |
| 在哪里检查 | `_advance` 末尾、`_reap` 末尾 | supervisor.py:770, 909 |

### 写进存储的内容（经 bus）

| 写入 | 时机 | 位置 |
|---|---|---|
| `set_node_state`（每个实例一行） | 每次 `_register`、`_set_state`，当前实例和陈旧实例都写；会话关闭中不写 | bus.py:246-252, 493-502 |
| `next_content_time` | 每次盖章 | 见盖章表 |
| `put_port_message` | 每次 publish | bus.py:399 |
| `queue_node_message` / `mark_node_messages_delivered` | 给没进程的实例发消息 / 实例 attach | bus.py:296, 211 |
| `add_ask` / `answer_ask` / `void_ask` | 节点提问 / 主 Agent 回答 / 实例结束 | bus.py:449, 309, 326 |
| `set_node_snapshot` | 节点写状态快照 | bus.py:462 |
| 读取 | `bus.start`：`latest_port_messages`、`queued_node_messages(include_delivered=True)`；attach：该实例未送达的消息；fetch：`port_messages`；`status_snapshot`：`node_snapshot` | bus.py:183, 187, 207, 471; supervisor.py:193 |
| 不在这一层写 | 图（`on_commit` 里由 mutations 写）、运行状态与 `runs.paused`（服务层） | mutations.py:172-183 |

### 发出的事件

所有节点事件都带 run_id、node_id、instance_id、acts_on_environment；bus 造的事件 timestamp 是主进程时刻，节点发来的保留节点给的 timestamp（bus.py:430-437）。

| 事件 | 时机 | 盖章 | payload | 位置 |
|---|---|---|---|---|
| `node.state` | 当前实例登记或状态变化 | 是 | state, previous, outcome, reason, error, pid | bus.py:250-267 |
| `node.stale` | 陈旧实例的状态变化；陈旧实例发来的非生命周期消息 | 否 | `{kind: "state", state}` 或 `{kind: 原种类, port}` | bus.py:246-249, 328-338, 370 |
| `port.published` | publish 写库并推送之后 | 是 | port, version, text, data, refs | bus.py:407-414 |
| `node.report` | report | 是 | text | bus.py:416-426 |
| `node.custom` | `emit_event(dict)` 或 `emit_event(Event(node.custom))` | 是 | 节点给的 payload | bus.py:442-444 |
| `model.*`、`tool.*`、`agent.message` | 节点里的模型客户端、agent 循环经 `ctx.emitter` 发出 | 否 | 节点给的 | bus.py:438-441 |
| `node.ask` | 提问入库之后 | 是 | ask_id, question | bus.py:451-458 |
| `node.answer` | 回答入库之后 | 是 | ask_id, answer | bus.py:310-317 |
| `node.main_agent_message` | deliver | 是 | seq, timestamp, content_time, text, data, refs | bus.py:292 |

## 疑点（待核实）

实验脚本在 `/data/yuqingchi/tmp/emerge-code-walkthrough/scratch/runtime/`：`exp_core.py`（E1–E4）、`exp_fork.py`（E5）、`exp_more.py`（E6–E7）、`exp_void_race.py`（E8，假存储，不起进程）。下面的 `HANDOFF.md` 指计划目录里的 `/data/yuqingchi/Code/EMERGE-Policy-2.0-Plan/HANDOFF.md`。

1. **结束运行时，pending 节点会被记成 skipped，与决策记录不符。** HANDOFF.md:447 写 `shutdown(closing=False)` 时"pending 的节点保持 pending"；代码里 `_advance` 只在启动那一轮判断 `_shutting_down`（supervisor.py:759），标 skipped 的一轮照常执行（supervisor.py:754-758）。被停的节点没有结局，等它的 pending 节点随之变成 NEVER。E1：a 运行中、b `after=[a]`，`shutdown()` 后库里 a=stopped、b=skipped，并发了 `node.state`。

2. **启动中被暂停再恢复，状态会提前记成 running。** `pause` 把 starting 转为 paused（supervisor.py:344-346），`resume` 对 paused 一律转 running（supervisor.py:352-353, 819-821），不看 READY 到没到；之后的 READY 因为状态不是 starting 被忽略（supervisor.py:604）。E2：setup 要 2.5 s，状态在 setup 完成前约 4 s 就是 running。以暂停状态启动的那条路径专门保留 starting 直到 READY（supervisor.py:356-357, 814-817；HANDOFF.md:708-709），这条路径没有这样做。`pause_all` + `resume_all` 也一样。

3. **`void_asks` 与 `answer_ask` 并发会抛 KeyError，节点结束的后续处理被跳过。** `void_asks` 不持锁，遍历快照，每条之间 await 存储（bus.py:323-326）；这期间 `answer_ask` 可以 pop 同一实例的另一条提问（bus.py:303-304），轮到它时 `del self._asks[ask_id]` 抛 KeyError。E8 用假存储复现了 `KeyError('a2')`。在 `_finish` 里这会跳过 `_on_current_ended`（supervisor.py:846-848）：它喂的输入不关闭，这次也不推进启动边；随后的 disconnected 看到记录已终止，直接返回（supervisor.py:842-843）。端到端后果是按代码推断的，没有用真实进程复现。

4. **只做同步工作、靠 `wait_for_run_permission()` 检查的循环，收不到暂停和停止。** 没暂停时 `wait_for_run_permission()` 不让出事件循环就返回 True（context.py:359-361）；control 消息要等事件循环运行 `receive` 才会设标志（bus.py:136; context.py:175-183）。E7：`while await ctx.wait_for_run_permission(): time.sleep(0.05)`，stop 后 3 s 被强杀，teardown 没跑。policies.py:16-19 的注释和 HANDOFF.md:442 都说"到达 `wait_for_run_permission()` 就能停"。同理，这种循环里 `take_main_agent_messages()` 也拿不到新消息。

5. **节点进程 fork 出的子进程拿着管道副本时，`exited` 要等它退出，强杀后的这次等待没有上限。** EOF 要等子端的所有副本都关闭（process.py:112-115 的注释），`exited` 只在收到 disconnected 之后的 `_reap` 里设（supervisor.py:617-624; process.py:138-143）；`_await_exit` 强杀后的 `await process.exited.wait()` 不带超时（supervisor.py:834-835），而 `kill()` 只杀节点进程本身。E5：节点用 fork 起一个睡 6 s 的子进程后返回，记录立即是 finished，空闲却晚了 6 s。stop、shutdown、替换后新实例的启动都会被这样拖住（Linux 上 Python 3.12 的 multiprocessing 默认就是 fork）。

6. **`ctx.aclose()` 抛错会丢掉结束报告。** 它在内层 try 之外（process.py:195-201），异常逃出 `_run`，EXIT 不发（process.py:163-164），主进程按"没报告就退出"记 failed（supervisor.py:868-869）。E6：run() 返回已声明的结局，记录却是 failed `the node process exited unexpectedly (exit code 1)`。

7. **经 `ctx.emitter` 能发任何类型的事件，包括 `node.state`。** 类型核对只在 `emit_event` 里（context.py:327-331）；`_BusEmitter.emit_nowait` 不核对（context.py:113-114），bus 对非 custom 类型原样转发（bus.py:438-441）。E4：节点发出的 `node.state`（payload 由节点定、content_time=None）到了主进程回调。HANDOFF.md:467 只约束了 `emit_event`。

8. **停止请求之后调用 `ask_main_agent()` 仍会产生一条提问。** 先 `_submit` 发出 ask，进 `_wait` 才检查停止（context.py:339-340）。E3：主 Agent 收到 `node.ask`，随后库里这条变为 void。

9. **排队的主 Agent 消息先发事件、后写库。** `deliver_main_agent_message` 在 `queue_node_message` 之前就 `_emit`（bus.py:292, 295-296），与 HANDOFF.md:426"写完才推送和发事件"不一致；写库失败时主 Agent 已看到这条消息，seq 也已占用（bus.py:290）。

10. **存储调用出错没有专门处理，可能留下不一致（按代码推断，没有实验）。** 例如 `_launch` 里进程已起并记进 `_processes` 之后，`bus.attach` 或 `_set_state(STARTING)` 抛错（supervisor.py:803-807），记录仍是 pending：它的 READY 会被忽略（supervisor.py:604），下一次 `_advance` 会给同一实例再起一个进程（supervisor.py:763-769），两个进程发的消息都算当前实例。`answer_ask` 先 pop 再写库（bus.py:304-309），写库失败后这条提问再也答不了。

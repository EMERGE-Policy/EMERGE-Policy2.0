# 主 Agent（阶段 8）

> "位置"列：不带目录的文件名指 `emerge/agent/` 下的文件，`tools/x.py` 指 `emerge/agent/tools/x.py`，其他写全路径。"实验"指 `/data/yuqingchi/tmp/emerge-code-walkthrough/scratch/agent/` 下的脚本（脚本模型，无真实 API 调用）。

## 概览

| 文件 | 职责 |
|---|---|
| `agent.py` | `MainAgent`：会话期间常驻的编排者。装配阶段 3 的 `AgentLoop`，睡在 inbox 上，被唤醒后跑一轮；含 checkpoint hook、`StaleCallGuard`、`PromptCacheKey`、节点提问分叉、system 与运行信息、事件 |
| `inbox.py` | 待处理项：四种类别、优先级、合并成消息、意图计数 |
| `control.py` | 控制通道：`TurnControl` 中断当前轮；`Controller` 把 pause/resume/stop 直接交给 Runtime |
| `session.py` | 会话：逐条写库、pending 用户消息、system 快照、关闭与打开 |
| `memory.py` | 全局/会话两份 `MEMORY.md`：写入、切条目、索引刷新、搜索 |
| `tools/`、`prompts/`、`__init__.py` | 20 个工具与 `ToolContext`；system 文本的前几段；`__init__` 用 PEP 562 `__getattr__` 延迟导入主 Agent 的名字，阶段 3 模块不会拖入 runtime/storage |

## MainAgent：`agent.py`

`MainAgent` 一次服务一个会话：`Session.create/open` 调 `attach()` 再 `start()`，`Session.close()` 调 `detach()`。用户侧入口是 `submit`、`control`；Runtime 侧入口是 `on_feedback`（装配时 `RuntimeService.open(..., on_feedback=lambda item: agent.on_feedback(item))`）。

### 对外接口

| 方法 / 属性 | 作用 | 前置条件 | 报错与边界 |
|---|---|---|---|
| `MainAgent(config, storage, runtime, registry=None, *, client=None, callbacks=None, stream=True, extra_tools=())` | 构造。`registry` 缺省取 `runtime.registry`；事件缺省发到 `runtime.callbacks`（那里已挂唯一的 tracer）；`client` 缺省 `create_client(config, callbacks=self.events)`；`extra_tools` 排在内置工具之后 | — | 只建对象；`tools`、`loop`、`history`、`memory`、`builder` 等在 `attach` 后才存在 |
| `submit(text, images=())` | 用户消息：先写 `pending_inputs`，再进 inbox（意图 +1） | 已挂会话 | 无会话：`RuntimeControlError("the main agent serves no session; open or create one first")`。不看 `status`：closing 期间提交的消息只留在 `pending_inputs`，下次打开时处理 |
| `control(action, node_id=None) -> ControlResult` | 控制命令，立即执行、不经模型；执行后发 `agent.control` | 已挂会话 | 无会话抛 `RuntimeControlError`；Runtime 的 `RuntimeControlError` 变成 `done=False`，其他异常原样抛出；未知 action 落到 stop 分支（疑点 1） |
| `note_control(text, *, run_id)` | inbox 放一条 control 记录，意图 +1，排队写 `intent_count` | — | 由 `Controller` 调用；无会话时只进 inbox |
| `on_feedback(item)` | Runtime 反馈：`kind="ask"` 开分叉，其余作 notice 进 inbox | 有会话且 `status` 不是 `detached`/`closing` | 否则静默丢弃（agent.py:244-245） |
| `wait_until_idle(timeout=None)` | 每 50 ms 轮询，直到 `sleeping`、inbox 为空、没有分叉在跑 | — | 超时抛 `TimeoutError`；`detached` 时永不满足；不看 FeedbackHandler 时间窗里还没推出的批 |
| `fork_request(text) -> ModelRequest` | 构造分叉请求（公开以便检查） | 已 `attach` | — |
| `attach(session)` | 装配（见下表），`status` → `sleeping` | 没在服务别的会话 | 已在服务：`RuntimeControlError("the main agent already serves session <id>")`；中途抛错不回滚（疑点 7） |
| `start(*, reports=(), unfinished_turn=False, pending=())` | 放回 pending 消息；决定接续旧轮次或保存"重开说明"；起 `_serve` 任务 | 已挂会话 | 重复调用不拦，会再起一个 `_serve` |
| `detach()` | 收尾并解除绑定 | — | 已是 `detached` 直接返回 |
| `status` | `detached` / `sleeping` / `working` / `closing`，见下表 | | |
| `current_run_id` | `session.current_run_id`，无会话为 None | | |

### 状态

| `status` | 何时进入 | 位置 |
|---|---|---|
| `detached` | 构造后；`detach()` 结束 | agent.py:199,374 |
| `sleeping` | `attach()` 结束；每轮结束且不在 closing | agent.py:327,420-421 |
| `working` | `start(unfinished_turn=True)` 当场（不等任务开跑，免得 `wait_until_idle` 在那一轮开始前返回，决策记录［阶段 8］计划之外加的）；每轮开始 | agent.py:345,401 |
| `closing` | `detach()` 开始；此后 `on_feedback` 丢弃、`_serve` 不开新轮、hook 不合并 | agent.py:355 |

### 装配（`attach`）

| 组件 | 内容 | 位置 |
|---|---|---|
| 绑定 | `self.session = session`，`session.agent = self`（在其余步骤之前） | agent.py:268-269 |
| inbox | `Inbox(record.intent_count)`：意图计数从库恢复 | agent.py:270 |
| 用量 | 取 `record.usage` 接着累加 | agent.py:271 |
| 记忆 | `MemoryStore`，两个范围各 `refresh` 一次 | agent.py:272-273 |
| skills | `SkillStore(<workspace>/skills)`，只在此时读一次 | agent.py:274 |
| system | `ContextBuilder(6 个片段, runtime_info=self._runtime_info, system=record.system)`：有快照就原样用 | agent.py:275-277 |
| 工具 | `Toolset`：catalog → files → graph → execution → memory → `extra_tools`，顺序即发给模型的顺序；重名抛 `ToolError` | agent.py:278-297 |
| 请求构造 | `RequestBuilder(context, history, tools.specs())`：工具定义从此固定 | agent.py:299 |
| middleware | 新的 `TurnControl`、`StaleCallGuard`、`PromptCacheKey`（仅 OpenAI）、`CompactionMiddleware`（`max_context_tokens` 缺省 100000，`compact_threshold` 缺省 0.8） | agent.py:300-312 |
| 循环 | `AgentLoop(client, tools, builder, history, chain, RunLimits.from_config(config.agent.limits), callbacks=events, stream)`；预算缺省全为 None（不限） | agent.py:313-322；emerge/config.py:105-108 |
| 会话钩子 | `session.system_source = context.get_system_prompt`；History 的监听者设为 session | agent.py:323-324 |
| 快照 | `record.system is None`（新会话）→ 生成 system 并排队 `set_system_snapshot` | agent.py:325-326 |

### middleware 链（外 → 内）

| 顺序 | middleware | 模型调用 | 工具调用 | 位置 |
|---|---|---|---|---|
| 1 | `TurnControl` | 标记 `phase="model"` | 标记 `phase="tool"` | control.py:82-94 |
| 2 | `StaleCallGuard` | 调用前读 `ctx.intent_counter`，给回复里每个工具调用记下这个值 | 执行前比对；过期的受管调用直接返回拒绝、不进内层（因此没有 `tool.started/finished`） | agent.py:111-122 |
| 3 | `PromptCacheKey` | `before_model` 并入 `prompt_cache_key` | — | agent.py:136-141 |
| 4 | `CompactionMiddleware` | `before_model` 超预算时压缩并重建请求（`extra` 保留）；`after_model` 记 input tokens | — | middleware.py:128-141 |

### 一条用户消息的处理流程（主 Agent 在睡眠）

1. `submit`：`_require_session()`，构造 `Message.user(text, images)`（agent.py:223-224）。
2. `session.add_pending(message)`：直接 await 写 `pending_inputs`，拿到 seq（agent.py:225；session.py:156-158）。
3. `inbox.put_user(message, pending_seq=seq)`：意图 +1，置位 `_arrived` 唤醒等待者（agent.py:226；inbox.py:69-73,98-101）。
4. `session.set_intent(...)`：排队写 `sessions.intent_count`（agent.py:227）。
5. `_serve` 从 `inbox.wait()` 醒来；若已 closing 就退出；`take()` 取走全部（按优先级排好）；转出的消息为空就继续等（防御分支）（agent.py:381-387）。
6. `_admit`：每项发 `agent.inbox`；用户项调用 `session.hold_pending(message, seq)`；`to_messages` 转成消息（agent.py:439-446）。
7. 第一条消息开新一轮，其余放进 `_carry`（agent.py:388-389）。
8. `_run_turn`：`RunContext(intent_counter=当前意图, run_id=会话 id)`；建 task 跑 `loop.run_turn(msg, ctx, checkpoint_hook=self._checkpoint)`；`turns.begin(task)`；会话状态写 `turn_open`；`status=working`；发 `agent.turn`（started）（agent.py:393-402）。
9. 轮次 task：`history.start_turn(msg)` → 监听者排队 `append_message`，紧跟 `remove_pending_input`；发 `agent.message`（loop.py:94-95；session.py:137-138,185-190）。
10. 每次调用模型前：`_checkpoint` 合并 `_carry` 与 inbox 新项并更新 `ctx.intent_counter` → `append_runtime_info`（变了才追加）→ `builder.build()` → 经 middleware 链调用模型（loop.py:160-169）。
11. 回复写入 History（即写库）；不含工具调用 → 本轮以 `stop` 结束（loop.py:173-179）。
12. 含工具调用 → 逐个：`_checkpoint`（此时不合并）→ 链（`TurnControl` 标记 tool → `StaleCallGuard` 可能拒绝 → `_tool_call` 发 `tool.started`、执行、发 `tool.finished`）→ 结果写入；全部做完回到第 10 步（loop.py:182-196）。
13. 轮次结束：`turns.end()`、清空 `_carry`；按结束方式定 `finish`；用量累加并排队写库；非 `closed` 时会话状态写 `idle`；不在 closing 时 `status=sleeping`；发 `agent.turn`（finished）；`_turn_over.set()`（agent.py:404-423）。
14. `_serve` 回到 `inbox.wait()`。

运行通知走同一条路：`FeedbackHandler` → `on_feedback`（kind ≠ ask）→ `put_notice`（不计意图），从第 5 步起相同。节点提问不进 inbox，见"节点提问的分叉回答"。

### 忙时到达的消息

| 情况 | 处理 | 位置 |
|---|---|---|
| 主 Agent 正在流式回复时用户发来新消息 | 照常写 pending、意图 +1、进 inbox。这条回复照常收完并写入；它的受管工具调用（记下的是请求时的意图）全部被拒，非受管调用照常执行；全部结果到齐后、下一次模型调用前合并进本轮 | agent.py:221-227,118-122,425-437 |
| 同上，但回复不含工具调用 | 本轮以 `stop` 结束；新消息留在 inbox，`_serve` 下一圈取出开新一轮 | loop.py:178-179；agent.py:381-389 |
| 主 Agent 正在执行工具时用户发来新消息 | 正在跑的工具做完（不中途拦）；同一回复里余下的受管调用被拒；下一次模型调用前合并 | agent.py:118-122,429-436 |
| 忙时连续到达多条通知、问答记录 | 留在 inbox，到边界一起取出，合成一条消息 | inbox.py:113-125 |
| 忙时用户执行 pause / resume / stop | 命令立即执行；control 记录进 inbox、意图 +1；下一个边界合并，排在最前；旧的受管调用被拒 | control.py:112-125 |
| 忙时节点提问 | 不排队，立即开分叉，本轮不受影响 | agent.py:246-249 |
| 唤醒时取出的第 2 条及以后 | 放 `_carry`，在本轮第一个边界（第一次模型调用前）追加；与那时 inbox 新到的项拼接时 `_carry` 在前、新项在后（优先级只在一次 `take()` 内排序） | agent.py:388,432 |
| 会话正在关闭 | 边界处不再合并任何项 | agent.py:429-431 |

### checkpoint hook（`_checkpoint`）

| 时机 / 状态 | hook 返回 | 效果 | 位置 |
|---|---|---|---|
| 已请求中断（任何边界，含两个工具之间） | `False` | 循环抛 `_Cancelled`，整轮丢弃 | agent.py:427-428；loop.py:228-230 |
| closing | `None` | 不合并；循环随后因 `_closing` 以 `closed` 结束 | agent.py:429-431；loop.py:161-162,189-192 |
| 最后一条回复的工具调用还没全部有结果（两个工具之间） | `None` | 不合并：工具调用与结果之间插入别的消息，Anthropic 会拒绝（决策记录［阶段 8］inbox） | agent.py:429-431,570-579 |
| 其余（每次模型调用前） | `None` | `_carry` + `take()` 的消息追加到当前轮末尾；`ctx.intent_counter = inbox.intent` | agent.py:432-437 |

### 轮次结束方式

| 结束方式 | 触发 | History 与库 | 会话状态 | `finish_reason` |
|---|---|---|---|---|
| 正常 | 回复不含工具调用 | 保留 | `idle` | `stop` |
| 预算 | 模型调用前 `max_iterations` / `max_input_tokens` / `max_output_tokens_total` 已达；或 `max_tool_calls` 用完（余下调用补 `"not executed: tool call budget exhausted"`） | 保留 | `idle` | 对应名称 |
| 中断 | `control("interrupt")` | 整轮 truncate，库里 `discard_turn`；接续的轮次连同关闭前的部分一起丢 | `idle` | `cancelled` |
| 关闭 | `detach()` → `loop.close_turn()` | 已完成的保留；正在出的回复不写；未执行的调用补 `"not executed: session closed"` | 保持 `turn_open` | `closed` |
| 异常 | 模型调用抛 `ModelError`（重试已用完）或其他未捕获异常 | 不回滚，已写的消息留下；模型不会被告知（疑点 6） | `idle` | `error`，`error="<类型>: <消息>"`，另 `logger.error` |

位置：agent.py:403-423；loop.py:129-200。

### system 文本

片段顺序（agent.py:504-521），非空片段以空行相连（context.py:70-76）：

1. `prompts/system.md`
2. `prompts/graph.md`
3. `prompts/tools.md`
4. `Contents of AGENTS.md (workspace instructions from the user):` + 内容（文件不存在或为空 → 整段省略）
5. `Contents of USER.md (about the user):` + 内容（同上）
6. `prompts/memory.md`，接 `Contents of memory/MEMORY.md (global memory):` 与 `Contents of sessions/<id>/memory/MEMORY.md (this session's memory):`，空文件写 `(empty)`

| 时机 | system 来源 | 位置 |
|---|---|---|
| 新会话 | 首次 `get_system_prompt()` 按片段生成，排队写快照 | agent.py:325-326 |
| 打开会话 | `record.system` 原样使用，不读工作区文件；原因：关闭期间文件可能改过，重建会让 Anthropic thinking 签名对不上（决策记录［存储：退出后打开会话接着跑］） | agent.py:275-277 |
| 压缩后 | `rebuild_system_prompt()` 重读全部片段；快照在写库任务执行时取新值 | context.py:49-53；session.py:148-152 |
| 会话中改了 AGENTS.md、USER.md、MEMORY.md、prompts | 不生效，直到压缩或新会话 | — |

### 运行信息（每次模型调用前求值，变了才作为用户消息追加，不进 system）

| 情况 | 追加内容 | 位置 |
|---|---|---|
| 没有当前运行，或当前运行不被 Runtime 持有 | 空串 → 不追加 | agent.py:536-539 |
| 运行未结束，图文件与上次写出的一致 | `[Run status, shown again whenever it changes]` + `runtime.summary()` + `graph file <rel> matches graph version N.` | agent.py:540-548 |
| 图文件有未提交改动 | 末行改为 `... holds edits not committed yet: commit_graph applies them, reset_graph drops them.` | agent.py:544-546 |
| 改动基于旧版本 | 再加 ` It was written from an older graph version: reset_graph, then redo the edits.` | agent.py:545 |
| 图文件被删掉 | 仍显示 `matches graph version N`（疑点 4） | agent.py:548 |
| 运行已结束（仍被持有） | 只有标题与摘要 | agent.py:541 |
| 打开会话时空闲、有恢复报告 | 前缀 `[Session reopened] ...` + 各运行 `ResumeReport.describe()`；运行信息变化前每次求值都带，变化后丢掉 | agent.py:523-533,346-347 |
| 文本与上次追加的相同 | 不追加 | context.py:63-65 |
| 压缩后 | 去重记录清空，下一次请求重新追加 | context.py:49-53 |

### 事件

主 Agent 的发射器 `_RunEvents` 把所有经它发出的事件（含循环与它自建的模型客户端发出的）的 `run_id` 改成会话当前运行；没有运行时为 None，tracer 写进 `runs/_session/trace.jsonl`（agent.py:144-160；emerge/callbacks/tracers.py:9,22）。外部传入、且没挂 `agent.events` 的 client，其模型事件不带运行 id。`_RunEvents.with_event_defaults` 直接转给原发射器、不改 run_id（目前没有调用方）。

| 事件 | 发出者 | 时机 | payload |
|---|---|---|---|
| `agent.turn` | `_run_turn` | 轮次开始 / 结束 | started：`{phase, continued}`；finished：`{phase, finish_reason, error, text}` |
| `agent.inbox` | `_admit` | 每个被取出的 inbox 项（开轮时或边界合并时） | `{kind, text}` |
| `agent.fork` | `_answer_node` | 分叉开始 / 回答送出后 | started：`{phase, ask_id, node_id, question}`；answered：另加 `answer` |
| `agent.control` | `control()` | 每次控制命令之后（成功或失败） | `{action, node_id, done, text}` |
| `agent.message` | `AgentLoop` | 开轮/接续的那条消息（role 为 user，可能是通知或 control 合并消息）；含文本的助手回复；边界合并进来的消息不发 | `{role, text}` |
| `tool.started` / `tool.finished` | `AgentLoop._tool_call` | 每个真正执行的工具；被过期校验拦下的没有 | `{tool, call_id, arguments}` / `{tool, call_id, is_error, content}` |
| `model.*` | `ModelClient` | 主请求、分叉请求、压缩摘要请求都发，靠 `call_id` 与 `agent.fork` 区分 | 见模型层 |

### `prompt_cache_key`

| 情况 | 处理 | 位置 |
|---|---|---|
| `client.provider.provider == "openai"`（responses 与 chat_completions） | 键 = 会话 id；`PromptCacheKey.before_model` 把 `{"prompt_cache_key": id, **原 extra}` 写进 `request.extra`（原 extra 已有同名键时以原值为准） | agent.py:311-312,136-141 |
| 分叉请求 | `fork_request` 直接加同一个键 | agent.py:490 |
| 压缩摘要请求 | 不经过链，不带键 | context.py:186-189 |
| Anthropic | 不加（靠 `cache_control`） | agent.py:311 |
| 原因 | 不带键时，同前缀请求被中转派到不同处，缓存不命中（决策记录［阶段 8］OpenAI 请求带 `prompt_cache_key`） | — |

### 节点提问的分叉回答（`_answer_node` / `fork_request`）

| 项 | 内容 | 位置 |
|---|---|---|
| 触发 | FeedbackHandler 推来 `kind="ask"`（节点调用 `ask_main_agent()`），且会话存在、不在 detached/closing | agent.py:242-249 |
| 并发 | 每个提问一个独立 task（`main-agent-answer:<ask_id>`），放进 `_forks`；不进 inbox，不打断当前轮 | agent.py:247-249 |
| 提问者描述 | `Node <id> (instance <iid>, type <type>, described as "<desc>")`；没有 description 省略最后一段；运行不被持有或节点不在图中 → `Node <id>`；反馈里缺 `ask_id` 时写成 `None`，缺 `node_id` 时写成 `?` | agent.py:451-454,492-500 |
| 请求 | `builder.build()`（同一份缓存的 system、同一组 `ToolSpec`、History 全部消息）→ `_answered` 去掉"工具调用还没全部有结果"的最后一条回复及其后的结果 → 末尾加用户消息 `[Question from a node] <提问者> is asking while it runs (question <ask_id>): <问题> ... You cannot call tools here ...` → `tool_choice="none"` → OpenAI 时加缓存键 | agent.py:456-461,481-490,582-589 |
| 不做的事 | 不经过 middleware 链（不压缩、无过期校验）；不调 `append_runtime_info`；不改 History | agent.py:481-490 |
| 模型调用 | `call_model(self.client, request, stream=self.stream)`；用量计入会话 | agent.py:463-464 |
| 回答为空 | `"(no answer)"` | agent.py:465 |
| `ModelError` | 回答改为 `The main agent could not answer just now (<e>). Carry on with your own best judgement.`，照样交给节点，不转给用户 | agent.py:466-467 |
| 送回节点 | `runtime.answer(ask_id, answer)` | agent.py:469 |
| 提问已不在等待（节点结束或被替换，`RuntimeControlError`） | 问答记录末尾加 ` The node did not get it: its question was no longer waiting (it ended or was replaced).` | agent.py:471-472 |
| 记录 | `put_qa("<提问者> asked (question <id>): <问题>\nYou answered on the side, from this conversation and without tools: <回答>[未送达说明]\nKeep what you do next consistent with this answer.")`，不计意图 | agent.py:473-478 |
| 关闭会话 | `detach` 取消全部分叉：不回答、不留记录；提问在重开时由 Runtime 作废 | agent.py:366-369 |
| 其他异常 | 未捕获，task 静默结束（疑点 8） | agent.py:462-472 |

分叉前缀随主 Agent 当时所处位置而定：

| 主 Agent 在做什么 | 分叉前缀 |
|---|---|
| 睡眠 | 全部历史 |
| 等模型回复 | 正在进行的那次主请求的消息（含刚追加的运行信息）：未完成的回复本就不在 History |
| 执行某条回复的工具、还有调用没结果 | 去掉这条回复及其已有的结果，即产生这条回复的那次请求 |
| 工具都已完成、下一次请求前 | 全部历史 |

## Inbox：`inbox.py`

主 Agent 的待处理队列：`MainAgent` 往里放，`_serve`（睡眠时）和 `_checkpoint`（忙时，在模型调用前）取。

| 类别 | 来源 | 优先级 | 合并 | 唤醒 | 计入意图 |
|---|---|---|---|---|---|
| `control` | `note_control`：控制通道 pause/resume/stop 成功后 | 0（最先） | 一次取出的全部 control 合成一条：`[Control channel] Done directly by the user, not through you. Do not undo it unless asked:` + 每条一行 | 是 | 是（+1） |
| `user` | `submit`；打开会话时 `start()` 放回的 pending | 1 | 不合并，每条用原 `Message`（含图片） | 是 | `submit` +1；放回的 `counted=False` 不重复计 |
| `notice` | `on_feedback`，kind 为 `node_ended` / `node_skipped` / `graph_idle` / `batch`（节点汇报与非结束的状态变化，FeedbackHandler 已按节点和种类在时间窗内合并） | 2 | 与 qa 合成一条：全部来自一个运行时标题 `[Run notifications: run <id>]`，否则 `[Run notifications]` 且每行前加 `run <id>: ` | 是 | 否 |
| `qa` | `_answer_node` 完成后 | 2（与 notice 同级，按到达顺序） | 同一条消息里的 `[Node questions answered]` 段，逐条列出 | 是 | 否 |
| 节点提问 | `on_feedback` kind=`ask` | 不进 inbox | — | 否（开分叉） | 否 |
| 中断 | `control("interrupt")` | 不进 inbox | — | 否 | 否 |

端口新消息（`port.published`）和 `node.custom` 不由 FeedbackHandler 推送，不会到这里（emerge/runtime/feedback.py:7-12）。

| 方法 | 行为 | 位置 |
|---|---|---|
| `take()` | 取走全部，按优先级稳定排序（同级保持到达顺序），清空列表并复位事件 | inbox.py:85-90 |
| `wait()` | 列表为空时睡在 `_arrived` 上；有项立即返回 | inbox.py:92-96 |
| `to_messages(items)` | 顺序：control 合并消息 → 各条用户消息 → notice+qa 合并消息；一次 `take()` 的结果最多产生"1 + 用户消息数 + 1"条 | inbox.py:104-126 |
| `_bullet(text)` | 多行文本首行加 `- `，其余行缩进两格（如 batch 的多行） | inbox.py:129-131 |
| `intent` / `__len__` | 意图计数（初值来自 `sessions.intent_count`）/ 待处理项数 | inbox.py:61-67 |

## 意图计数与过期校验：`StaleCallGuard`

意图计数（`Inbox.intent`）在每条 `submit` 的用户消息、每个成功的 pause/resume/stop 后 +1，变化即排队写 `sessions.intent_count`。`StaleCallGuard` 在 `wrap_model_call` 里给回复的每个工具调用记下 `ctx.intent_counter`（即这次请求发出时模型已看到的计数，只在 hook 合并消息时更新），在 `wrap_tool_call` 里比对当前 `inbox.intent`。用"请求时已看到的"而不是"回复返回时的"计数，是因为生成期间到达、还没合并的消息模型没看到（决策记录［阶段 8］过期校验）。受管工具：`commit_graph`、`reset_graph`、`start_run`、`pause`、`resume`、`stop_node`、`end_run`、`send_to_node`，以及路径解析后为 `runs/<run_id>/graph.json` 的 `edit_file` / `write_file`（agent.py:553-554；tools/graph.py:26；tools/execution.py:21；tools/files.py:49-63）。

| 情况 | 是否被拦 | 返回给模型 |
|---|---|---|
| 受管调用，请求发出后意图没变 | 否 | 正常结果 |
| 受管调用，模型生成期间用户发了消息（意图变了，消息还没合并） | 是 | `{"tool": "<name>", "error": "not executed: the user said something new, or used the control channel, after this call was made. Read the latest messages and decide again whether it is still wanted."}`（`is_error=True`） |
| 受管调用，同一回复里前面的工具执行期间用户发消息 | 是（之后的每个受管调用） | 同上 |
| 受管调用，期间控制通道 pause/resume/stop 成功 | 是 | 同上 |
| 控制命令失败（`done=False`）、`interrupt` | 不计意图，不因此拦（interrupt 会把整轮丢掉） | — |
| 期间只到达运行通知、问答记录 | 否（不计意图） | 正常结果 |
| 受管调用在意图变化时已经开始执行 | 否，执行完 | 正常结果 |
| 非受管调用（`read_file`、`show_graph`、`run_status`、`node_detail`、catalog、memory、`register_node_code`、写其他路径的 `edit_file`/`write_file`、`extra_tools`）意图已变 | 否 | 正常结果 |
| `edit_file`/`write_file` 经符号链接或 `./` 前缀写到 `runs/<id>/graph.json` | 是（按解析后的路径判断） | 同上 |
| 写 `runs/<id>/nodes/.../graph.json` 等非三段路径 | 否 | 正常结果 |
| 调用没有记录（没经过这个 guard 的模型调用） | 否 | 正常结果 |

被拒的调用不经过循环的核心处理，没有 `tool.*` 事件，拒绝结果作为工具结果留在对话里。没执行到的调用（轮次被取消/关闭、预算用完）的记录不会从 `_stamps` 删除（agent.py:115,119）。

## 控制通道：`control.py`

用户的显式命令立即执行，不经模型。`TurnControl` 是链最外层的 middleware，知道当前轮在等模型还是在执行工具；`Controller` 由 `MainAgent.control()` 调用。

| action | `node_id` | 对 Runtime 的动作 | 对当前轮次的影响 | `ControlResult` |
|---|---|---|---|---|
| `interrupt` | 忽略 | 无 | 见下一张表；不留记录、不计意图、不唤醒 | `done=True, "the current turn was cancelled"` / 没有轮次时 `done=False, "no turn is running"`；`run_id`、`node_id` 为 None |
| `pause` | 给出 | `runtime.pause(run, node)`：只能暂停 starting/running 的节点，已暂停的直接返回 | 成功后 control 记录进 inbox（意图 +1），下一个边界被模型看到，旧受管调用被拒 | `done=True, "paused node <n> of run <r>"` |
| `pause` | 不给 | `pause_all` + `set_paused(True)`：之后启动的节点也以暂停状态启动 | 同上 | `"paused the whole run <r>"` |
| `resume` | 给出 / 不给 | 节点：已暂停的恢复，运行中的不动；运行：`resume_all` + `set_paused(False)` | 同上 | `"resumed node <n> of run <r>"` / `"resumed the whole run <r>"` |
| `stop` | 给出 | `supervisor.stop(node)`：请求停止并等进程退出（超时 kill）；已结束的直接返回；pending 的拒绝 | 同上 | `"stopped node <n> of run <r>"` |
| `stop` | 不给 | `runtime.stop(run)` → 停下全部节点，运行以 `stopped` 结束 | 同上 | `"stopped the whole run <r>, which ended the run as stopped"` |
| pause/resume/stop | — | 当前没有运行，或运行不被持有/已结束/已关闭：不调用 Runtime | 无记录、不计意图 | `done=False, "there is no open run"` |
| pause/resume/stop | — | Runtime 抛 `RuntimeControlError`（节点不存在、状态不允许） | 无记录、不计意图 | `done=False, text=str(e)` |
| 其他任意字符串 | — | 落进 stop 分支，按 stop 执行（实验 e2_control.py） | 同 stop | 同 stop（疑点 1） |

位置：control.py:103-125；emerge/runtime/supervisor.py:326-358。

`TurnControl.interrupt()`（control.py:69-80）按当时阶段处理：

| 中断时轮次在做什么 | 处理 | 返回 |
|---|---|---|
| 没有轮次，或 task 已结束 | 不动 | `False` |
| 等模型（`phase="model"`，含压缩摘要请求） | 立即 cancel 轮次 task，连带取消正在进行的模型请求；`_drive` 整轮 truncate 后重新抛出，task 以 cancelled 结束 | `True` |
| 执行工具（`phase="tool"`） | 不 cancel；`control()` 一直等到工具做完；下一个边界 hook 返回 False → `_Cancelled` → 整轮 truncate | `True` |
| 两步之间（`phase=None`：hook、构造请求、轮次 task 还没开始跑） | 立即 cancel；task 还没开始跑时这一轮的消息从未写入（疑点 2） | `True` |

中断与关闭的区别：

| | 中断（`interrupt`） | 关闭（`Session.close`） |
|---|---|---|
| 入口 | `TurnControl.interrupt` | `detach` → `AgentLoop.close_turn` |
| 正在出的回复 | 取消，整轮丢弃 | 取消，不写 |
| 正在执行的工具 | 等完成，然后整轮丢弃 | 等完成，结果照写 |
| 同一回复里还没执行的调用 | 随整轮丢弃 | 各补 `"not executed: session closed"` |
| History / 库 | truncate / `discard_turn` | 已完成的保留 |
| 会话状态 | `idle` | 保持 `turn_open`，重开时接着做 |

## 会话：`session.py`

`Session` 把一次对话落到存储：History 的每次变化同步通知监听者（`Session`），写入排进一个队列，由一个后台任务按序执行；`Session.create/open/close` 驱动主 Agent 的 `attach/start/detach`。

### 写库

| 写入 | 触发 | 方式 | 位置 |
|---|---|---|---|
| `append_message(会话, 轮号, 消息)` | 新开一轮、追加消息：用户消息、收完整的回复、工具结果、运行信息、合并进来的 inbox 消息、关闭时补的 not executed | 队列 | session.py:137-142,185-186 |
| `remove_pending_input(seq)` | 该用户消息的 `append_message` 之后，按消息对象（`id(message)`）匹配 | 队列，紧跟其后 | session.py:160-162,187-190 |
| `discard_turn` | `truncate_to_turn_count`（中断/取消），被丢的每一轮一条 | 队列 | session.py:144-146 |
| `compact_messages` + `set_system_snapshot` | 压缩完成 | 队列；快照在执行时才求值 `system_source()` | session.py:148-152 |
| `add_pending_input` | `submit` | 直接 await | session.py:156-158 |
| `update_session(status=...)` | 状态真的变了（`turn_open`/`idle`） | 队列 | session.py:164-167 |
| `update_session(intent_count=...)` | `submit`、`note_control` | 队列 | session.py:169-170 |
| `update_session(usage=...)` | 每轮结束、每次分叉回答 | 队列 | session.py:172-173 |
| `set_system_snapshot` | 新会话 attach | 队列 | session.py:175-176 |
| 某次写入抛异常 | `logger.exception` 后跳过，后续照写（疑点 12） | — | session.py:197-205 |

流式到一半的回复不在 History 里，所以永远不写。写库任务在第一次排队时才创建（session.py:192-195）。

### 关闭（`Session.close()`）

1. 已关闭直接返回；置 `closed=True`（session.py:110-112）。
2. `agent.detach()`：`status=closing` → 有轮次在跑就 `loop.close_turn()` → 等 `_turn_over`（`_serve` 记完这一轮的结束：用量、事件；`closed` 不改会话状态）→ 取消 `_serve` → 取消全部分叉 → 摘掉 History 监听者 → 解除绑定、`status=detached`（agent.py:350-374）。
3. 对 `runtime.runs()` 里属于本会话、未结束的运行逐个 `close_run`：停进程、运行记为 `closed`、节点保持关闭前的状态（session.py:116-118）。
4. `flush()` 等队列写完，再取消写库任务（session.py:119-123）。

| 关闭时的情况 | 处理 | 位置 |
|---|---|---|
| 主 Agent 空闲 | 取消睡在 inbox 上的 `_serve`；会话状态保持 `idle` | agent.py:361-365 |
| 模型正在输出 | `close_turn` 取消 `_inflight`；回复不写；以 `closed` 结束；状态保持 `turn_open` | loop.py:118-127,202-214 |
| 工具正在执行 | 等它做完、结果照写；同回复余下的调用补 `not executed: session closed` | loop.py:188-192 |
| 正在压缩（摘要请求在 `_inflight` 里） | 一并取消，History 不变 | loop.py:202-214 |
| `start(unfinished_turn=True)` 后立刻关闭、轮次还没开跑 | `_serve` 在开跑前被取消；库里状态仍是原来的 `turn_open` | agent.py:361-365 |
| 分叉正在回答 | 取消；节点收不到回答，提问在重开时作废 | agent.py:366-369 |
| inbox 里还没取走的项 | 用户消息：`pending_inputs` 行还在，下次打开放回；notice/qa/control：丢弃（不持久化） | — |
| `_carry` 里还没合并的项（轮次在第一个边界前就以 closed 结束） | 同上 | agent.py:429-433 |
| 运行已结束（finished/stopped） | 不动，仍留在 `runtime.runs()` 里 | session.py:117 |
| 关闭后 Runtime 推来的反馈 | `status` 已是 detached，丢弃 | agent.py:244-245 |
| 会话没有挂主 Agent（`session.agent` 为 None） | 跳过 detach 与 `close_run`，只等写库队列 | session.py:113-119 |

### 打开（`Session.open(agent, session_id)`）

1. `get_session`；不存在抛 `StorageError("no session '<id>'")`（session.py:92-94）。
2. `load_messages`（最近的摘要起、去掉已压缩/已丢弃的行）→ `history_from_rows`：轮号相同的连续行是一轮，`summary` 取该轮首行（session.py:95,208-217）。
3. `agent.attach(session)`：用存下的意图计数、用量、system 快照（session.py:96）。
4. 会话里状态为 `closed` 的运行逐个 `runtime.resume_run`，收集 `ResumeReport`（不询问用户）（session.py:97-101）。
5. `agent.start(reports, unfinished_turn=record.status == "turn_open", pending=pending_inputs)`（session.py:102-106）。

| 打开时的情况 | 处理 | 位置 |
|---|---|---|
| 主 Agent 已在服务别的会话 | `attach` 抛 `RuntimeControlError` | agent.py:266-267 |
| 关闭时有一轮没做完（`turn_open`） | `continue_turn` 在同一轮末尾追加用户消息 `[Session reopened] ... This turn was in progress when the session closed. Everything it did before is above: do not repeat it. A tool result 'not executed: session closed' means that call never ran; ...` + 各运行的 `ResumeReport.describe()`，接着跑；`status` 当场为 `working` | agent.py:83-93,342-345,379-380 |
| History 为空却是 `turn_open` | `continue_turn` 退化为 `run_turn` | loop.py:110-111 |
| 关闭时空闲、有恢复报告 | 不接着做任何一轮；`[Session reopened] ...`（不含"这一轮"那句）+ 报告存为 `_reopen_note`，随下一条运行信息送到。有节点重跑时，它们的状态变化通知会叫醒主 Agent，报告就随那一次的运行信息到达；否则等用户说话 | agent.py:346-347,523-533 |
| 关闭时空闲、没有报告 | 什么都不做，睡眠 | agent.py:348 |
| `pending_inputs` 里有消息 | 按存储顺序放回 inbox（`counted=False`）；`turn_open` 时在接续轮的第一个边界合并，否则唤醒并开新一轮 | agent.py:338-340 |
| `resume_run` 期间 Runtime 推来的反馈 | `attach` 后 `status` 已是 `sleeping`，照常进 inbox | agent.py:244,327 |
| 会话的运行状态不是 `closed`（finished/stopped，或库里留着 running 等） | 不 resume，不被持有；只读工具报 "has ended and is no longer held"，`start_run` 可开新运行 | session.py:100；tools/__init__.py:54-59 |
| `record.system` 为 None | 生成并写快照 | agent.py:325-326 |
| `resume_run` 抛错 | `open` 中断：主 Agent 已 attach 但没 start，`Session` 对象没返回（疑点 7） | session.py:96-101 |

### 压缩

| 步骤 | 内容 | 位置 |
|---|---|---|
| 触发 | 每次模型调用前，估算 input tokens > `max_context_tokens × compact_threshold` | middleware.py:128-133 |
| 摘要 | 最旧的若干轮转成文字记录，用 `prompts/compact.md` 请模型写交接说明（用量计入本轮） | context.py:157-192 |
| 替换 | `History.compact`：旧轮换成一条摘要轮，并去掉保留轮里的 thinking；监听者只收到一次 `compacted` | messages.py:84-93 |
| 写库 | `compact_messages(会话, 摘要, 保留各轮)`，紧接着 `set_system_snapshot(新 system)` | session.py:148-152 |
| system | `rebuild_system_prompt()` 重读片段；请求按新 History 重建 | context.py:181-184；middleware.py:129-130 |

### 其他

| 接口 | 行为 | 位置 |
|---|---|---|
| `Session.create(agent, *, title="")` | `create_session` → `attach` → `start()`；`attach` 失败时库里已留下这条会话记录 | session.py:79-86 |
| `Session.list(storage, *, limit=None)` | 按最近更新排序的会话记录（`/sessions` 用） | session.py:125-128 |
| `Session.delete(storage, session_id)` | `delete_session`：删对话、pending、会话记忆索引、它的运行记录，`rmtree sessions/<id>/`；全局记忆不动；有未结束（running/idle/paused）的运行时抛 `StorageError`；不检查会话是否正被服务 | session.py:130-133；emerge/storage/sqlite.py:441-462 |
| `current_run_id` | 构造时取 `record.current_run_id`；`start_run` 工具改内存值（库里由 `create_run` 改）；运行结束后不清空 | session.py:63；tools/execution.py:37 |

## 记忆：`memory.py`

两份 `MEMORY.md`，文件为准，SQLite 只做索引（条目全文、图片登记、上次索引的文件摘要）。`MainAgent.attach` 建 `MemoryStore`，`write_memory` / `search_memory` 工具调用它；`_memory_text` 把两个文件的正文放进 system。

| 范围 | 文件 | 图片目录 | 谁能看到 | 删除 |
|---|---|---|---|---|
| global（存储 scope 为 `"global"`） | `memory/MEMORY.md` | `memory/asset/` | 所有会话 | 不随会话删 |
| session（scope 为会话 id） | `sessions/<id>/memory/MEMORY.md` | `sessions/<id>/memory/asset/` | 本会话 | `Session.delete` 时删索引与整个 `sessions/<id>/` |

`split_entries` 切条目的规则（memory.py:62-106）：

| 行 | 归属 |
|---|---|
| 第 0 列以 `- `、`* `、`+ `、`1. `、`1) ` 开头 | 开始新条目 |
| 缩进的行（空格或 Tab 开头），且已有当前条目 | 并入当前条目；中间的空行保留为空行（列表项和普通段落都这样处理） |
| 空行后的非缩进行 | 结束当前条目，开始新条目 |
| 紧跟在条目后、无空行、无缩进的行 | 并入当前条目（段落续行或列表项的懒续行） |
| 代码围栏（``` 或 ~~~）内 | 原样并入，直到以同一标记开头的行；未闭合的围栏吞掉文件余下部分 |
| 第 0 列 `#`～`######` 标题、分隔线（`---`、`***`、`- - -`、`___`） | 结束当前条目，自身不是条目 |
| 文件开头没有当前条目时的缩进行 | 开始新条目 |

写、刷新、搜索：

| 操作 | 行为 | 位置 |
|---|---|---|
| `write(scope, text, images)` | 条目格式：`- <首行>`，其余行缩进两格（空行留空）；每张图先 `copy_memory_asset` 复制到 `asset/<sha256><后缀>`（同内容只存一份），`add_memory_asset` 登记（同路径则更新），再加一行 `  ![<说明，"]" 换成 ")">](<相对工作区路径>)`；与已有内容之间空一行追加；最后 `refresh` | memory.py:140-157,192-196 |
| `refresh(scope)` | 文件 sha256 与登记的相同 → 跳过；否则 `replace_memory_entries` 整体替换，并把文件全文里不再出现其路径的登记图片删掉（登记与文件一起） | memory.py:159-171 |
| 何时 `refresh` | `attach` 时两个范围各一次；每次 `search` 前两个范围各一次；`write` 后该范围一次。用文件工具改 `MEMORY.md` 后不立即刷新，下次搜索时才刷新 | agent.py:273；memory.py:156,179 |
| `search(query, limit)` | 在 global 与本会话两个范围全文搜索（FTS5：中文按字、拉丁词整词不分大小写），按相关度排序；每条结果附该范围登记、且路径出现在条目文本里的图片 | memory.py:177-189 |
| 进 system | 只放两个文件的正文（图片为 Markdown 路径）；会话开始时定下，之后的修改在压缩或新会话时生效 | agent.py:515-521 |

| 简易版做了 | 没做 |
|---|---|
| 两种范围的文件与 asset 目录、条目索引、文件摘要比对、两个工具、文件工具直接编辑后由摘要发现变化、图片复制与清理 | 自动提取（会话结束或压缩时让模型挑值得记的内容）、自动合并去重；两种记忆各放什么的规则（`prompts/memory.md` 只写工具用法） |

## 工具：`tools/`

所有工具用 `@tool` 定义、在代码里注册（agent.py:288-297）；每个模块的 `make_tools(ctx)` 围绕同一个 `ToolContext` 构造。

### `ToolContext` 与通用错误格式

| 方法 | 用途 | 失败（`ToolError`） | 位置 |
|---|---|---|---|
| `run_id()` | 会话当前运行 id | 没有：`there is no run yet; start_run creates one` | tools/__init__.py:40-45 |
| `held_run_id()` | 查看用：运行须仍被 Runtime 持有（已结束也行）；`show_graph`、`run_status`、`node_detail` 用 | 不被持有：`run <id> has ended and is no longer held; start_run begins a new one` | tools/__init__.py:54-59 |
| `open_run_id()` | 修改用：须被持有且没结束；`commit_graph`、`reset_graph`、`pause`、`resume`、`stop_node`、`end_run`、`send_to_node` 用 | 已结束：`run <id> has ended (<status>); start_run begins a new one` | tools/__init__.py:61-67 |
| `relative(path)` | 转成相对工作区的写法；不在工作区内则原样 | — | tools/__init__.py:47-52 |

| 情况 | 返回给模型 | 位置 |
|---|---|---|
| 工具内抛 `ToolError`（或任何 `Exception`） | `{"tool": "<name>", "error": "ToolError: <msg>"}`，`is_error=True`；其他异常类名照写，如 `UnicodeDecodeError: ...` | tool.py:55-61 |
| 参数校验失败（缺参数、类型错、多余字段） | `{"tool": ..., "error": "invalid arguments", "details": [{"loc": ..., "msg": ...}]}` | tool.py:49-53 |
| 参数不是合法 JSON | `{"tool": ..., "error": "arguments are not valid JSON: <raw>"}` | tool.py:47-48 |
| 工具名不存在 | `{"tool": ..., "error": "unknown tool; available: ..."}` | loop.py:237-239 |
| 被过期校验拦下 | `{"tool": ..., "error": "not executed: ..."}`（无 `ToolError:` 前缀） | agent.py:120-121 |
| `commit_graph` 各种被拒 | 普通文本，`is_error=False`（实验 e3_tools.py） | tools/graph.py:54-77 |

### 20 个工具

按注册顺序列出；工作区没有 skill 时不注册 `use_skill`，只有 19 个；`extra_tools` 追加在最后。

| 组 | 工具 | 参数 | 行为 / 返回 | 报错与拒绝 | 受管 |
|---|---|---|---|---|---|
| catalog | `list_node_types` | — | 每类一行：`- <name>: <desc>; outcomes: ...; inputs: p (tag); outputs: ...; IRREVERSIBLE: acts on the environment; generated code, version N` | 没有类型：返回 `No node types are registered.` | 否 |
| catalog | `describe_node_type` | `name`, `version=None` | `NodeEntry.to_dict()` 的 JSON（端口、`params_schema`、`outcomes`、`irreversible`、`reads`、`locator`）；有生成版本时加 `generated_versions` | 找不到：`no node type 'x'[ version 'v']; registered: ...` | 否 |
| catalog | `use_skill` | `name` | `Skill <name>:\n\n<正文>`；只在 attach 时工作区 `skills/` 有可解析的 skill 才注册，工具描述里列出全部名字和描述 | 未知：`no skill 'x'; available: ...` | 否 |
| files | `read_file` | `path`, `offset=None`, `limit=None` | 带行号的文本；图片返回图片 | 见下文 files | 否 |
| files | `edit_file` | `path`, `old_text`, `new_text`, `replace_all=False` | 精确替换 | 见下文 files | 写图文件时是 |
| files | `write_file` | `path`, `content` | 整个文件写入，自动建父目录 | 见下文 files | 写图文件时是 |
| graph | `commit_graph` | `dry_run=False`, `replace=None` | 预演 → 查 description → 正式提交 | 见下文 commit | 是 |
| graph | `reset_graph` | — | 按运行中的图重写图文件 | `open_run_id` 的错误 | 是 |
| graph | `show_graph` | — | `run <id>: <status>` + `to_text(图, 状态, 结局)` + 不可逆节点列表 + 图文件状态（有未提交改动 / 基于旧版本 / 与运行中的图一致） | `held_run_id` 的错误；已结束但仍被持有的运行可看 | 否 |
| graph | `register_node_code` | `name`, `source` | 写 `code/<name>/v<N>.py` 并注册 | 见下文 register | 否 |
| execution | `start_run` | — | 建空图运行并设为当前运行；返回运行 id 与图文件路径 | 当前运行被持有且未结束：`run <id> is still open; end_run first, or keep drawing in it` | 是 |
| execution | `run_status` | — | `run <id>: <status>[ (whole run paused)], graph version N`；每节点一行 `id (type, instance): state; outcome o (reason); error: e; irreversible; about: desc`（没有节点写 `(no nodes yet)`）；待答提问；图文件一行 | `held_run_id` 的错误 | 否 |
| execution | `node_detail` | `node_id` | `NodeDetail` 的 JSON（结局与理由、快照、各输出最新消息、产物目录、替换历史、参数、代码版本），旧实例另列 `earlier_instances` | 节点不存在：`no node 'x' in run <r>` | 否 |
| execution | `pause` | `node_id=None` | 节点或整个运行；`<n> is now paused` / `run <r> is now paused` | Runtime 拒绝（如 `node 'n' is finished; only a starting or running node can be paused`） | 是 |
| execution | `resume` | `node_id=None` | 节点或整个运行；运行中的节点 resume 不报错 | 如 `node 'w' is pending; only a paused node can be resumed` | 是 |
| execution | `stop_node` | `node_id`（必填） | 停一个节点并等进程退出；已结束的直接回报其状态 | pending：`node 'w' has not started; remove it instead of stopping it` | 是 |
| execution | `end_run` | `stopped=False` | `runtime.end_run`（finished）或 `stopped=True` 时 `runtime.stop`（stopped）；`run <id> ended as <status> at graph version N` | 已结束：`open_run_id` 的错误 | 是 |
| execution | `send_to_node` | `node_id`, `text`, `data=None` | 已启动：`delivered to <n> (<iid>) as message <seq>`；未启动：`queued for <n> (<iid>): it has not started yet ...` | `not delivered: no node 'zz' in the graph` / `not delivered: node 'n' is finished; ...` | 是 |
| memory | `write_memory` | `text`, `scope="session"`（或 `"global"`）, `images=None`（每项 `{path, description}`） | 追加一条，图片复制进 `asset/`；`Written to <MEMORY.md>:\n<条目>` | 见下文 memory | 否 |
| memory | `search_memory` | `query`, `limit=10` | 两个范围全文搜索，每条 `[global] <条目>` 或 `[session] <条目>`，其下列图片行 | 没结果：`No memory entry matches '<q>'.` | 否 |

### files：路径规则与精确替换

路径解析（`resolve_path`，tools/files.py:30-46）：相对路径接在工作区后，`~` 先展开，再 `resolve()`（跟随符号链接，路径可以不存在）。

| 情况 | 处理 | 位置 |
|---|---|---|
| 解析后不在工作区内（`../`、绝对路径、指向外部的符号链接、`~/x`） | `<path> is outside the workspace <root>` | tools/files.py:37-38 |
| 解析后是 `config.json`（含 `./config.json`、`runs/../config.json`、指向它的符号链接） | `config.json holds the API keys; it can be neither read nor written` | tools/files.py:40-41 |
| `.emerge` 或其下 | `.emerge/ holds the runtime's database; it is not for editing` | tools/files.py:42-43 |
| `sessions/<别的会话 id>/...` | `that belongs to another session` | tools/files.py:44-45 |
| `sessions` 本身、`sessions/<本会话>/...`、`runs/<任意运行>/...` | 允许 | tools/files.py:44 |
| 空字符串 | 解析成工作区根目录；`read_file` 报 ` is a directory` | — |

`read_file`（tools/files.py:75-109）：

| 情况 | 返回 |
|---|---|
| 不存在 / 是目录 | `<path> does not exist` / `<path> is a directory` |
| 后缀 `.png .jpg .jpeg .gif .webp`（不分大小写） | `Image: <rel>`，`images=[<rel>]`；不检查内容 |
| 不是 UTF-8 | `<path> is not a text file` |
| 空文件 | `<rel> is empty` |
| `offset` 为 None 或 ≤ 0 | 从第 1 行起 |
| `offset` 超过行数 | `<path> has N lines; offset K is past the end` |
| `limit` 为 None 或 ≤ 0 | 2000 行 |
| 单行超过 2000 字符 | 截到 2000 字符加 ` ...(line cut)` |
| 正常 | `<rel>, lines a-b of n[; k more after line b]:` + 每行 `%6d\t<内容>` |

`edit_file`（tools/files.py:111-142）：

| 情况 | 处理 |
|---|---|
| 文件不存在，或路径是目录 | `<path> does not exist; write_file creates a file` |
| `old_text` 为空 | `old_text is empty; use write_file to write a whole file` |
| `old_text == new_text` | `old_text and new_text are the same; nothing would change` |
| `old_text` 出现 0 次（含误带了 `read_file` 的行号前缀） | `old_text does not appear in <path>; read the file again and copy the text exactly` |
| 出现 N ≥ 2 次且 `replace_all=False` | `old_text appears N times in <path>; include more surrounding text to make it unique, or set replace_all` |
| `replace_all=True` | 全部替换，`replaced N occurrence(s)` |
| 恰好一次 | 替换，`Edited <rel>: replaced 1 occurrence(s). <下一步>` |
| 计数方式 | `str.count`，重叠出现只计一次（`"aa"` 在 `"aaa"` 中算 1 次） |
| 不是 UTF-8 | 未专门处理：`UnicodeDecodeError: ...`（实验 e1_files.py） |
| 文件是 CRLF 换行 | `read_text`/`write_text` 的通用换行使整份文件变成 LF（实验 e1_files.py） |
| 没先读、或读后文件被改过 | 不检查，照样按当前内容替换 |
| 路径是 `runs/<id>/graph.json` | 受过期校验；提示要 `commit_graph` |

`write_file`（tools/files.py:144-161）：目标是目录 → `<path> is a directory`；否则建父目录、写入，返回 `Created|Replaced <rel> (N lines). <下一步>`（空内容为 `0 lines`）。

"下一步"一句（`_effect`，tools/files.py:166-175）：

| 写的文件 | 附加的说明 |
|---|---|
| `runs/<id>/graph.json` | `The graph file is a draft: nothing changes in the run until commit_graph.` |
| `code/...` | `Code takes effect only once registered with register_node_code.`（不会自动注册） |
| 任何名为 `MEMORY.md` 的文件 | `Memory searches see it right away; the system prompt picks it up after the next compaction or session.` |
| 其他 | `Writing a file has no effect on the run by itself.` |

### `commit_graph` 与 `reset_graph`

`commit_graph` 的流程（tools/graph.py:36-81）：

1. `ctx.open_run_id()`；没有运行 / 不被持有 / 已结束 → `ToolError`。
2. 读图文件（读不到当作 `""`），算 `key = (当前图版本, sha256(文本), tuple(replace))`。
3. 非 dry run 且 `refused[run_id] == key` → 直接返回"同一份文件刚被拒"，不调用 Runtime（"不重复提交同一份文件"在工具层兜底，决策记录［阶段 8］计划之外加的）。
4. 一律先 `runtime.commit_graph_file(run_id, replace=..., dry_run=True)`。
5. 预演没有报错 → 新增节点与换实例节点（含 `replace` 列出的）在文件里 `description` 为空（只看去掉空白后）的，列出并拒绝；非 dry run 时记入 `refused`。第 2 步读到的文本解析失败时视为没有缺的（tools/graph.py:160-169）。
6. 非 dry run、预演没报错且有改动 → 正式 `runtime.commit_graph_file(run_id, replace=...)`（与预演之间不持锁）。
7. 有错 → 非 dry run 时记入 `refused`，按错误种类加后缀返回。
8. 没错 → 清掉 `refused[run_id]`；正式提交成功时加 `The graph file was rewritten at version N; read it again before the next edit.`

`refused` 是 `make_tools` 闭包里的字典，每次 attach 重建，每个运行只记最近一次被拒的 key（tools/graph.py:34）。dry run 既不记录也不受它拦。

| 情况 | 返回给模型 | 位置 |
|---|---|---|
| 与刚被拒的同一份文件（版本、内容、`replace` 都相同），非 dry run | `not committed: this is the same file that was refused a moment ago, at the same graph version. Change it according to that error first, or do it another way (stop_node, a new node, reset_graph).` | tools/graph.py:54-59 |
| 图文件不存在 | `not committed (missing_file): <绝对路径> does not exist; reset_graph_file writes the current graph` + `The file was left exactly as you wrote it; fix it in place and commit again.`（疑点 3、4） | emerge/runtime/mutations.py:333-336；tools/graph.py:77 |
| JSON 语法错、重复键、字段错、引用文件里没有的节点 | `not committed (format): graph file is not valid JSON: line L column C: ...` 等 + 同上后缀 | emerge/runtime/mutations.py:337-340 |
| `graph_version` 与当前图不同 | `not committed (stale): the graph file was written from graph version A, but the graph is at version B now; take a fresh file of the current graph and make the edit again. Call reset_graph_file, read the file again and redo the edit.` + `The file was not changed. Call reset_graph, read the file again and redo the edit.` | emerge/runtime/mutations.py:341-348；tools/graph.py:75-76 |
| `replace` 里有不同时在图和文件中的节点（版本没过期时） | `not committed (rejected): cannot replace 'zz': only a node in both the graph and the file has an instance to replace` + "fix it in place" 后缀 | emerge/graph/spec.py:312-318 |
| 运行状态不允许（删运行中的节点、改已结束的节点等） | `not committed (rejected): node 'h' is running: only a node that has not started can be removed; stop it instead; it then stays in the graph` + "fix it in place" 后缀；dry run 返回同样的拒绝 | emerge/runtime/mutations.py:133-141,192-195 |
| 新增或换实例的节点 `description` 为空（dry run 也这样报） | `not committed: new and replaced nodes need a description saying what they do; empty for: <ids>. The file was not changed; add the descriptions and commit again.` | tools/graph.py:61-68,160-169 |
| 文件与运行中的图一致 | `no changes; the graph stays at version N` | emerge/runtime/mutations.py:83-84,350-351 |
| dry run 通过 | `dry run, nothing committed` + 每项改动的影响行（added/removed/replaced/modified/rewired/connected/disconnected）+ 按去向分组：`starts now` / `waiting for start edges` / `can never start (skipped)` / `starts once the old instance has stopped` | emerge/runtime/mutations.py:79-99 |
| 提交成功 | `committed: version A -> B` + 影响行 + 去向（`started` 等）+ 重读提示 | 同上；tools/graph.py:79-80 |
| 预演通过、正式提交时被拒（两次调用之间节点状态变了） | 正式提交的拒绝原因 + 后缀；记入 `refused` | tools/graph.py:69-77 |

`reset_graph`（tools/graph.py:83-93）：`open_run_id` → `runtime.reset_graph_file`（在提交锁内按运行中的图强制重写文件并记摘要）→ 清 `refused[run_id]` → `<rel> now holds graph version N, with no uncommitted edits.`。Runtime 本身允许对已结束的运行重写，工具层用 `open_run_id` 拒绝。

### `register_node_code`（tools/graph.py:115-155）

| 情况 | 处理 |
|---|---|
| `name` 不匹配 `^[A-Za-z][A-Za-z0-9_]*$`（它也是目录名） | `type name 'x' must start with a letter and hold only letters, digits and underscores`；不写文件 |
| 版本号 | `code/<name>/` 下已写版本与注册表里该名字版本中最大的数字 + 1；对应文件已存在就继续加一（tools/graph.py:172-179） |
| 写文件 | `write_generated_code` 写 `code/<name>/v<N>.py` |
| 语法错误、导入失败 | 删掉刚写的文件；`not registered: cannot load node file <绝对路径>: SyntaxError: ...` |
| 文件声明的类型不是恰好一个、且名为 `name`（在一个临时 `NodeRegistry` 里试注册，免得别的名字进入真实目录） | 删文件；`not registered: the file must declare exactly one node type named 'x'; it declares [...]` |
| 真实注册表拒绝（如与配置模块的类型重名，`RegistryError`/`GraphValidationError`） | 删文件；`not registered: <原因>` |
| 其他异常 | 删文件；`not registered: <类型>: <消息>` |
| 成功 | `registered <name> version N from code/<name>/vN.py: outcomes ...; in p (tag), out q (tag)\|no ports; reversible\|irreversible. In graph.json write "type": "<name>" (and "code": "N" to move an existing node onto this version).` |

### memory 工具（tools/memory.py）

| 情况 | 处理 | 位置 |
|---|---|---|
| `text` 只有空白 | `the entry is empty` | tools/memory.py:49-50 |
| `scope` 不是 `global`/`session`；图片项多了字段 | 参数校验失败（`Literal`；`ImageToKeep` 为 `extra="forbid"`，`path` 与 `description` 必填）；`MemoryStore.scope` 对其他名字另有 `ValueError`，工具层先挡住了 | tools/memory.py:21-27,35；memory.py:125-131 |
| 图片路径越界、是 `config.json`、在 `.emerge/` 或别的会话目录 | 与文件工具相同的 `ToolError` | tools/memory.py:81-82 |
| 图片路径是目录 | 目录下按后缀恰好一张图片就用它（如节点的引用目录）；否则 `<path> holds N images; name the image file itself` | tools/memory.py:83-87 |
| 图片路径不存在 / 后缀不是图片 | `<path> does not exist` / `<path> is not an image (.gif, .jpeg, .jpg, .png, .webp)` | tools/memory.py:88-91 |
| 任一图片不合格 | 什么都不写：先校验全部图片再写 | tools/memory.py:51-53 |
| 写入成功 | `Written to <MEMORY.md 相对路径>:\n<条目>`；会话范围的文件和目录第一次写时创建 | tools/memory.py:53-55；memory.py:152-155 |
| `search_memory` 的 `limit` ≤ 0 | 当作 1 | tools/memory.py:68 |
| 查询无法解析（只有 FTS5 语法字符，如 `"`） | 存储层返回空 → `No memory entry matches '<q>'.` | emerge/storage/sqlite.py:597-602 |
| 命中 | 每条 `[global] ` 或 `[session] ` + 条目全文，其下每张图一行 `    image <path>: <description>` | tools/memory.py:71-76 |
| 别的会话的记忆 | 搜索只查 global 与本会话两个范围；文件工具拦 `sessions/<别的 id>/` | memory.py:121-123；tools/files.py:44-45 |

## 提示词：`prompts/`

| 文件 | 要点 |
|---|---|
| `system.md` | 身份：编排者，活由节点做；职责：以用户最新的话为准、分阶段画图、简短说明在做什么（用用户的语言）、只在真需要用户决定时提问。会被四类消息唤醒（用户、`[Run notifications]`、`[Node questions answered]`、`[Control channel]`），`[Run status ...]` 只是信息；没有等节点的工具，提交后就结束这一轮。动环境前先确认状态，失败先查清，不编造节点类型 |
| `graph.md` | 图的语义（并行进程、数据订阅与启动边的 AND/OR、结局按类型不同、只认节点自己给出的结局、skipped、不自动重启）；端口消息只带引用、内容时刻判先后；按阶段续写、图只增不改；改图三步（读 → 精确编辑 → `commit_graph`，拿不准先 dry run）及文件格式示例、description 必填、`graph_version` 不动；Runtime 强制的规则与被拒后的做法；不可逆动作失败先感知再前进；重开会话怎么读报告；符合预期的状态变化至多一句话 |
| `tools.md` | 用工具查事实；路径规则；先读后改、`old_text` 不带行号；改文件要提交或注册才生效；看结果、不原样重试失败的调用；"not executed: the user said something new" 的含义；改图/改运行的调用一次一个；何时用 skill |
| `memory.md` | 两份记忆及何时 `write_memory`（用户偏好与更正、费力查到的事实、失败教训，global 只放跨会话成立的）；图片会被复制；何时 `search_memory`、用 `read_file` 看图；直接编辑 `MEMORY.md` 时搜索立即可见、system 要到压缩或新会话 |
| `compact.md`（阶段 3，压缩时用） | 写给接手者的交接说明：用户意图、已完成、未完成、已做的环境动作、关键事实，保留原样的名字、路径、数字 |

## 疑点（待核实）

1. **未知 action 会被当成 stop。** control.py:113-121 的 `else` 分支接住 interrupt/pause/resume 以外的任何字符串，`ControlAction` 只是类型标注、没有运行时校验。实验 e2_control.py：`control("foo")` 停掉了整个运行，`control("Pause", "n1")` 停掉了节点 n1，两者都 `done=True` 并计入意图。
2. **中断落在轮次 task 开跑之前时，消息既不在对话里、也没被删，重开后会"复活"。** `_serve` 取出消息、`hold_pending` 后建 task（agent.py:385-398）；若同一 tick 内 `interrupt` 先执行，task 在 `run_turn` 运行前被 cancel（control.py:75-79），`start_turn` 从未调用，`pending_inputs` 行保留。实验 e4_agent.py：`submit` 后立即 `control("interrupt")`，History 与库里都没有这条消息、`pending_inputs` 仍有，重开会话后它被当作新消息处理。同类：轮次在第一个边界前结束（关闭或中断）时，`_carry` 里的 notice/qa/control 被直接丢掉（agent.py:405,429-433）；`Session._pending` 以 `id(message)` 为键，这些没写入的消息的条目永不清除，对象回收后 id 可能被复用而误删别的 pending 行（session.py:71,160-162,187；未实验）。
3. **给模型的文字里出现不存在的工具名 `reset_graph_file`。** 过期拒绝同时说 "Call reset_graph_file ..." 和 "Call reset_graph ..."（emerge/runtime/mutations.py:347 + tools/graph.py:76）；文件缺失时说 "reset_graph_file writes the current graph"（mutations.py:335）；重开报告说 "reset_graph_file before editing"（emerge/runtime/checkpoint.py:104）。实验 e3_tools.py 已见前两处原文。
4. **图文件被删时各处说法不一。** `commit_graph` 给 `missing_file` 加上 "The file was left exactly as you wrote it; fix it in place"（tools/graph.py:77），且错误里是绝对路径；而 `graph_file_status` 对不存在的文件返回 `has_changes=False`（emerge/runtime/mutations.py:370-371），于是 `run_status`/`show_graph` 说 "same as the running graph"（tools/execution.py:56-59，tools/graph.py:111-112），运行信息说 "matches graph version N"（agent.py:548）。实验 e3_tools.py 已复现。
5. **历史里引用的图片被删后，之后每次请求都失败。** provider 构造请求时读不到图片抛 `ModelError("cannot read image ...")`（emerge/models/base.py:180-189，实验 e5_image.py 离线复现），轮次以 `error` 结束且历史不变，分叉回答也会失败；只有压缩会去掉图片。可能的触发：`read_file` 把 `memory/asset/...` 图片放进工具结果（tools/files.py:90-91），之后编辑 `MEMORY.md` 删掉引用，下一次 `search_memory` 的 `refresh` 删除该文件（memory.py:166-170）。
6. **模型调用失败的一轮既不回滚也不留说明。** `_drive` 只处理取消（loop.py:129-151），`ModelError` 直接成为 task 异常，`_run_turn` 记 `error` 并把会话置 `idle`（agent.py:409-419）；这一轮已写的消息留在 History 与库里，模型下次看不到失败的说明。实验 e4_agent.py：下一次请求出现两条相邻的用户消息（"please do it"、"try again"）。异常路径也不调用 `notify_turn_end`，压缩的精确 token 计数不更新。
7. **`attach` 不是原子的，失败后主 Agent 无法恢复。** agent.py:268-270 先绑定会话，之后的 `memory.refresh_all`、`Toolset`（重名抛 `ToolError`，agent.py:288-297）等一旦抛错，`status` 仍是 `detached`，`detach()` 在 agent.py:353-354 直接返回，绑定解不开，此后 `Session.create/open` 都报 "already serves session"。实验 e6_attach.py：`extra_tools` 里放一个叫 `read_file` 的工具即复现，且每次 `create` 失败都在库里留下一条空会话（session.py:82）。`Session.open` 在 `resume_run` 抛错时（session.py:96-101）主 Agent 已 attach 未 start、调用方拿不到 `Session`，需手动 `agent.detach()`。
8. **分叉回答只捕获 `ModelError` 与 `RuntimeControlError`。** 其他异常（如 `runtime.answer` → `answer_ask` 的存储错误）会让 task 静默结束：节点收不到回答、inbox 没有问答记录、没有 `agent.fork answered`，异常只会以 "Task exception was never retrieved" 出现在日志里（agent.py:462-472,247-249）。
9. **`edit_file` / `write_file` 的副作用与提示。** CRLF 文件被整体改成 LF（tools/files.py:132,141）；非 UTF-8 文件报原始 `UnicodeDecodeError`（tools/files.py:132 未捕获）；目录被说成 "does not exist"（tools/files.py:126-127）；任何名为 `MEMORY.md` 的文件都附带记忆说明（tools/files.py:173）；不检查"先读后改"或读后是否被改。前四项实验 e1_files.py 已复现。
10. **文件工具可以读写别的会话的运行目录。** 只拦 `sessions/<别的 id>/`（tools/files.py:44-45），`runs/<任意运行>/` 包括其 `graph.json` 都可读写；写了别的运行的 `graph.json` 会让它成为"草稿"，Runtime 之后不再覆盖它（emerge/runtime/mutations.py:298-310）。计划只要求不能读写别的会话的记忆，是否有意待确认。
11. **`Session.delete` 不检查会话是否正被服务。** 存储只在有 running/idle/paused 的运行时拒绝（emerge/storage/sqlite.py:442-446）；删掉一个正被服务、没有未结束运行的会话后，主 Agent 继续写库只会失败并记日志（session.py:130-133,200-205）。
12. **写库失败时用户消息可能两处都不在。** `_write_all` 记日志后跳过失败的写入（session.py:200-205）；若某条用户消息的 `append_message` 失败，紧随其后的 `remove_pending_input` 照样执行，与 session.py:189 注释"任何时刻至少在一处"不符。

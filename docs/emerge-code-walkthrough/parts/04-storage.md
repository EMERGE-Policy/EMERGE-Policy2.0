# 存储层（阶段 5）

## 概览

| 文件 | 职责 |
|---|---|
| `base.py` | `Storage` 抽象接口（全部是协程）、记录类型、`open_storage()` |
| `sqlite.py` | 唯一实现 `SqliteStorage`：建表与版本检查、一个连接加一把锁、每次调用一个事务、计数器、FTS5 检索 |
| `artifacts.py` | 工作区路径规则、记忆图片复制、生成代码文件、JSON 序列化 |
| `__init__.py` | 重新导出以上公开名字和 `SCHEMA_VERSION` |

调用方是 `runtime/`（阶段 7）和 `agent/`（阶段 8）。存储只管存到哪、怎么读回，什么时候写由调用方决定（base.py:3-4）。包内还没有代码调用 `open_storage`，留给阶段 9 装配。位置写法：`sqlite.py:240` 指 `emerge/storage/sqlite.py:240`，`runtime/bus.py:399` 指 `emerge/runtime/bus.py:399`。下文"实验"指在临时库上跑过的脚本（`/data/yuqingchi/tmp/emerge-code-walkthrough/scratch/storage/probe*.py`）。

## 打开、表结构版本与关闭

`SqliteStorage.open()` 打开或新建 `<workspace>/.emerge/state.db`。`open_storage(workspace, *, port_buffer=64)` 只是转调它（base.py:451-455）。

打开流程：

1. `port_buffer < 1` 时抛 `StorageError("port_buffer must be at least 1, got N")`，这时还没建任何目录（sqlite.py:234-235）。
2. `workspace` 先 `resolve()` 成绝对路径再 `mkdir(parents=True, exist_ok=True)`，然后建 `.emerge/`（sqlite.py:236-239）。`storage.workspace` 因此总是绝对路径。
3. `aiosqlite.connect(path, isolation_level=None)`：连接处于自动提交模式，事务语句由代码自己发。设置 `row_factory = aiosqlite.Row`，按列名取值（sqlite.py:240-241）。
4. 设置下表里的 PRAGMA（sqlite.py:242-250）。
5. `_init_schema()` 读 `PRAGMA user_version`，按版本处理（sqlite.py:251-252, 255-284）。

| 设置 | 值 | 说明 | 位置 |
|---|---|---|---|
| `journal_mode` | WAL | 写进库文件，之后一直有效 | sqlite.py:242 |
| `synchronous` | NORMAL | 对每个连接单独生效。WAL 模式下不是每次提交都 fsync，断电可能丢掉最近几次提交，但不会损坏库。这和"不考虑崩溃"一致 | sqlite.py:243 |
| `foreign_keys` | ON | 没有一张表声明了外键（实验：`PRAGMA foreign_key_list` 全为空），所以这个设置实际不起作用 | sqlite.py:244 |
| `wal_autocheckpoint` | 20000 页（`page_size` 4096，约 80 MB） | 默认是 1000 页。每次写都是独立提交，默认阈值下检查点很频繁，检查点期间写者要停下等。实测 1000 条端口消息从 6.2 s 降到 1.0 s（决策记录［阶段 5］WAL 与检查点） | sqlite.py:58, 249 |
| `journal_size_limit` | 64 MiB | 检查点后 WAL 文件截回到这个大小 | sqlite.py:59, 250 |
| 忙等时间 | 5 s | 代码没有显式设置，用的是 `sqlite3.connect` 默认的 `timeout=5.0`（实验：`PRAGMA busy_timeout` = 5000） | sqlite.py:240 |
| `SCHEMA_VERSION` | 2 | 存在 `PRAGMA user_version` 里 | sqlite.py:53-54 |

| 情况 | 处理 | 位置 |
|---|---|---|
| 新库（`user_version` = 0） | 用 `executescript(_SCHEMA)` 建全部表、索引、触发器（这一步自己提交）。然后在一个事务里设 `user_version=2`，给 `session_seq`、`pending_seq`、`ask_seq` 播种，值取对应表现有行的最大值（新库都是 0），写法是 `ON CONFLICT DO NOTHING` | sqlite.py:269-284 |
| 已是 v2 | 照样执行一遍 `executescript`。语句全是 `IF NOT EXISTS`，只会补建缺失的对象。然后重设 `user_version=2`，不播种 | sqlite.py:269-272 |
| v1（阶段 5 原来的库） | 抛 `StorageError("… has schema version 1, written by older code; schema 2 is not migrated to, so move the file away to start a new one")`。不迁移的原因是 v2 的 `runs` 多了 `paused` 列，而 `CREATE TABLE IF NOT EXISTS` 不会给旧表加列（决策记录［阶段 7］存储的改动） | sqlite.py:263-267 |
| 比代码新（≥ 3） | 抛 `StorageError("… has schema version N; this code understands 2")` | sqlite.py:258-262 |
| `user_version` 为负数（只能是手工设的） | 两个判断都不命中，按新库处理（建表、播种） | sqlite.py:258-272 |
| `user_version` = 0，但库里已有同名表且列不同 | `CREATE TABLE IF NOT EXISTS` 会跳过这张旧表，等到 `CREATE INDEX` 引用不存在的列时抛 `sqlite3.OperationalError`（实验：`no such column: session_id`）。出错之前的语句已经生效（实验中 `sessions` 已建好），之后的语句不再执行 | sqlite.py:269 |
| 读版本时另一个连接正持有写事务 | 读版本也走 `BEGIN IMMEDIATE`，等 5 s 后抛 `sqlite3.OperationalError: database is locked` | sqlite.py:256, 296 |
| 上面任何一种失败 | `open()` 不关闭已经建好的连接。被拒的库文件在第 4 步已经改成了 WAL（实验：v1、v3 库的 `journal_mode` 从 delete 变成 wal）。见疑点 2 | sqlite.py:240-252 |
| `workspace` 指向一个已存在的普通文件 | `mkdir` 抛 `FileExistsError` | sqlite.py:237 |

`aclose()` 先执行 `PRAGMA wal_checkpoint(TRUNCATE)`，把 WAL 并回主库，然后在 `finally` 里关连接（sqlite.py:337-343）。

| 情况 | 处理 | 位置 |
|---|---|---|
| 正常关闭 | WAL 并回主库再关闭。实验中关闭后 `state.db-wal` 文件已不存在 | sqlite.py:341-343 |
| 另一个连接还在读，检查点做不完 | 这条 PRAGMA 会返回一行带 busy 标志的结果。代码不读这个结果，照常关闭（按代码推断，没做实验） | sqlite.py:341 |
| 本连接上另一个协程正在事务中 | `aclose` 不取锁。检查点抛 `OperationalError: database table is locked`，`finally` 仍然关掉连接。那个事务丢失，发起它的协程在下一条语句收到 `ValueError: no active connection`（实验）。见疑点 6 | sqlite.py:337-343 |
| 重复调用 `aclose()`，或关闭后再调用任何方法 | `ValueError: no active connection`（实验） | sqlite.py:341 |

## 表结构总表

库里有 13 张普通表和 1 张 FTS5 虚表（它另有 4 张影子表 `memory_entries_fts_config/_data/_docsize/_idx`），全在一个文件里。复杂值（`Message`、`Usage`、图、快照）都存成 JSON 文本，写法见"写入值的序列化与校验"。v2 表示阶段 7 新增。

| 表 | 主要列 | 存什么 | 谁写（方法 ← 调用方） | 谁读 | 删除 / 清理 |
|---|---|---|---|---|---|
| `sessions` | PK `id`；`title`、`status`（`idle`/`turn_open`）、`system`、`current_run_id`、`intent_count`、`usage`、`metadata`、`created_at`、`updated_at` | 每个会话一行 | `create_session` ← agent/session.py:82；`update_session`（只用到 status、intent_count、usage）← session.py:167, 170, 173；`set_system_snapshot` ← session.py:152, 176；`create_run` 会改 `current_run_id` 和 `updated_at` | `get_session` ← session.py:92；`list_sessions` ← session.py:128 | 只在 `delete_session` 时删 |
| `messages` | PK `(session_id, seq)`；`turn`、`message`、`is_summary`、`is_compacted`、`is_discarded`、`created_at`；索引 `ix_messages_turn(session_id, turn)` | 对话，一条消息一行，只追加 | `append_message` ← session.py:186；`compact_messages` ← session.py:149；`discard_turn`（只改标记）← session.py:146 | `load_messages` ← session.py:95 | 压缩和丢弃都只打标记不删行；只在 `delete_session` 时删 |
| `pending_inputs` | PK `(session_id, seq)`；`message`、`received_at` | 已收到、还没进入对话的用户消息 | `add_pending_input` ← session.py:158 | `pending_inputs` ← session.py:105 | `remove_pending_input` ← session.py:190（这条消息写进 `messages` 之后才删）；`delete_session` |
| `memory_entries` | PK `(scope, position)`；`text`（条目原文）、`index_text`（CJK 字符逐字加空格）；索引 `ix_memory_entries_scope` | 从某个范围的 `MEMORY.md` 切出的条目 | `replace_memory_entries` ← agent/memory.py:165 | `search_memory`（与 FTS 表 JOIN）← memory.py:180 | 每次 replace 先删光该范围再插入；`delete_session` 删会话自己的范围；全局范围不会被删 |
| `memory_entries_fts` | FTS5 external content（`content='memory_entries'`，`unicode61`），只索引 `index_text` | 全文索引 | 触发器 `memory_entries_ai` / `memory_entries_ad` 跟着基表的插入、删除同步。没有 UPDATE 触发器，代码也从不 UPDATE 这张基表 | `search_memory` | 跟随基表 |
| `memory_assets` | PK `(scope, path)`；`digest`、`description`、`source`、`added_at` | 记忆图片的登记，`path` 是相对工作区的路径 | `add_memory_asset` ← memory.py:147 | `memory_assets` ← memory.py:166, 181 | `remove_memory_assets` ← memory.py:168（只删登记，文件由调用方删，memory.py:170）；`delete_session` |
| `memory_files` | PK `scope`；`digest`、`indexed_at` | 每个范围上次建索引时 `MEMORY.md` 的内容摘要 | `replace_memory_entries` | `memory_file_digest` ← memory.py:163 | `delete_session`（只删会话范围） |
| `runs` | PK `id`；`session_id`、`status`、`content_time`、`graph_file_digest`、`paused`（v2）、`started_at`、`finished_at`；索引 `ix_runs_session` | 每次运行一行 | `create_run` ← runtime/run.py:53；`set_run_status` ← run.py:151；`set_run_paused` ← run.py:114；`set_graph_file_digest` ← runtime/mutations.py:315；`next_content_time` 改 `content_time` ← runtime/bus.py | `get_run` ← runtime/service.py:184；`runs_of_session` ← agent/session.py:99；`load_run_state` | 只在 `delete_session` 时删 |
| `graphs` | PK `run_id`；`schema_version`、`data`、`saved_at` | 当前的图，只保留最新一版。`schema_version` 是序列化格式版本（写入时是 3），不是图的版本号，读的时候也不看 | `save_graph` ← service.py:159, mutations.py:173 | `load_graph`（只在 `load_run_state` 里调用） | `delete_session` |
| `node_states` | PK `(run_id, node_id, instance_id)`；`state`、`outcome`、`reason`、`error`、`snapshot`、`updated_at` | 每个节点实例一行，被替换的旧实例那一行也留着 | `set_node_state` ← bus.py:494；`set_node_snapshot` ← bus.py:462 | `node_states`（只在 `load_run_state` 里调用）；`node_snapshot` ← service.py:300, runtime/supervisor.py:193 | `delete_session` |
| `port_messages` | PK `(run_id, node, port, version)`；`message`、`stored_at` | 每个输出端口最近 `port_buffer` 条消息 | `put_port_message` ← bus.py:399 | `port_messages` ← bus.py:471；`latest_port_messages` ← bus.py:183, service.py:292, 376；`load_run_state` | 每次 put 在同一个事务里裁掉窗口外的旧版本；`delete_session` |
| `asks` | PK `id`（形如 `ask<N>`）；`run_id`、`node_id`、`instance_id`、`question`、`status`（pending/answered/void）、`answer`、`created_at`、`answered_at`；索引 `ix_asks_run_status` | 节点提出的问题 | `add_ask` ← bus.py:449；`answer_ask` ← bus.py:309；`void_ask` ← bus.py:326, runtime/checkpoint.py:132 | `pending_asks` ← service.py:189, 281, 305；`load_run_state` | `delete_session` |
| `queued_node_messages`（v2） | PK `(run_id, instance_id, seq)`；`node_id`、`message`（`MainAgentMessage`）、`delivered`、`queued_at`、`delivered_at` | 主 Agent 发给还没启动的实例的消息 | `queue_node_message` ← bus.py:296；`mark_node_messages_delivered` ← bus.py:211 | `queued_node_messages` ← bus.py:187（包括已送达的，用来取最大 seq）, 207；`load_run_state`（只取未送达的） | 送达后只标记不删，bus 重开时按最大 seq 续编（决策记录［阶段 7］存储的改动）；只在 `delete_session` 时删 |
| `counters` | PK `key`；`value` | 单调递增的计数器：`session_seq`、`pending_seq`、`ask_seq`（全库共用），以及每个运行一个 `timeline:<run_id>` | `_counter()`，见"序号与时间线编号" | 同左 | 只有 `timeline:<run_id>` 会随 `delete_session` 删除；三个全局计数器永远不删 |

记录类型都是 `frozen=True`、`extra="forbid"` 的 Pydantic 模型（base.py:38-39）。

| 类型 | 对应 | 字段约束 | 位置 |
|---|---|---|---|
| `SessionRecord` | `sessions` 一行 | `status` 只能是 `idle`/`turn_open`；`intent_count ≥ 0`；`metadata: dict[str, Any]`（键必须是 str） | base.py:42-55 |
| `StoredMessage` | `messages` 一行（不含 compacted/discarded 标记） | `seq ≥ 1`，`turn ≥ 0` | base.py:58-66 |
| `PendingInput` | `pending_inputs` 一行 | `seq ≥ 1` | base.py:69-73 |
| `MemoryHit` | 一条检索命中 | `position ≥ 0` | base.py:76-79 |
| `MemoryAsset` | `memory_assets` 一行 | — | base.py:82-91 |
| `RunRecord` | `runs` 一行 | `status` 取六个值之一；`content_time ≥ 0`；`paused` 默认 False | base.py:94-106 |
| `NodeState` | `node_states` 一行（不含 `snapshot`） | `state` 取九个值之一 | base.py:109-120 |
| `PortRecord` | `port_messages` 一行 | `version ≥ 0`；`message` 是 `graph.ports.Message` | base.py:123-129 |
| `QueuedNodeMessage` | `queued_node_messages` 一行 | `seq ≥ 1` | base.py:132-143 |
| `AskRecord` | `asks` 一行 | `status` 取 pending/answered/void | base.py:146-155 |
| `RunState` | `load_run_state` 的返回值 | 包含 `run`、`graph`（可为 None）、`node_states`、`port_messages`、`pending_asks`、`queued_messages` | base.py:158-168 |

## 事务与并发

一个 `SqliteStorage` 只有一个 aiosqlite 连接和一把 `asyncio.Lock`。每个公开方法（读方法也一样）都是"取锁 → `BEGIN IMMEDIATE` → 执行语句 → `COMMIT`"。aiosqlite 用一个工作线程按提交顺序执行语句。

| 机制 | 代码做什么 | 位置 |
|---|---|---|
| `_transaction()` | 先取锁，再发 `BEGIN IMMEDIATE`。块正常结束就 `commit()`。块里抛出任何 `BaseException`（包括 `CancelledError`）时先 `rollback()` 再抛出 | sqlite.py:288-302 |
| 读也走事务 | `_fetch` / `_fetch_one` 同样取锁、发 `BEGIN IMMEDIATE`，所以读不到别人写了一半的状态 | sqlite.py:312-318 |
| 事务块里只用 `_raw_*` | 锁不可重入，事务块里再调 `_fetch*` 会自己等自己（决策记录［阶段 5］一个连接：实现时修过一次） | sqlite.py:292-293, 304-310 |
| 多步写放在一个事务里 | 压缩（标记 + 摘要 + 保留轮）、替换记忆条目（删 + 插 + 文件摘要）、取时间线号（计数器 + `runs.content_time`）、删会话（全部相关表）、建运行（插运行 + 改会话的当前运行） | sqlite.py:486-508, 572-582, 982-986, 447-461, 663-673 |
| 存在性检查 | 只有部分方法检查。先查会话：`update_session`、`set_system_snapshot`、`delete_session`、`append_message`、`compact_messages`、`add_pending_input`、`create_run`。先查运行：`set_run_status`、`set_graph_file_digest`、`set_run_paused`、`save_graph`、`queue_node_message`、`add_ask`、`next_content_time`。先查提问：`answer_ask`、`void_ask`。查不到就抛 `StorageError("session/run/ask 'x' does not exist")`，并回滚 | sqlite.py:329-335, 925-926, 935-936 |
| 不做存在性检查的写 | `discard_turn`、`remove_pending_input`、`replace_memory_entries`、`add_memory_asset`、`remove_memory_assets`、`set_node_state`、`set_node_snapshot`、`put_port_message`、`mark_node_messages_delivered` 直接写，或者影响 0 行 | 各方法 |

| 情况 | 处理 | 位置 |
|---|---|---|
| 同一进程里多个协程同时调用 | 在锁上排队，`asyncio.Lock` 先来先得，各次调用的语句不会交错。实验：200 个并发 `next_content_time` 得到 1–200，不重不漏 | sqlite.py:295 |
| 协程在事务进行中被取消 | 走 `except BaseException`：回滚，然后随 `async with` 释放锁（实验：取消后标题没变，锁是空闲的） | sqlite.py:299-301 |
| 协程在等待 `commit()` 时被取消 | `commit` 在 try 之外，不会回滚。提交已经排进工作线程，照样生效，但调用方收到的是 `CancelledError`（实验） | sqlite.py:302 |
| "先写、再读回"的方法：`update_session`、`set_system_snapshot`、`set_run_status`、`set_graph_file_digest`、`set_run_paused`、`answer_ask`、`void_ask` | 写和读回各取一次锁，中间可能插进别的调用。所以返回值里可能已经有之后别人的写（实验：并发执行 `update(title=A)` 和 `update(title=B)`，前者返回 B）。如果这一行在中间被删掉，`assert` 失败，抛 `AssertionError`（实验：在 `update_session` 上复现）。见疑点 5 | sqlite.py:426-428, 437-439, 697-699, 717-719, 725-727, 931, 942 |
| `load_run_state` | 由 6 次独立的读组成（运行、端口消息、图、节点状态、提问、排队消息），不是同一时刻的快照。实际调用它的是打开会话时的 `resume`，那时这个运行还没启动，没有并发写 | sqlite.py:990-1004 |
| `delete_session` | 检查运行状态在事务外做，删除在另一个事务里做，而且用的是检查时拿到的运行列表。见"删除会话" | sqlite.py:442-461 |
| 同一个库文件有第二个连接（另一个进程，或同一进程里第二个 `SqliteStorage`） | 两边的 asyncio 锁互相不知道，只能靠 SQLite 的写锁。对方持有事务时，本方的 `BEGIN IMMEDIATE`（读也会发）等 5 s 后抛 `sqlite3.OperationalError: database is locked`（实验） | sqlite.py:296 |
| SQL 层出错（主键冲突等） | 原样抛 `sqlite3.IntegrityError` / `OperationalError`，不包成 `StorageError`；事务已回滚 | sqlite.py:299-301 |

## 序号与时间线编号（`counters`）

`_counter(key)` 在调用方的事务里执行 `INSERT … ON CONFLICT(key) DO UPDATE SET value = value + 1`，然后用 `SELECT` 读回新值。某个 key 第一次用时得到 1（sqlite.py:320-327）。不用 `MAX(...)+1` 的原因有两个：删行后会把号重发一遍；发过号但没写进库的值会被跳过（决策记录［阶段 5］序号与时间线）。

| key | 范围 | 发给谁 | 位置 |
|---|---|---|---|
| `session_seq` | 全库一个 | `messages.seq`。`append_message` 和 `compact_messages` 每插一行取一个号 | sqlite.py:473, 494, 502 |
| `pending_seq` | 全库一个 | `pending_inputs.seq` | sqlite.py:539 |
| `ask_seq` | 全库一个 | `asks.id = "ask" + 号` | sqlite.py:905-906 |
| `timeline:<run_id>` | 每个运行一个，第一次取号时才建 | `next_content_time` 的返回值，同时写回 `runs.content_time` | sqlite.py:984-985 |

| 情况 | 结果 | 位置 |
|---|---|---|
| 事务回滚（会话不存在、压缩中途序列化失败等） | 计数器的 +1 跟着回滚，不留空号（实验：向不存在的会话追加消息后，`session_seq` 仍是 0） | sqlite.py:299-301 |
| 并发取号 | 在锁内串行执行，不重复、不倒退（实验） | sqlite.py:295 |
| 关闭后重开 | 计数器存在库里，接着往下发。`runs.content_time` 和计数器在同一个事务里更新，两者一致 | sqlite.py:981-986 |
| 删除会话 | 删掉该会话各运行的 `timeline:<run_id>`（运行本身也删了）。三个全局计数器不动，所以号永远不会被重复使用 | sqlite.py:459 |
| 同一会话内的 `seq` | 递增但不连续：所有会话共用一个计数器，压缩也会消耗号。阶段 8 只按先后顺序重建 History，不依赖具体值（决策记录［阶段 5］序号与时间线） | sqlite.py:473 |
| 时间线号取走了，但对应的消息没写进库 | `next_content_time` 自己单独提交。盖了这个号的消息由 bus 随后在另一个事务里写（bus.py:251→252、285→296、308→309、386→399、448→449）。汇报和自定义事件盖了号也只写进 trace，不进库（bus.py:418, 443）。所以库里的编号可能有空档，但不会重号。见疑点 7 | sqlite.py:981-986 |
| 新库播种 | 三个全局 key 播种为对应表现有的最大值；`timeline:*` 不播种 | sqlite.py:275-284 |

有两种号不由 `counters` 发：端口消息的 `version` 和 `MainAgentMessage.seq`。它们由 bus 在内存里递增分配，bus 启动时从库里接上：用 `latest_port_messages` 取每个端口的最新版本，用 `queued_node_messages(include_delivered=True)` 取每个实例的最大 seq（bus.py:183-189）。存储层按给定的号原样存，号重复时主键冲突，抛 `IntegrityError`。

## 接口方法：会话

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `create_session(*, title="", system=None, metadata=None)` | 用 `uuid4().hex` 作 id，插一行（status `idle`、usage 为空、intent 为 0），返回记录 | `metadata` 在事务之前先 `dump_json`：不能转成 JSON（如 set）就抛 `TypeError`，库里什么都不写。`metadata` 有非 str 键时，行已提交，构造返回值才抛 `ValidationError`（实验）。返回值里的 `metadata` 是传入的原对象，读回时元组会变成列表（实验） | session.py:82（只传 title） |
| `get_session(id)` | 读一行 | 不存在返回 `None` | session.py:92 |
| `list_sessions(*, limit=None)` | 按 `updated_at DESC, id DESC` 排序 | `limit=0` 返回空；负数在 SQLite 里等于不限条数（实验） | session.py:128 |
| `update_session(id, *, title, status, current_run_id, clear_current_run, intent_count, usage, metadata)` | 只改给出的字段，并且总会更新 `updated_at`；返回读回的记录 | 会话不存在抛 `StorageError`。一个字段都不给时只更新 `updated_at`。`current_run_id` 不检查这个运行是否存在。`clear_current_run=True` 时会忽略同时给的 `current_run_id`，清空优先（实验）。`metadata` 是整体替换，给 `{}` 就清空。`status`、`intent_count` 写之前不校验（见"写入值的序列化与校验"）。读回的竞态见"事务与并发" | session.py:167, 170, 173 |
| `set_system_snapshot(id, system)` | 替换 system 快照，更新 `updated_at` | 会话不存在抛 `StorageError` | session.py:152（压缩后执行，取当时重建好的 system）, 176 |
| `delete_session(id)` | 见"删除会话" | — | session.py:133 |

会改 `sessions.updated_at` 的只有 `create_session`、`update_session`、`set_system_snapshot` 和 `create_run`。追加消息、压缩、排队消息都不改它（sqlite.py:421-422, 434-435, 670-673）。

## 接口方法：对话消息（追加、压缩、丢弃、读回）

对话只追加，写过的行不再改写内容，只改三个标记：`is_summary`、`is_compacted`、`is_discarded`。存储层没有"一轮"这个概念，轮号只是一列，由调用方给出。会话状态什么时候写成 `turn_open` / `idle` 由 agent/session.py:164-167 决定。

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `append_message(session_id, turn, message, *, summary=False)` | 取一个 `session_seq`，插一行。`is_summary` 按参数，另外两个标记为 0 | 会话不存在抛 `StorageError`，计数器不变。`turn` 不校验：负数会写进库，然后返回值和之后的 `load_messages` 都抛 `ValidationError`（实验）。用 `summary=True` 直接写摘要时，前面的行不会被标成 compacted，但 `load_messages` 从最后一条摘要开始读，所以也读不到它们（实验） | session.py:186（`turn_started` 和 `messages_appended` 都走这里） |
| `compact_messages(session_id, summary, kept)` | 在一个事务里依次：① 把该会话所有 `is_compacted=0` 的行标为 1（包括已丢弃的）；② 插摘要行，`turn` 固定为 0，`is_summary=1`；③ 按 `kept` 的顺序逐条插入，轮号用调用方给的。返回新插入的行 | 会话不存在抛 `StorageError`。`kept` 为空时只插摘要。会话里还没有消息时也照样插摘要。`dump_model` 在事务内调用，`kept` 里某条序列化失败时整个事务回滚，标记和计数器都不变。如果 `kept` 里有轮号 0，读回时它会和摘要并成同一轮；阶段 8 给的是压缩后的新下标，从 1 开始（agent/messages.py:92） | session.py:149 |
| `discard_turn(session_id, turn)` | 把该轮里 `is_compacted=0 AND is_discarded=0` 的行标为 `is_discarded=1`，返回改了几行 | 不查会话，不存在就返回 0。重复调用返回 0（实验）。只影响压缩之后的行：压缩前同一轮号的旧行已经是 compacted，不受影响。如果丢弃第 0 轮（摘要），之后 `load_messages` 找不到摘要，只返回剩下未压缩的行（实验中为空） | session.py:146（`turns_dropped(start, stop)` 对每一轮各调一次） |
| `load_messages(session_id)` | 取 `is_compacted=0 AND is_discarded=0` 的行，按 `seq` 升序，从其中最后一条 `is_summary=1` 的行开始返回；没有摘要就从第一行开始 | 不查会话，不存在返回 `()`。某行 JSON 不合法或轮号为负时抛 `ValidationError` | session.py:95（之后由 `history_from_rows` 把轮号相同的连续行合成一轮） |

| 情况 | 结果 | 位置 |
|---|---|---|
| 在会话内取消一轮后，新的一轮用了同一个轮号 | `discard_turn` 只标记还没标过的行。新写的行没有标记，旧行已是 discarded，所以读回只有新行（实验：读回 `u1-again`，读不到被丢弃的 `u1`） | sqlite.py:513-517 |
| 压缩两次 | 第二次把第一次的摘要和保留行也标成 compacted，读回从新的摘要开始 | sqlite.py:490-493 |
| 被压缩或被丢弃的行 | 留在库里，以后不再读回。只有 `delete_session` 会删它们 | sqlite.py:490-493, 513-517 |
| 压缩后保留的轮 | 以新行重写一份（调用方传入的是已去掉 thinking 的版本），旧行标成 compacted。所以库里会同时有新旧两份 | sqlite.py:501-508 |
| 读回顺序 | `seq` 全库单调递增，所以按 seq 排序就是写入顺序 | sqlite.py:523 |
| 会话关闭时一轮没做完 | 存储层不做任何事，已写的行都保留。之后打开会话时由阶段 8 接着这一轮做 | — |

## 接口方法：排队的用户消息

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `add_pending_input(session_id, message)` | 取一个 `pending_seq`，插一行 | 会话不存在抛 `StorageError` | session.py:158 |
| `pending_inputs(session_id)` | 按 seq 升序返回 | 会话不存在返回 `()` | session.py:105（打开会话时） |
| `remove_pending_input(session_id, seq)` | 按（会话，seq）删一行 | 不查会话。行不存在，或 seq 属于别的会话时，什么都不做，也不报错（实验） | session.py:190 |

调用方保证顺序：同一个写库队列里先排"写这条消息"，再排"删 pending 行"，所以一条用户消息任何时刻都至少在一处（agent/session.py:185-190；决策记录［阶段 8］用户消息的持久化）。

## 接口方法：记忆索引与图片登记

记忆的内容是文件：`MEMORY.md` 和 `asset/`。库里没有整份文件的副本，但每条条目的原文存在 `memory_entries.text` 里，用来返回命中结果。这是从文件切出来的索引，文件一改就由调用方整体重建。怎么切条目由阶段 8 决定（agent/memory.py 的 `split_entries`）。重建的时机也由阶段 8 决定：会话打开时、每次搜索前、写记忆后，各做一次比较，文件的 sha256 和 `memory_file_digest` 不同才调用 `replace_memory_entries`（agent/memory.py:156, 159-171, 173-179）。

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `replace_memory_entries(scope, entries, file_digest)` | 在一个事务里：删掉该范围全部条目（触发器同步删 FTS）→ 按顺序插入 `position=0..n-1` 的 `text` 和 `index_text` → upsert `memory_files` 里的摘要和建索引时间 | 不查 scope，任何字符串都行。`entries` 为空时清空该范围，并照样记下摘要（实验）。读的一方要么看到全部旧条目，要么看到全部新条目 | agent/memory.py:165 |
| `search_memory(query, scopes, *, limit=20)` | 把查询转成 MATCH 表达式，限定 `e.scope IN (...)`，按 `f.rank`（bm25）、scope、position 排序，取前 `limit` 条 | 见下面的检索表 | memory.py:180 |
| `memory_file_digest(scope)` | 读 `memory_files.digest` | 从没建过索引返回 `None` | memory.py:163 |
| `add_memory_asset(scope, path, digest, *, description="", source="")` | upsert：同一个（scope, path）再登记一次，会覆盖 digest、description、source、added_at（实验） | 不查 scope，也不查文件是否存在。`path` 应当是 `copy_memory_asset` 返回的相对路径 | memory.py:147 |
| `memory_assets(scope)` | 按 path 排序返回 | 没有就返回 `()` | memory.py:166, 181 |
| `remove_memory_assets(scope, paths)` | `DELETE … AND path IN (...)`，返回删除的行数 | `paths` 为空时直接返回 0，不进事务。不存在的 path、或者属于别的范围的 path 不计入（实验）。只删登记，不删 `asset/` 里的文件 | memory.py:168 |

建索引用的文本由 `_index_text(text)` 生成：每一段连续的 CJK 字符拆成单字，字与字之间、整段前后都加空格（sqlite.py:1088-1090）。例如 `"UR5机械臂"` 变成 `"UR5 机 械 臂 "`，`"用户偏好，颜色"` 变成 `" 用 户 偏 好 ， 颜 色 "`。CJK 的范围是：平假名和片假名 `぀-ヿ`、汉字扩展 A `㐀-䶿`、基本汉字 `一-鿿`、兼容汉字 `豈-﫿`、谚文音节 `가-힯`（sqlite.py:68）。不在范围内的有：中文标点（`，` `。` `（`）、全角拉丁字母、汉字扩展 B 及以后的字。要逐字拆开，是因为 `unicode61` 会把一整串汉字当成一个词，而 `trigram` 要至少三个字才能命中（决策记录［阶段 5］记忆索引）。

查询由 `_match_expression(query)` 转成 MATCH 表达式（sqlite.py:1093-1112）：

1. 用 `_TOKEN_RE.findall` 从左往右切词：遇到 CJK 就取一段连续的 CJK，否则取一段连续的非空白字符。后一种会把紧跟在后面的 CJK 字符也吞进同一个词（sqlite.py:69, 1101）。
2. 整个词都是 CJK 的，拆成单字；其他词整词保留（sqlite.py:1102-1105）。
3. 去重，保持第一次出现的顺序（sqlite.py:1106-1109）。
4. 一个词都没有时返回 `None`，`search_memory` 直接返回 `()`（sqlite.py:1110-1111, 588-589）。
5. 每个词用双引号包起来（词里的 `"` 写成 `""`），再用 ` AND ` 连起来。所以用户输入的 `AND`、`OR`、`*`、`(` 都只是普通词，不会被当成 FTS5 语法（sqlite.py:1112）。

下表是实验结果。全局范围的条目包括 "UR5机械臂抓取红色方块"、"用户偏好，颜色是蓝色"、"Robots are great"、"café au lait"、"hello world again"、"版本v2发布"、"机器人的手臂很灵活"；会话范围 S1 有一条 "session only 偏好"。

| 查询 | 表达式 | 结果 | 位置 |
|---|---|---|---|
| `机械臂` | `"机" AND "械" AND "臂"` | 命中 | sqlite.py:1102-1103 |
| `机臂` | `"机" AND "臂"` | 命中"机器人的手臂很灵活"。逐字 AND 不要求字相邻，也不要求顺序 | sqlite.py:1112 |
| `偏好`，范围 `["global","S1"]` / `["S1"]` | `"偏" AND "好"` | 两个范围各命中一条 / 只命中 S1 那条 | sqlite.py:594 |
| `UR5机械臂`、`v2发布` | `"UR5机械臂"`、`"v2发布"`（整串成了一个词） | 查不到，尽管条目原文里就有这个串。见疑点 1 | sqlite.py:69, 1101 |
| `机械臂UR5`、`UR5 机械臂` | 拆成 `机`、`械`、`臂`、`UR5` 四个词 | 命中 | sqlite.py:1101-1105 |
| `偏好，颜色`、`（偏好）` | `… AND "，颜色"`、`"（偏好）"` | 查不到。原因同上，标点后面的汉字被吞进了一个词 | sqlite.py:1101 |
| `偏好。`、`hello !` | `… AND "。"`、`… AND "!"` | 查不到。只有符号的词是空短语，空短语匹配不到任何行，AND 之后整个查询为空 | sqlite.py:1112 |
| `robot` / `ROBOTS` | — | 整词匹配，不做词干还原：`robot` 查不到，`ROBOTS` 能查到（不区分大小写） | sqlite.py:120 |
| `cafe` | — | 命中 `café`（`unicode61` 默认去掉变音符号） | sqlite.py:120 |
| `hello,world` / `world,hello` | 引号里的整串是一个短语 | 前者命中相邻的 "hello world"，后者查不到 | sqlite.py:1112 |
| `AND`、`OR`、`NOT` | `"AND"` 等 | 当成普通词，能命中含这些词的条目，不报错 | sqlite.py:1112 |
| `***`、`-`、`"`、`(`、`。`、`NEAR` | 空短语，或普通词 | 返回 `()`，不报错 | sqlite.py:1112 |
| 空串，或只有空白 | `None` | 返回 `()`，不查库 | sqlite.py:588-589 |
| `scopes=[]` | — | 返回 `()`，不查库 | sqlite.py:588-589 |
| 含 NUL 字符 `\x00` | `"\x00"` | FTS5 报 `unterminated string`，抛 `OperationalError`，被代码接住：记一条 warning，返回 `()`。实验里只有这个输入走到了 except 分支 | sqlite.py:597-602 |
| 含孤立的代理字符（如 `\ud800`） | — | 绑定参数时抛 `UnicodeEncodeError`，不在 except 范围内，直接抛给调用方 | sqlite.py:597-599 |
| `limit=0` / 负数 | — | 0 条 / 不限条数（实验） | sqlite.py:595 |
| 整体替换一个范围之后 | — | 旧条目查不到。FTS `integrity-check` 通过 | sqlite.py:573-577 |

## 接口方法：运行、图、节点状态与快照

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `create_run(session_id, *, graph_file_digest=None)` | 在一个事务里插入运行（`running`、content_time 0、paused 0），把会话的 `current_run_id` 设为它，并更新会话的 `updated_at` | 会话不存在抛 `StorageError`。不检查会话是否已有没结束的运行，`current_run_id` 直接被覆盖（实验）；"一个会话同时只有一个运行"由 agent/tools/execution.py:33-35 检查。不建运行目录（runtime/run.py:54 调 `ensure_run_dir`），也不建 `timeline:` 计数器 | runtime/run.py:53 |
| `set_run_status(run_id, status, *, finished_at=None)` | 改状态，同时设 `finished_at`：给了 `finished_at` 就用它；没给时，状态是 `running/idle/paused/closed` 就置 NULL，其他状态（`finished`、`stopped`，或任何别的字符串）用当前时间 | 运行不存在抛 `StorageError`。不检查状态转移：`finished → running` 也允许，`finished_at` 会被清空；再设一次 `finished`，`finished_at` 改成新的时间；`closed` 的 `finished_at` 是 NULL，因为关闭不算结束（实验）。非法状态名照样写进库并打上时间，构造返回值时才抛 `ValidationError`。不动 `paused` 列 | runtime/run.py:151（`closed` 来自 checkpoint.py:114，`finished`/`stopped` 来自 service.py:339） |
| `get_run(run_id)` | 读一行 | 不存在返回 `None` | service.py:184 |
| `runs_of_session(session_id)` | 按 `started_at, id` 排序 | 会话不存在返回 `()` | agent/session.py:99（打开会话时从中挑出 `closed` 的运行） |
| `set_graph_file_digest(run_id, digest)` | 改 `graph_file_digest` | 运行不存在抛 `StorageError` | mutations.py:315（每次写完 graph.json 后调用） |
| `set_run_paused(run_id, paused)` | 把 `paused` 改成 0 或 1，和 `status` 互不影响（实验：`running` 且 `paused=True`） | 运行不存在抛 `StorageError`。单独存一列的原因：会话关闭时 status 会变成 `closed`，必须另外记下"整个运行处于暂停"，打开会话后才能回到 paused（base.py:102-103） | runtime/run.py:114 |
| `save_graph(run_id, graph)` | 在事务前先 `dumps_graph`，然后 upsert 覆盖，只留一版 | 运行不存在抛 `StorageError`。序列化失败在事务之前就抛出 | service.py:159, mutations.py:173 |
| `load_graph(run_id)` | 用 `loads_graph(data)` 还原 | 没有这一行返回 `None`。`loads_graph` 自己检查 JSON 里的 `schema_version` 是否在 {1,2,3} 里，不在就抛 `GraphValidationError`（graph/serialization.py:124-128）。表里的 `schema_version` 列不读 | 只在 `load_run_state` 里调用 |
| `set_node_state(run_id, node_id, instance_id, state, *, outcome=None, reason="", error=None)` | upsert 一个实例的状态。每次都整体覆盖 `outcome`、`reason`、`error`，不给的就回到 None / "" / None（实验）。不动 `snapshot` | 不查运行（实验：写到不存在的运行也成功）。非法状态名照样写进库，构造返回值时抛 `ValidationError`；之后 `node_states`、`load_run_state` 读到这一行都会抛（实验） | bus.py:494 |
| `node_states(run_id)` | 按 `node_id, instance_id` 的文本顺序排序（`n@v10` 排在 `n@v2` 前面）。包括被替换的旧实例，不包括 snapshot | 没有就返回 `()` | 只在 `load_run_state` 里调用 |
| `set_node_snapshot(run_id, node_id, instance_id, data)` | 事务前先 `dump_json`。upsert：已有这一行时只改 `snapshot` 和 `updated_at`；没有时插一行，`state='pending'` 作占位（实验） | 不查运行。不能转 JSON 时抛 `TypeError`，不写库。`data=None` 存成文本 `null`，读回是 `None`，和"没有快照"分不出来。`NaN` 存成 `NaN`，读回是 `nan`。int 键读回变成 str 键，元组读回变成列表（实验） | bus.py:462 |
| `node_snapshot(run_id, node_id, instance_id)` | `json.loads(snapshot)` | 没有这一行，或 snapshot 是 NULL，返回 `None` | service.py:300, supervisor.py:193 |

## 接口方法：端口消息（保留与裁剪）

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `put_port_message(message, *, run_id)` | 在一个事务里：按 `message.source_node`、`source_port`、`version` 插一行，再删掉同一（run, node, port）下 `version <= version - port_buffer` 的行 | 传入的不是 `graph.ports.Message`（例如误传了对话用的 `Message`）时抛 `StorageError("port messages hold graph.ports.Message, got …")`，在事务之前。同一（run, node, port, version）已存在时抛 `sqlite3.IntegrityError`，回滚（实验）。不查运行（实验：写到不存在的运行也成功） | bus.py:399 |
| `port_messages(run_id, node, port, after_version=None)` | 这个输出端口还保留的消息，按 version 升序。给了 `after_version` 就只要 `version > after_version` 的 | 游标落在已经被裁掉的范围里时，只返回还在的，不提示中间缺了消息（实验：`after_version=2` 时返回 `[100]`） | bus.py:471（节点追读输入时） |
| `latest_port_messages(run_id)` | 用窗口函数取每个（node, port）版本最大的一条，按 version、node、port 排序 | 没有就返回 `()` | bus.py:183（bus 启动时接上版本号）, service.py:292, 376 |

| 情况 | 结果 | 位置 |
|---|---|---|
| 版本连续（bus 的正常情况） | 每个端口留最新的 `port_buffer` 条。`port_buffer` 默认 64，来自 `open_storage(port_buffer=...)`，也就是 `runtime.port_buffer`。删除边界直接算出来，不数行数（决策记录［阶段 5］端口消息保留） | sqlite.py:822-830 |
| 版本有跳跃 | 窗口按版本号算，不按条数，所以留下的可能少于 `port_buffer` 条（实验：buffer 为 3，写完 1..5 后写 100，只剩 `[100]`） | sqlite.py:828 |
| 旧版本晚到 | 照常插入，裁剪按这条的版本算，所以旧版本会重新出现在窗口外（实验：已有 `[3,4,5]` 时写入 1，变成 `[1,3,4,5]`）。下一次正常写入时它会被裁掉。版本号由 bus 在 `_timeline` 锁内递增分配，正常情况下不会晚到 | sqlite.py:816-830 |
| 最新的一条 | 因为 `port_buffer ≥ 1`，永远不会被裁掉，所以 bus 重开时总能用 `latest_port_messages` 接上版本号 | sqlite.py:234, 828 |
| 节点被替换 | 行按逻辑节点 id 存，不按实例。新实例接着旧实例的版本号往下写（bus.py:172-174），所以保留窗口跨越新旧实例 | sqlite.py:819 |
| `version` 为 0 | 模型允许（`ge=0`），照样存（实验） | base.py:127 |
| `load_run_state` 里的端口消息 | 该运行所有端口保留的全部行，按 node、port、version 排序 | sqlite.py:994-996 |

## 接口方法：排队给节点的消息（v2）

主 Agent 发给某个还没启动的实例的消息，先存在这张表里，实例启动时（bus 的 `attach`）按 seq 交给它，然后标为已送达。

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `queue_node_message(run_id, node_id, instance_id, message)` | 插一行，`seq = message.seq`，`delivered=0` | 运行不存在抛 `StorageError`。同一（run, instance, seq）已存在时抛 `IntegrityError`（实验）。不检查实例是否已经启动，也不检查 `node_id` 和实例是否对得上 | bus.py:296（实例进程不在时） |
| `queued_node_messages(run_id, *, instance_id=None, include_delivered=False)` | 按 instance_id、seq 排序。默认只返回未送达的 | — | bus.py:187（包括已送达的，用来接上 seq）, 207（`attach` 时取这个实例未送达的） |
| `mark_node_messages_delivered(run_id, instance_id)` | 把这个实例所有未送达的行标为 `delivered=1`、记下 `delivered_at`，返回行数 | 不查运行。重复调用返回 0（实验） | bus.py:211 |

| 情况 | 结果 | 位置 |
|---|---|---|
| 送达之后 | 只标记，不删行。bus 重开时要用最大 seq 接着编号（决策记录［阶段 7］存储的改动） | sqlite.py:890-897 |
| `load_run_state` | `queued_messages` 只包含未送达的 | sqlite.py:1003, 885-886 |
| 删除 | 只会随 `delete_session` 删 | sqlite.py:456 |

## 接口方法：提问、时间线、接着跑

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `add_ask(run_id, node_id, instance_id, question)` | 取一个 `ask_seq`，id 是 `ask<N>`（全库统一编号，跨运行、跨会话），状态为 pending | 运行不存在抛 `StorageError` | bus.py:449 |
| `answer_ask(ask_id, answer)` | 改成 answered，写入回答和 `answered_at`，返回读回的记录 | 提问不存在抛 `StorageError("ask 'x' does not exist")`。不看当前状态：已作废的会被改成 answered，已回答的会被覆盖（实验）。存储层不拦这两种情况，因为 bus 只对内存里还在等回答的提问调用它，否则抛 `RuntimeControlError`（bus.py:304-306） | bus.py:309 |
| `void_ask(ask_id)` | 只把 pending 改成 void，并把 `answered_at` 记为作废的时间 | 提问不存在抛 `StorageError`。已回答或已作废的保持不变，回答也保留（实验）。返回读回的记录 | bus.py:326（实例结束时）, checkpoint.py:132（打开会话时作废旧实例的提问） |
| `pending_asks(run_id)` | 只要 status 为 pending 的，按 created_at、id 排序 | 没有就返回 `()` | service.py:189, 281, 305；`load_run_state` |
| `next_content_time(run_id)` | 在一个事务里：`timeline:<run_id>` 加一，写回 `runs.content_time`，返回新号 | 运行不存在抛 `StorageError`。第一次调用返回 1 | bus.py:251, 285, 308, 386, 418, 443, 448（都在 bus 的 `_timeline` 锁内） |
| `load_run_state(run_id)` | 返回运行记录、图、节点状态、全部保留的端口消息、待回答的提问、未送达的排队消息 | 运行不存在返回 `None`。没有图时 `graph=None`，调用方会改用空图（checkpoint.py:129）。不包含快照。当前时间线编号在 `run.content_time` 里。由 6 次独立的读组成 | checkpoint.py:127 |

## 删除会话

`delete_session(id)` 删掉会话本身、对话、排队消息、会话记忆的索引和登记，以及这个会话的全部运行记录；然后删 `sessions/<id>/` 目录（sqlite.py:441-462）。

1. 先单独做一次读：`runs_of_session(id)`（sqlite.py:442）。
2. 如果其中有 `running`、`idle` 或 `paused` 的运行，抛 `StorageError("session 'x' still has open runs: <id> (<status>), …")`，什么都不删（sqlite.py:443-446）。
3. 在一个事务里：先 `_require_session`；删 `messages`、`pending_inputs`；删 `memory_entries`（触发器同步删 FTS）、`memory_assets`、`memory_files` 里 scope 等于会话 id 的行；对第 1 步拿到的每个运行，删 `graphs`、`node_states`、`port_messages`、`queued_node_messages`、`asks` 里的行，删 `counters` 里的 `timeline:<run_id>`，再删 `runs` 里的行；最后删 `sessions` 那一行（sqlite.py:447-461）。
4. 事务提交后调用 `artifacts.remove_session_memory()`，即 `rmtree(sessions/<id>/, ignore_errors=True)`（sqlite.py:462, artifacts.py:112）。

| 情况 | 处理 | 位置 |
|---|---|---|
| 会话不存在 | 第 1 步得到空列表，第 3 步的 `_require_session` 抛 `StorageError("session 'x' does not exist")`，回滚，不删目录（实验） | sqlite.py:448 |
| 还有 running / idle / paused 的运行 | 抛 `StorageError`，列出这些运行和它们的状态（实验） | sqlite.py:443-446 |
| 运行是 `closed`（会话关闭时留下的） | 允许删除。这些运行的全部行一起删掉，以后再也不能接着跑。见疑点 9 | sqlite.py:61-65, 443 |
| 运行是 `finished` / `stopped` | 一起删 | sqlite.py:455-460 |
| 会话正被打开使用，但没有未结束的运行 | 存储层不知道会话是否被打开，照样删。之后这个会话的写入（`append_message` 等）会抛 `StorageError`，由 agent/session.py:200-205 的写库任务记日志 | sqlite.py:441-461 |
| 不会删的东西 | `runs/<run_id>/` 整个目录（产物、`trace.jsonl`、`graph.json`），因为它们记录了"对环境做过什么"，和会话索引不是一回事（决策记录［阶段 5］删除会话）。还有 `code/`、全局记忆（`memory/` 目录和 `scope='global'` 的索引行） | sqlite.py:451-460 |
| 检查和删除之间，别的协程给这个会话建了运行 | 检查在事务外做，删除时用的是检查时的运行列表，所以新建的运行不会被删，留下一个 `session_id` 指向已删会话的 running 运行（实验复现）。见疑点 4 | sqlite.py:442-460 |
| 在不存在的运行 id 下写入的孤儿行（`set_node_state`、`set_node_snapshot`、`put_port_message` 都不查运行） | 不在任何会话的运行列表里，`delete_session` 删不到，会一直留在库里（实验） | sqlite.py:455-457 |
| `sessions/<id>/` 删不掉（权限等原因） | 因为 `ignore_errors=True`，静默忽略；库里的行已经删了 | artifacts.py:112 |
| `sessions/<id>/` 下除了 `memory/` 还有别的文件 | 一起删掉，删的是整个目录（实验）。见疑点 10 | artifacts.py:112 |

## 写入值的序列化与校验

| 函数 | 做什么 | 位置 |
|---|---|---|
| `dump_json(value)` | `json.dumps(value, ensure_ascii=False, separators=(",", ":"))`。不能转 JSON 时抛 `TypeError`。`NaN`/`Infinity` 写成非标准 JSON 的 `NaN`。非 str 键转成 str，元组变成列表 | artifacts.py:184-185 |
| `dump_model(model)` | `dump_json(model.model_dump(mode="json"))`，不用 `sort_keys`：要保留服务端给出的字段顺序，同一个模型写两次得到的字节相同（决策记录［阶段 5］序列化：原生 item 要逐字节一致） | artifacts.py:188-194 |
| `load_model(cls, text)` | `cls.model_validate_json(text)` | artifacts.py:197-198 |
| 读回 | `_session` 用 `json.loads` 加 `Usage.model_validate`；快照用 `json.loads`；其他 JSON 列用 `load_model`；图用 `loads_graph`（图那边自己用 `sort_keys=True`） | sqlite.py:1010-1082, 806, 744 |

| 情况 | 处理 | 位置 |
|---|---|---|
| 写之前会检查的 | 只有这几项：会话或运行是否存在（部分方法）、`put_port_message` 的参数类型、`port_buffer ≥ 1`、能否转成 JSON | sqlite.py:234, 329-335, 811-812 |
| 状态名、非负数这类取值 | 实现里的参数类型只是 `str` / `int`，写库前不校验。事务提交后构造返回记录时 Pydantic 才校验，抛 `ValidationError`，但这时行已经提交（实验：`update_session(status="bogus")`、`intent_count=-1`、`set_run_status("bogus")`、`set_node_state(state="bogus")`、`append_message(turn=-1)`） | sqlite.py:389-428, 683-699, 748-778, 466-479 |
| 之后再读到这一行 | `get_session`、`list_sessions`、`get_run`、`runs_of_session`、`node_states`、`load_run_state`、`load_messages` 都抛 `ValidationError`（实验：一个会话的状态非法，`list_sessions` 就整个失败）。见疑点 3 | sqlite.py:1010-1082 |
| 实际的调用方 | 阶段 7、8 传入的都是 Literal 类型里的合法值，例如 bus.py:498 的 `str(record.state)` | — |

## artifacts.py：工作区路径与文件

所有路径都从工作区根目录算出。对外交出的路径（`copy_memory_asset` 的返回值、`memory_assets.path`）是相对工作区的 POSIX 字符串，所以工作区整体搬走后仍然有效（artifacts.py:3-4）。这个模块只管到"键目录"一级，键目录里怎么摆由节点决定。除了生成代码的版本号，它不校验 id 或名字里的字符。

| 函数 | 路径（相对工作区） | 调用方 |
|---|---|---|
| `state_db_path` | `.emerge/state.db` | sqlite.py:238 |
| `runs_dir` / `run_dir` | `runs/`、`runs/<run_id>/` | `runs_dir` ← service.py:120（tracer 往 `runs/<run_id>/trace.jsonl` 写）；`run_dir` 只在本模块内用 |
| `graph_file` | `runs/<run_id>/graph.json` | runtime/run.py:91 |
| `node_dir` | `runs/<run_id>/nodes/<node_id>/<instance_id>/` | service.py:295 |
| `ref_dir(ws, run_id, ref)` | `node_dir(ref.producer, ref.producer_instance)/<ref.port>/<ref.key>/`。实例在路径里，所以新旧实例、或两个不同的节点写同名的键，不会互相覆盖 | runtime/context.py:356, checkpoint.py:200 |
| `memory_dir` | scope 为 `"global"` 时是 `memory/`，否则是 `sessions/<scope>/memory/` | 内部使用 |
| `memory_file` / `memory_asset_dir` | `<memory_dir>/MEMORY.md`、`<memory_dir>/asset/` | agent/memory.py:134 |
| `code_path(ws, name, version)` | `code/<name>/v<规范化后的版本>.py` | checkpoint.py:190, agent/tools/graph.py:177 |
| `ensure_run_dir` / `ensure_node_dir` / `ensure_memory_dir` | 路径同上，并 `mkdir(parents=True, exist_ok=True)` | runtime/run.py:54、runtime/context.py:132；`ensure_memory_dir` 没有调用方 |
| `remove_session_memory(ws, id)` | 删除整个 `sessions/<id>/` | sqlite.py:462 |

| 情况 | 处理 | 位置 |
|---|---|---|
| `ensure_*` 的目标目录已存在 | 不报错 | artifacts.py:94, 100, 106 |
| 路径上某一级是普通文件 | `mkdir` 抛 `FileExistsError` 或 `NotADirectoryError`（实验） | artifacts.py:94, 100, 106 |
| `Ref.key` 是 `.`、`..`，或含 `/`、`\` | 构造 `Ref` 时就抛 `ValidationError`（graph/ports.py:90-96），根本到不了这里 | — |
| `node_id`、`producer_instance` 含 `/` | 不校验，会多出目录层级。节点 id 的规则是 `^[^.\s]+$`（graph/ports.py:13），排除了点和空白，但没有排除 `/`。实例 id 的格式是 `<node_id>@v<图版本>`（graph/graph.py:32-38）。`producer_instance` 只要求非空，实验中给它 `a/../..` 时路径爬出了节点目录（这个值由框架生成） | artifacts.py:60-70 |
| scope 含 `/` 或 `..`，或者是空串 | 不校验：`"../x"` 得到 `sessions/../x/memory`，`""` 得到 `sessions/memory`（实验）。scope 由调用方给，只会是 `"global"` 或会话 id | artifacts.py:73-77 |
| `remove_session_memory` 的目录不存在 | `ignore_errors=True`，静默 | artifacts.py:112 |
| `remove_session_memory(ws, "")` | 删掉整个 `sessions/`（实验）。唯一的调用方 `delete_session` 会先确认会话存在，而会话 id 是 uuid hex，所以实际碰不到 | artifacts.py:112 |

记忆图片 `copy_memory_asset(workspace, scope, source)`（artifacts.py:118-133）：

| 情况 | 处理 | 位置 |
|---|---|---|
| 正常情况 | 读入整个文件，用 sha256 加原扩展名（转小写）作文件名，复制进该范围的 `asset/`，返回相对工作区的路径 | artifacts.py:125-133 |
| 同一张图复制两次，或扩展名只差大小写（`.PNG` 和 `.png`） | 目标已存在就不再写，返回同一个路径（实验） | artifacts.py:130-132 |
| 内容相同、扩展名不同（`.png` 和 `.jpg`） | 名字不同，存成两个文件（实验） | artifacts.py:130 |
| 源文件没有扩展名 | 文件名只有哈希（实验） | artifacts.py:130 |
| 两个范围复制同一张图 | 各存一份，互不影响（实验） | artifacts.py:128 |
| 目标文件已存在但内容被改过 | 不比对内容，原样保留：同名就当作同内容（实验） | artifacts.py:131 |
| 源文件不存在 / 是目录 | 抛 `FileNotFoundError` / `IsADirectoryError`（实验） | artifacts.py:126 |
| 源是相对路径 | 按进程的当前目录解析，不是按工作区（实验）。阶段 8 会先把路径解析成工作区内的绝对路径（agent/tools/memory.py:51） | artifacts.py:125 |
| 登记 | 这个函数只复制，不登记。登记由调用方用 `add_memory_asset` 做（agent/memory.py:145-149），两步不在一个事务里 | — |
| 为什么复制而不是记录原路径 | 节点以后可能覆盖自己产出的文件，记忆就会悄悄指向别的内容（artifacts.py:121-123） | — |

生成代码（artifacts.py:139-178）：

| 情况 | 处理 | 位置 |
|---|---|---|
| 版本写成 `"7"`、`"v7"`、`"v007"` | 都规范成 `v7.py`，是同一个文件（实验） | artifacts.py:173-175 |
| 版本为空串，或含 `/`、`\` | 抛 `ValueError("generated code version … cannot name a file")` | artifacts.py:176-177 |
| 其他非数字版本（`"beta"`、`"V2"`、`"-1"`、`".."`） | 原样拼进文件名：`vbeta.py`、`vV2.py`、`v-1.py`、`v...py`（实验） | artifacts.py:178 |
| `name` 含 `/` 或 `..` | 不校验，`"../x"` 得到 `code/../x/v1.py`（实验）。调用方 `register_node_code` 要求名字只含字母、数字和下划线（agent/tools/graph.py:129-130） | artifacts.py:89 |
| `write_generated_code` 写一个已有的版本 | 直接覆盖（docstring 里写明了） | artifacts.py:139-144 |
| `read_generated_code` 读的文件不存在 | 抛 `FileNotFoundError`（实验） | artifacts.py:147-148 |
| 读最新版本 | 没有专门的函数，要先取 `list_generated_versions(...)[-1]`，再 `read_generated_code` | — |
| `list_generated_versions` 时类型目录不存在 | 返回 `()` | artifacts.py:157-159 |
| `list_generated_versions` 的排序 | 只看 `v*.py`。匹配 `v<数字>` 的按数值升序（v9 在 v10 前），其余按文本排在后面 | artifacts.py:160-168 |
| 同一个数值有两个文件（手工放进去的 `v10.py` 和 `v010.py`） | 列出两个 `"10"`（实验） | artifacts.py:163-165 |
| 文件名正好是 `v.py` | 列出 `""`，但 `""` 是读不了的版本（会抛 ValueError）（实验） | artifacts.py:167 |

## 疑点（待核实）

1. **中文检索的切词有两种情况查不到**。（a）`_TOKEN_RE` 的第二个分支 `[^\s]+` 会把紧跟在非 CJK 字符后面的汉字吞进同一个词，这个词整体当作一个短语去匹配，而索引里汉字是逐字分开的。所以 `UR5机械臂`、`v2发布`、`偏好，颜色`、`（偏好）` 都查不到，尽管条目原文里就有这些串（实验）。（b）只有符号的词（如 `。`、`!`）会变成空短语，和其他词 AND 之后整个查询为空，`偏好。`、`hello !` 也查不到。这和 base.py:287-289 的说明"a query of any length finds a substring"不符。决策记录 HANDOFF.md:364 只写了"只含符号的查询返回空"。证据：sqlite.py:69, 1101-1105, 1112。
2. **`open()` 失败时不关闭连接**。`_init_schema` 抛错时（版本不对、库被锁、遇到外来的表结构），`conn` 不会被关闭。实验中：版本被拒的 `StorageError` 如果没人捕获，进程在退出时卡住，因为 aiosqlite 的工作线程不是 daemon；`executescript` 抛出的 `OperationalError` 即使被捕获，进程也卡住；只有捕获了 `StorageError` 的情况，靠垃圾回收关闭连接，并带一条 ResourceWarning。另外 `PRAGMA journal_mode=WAL` 在版本检查之前执行，被拒绝的库文件也被改成了 WAL。证据：sqlite.py:240-252。
3. **取值在写之后才校验**。实现中的状态名、轮号、计数这些参数不校验就写进库，构造返回值时才抛 `ValidationError`，而行已提交。之后读这一行的方法全部抛错，例如 `list_sessions` 会因为一个会话整个失败。目前的调用方传的都是合法值。证据：sqlite.py:389-428, 466-479, 683-699, 748-778。
4. **`delete_session` 先检查、后删除，不在同一个事务里**。删除时用的是检查时拿到的运行列表。两步之间建的运行不会被删，留下指向已删会话的 running 运行（实验复现）。证据：sqlite.py:442-460。
5. **写完再读回要取两次锁**。7 个方法在写和读回之间可能插进别的调用：返回值可能已经包含别人之后的写；如果这一行在中间被删，`assert session is not None` 之类的检查失败，抛 `AssertionError`（实验：在 `update_session` 上复现）。证据：sqlite.py:426-428, 437-439, 697-699, 717-719, 725-727, 931, 942, 946。
6. **`aclose()` 不取锁**。关闭时如果还有事务在进行，检查点报 `database table is locked`，连接照样被关掉，那个事务丢失，发起它的协程收到 `ValueError`（实验）。目前要靠调用方先把写都做完（例如 `Session.close` 会先 flush 写库队列）。证据：sqlite.py:337-343。
7. **"取号和写盖章消息在同一个事务里"与代码不符**。HANDOFF.md:270、sqlite.py:4-5 和 base.py:433 都说时间线编号和盖了这个号的消息在同一个事务里更新。实际上 `next_content_time` 自己单独提交（sqlite.py:981-986），消息由 bus 随后另起事务写入（runtime/bus.py:251→252、285→296、308→309、386→399、448→449），两步的先后顺序靠 bus 的 `_timeline` 锁保证。结果是只会出现空号、不会出现重号，但两步并不在一个事务里。
8. **`base.py` 里 `put_port_message` 的类型注解写错了**。base.py:377 写的是 `message: Message`，而这个文件里的 `Message` 是 `models.types.Message`（对话消息）。实现要求的是 `graph.ports.Message`，否则抛 `StorageError`（sqlite.py:810-812）。HANDOFF.md:344 写的也是 `graph.ports.Message`。
9. **"会话的运行要先结束才能删"的口径不一致**。base.py:225 说"Raises StorageError while any run is still open"，而 `_OPEN_RUN_STATES` 把 `closed` 也算作 open（sqlite.py:65）；但代码实际只拒绝 `running`/`idle`/`paused`（sqlite.py:443），`closed` 的运行会被一起删掉。计划 phase-05-storage.md:92 写的是"会话的运行要先结束才能删"。决策记录 HANDOFF.md:365 和代码一致。
10. **`remove_session_memory` 的 docstring 和实现不符**。artifacts.py:111 说"the rest of the session directory is untouched"，但 artifacts.py:112 删的是整个 `sessions/<id>/`。HANDOFF.md:366 的描述和代码一致。
11. **决策记录对序号的描述不准**。HANDOFF.md:308 说 `counters` 只存两个全局序号（`session_seq`、`pending_seq`），实际还有 `ask_seq`（sqlite.py:278, 905）。HANDOFF.md:357 说 `pending_inputs` 的序号是每个会话单独编的，所以删除时必须带会话 id；实际 `pending_seq` 是全库统一编号（sqlite.py:539），不带会话 id 也不会删错。
12. **计划和代码有三处不同**。① phase-05-storage.md:65 说 `runs` 记"当前图版本"，表里没有这一列（sqlite.py:145-154），图版本存在 `graphs.data` 里（graph/serialization.py:31）。② phase-05-storage.md:102 说 `load_run_state` 包含快照，但 `RunState` 没有快照字段（base.py:158-168），`_node_state` 也不读 snapshot 列（sqlite.py:1048-1058）。③ phase-05-storage.md:127 要求能"读取最新版本"的生成代码，但没有对应的函数（artifacts.py:139-168）。
13. **`foreign_keys=ON` 不起作用，不查存在性的写会留下孤儿行**。外键检查打开了（sqlite.py:244），但没有一张表声明外键。`set_node_state`、`set_node_snapshot`、`put_port_message` 可以写到不存在的运行下面，这些行 `delete_session` 也删不到（实验）。证据：sqlite.py:761-768, 791-797, 816-820。
14. **`RunState.port_messages` 的注释写的是"per input port"**（base.py:164）。实际返回的是本运行每个输出端口（source node、port）保留的全部消息，按 node、port、version 排序（sqlite.py:994-996）。

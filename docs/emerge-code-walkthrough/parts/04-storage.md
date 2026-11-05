# 存储层（阶段 5）

## 概览

| 文件 | 职责 |
|---|---|
| `base.py` | `Storage` 抽象接口（全部是协程）、记录类型、`open_storage()`、`read_session()` |
| `sqlite.py` | 唯一实现 `SqliteStorage`：一个对话一个库；建表与版本检查、一个连接加一把锁、每次调用一个事务、计数器、FTS5 检索；`read_session_summary()` 只读地读出一个对话的记录 |
| `artifacts.py` | 路径规则（工作区下的对话文件夹，以及对话文件夹里的库、trace、记忆、生成代码、运行目录）、记忆图片复制、生成代码文件、JSON 序列化 |
| `__init__.py` | 重新导出以上公开名字和 `SCHEMA_VERSION` |

一个对话的全部东西放在它自己的文件夹 `sessions/<session_id>/` 里（布局见"artifacts.py：工作区、对话文件夹与路径"）。一个 `SqliteStorage` 就是一个对话的库 `<对话文件夹>/.emerge/state.db`，里面只有这个对话自己的会话记录、消息、运行和记忆索引，所以接口上不带 `session_id`。调用方是 `runtime/`（阶段 7）、`agent/`（阶段 8）和 `application.py`：应用每打开一个对话，就在它的文件夹上 `open_storage` 一次（application.py:193）；列出、删除对话由 agent/session.py 的 `list_sessions`、`delete_session` 按文件夹做，不经过某个打开的库。存储只管存到哪、怎么读回，什么时候写由调用方决定（base.py:7-8）。位置写法：`sqlite.py:229` 指 `emerge/storage/sqlite.py:229`，`runtime/bus.py:399` 指 `emerge/runtime/bus.py:399`。下文"实验"指在临时库上跑过的脚本：原来的在 `/data/yuqingchi/tmp/emerge-code-walkthrough/scratch/storage/probe*.py`；2026-11-04 改成一个对话一个库之后，新增和改动的行为由 `/data/yuqingchi/tmp/emerge-sessions/` 下的 `check_storage.py`、`check_sessions.py`、`check_bad_row.py` 验证，标"实验"的旧结论只保留对应代码没变的。

## 打开、表结构版本与关闭

`SqliteStorage.open(session_dir)` 打开或新建 `<session_dir>/.emerge/state.db`。`open_storage(session_dir, *, port_buffer=64)` 只是转调它（base.py:441-445）。

打开流程：

1. `port_buffer < 1` 时抛 `StorageError("port_buffer must be at least 1, got N")`，这时还没建任何目录（sqlite.py:224-225）。
2. `session_dir` 先 `resolve()` 成绝对路径，再建 `.emerge/`，对话文件夹本身不存在时一起建出来（`mkdir(parents=True)`）（sqlite.py:226-228）。`storage.session_dir` 因此总是绝对路径。
3. `aiosqlite.connect(path, isolation_level=None)`：连接处于自动提交模式，事务语句由代码自己发。设置 `row_factory = aiosqlite.Row`，按列名取值（sqlite.py:229-230）。
4. 设置下表里的 PRAGMA（sqlite.py:231-239）。
5. `_init_schema()` 读 `PRAGMA user_version`，按版本处理；它抛错时先关掉连接，再把异常抛出去（sqlite.py:240-246, 248-259）。

| 设置 | 值 | 说明 | 位置 |
|---|---|---|---|
| `journal_mode` | WAL | 写进库文件，之后一直有效 | sqlite.py:231 |
| `synchronous` | NORMAL | 对每个连接单独生效。WAL 模式下不是每次提交都 fsync，断电可能丢掉最近几次提交，但不会损坏库。这和"不考虑崩溃"一致 | sqlite.py:232 |
| `foreign_keys` | ON | 没有一张表声明了外键（实验：`PRAGMA foreign_key_list` 全为空），所以这个设置实际不起作用 | sqlite.py:233 |
| `wal_autocheckpoint` | 20000 页（`page_size` 4096，约 80 MB） | 默认是 1000 页。每次写都是独立提交，默认阈值下检查点很频繁，检查点期间写者要停下等。实测 1000 条端口消息从 6.2 s 降到 1.0 s（决策记录［阶段 5］WAL 与检查点） | sqlite.py:60, 238 |
| `journal_size_limit` | 64 MiB | 检查点后 WAL 文件截回到这个大小 | sqlite.py:61, 239 |
| 忙等时间 | 5 s | 代码没有显式设置，用的是 `sqlite3.connect` 默认的 `timeout=5.0`（实验：`PRAGMA busy_timeout` = 5000） | sqlite.py:229 |
| `SCHEMA_VERSION` | 3 | 存在 `PRAGMA user_version` 里。v3 是一个对话一个库的表结构 | sqlite.py:55-56 |

| 情况 | 处理 | 位置 |
|---|---|---|
| 新库（`user_version` = 0） | 用 `executescript(_SCHEMA)` 建全部表、索引、触发器（这一步自己提交），然后在一个事务里设 `user_version=3`。计数器不播种，每个 key 第一次取号时才插入 | sqlite.py:256-259 |
| 已是 v3 | 照样执行一遍 `executescript`。语句全是 `IF NOT EXISTS`，只会补建缺失的对象。然后重设 `user_version=3` | sqlite.py:256-259 |
| 其他任何版本（v1、v2 都是一个工作区一个库的旧布局，或比代码新的版本，或手工设的负数） | 抛 `StorageError("… has schema version N; this code reads only 3 and migrates nothing")`，不迁移 | sqlite.py:251-255 |
| `user_version` = 0，但库里已有同名表且列不同 | `CREATE TABLE IF NOT EXISTS` 会跳过这张旧表，错误要等到用到不一样的列时才出现（按代码推断；原来的实验是 v2 的库，见 git 历史） | sqlite.py:257 |
| 读版本时另一个连接正持有写事务 | 读版本也走 `BEGIN IMMEDIATE`，等 5 s 后抛 `sqlite3.OperationalError: database is locked` | sqlite.py:249, 271 |
| 上面任何一种失败 | `open()` 先关掉已经建好的连接，再把异常抛出去（2026-11-04 起；之前不关，见疑点 2）。被拒的库文件在第 4 步已经改成了 WAL（实验：v1、v3 库的 `journal_mode` 从 delete 变成 wal） | sqlite.py:231, 241-245 |
| 对话文件夹的路径上某一级是普通文件 | `mkdir` 抛 `FileExistsError` 或 `NotADirectoryError` | sqlite.py:228 |

`aclose()` 先执行 `PRAGMA wal_checkpoint(TRUNCATE)`，把 WAL 并回主库，然后在 `finally` 里关连接（sqlite.py:312-318）。

| 情况 | 处理 | 位置 |
|---|---|---|
| 正常关闭 | WAL 并回主库再关闭。实验中关闭后 `state.db-wal` 文件已不存在 | sqlite.py:316-318 |
| 另一个连接还在读，检查点做不完 | 这条 PRAGMA 会返回一行带 busy 标志的结果。代码不读这个结果，照常关闭（按代码推断，没做实验） | sqlite.py:316 |
| 本连接上另一个协程正在事务中 | `aclose` 不取锁。检查点抛 `OperationalError: database table is locked`，`finally` 仍然关掉连接。那个事务丢失，发起它的协程在下一条语句收到 `ValueError: no active connection`（实验）。见疑点 6 | sqlite.py:312-318 |
| 重复调用 `aclose()`，或关闭后再调用任何方法 | `ValueError: no active connection`（实验） | sqlite.py:316 |

## 表结构总表

库里有 13 张普通表和 1 张 FTS5 虚表（它另有 4 张影子表 `memory_entries_fts_config/_data/_docsize/_idx`），全在一个文件里，每个对话一个文件。复杂值（`Message`、`Usage`、图、快照）都存成 JSON 文本，写法见"写入值的序列化与校验"。v2 表示阶段 7 新增，v3 表示 2026-11-04 改成一个对话一个库时改过的表：去掉了 `session_id` 列和记忆的范围（scope）列。

一个对话被删除时，整个库文件随对话文件夹一起删掉（见"列出与删除对话"），所以下表"删除 / 清理"一列只写库还在时会删行的情况。

| 表 | 主要列 | 存什么 | 谁写（方法 ← 调用方） | 谁读 | 删除 / 清理 |
|---|---|---|---|---|---|
| `session`（v3） | PK `id`；`title`、`status`（`idle`/`turn_open`）、`system`、`current_run_id`、`intent_count`、`usage`、`metadata`、`created_at`、`updated_at` | 这个库所属的会话，只有一行 | `create_session` ← agent/session.py:98；`update_session`（status、intent_count、usage）← session.py:169, 172, 175，title ← tui/app.py:430；`set_system_snapshot` ← session.py:154, 178；`create_run` 会改 `current_run_id` 和 `updated_at` | `get_session` ← session.py:108；`read_session_summary`（列出对话时只读） | 不删 |
| `messages`（v3） | PK `seq`；`turn`、`message`、`is_summary`、`is_compacted`、`is_discarded`、`created_at`；索引 `ix_messages_turn(turn)` | 对话，一条消息一行，只追加 | `append_message` ← session.py:188；`compact_messages` ← session.py:151；`discard_turn`（只改标记）← session.py:148 | `load_messages` ← session.py:111 | 压缩和丢弃都只打标记不删行 |
| `pending_inputs`（v3） | PK `seq`；`message`、`received_at` | 已收到、还没进入对话的用户消息 | `add_pending_input` ← session.py:160 | `pending_inputs` ← session.py:117 | `remove_pending_input` ← session.py:192（这条消息写进 `messages` 之后才删） |
| `memory_entries`（v3） | PK `position`；`text`（条目原文）、`index_text`（CJK 字符逐字加空格） | 从这个对话的 `MEMORY.md` 切出的条目 | `replace_memory_entries` ← agent/memory.py:147 | `search_memory`（与 FTS 表 JOIN）← memory.py:158 | 每次 replace 先删光再插入 |
| `memory_entries_fts` | FTS5 external content（`content='memory_entries'`，`unicode61`），只索引 `index_text` | 全文索引 | 触发器 `memory_entries_ai` / `memory_entries_ad` 跟着基表的插入、删除同步。没有 UPDATE 触发器，代码也从不 UPDATE 这张基表 | `search_memory` | 跟随基表 |
| `memory_assets`（v3） | PK `path`；`digest`、`description`、`source`、`added_at` | 记忆图片的登记，`path` 是相对对话文件夹的路径（`memory/asset/<hash>.<ext>`） | `add_memory_asset` ← memory.py:132 | `memory_assets` ← memory.py:148, 159 | `remove_memory_assets` ← memory.py:150（只删登记，文件由调用方删，memory.py:152） |
| `memory_file`（v3） | PK `id`（只能是 1）；`digest`、`indexed_at` | 上次建索引时 `MEMORY.md` 的内容摘要，最多一行 | `replace_memory_entries` | `memory_file_digest` ← memory.py:145 | 不删，每次覆盖 |
| `runs`（v3） | PK `id`；`status`、`content_time`、`graph_file_digest`、`paused`（v2）、`started_at`、`finished_at` | 这个对话的每次运行一行 | `create_run` ← runtime/run.py:53；`set_run_status` ← run.py:149；`set_run_paused` ← run.py:112；`set_graph_file_digest` ← runtime/mutations.py:315；`next_content_time` 改 `content_time` ← runtime/bus.py | `get_run` ← runtime/service.py:186；`runs` ← agent/session.py:113, tui/app.py:541；`load_run_state` | 不删 |
| `graphs` | PK `run_id`；`schema_version`、`data`、`saved_at` | 当前的图，只保留最新一版。`schema_version` 是序列化格式版本（写入时是 3），不是图的版本号，读的时候也不看 | `save_graph` ← service.py:161, mutations.py:173 | `load_graph`（`load_run_state` 和 tui/app.py:550 里调用） | 每次覆盖 |
| `node_states` | PK `(run_id, node_id, instance_id)`；`state`、`outcome`、`reason`、`error`、`snapshot`、`updated_at` | 每个节点实例一行，被替换的旧实例那一行也留着 | `set_node_state` ← bus.py:494；`set_node_snapshot` ← bus.py:462 | `node_states`（只在 `load_run_state` 里调用）；`node_snapshot` ← service.py:302, runtime/supervisor.py:193 | 不删 |
| `port_messages` | PK `(run_id, node, port, version)`；`message`、`stored_at` | 每个输出端口最近 `port_buffer` 条消息 | `put_port_message` ← bus.py:399 | `port_messages` ← bus.py:471；`latest_port_messages` ← bus.py:183, service.py:294, 378；`load_run_state` | 每次 put 在同一个事务里裁掉窗口外的旧版本 |
| `asks` | PK `id`（形如 `ask<N>`）；`run_id`、`node_id`、`instance_id`、`question`、`status`（pending/answered/void）、`answer`、`created_at`、`answered_at`；索引 `ix_asks_run_status` | 节点提出的问题 | `add_ask` ← bus.py:449；`answer_ask` ← bus.py:309；`void_ask` ← bus.py:326, runtime/checkpoint.py:132 | `pending_asks` ← service.py:191, 283, 307；`load_run_state` | 不删 |
| `queued_node_messages`（v2） | PK `(run_id, instance_id, seq)`；`node_id`、`message`（`MainAgentMessage`）、`delivered`、`queued_at`、`delivered_at` | 主 Agent 发给还没启动的实例的消息 | `queue_node_message` ← bus.py:296；`mark_node_messages_delivered` ← bus.py:211 | `queued_node_messages` ← bus.py:187（包括已送达的，用来取最大 seq）, 207；`load_run_state`（只取未送达的） | 送达后只标记不删，bus 重开时按最大 seq 续编（决策记录［阶段 7］存储的改动） |
| `counters` | PK `key`；`value` | 单调递增的计数器：`message_seq`、`pending_seq`、`ask_seq`，以及每个运行一个 `timeline:<run_id>`。都只在这个对话的库里有效 | `_counter()`，见"序号与时间线编号" | 同左 | 不删 |

记录类型都是 `frozen=True`、`extra="forbid"` 的 Pydantic 模型（base.py:42-43）。

| 类型 | 对应 | 字段约束 | 位置 |
|---|---|---|---|
| `SessionRecord` | `session` 那一行 | `status` 只能是 `idle`/`turn_open`；`intent_count ≥ 0`；`metadata: dict[str, Any]`（键必须是 str） | base.py:46-59 |
| `StoredMessage` | `messages` 一行（不含 compacted/discarded 标记） | `seq ≥ 1`，`turn ≥ 0` | base.py:62-70 |
| `PendingInput` | `pending_inputs` 一行 | `seq ≥ 1` | base.py:73-76 |
| `MemoryHit` | 一条检索命中 | `position ≥ 0` | base.py:79-81 |
| `MemoryAsset` | `memory_assets` 一行 | — | base.py:84-92 |
| `RunRecord` | `runs` 一行 | `status` 取六个值之一；`content_time ≥ 0`；`paused` 默认 False | base.py:95-106 |
| `NodeState` | `node_states` 一行（不含 `snapshot`） | `state` 取九个值之一 | base.py:109-120 |
| `PortRecord` | `port_messages` 一行 | `version ≥ 0`；`message` 是 `graph.ports.Message` | base.py:123-129 |
| `QueuedNodeMessage` | `queued_node_messages` 一行 | `seq ≥ 1` | base.py:132-143 |
| `AskRecord` | `asks` 一行 | `status` 取 pending/answered/void | base.py:146-155 |
| `RunState` | `load_run_state` 的返回值 | 包含 `run`、`graph`（可为 None）、`node_states`、`port_messages`、`pending_asks`、`queued_messages` | base.py:158-168 |
| `SessionSummary`（v3） | `read_session` 的返回值，列出对话用 | `session`（会话记录）和 `runs`（这个对话的全部运行，按开始时间排） | base.py:171-176 |

## 事务与并发

一个 `SqliteStorage` 只有一个 aiosqlite 连接和一把 `asyncio.Lock`。每个公开方法（读方法也一样）都是"取锁 → `BEGIN IMMEDIATE` → 执行语句 → `COMMIT`"。aiosqlite 用一个工作线程按提交顺序执行语句。

| 机制 | 代码做什么 | 位置 |
|---|---|---|
| `_transaction()` | 先取锁，再发 `BEGIN IMMEDIATE`。块正常结束就 `commit()`。块里抛出任何 `BaseException`（包括 `CancelledError`）时先 `rollback()` 再抛出 | sqlite.py:263-277 |
| 读也走事务 | `_fetch` / `_fetch_one` 同样取锁、发 `BEGIN IMMEDIATE`，所以读不到别人写了一半的状态 | sqlite.py:287-293 |
| 事务块里只用 `_raw_*` | 锁不可重入，事务块里再调 `_fetch*` 会自己等自己（决策记录［阶段 5］一个连接：实现时修过一次） | sqlite.py:267-268, 279-285 |
| 多步写放在一个事务里 | 压缩（标记 + 摘要 + 保留轮）、替换记忆条目（删 + 插 + 文件摘要）、取时间线号（计数器 + `runs.content_time`）、建运行（插运行 + 改会话的当前运行）、建会话（确认还没有会话 + 插入） | sqlite.py:428-448, 498-508, 890-894, 577-584, 333-342 |
| 存在性检查 | 只有部分方法检查。先查库里有没有会话记录：`update_session`、`set_system_snapshot`、`append_message`、`compact_messages`、`add_pending_input`、`create_run`，没有就抛 `StorageError("<库路径> holds no session yet")`；`create_session` 反过来，已有会话就抛 `StorageError("<库路径> already holds session 'x'")`。先查运行：`set_run_status`、`set_graph_file_digest`、`set_run_paused`、`save_graph`、`queue_node_message`、`add_ask`、`next_content_time`。先查提问：`answer_ask`、`void_ask`。运行或提问查不到就抛 `StorageError("run/ask 'x' does not exist")`。都会回滚 | sqlite.py:304-310, 334-337, 833, 843 |
| 不做存在性检查的写 | `discard_turn`、`remove_pending_input`、`replace_memory_entries`、`add_memory_asset`、`remove_memory_assets`、`set_node_state`、`set_node_snapshot`、`put_port_message`、`mark_node_messages_delivered` 直接写，或者影响 0 行 | 各方法 |

| 情况 | 处理 | 位置 |
|---|---|---|
| 同一进程里多个协程同时调用 | 在锁上排队，`asyncio.Lock` 先来先得，各次调用的语句不会交错。实验：200 个并发 `next_content_time` 得到 1–200，不重不漏 | sqlite.py:270 |
| 协程在事务进行中被取消 | 走 `except BaseException`：回滚，然后随 `async with` 释放锁（实验：取消后标题没变，锁是空闲的） | sqlite.py:274-276 |
| 协程在等待 `commit()` 时被取消 | `commit` 在 try 之外，不会回滚。提交已经排进工作线程，照样生效，但调用方收到的是 `CancelledError`（实验） | sqlite.py:277 |
| "先写、再读回"的方法：`update_session`、`set_system_snapshot`、`set_run_status`、`set_graph_file_digest`、`set_run_paused`、`answer_ask`、`void_ask` | 写和读回各取一次锁，中间可能插进别的调用。所以返回值里可能已经有之后别人的写（实验：并发执行 `update(title=A)` 和 `update(title=B)`，前者返回 B）。如果这一行在中间被删掉，`assert` 失败，抛 `AssertionError`（实验：在 `update_session` 上复现）。见疑点 5 | sqlite.py:396-398, 404-406, 607-609, 625-627, 633-635, 839, 850 |
| `load_run_state` | 由 6 次独立的读组成（运行、端口消息、图、节点状态、提问、排队消息），不是同一时刻的快照。实际调用它的是打开会话时的 `resume`，那时这个运行还没启动，没有并发写 | sqlite.py:898-912 |
| 列出对话（`read_session_summary`） | 另开一个只读连接（`mode=ro`）读会话那一行和全部运行，不经过打开着的 `SqliteStorage`，不取它的锁，也不进写事务；读到的是已提交的数据 | sqlite.py:915-939 |
| 同一个库文件有第二个可写连接（另一个进程，或同一进程里第二个 `SqliteStorage`） | 两边的 asyncio 锁互相不知道，只能靠 SQLite 的写锁。对方持有事务时，本方的 `BEGIN IMMEDIATE`（读也会发）等 5 s 后抛 `sqlite3.OperationalError: database is locked`（实验） | sqlite.py:271 |
| SQL 层出错（主键冲突等） | 原样抛 `sqlite3.IntegrityError` / `OperationalError`，不包成 `StorageError`；事务已回滚 | sqlite.py:274-276 |

## 序号与时间线编号（`counters`）

`_counter(key)` 在调用方的事务里执行 `INSERT … ON CONFLICT(key) DO UPDATE SET value = value + 1`，然后用 `SELECT` 读回新值。某个 key 第一次用时得到 1（sqlite.py:295-302）。不用 `MAX(...)+1` 的原因有两个：删行后会把号重发一遍；发过号但没写进库的值会被跳过（决策记录［阶段 5］序号与时间线）。计数器存在对话自己的库里，所以每个对话各有一套。

| key | 范围 | 发给谁 | 位置 |
|---|---|---|---|
| `message_seq` | 这个对话一个 | `messages.seq`。`append_message` 和 `compact_messages` 每插一行取一个号 | sqlite.py:415, 433, 441 |
| `pending_seq` | 这个对话一个 | `pending_inputs.seq` | sqlite.py:473 |
| `ask_seq` | 这个对话一个 | `asks.id = "ask" + 号`。一个对话里跨运行不重复；不同对话各自从 `ask1` 编起 | sqlite.py:813-814 |
| `timeline:<run_id>` | 每个运行一个，第一次取号时才建 | `next_content_time` 的返回值，同时写回 `runs.content_time` | sqlite.py:892-893 |

| 情况 | 结果 | 位置 |
|---|---|---|
| 事务回滚（库里还没有会话、压缩中途序列化失败等） | 计数器的 +1 跟着回滚，不留空号。`append_message` 先查会话、再取号（sqlite.py:414-415） | sqlite.py:274-276 |
| 并发取号 | 在锁内串行执行，不重复、不倒退（实验） | sqlite.py:270 |
| 关闭后重开 | 计数器存在库里，接着往下发。`runs.content_time` 和计数器在同一个事务里更新，两者一致 | sqlite.py:889-894 |
| 读回的 `seq` | 递增但不连续：压缩把保留的轮重写成新行，被压缩、被丢弃的行还占着号。阶段 8 只按先后顺序重建 History，不依赖具体值（决策记录［阶段 5］序号与时间线） | sqlite.py:415 |
| 时间线号取走了，但对应的消息没写进库 | `next_content_time` 自己单独提交。盖了这个号的消息由 bus 随后在另一个事务里写（bus.py:251→252、285→296、308→309、386→399、448→449）。汇报和自定义事件盖了号也只写进 trace，不进库（bus.py:418, 443）。所以库里的编号可能有空档，但不会重号。见疑点 7 | sqlite.py:889-894 |
| 新库 | 计数器表是空的，不播种；每个 key 第一次取号时插入，得到 1 | sqlite.py:295-302 |

有两种号不由 `counters` 发：端口消息的 `version` 和 `MainAgentMessage.seq`。它们由 bus 在内存里递增分配，bus 启动时从库里接上：用 `latest_port_messages` 取每个端口的最新版本，用 `queued_node_messages(include_delivered=True)` 取每个实例的最大 seq（bus.py:183-189）。存储层按给定的号原样存，号重复时主键冲突，抛 `IntegrityError`。

## 接口方法：会话

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `create_session(session_id, *, title="", system=None, metadata=None)` | 在一个事务里先确认库里还没有会话，再插一行（status `idle`、usage 为空、intent 为 0），返回记录。id 由调用方给，就是对话文件夹的名字：应用用 `uuid4().hex` 起名，建文件夹、开库，再建会话（application.py:191, 209） | 库里已有会话：抛 `StorageError("<库路径> already holds session 'x'")`，什么都不写（实验：check_storage.py）。`metadata` 在事务之前先 `dump_json`：不能转成 JSON（如 set）就抛 `TypeError`，库里什么都不写。`metadata` 有非 str 键时，行已提交，构造返回值才抛 `ValidationError`（实验）。返回值里的 `metadata` 是传入的原对象，读回时元组会变成列表（实验） | session.py:98（只传 title） |
| `get_session()` | 读那一行 | 还没有 `create_session` 时返回 `None` | session.py:108 |
| `update_session(*, title, status, current_run_id, clear_current_run, intent_count, usage, metadata)` | 只改给出的字段，并且总会更新 `updated_at`；返回读回的记录 | 库里没有会话时抛 `StorageError`。一个字段都不给时只更新 `updated_at`。`current_run_id` 不检查这个运行是否存在。`clear_current_run=True` 时会忽略同时给的 `current_run_id`，清空优先（实验）。`metadata` 是整体替换，给 `{}` 就清空。`status`、`intent_count` 写之前不校验（见"写入值的序列化与校验"）。读回的竞态见"事务与并发" | session.py:169, 172, 175；tui/app.py:430（标题） |
| `set_system_snapshot(system)` | 替换 system 快照，更新 `updated_at` | 库里没有会话时抛 `StorageError` | session.py:154（压缩后执行，取当时重建好的 system）, 178 |

列出和删除对话不在 `Storage` 上：一个库只装一个对话，这两件事要按 `sessions/` 下的文件夹做，见"列出与删除对话"。

会改 `session.updated_at` 的只有 `create_session`、`update_session`、`set_system_snapshot` 和 `create_run`。追加消息、压缩、排队消息都不改它（sqlite.py:391-392, 403, 584）。

## 接口方法：对话消息（追加、压缩、丢弃、读回）

对话只追加，写过的行不再改写内容，只改三个标记：`is_summary`、`is_compacted`、`is_discarded`。存储层没有"一轮"这个概念，轮号只是一列，由调用方给出。会话状态什么时候写成 `turn_open` / `idle` 由 agent/session.py:166-169 决定。

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `append_message(turn, message, *, summary=False)` | 取一个 `message_seq`，插一行。`is_summary` 按参数，另外两个标记为 0 | 库里没有会话时抛 `StorageError`，计数器不变。`turn` 不校验：负数会写进库，然后返回值和之后的 `load_messages` 都抛 `ValidationError`（实验）。用 `summary=True` 直接写摘要时，前面的行不会被标成 compacted，但 `load_messages` 从最后一条摘要开始读，所以也读不到它们（实验） | session.py:188（`turn_started` 和 `messages_appended` 都走这里） |
| `compact_messages(summary, kept)` | 在一个事务里依次：① 把所有 `is_compacted=0` 的行标为 1（包括已丢弃的）；② 插摘要行，`turn` 固定为 0，`is_summary=1`；③ 按 `kept` 的顺序逐条插入，轮号用调用方给的。返回新插入的行 | 库里没有会话时抛 `StorageError`。`kept` 为空时只插摘要。还没有消息时也照样插摘要。`dump_model` 在事务内调用，`kept` 里某条序列化失败时整个事务回滚，标记和计数器都不变。如果 `kept` 里有轮号 0，读回时它会和摘要并成同一轮；阶段 8 给的是压缩后的新下标，从 1 开始（agent/messages.py:92） | session.py:151 |
| `discard_turn(turn)` | 把该轮里 `is_compacted=0 AND is_discarded=0` 的行标为 `is_discarded=1`，返回改了几行 | 不查会话。重复调用返回 0（实验）。只影响压缩之后的行：压缩前同一轮号的旧行已经是 compacted，不受影响。如果丢弃第 0 轮（摘要），之后 `load_messages` 找不到摘要，只返回剩下未压缩的行（实验中为空） | session.py:148（`turns_dropped(start, stop)` 对每一轮各调一次） |
| `load_messages()` | 取 `is_compacted=0 AND is_discarded=0` 的行，按 `seq` 升序，从其中最后一条 `is_summary=1` 的行开始返回；没有摘要就从第一行开始 | 没有消息返回 `()`。某行 JSON 不合法或轮号为负时抛 `ValidationError` | session.py:111（之后由 `history_from_rows` 把轮号相同的连续行合成一轮） |

| 情况 | 结果 | 位置 |
|---|---|---|
| 在会话内取消一轮后，新的一轮用了同一个轮号 | `discard_turn` 只标记还没标过的行。新写的行没有标记，旧行已是 discarded，所以读回只有新行（实验：读回 `u1-again`，读不到被丢弃的 `u1`） | sqlite.py:452-455 |
| 压缩两次 | 第二次把第一次的摘要和保留行也标成 compacted，读回从新的摘要开始 | sqlite.py:432 |
| 被压缩或被丢弃的行 | 留在库里，以后不再读回，直到整个对话被删 | sqlite.py:432, 452-455 |
| 压缩后保留的轮 | 以新行重写一份（调用方传入的是已去掉 thinking 的版本），旧行标成 compacted。所以库里会同时有新旧两份 | sqlite.py:440-447 |
| 读回顺序 | `seq` 在这个库里单调递增，所以按 seq 排序就是写入顺序 | sqlite.py:459 |
| 会话关闭时一轮没做完 | 存储层不做任何事，已写的行都保留。之后打开会话时由阶段 8 接着这一轮做 | — |

## 接口方法：排队的用户消息

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `add_pending_input(message)` | 取一个 `pending_seq`，插一行 | 库里没有会话时抛 `StorageError` | session.py:160 |
| `pending_inputs()` | 按 seq 升序返回 | 没有就返回 `()` | session.py:117（打开会话时） |
| `remove_pending_input(seq)` | 按 seq 删一行 | 不查会话。行不存在时什么都不做，也不报错 | session.py:192 |

调用方保证顺序：同一个写库队列里先排"写这条消息"，再排"删 pending 行"，所以一条用户消息任何时刻都至少在一处（agent/session.py:187-192；决策记录［阶段 8］用户消息的持久化）。

## 接口方法：记忆索引与图片登记

记忆的内容是文件：对话文件夹里的 `memory/MEMORY.md` 和 `memory/asset/`。一个对话只有一份记忆，表里没有范围（scope）这一说。库里没有整份文件的副本，但每条条目的原文存在 `memory_entries.text` 里，用来返回命中结果。这是从文件切出来的索引，文件一改就由调用方整体重建。怎么切条目由阶段 8 决定（agent/memory.py 的 `split_entries`）。重建的时机也由阶段 8 决定：主 Agent 挂上会话时、每次搜索前、写记忆后，各做一次比较，文件的 sha256 和 `memory_file_digest` 不同才调用 `replace_memory_entries`（agent/agent.py:300；agent/memory.py:141-153, 157, 138）。

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `replace_memory_entries(entries, file_digest)` | 在一个事务里：删掉全部条目（触发器同步删 FTS）→ 按顺序插入 `position=0..n-1` 的 `text` 和 `index_text` → upsert `memory_file` 那一行（`id` 固定为 1）的摘要和建索引时间 | `entries` 为空时清空，并照样记下摘要（实验）。读的一方要么看到全部旧条目，要么看到全部新条目 | agent/memory.py:147 |
| `search_memory(query, *, limit=20)` | 把查询转成 MATCH 表达式，按 `f.rank`（bm25）、position 排序，取前 `limit` 条 | 见下面的检索表 | memory.py:158 |
| `memory_file_digest()` | 读 `memory_file.digest` | 从没建过索引返回 `None` | memory.py:145 |
| `add_memory_asset(path, digest, *, description="", source="")` | upsert：同一个 path 再登记一次，会覆盖 digest、description、source、added_at（实验） | 不查文件是否存在。`path` 应当是 `copy_memory_asset` 返回的相对路径 | memory.py:132 |
| `memory_assets()` | 按 path 排序返回 | 没有就返回 `()` | memory.py:148, 159 |
| `remove_memory_assets(paths)` | `DELETE … WHERE path IN (...)`，返回删除的行数 | `paths` 为空时直接返回 0，不进事务。不存在的 path 不计入（实验）。只删登记，不删 `asset/` 里的文件 | memory.py:150 |

建索引用的文本由 `_index_text(text)` 生成：每一段连续的 CJK 字符拆成单字，字与字之间、整段前后都加空格（sqlite.py:1022-1024）。例如 `"UR5机械臂"` 变成 `"UR5 机 械 臂 "`，`"用户偏好，颜色"` 变成 `" 用 户 偏 好 ， 颜 色 "`。CJK 的范围是：平假名和片假名 `぀-ヿ`、汉字扩展 A `㐀-䶿`、基本汉字 `一-鿿`、兼容汉字 `豈-﫿`、谚文音节 `가-힯`（sqlite.py:68）。不在范围内的有：中文标点（`，` `。` `（`）、全角拉丁字母、汉字扩展 B 及以后的字。要逐字拆开，是因为 `unicode61` 会把一整串汉字当成一个词，而 `trigram` 要至少三个字才能命中（决策记录［阶段 5］记忆索引）。

查询由 `_match_expression(query)` 转成 MATCH 表达式（sqlite.py:1027-1046）：

1. 用 `_TOKEN_RE.findall` 从左往右切词：遇到 CJK 就取一段连续的 CJK，否则取一段连续的非空白字符。后一种会把紧跟在后面的 CJK 字符也吞进同一个词（sqlite.py:69, 1035）。
2. 整个词都是 CJK 的，拆成单字；其他词整词保留（sqlite.py:1036-1039）。
3. 去重，保持第一次出现的顺序（sqlite.py:1040-1043）。
4. 一个词都没有时返回 `None`，`search_memory` 直接返回 `()`（sqlite.py:1044-1045；sqlite.py:511-513）。
5. 每个词用双引号包起来（词里的 `"` 写成 `""`），再用 ` AND ` 连起来。所以用户输入的 `AND`、`OR`、`*`、`(` 都只是普通词，不会被当成 FTS5 语法（sqlite.py:1046）。

下表是实验结果（在去掉范围之前做的；匹配和切词的代码没有变）。条目包括 "UR5机械臂抓取红色方块"、"用户偏好，颜色是蓝色"、"Robots are great"、"café au lait"、"hello world again"、"版本v2发布"、"机器人的手臂很灵活"。

| 查询 | 表达式 | 结果 | 位置 |
|---|---|---|---|
| `机械臂` | `"机" AND "械" AND "臂"` | 命中 | sqlite.py:1036-1037 |
| `机臂` | `"机" AND "臂"` | 命中"机器人的手臂很灵活"。逐字 AND 不要求字相邻，也不要求顺序 | sqlite.py:1046 |
| `UR5机械臂`、`v2发布` | `"UR5机械臂"`、`"v2发布"`（整串成了一个词） | 查不到，尽管条目原文里就有这个串。见疑点 1 | sqlite.py:69, 1035 |
| `机械臂UR5`、`UR5 机械臂` | 拆成 `机`、`械`、`臂`、`UR5` 四个词 | 命中 | sqlite.py:1035-1039 |
| `偏好，颜色`、`（偏好）` | `… AND "，颜色"`、`"（偏好）"` | 查不到。原因同上，标点后面的汉字被吞进了一个词 | sqlite.py:1035 |
| `偏好。`、`hello !` | `… AND "。"`、`… AND "!"` | 查不到。只有符号的词是空短语，空短语匹配不到任何行，AND 之后整个查询为空 | sqlite.py:1046 |
| `robot` / `ROBOTS` | — | 整词匹配，不做词干还原：`robot` 查不到，`ROBOTS` 能查到（不区分大小写） | sqlite.py:114 |
| `cafe` | — | 命中 `café`（`unicode61` 默认去掉变音符号） | sqlite.py:114 |
| `hello,world` / `world,hello` | 引号里的整串是一个短语 | 前者命中相邻的 "hello world"，后者查不到 | sqlite.py:1046 |
| `AND`、`OR`、`NOT` | `"AND"` 等 | 当成普通词，能命中含这些词的条目，不报错 | sqlite.py:1046 |
| `***`、`-`、`"`、`(`、`。`、`NEAR` | 空短语，或普通词 | 返回 `()`，不报错 | sqlite.py:1046 |
| 空串，或只有空白 | `None` | 返回 `()`，不查库 | sqlite.py:511-513 |
| 含 NUL 字符 `\x00` | `"\x00"` | FTS5 报 `unterminated string`，抛 `OperationalError`，被代码接住：记一条 warning，返回 `()`。实验里只有这个输入走到了 except 分支 | sqlite.py:520-525 |
| 含孤立的代理字符（如 `\ud800`） | — | 绑定参数时抛 `UnicodeEncodeError`，不在 except 范围内，直接抛给调用方 | sqlite.py:520-522 |
| `limit=0` / 负数 | — | 0 条 / 不限条数（实验） | sqlite.py:518 |
| 整体替换之后 | — | 旧条目查不到。FTS `integrity-check` 通过 | sqlite.py:499-503 |

## 接口方法：运行、图、节点状态与快照

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `create_run(*, graph_file_digest=None)` | 在一个事务里插入运行（`running`、content_time 0、paused 0），把会话的 `current_run_id` 设为它，并更新会话的 `updated_at` | 库里没有会话时抛 `StorageError`。不检查会话是否已有没结束的运行，`current_run_id` 直接被覆盖（实验）；"一个会话同时只有一个运行"由 agent/tools/execution.py:33-35 检查。不建运行目录（runtime/run.py:54 调 `ensure_run_dir`），也不建 `timeline:` 计数器 | runtime/run.py:53 |
| `set_run_status(run_id, status, *, finished_at=None)` | 改状态，同时设 `finished_at`：给了 `finished_at` 就用它；没给时，状态是 `running/idle/paused/closed` 就置 NULL，其他状态（`finished`、`stopped`，或任何别的字符串）用当前时间 | 运行不存在抛 `StorageError`。不检查状态转移：`finished → running` 也允许，`finished_at` 会被清空；再设一次 `finished`，`finished_at` 改成新的时间；`closed` 的 `finished_at` 是 NULL，因为关闭不算结束（实验）。非法状态名照样写进库并打上时间，构造返回值时才抛 `ValidationError`。不动 `paused` 列 | runtime/run.py:149（`closed` 来自 checkpoint.py:114，`finished`/`stopped` 来自 service.py:341） |
| `get_run(run_id)` | 读一行 | 不存在返回 `None` | service.py:186 |
| `runs()` | 这个对话的全部运行，按 `started_at, id` 排序 | 没有就返回 `()` | agent/session.py:113（打开会话时从中挑出 `closed` 的运行）；tui/app.py:541（`/runs`） |
| `set_graph_file_digest(run_id, digest)` | 改 `graph_file_digest` | 运行不存在抛 `StorageError` | mutations.py:315（每次写完 graph.json 后调用） |
| `set_run_paused(run_id, paused)` | 把 `paused` 改成 0 或 1，和 `status` 互不影响（实验：`running` 且 `paused=True`） | 运行不存在抛 `StorageError`。单独存一列的原因：会话关闭时 status 会变成 `closed`，必须另外记下"整个运行处于暂停"，打开会话后才能回到 paused（base.py:102-103） | runtime/run.py:112 |
| `save_graph(run_id, graph)` | 在事务前先 `dumps_graph`，然后 upsert 覆盖，只留一版 | 运行不存在抛 `StorageError`。序列化失败在事务之前就抛出 | service.py:161, mutations.py:173 |
| `load_graph(run_id)` | 用 `loads_graph(data)` 还原 | 没有这一行返回 `None`。`loads_graph` 自己检查 JSON 里的 `schema_version` 是否在 {1,2,3} 里，不在就抛 `GraphValidationError`（graph/serialization.py:124-128）。表里的 `schema_version` 列不读 | `load_run_state`；tui/app.py:550 |
| `set_node_state(run_id, node_id, instance_id, state, *, outcome=None, reason="", error=None)` | upsert 一个实例的状态。每次都整体覆盖 `outcome`、`reason`、`error`，不给的就回到 None / "" / None（实验）。不动 `snapshot` | 不查运行（实验：写到不存在的运行也成功）。非法状态名照样写进库，构造返回值时抛 `ValidationError`；之后 `node_states`、`load_run_state` 读到这一行都会抛（实验） | bus.py:494 |
| `node_states(run_id)` | 按 `node_id, instance_id` 的文本顺序排序（`n@v10` 排在 `n@v2` 前面）。包括被替换的旧实例，不包括 snapshot | 没有就返回 `()` | 只在 `load_run_state` 里调用 |
| `set_node_snapshot(run_id, node_id, instance_id, data)` | 事务前先 `dump_json`。upsert：已有这一行时只改 `snapshot` 和 `updated_at`；没有时插一行，`state='pending'` 作占位（实验） | 不查运行。不能转 JSON 时抛 `TypeError`，不写库。`data=None` 存成文本 `null`，读回是 `None`，和"没有快照"分不出来。`NaN` 存成 `NaN`，读回是 `nan`。int 键读回变成 str 键，元组读回变成列表（实验） | bus.py:462 |
| `node_snapshot(run_id, node_id, instance_id)` | `json.loads(snapshot)` | 没有这一行，或 snapshot 是 NULL，返回 `None` | service.py:302, supervisor.py:193 |

## 接口方法：端口消息（保留与裁剪）

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `put_port_message(message, *, run_id)` | 在一个事务里：按 `message.source_node`、`source_port`、`version` 插一行，再删掉同一（run, node, port）下 `version <= version - port_buffer` 的行 | 传入的不是 `graph.ports.Message`（例如误传了对话用的 `Message`）时抛 `StorageError("port messages hold graph.ports.Message, got …")`，在事务之前。同一（run, node, port, version）已存在时抛 `sqlite3.IntegrityError`，回滚（实验）。不查运行（实验：写到不存在的运行也成功） | bus.py:399 |
| `port_messages(run_id, node, port, after_version=None)` | 这个输出端口还保留的消息，按 version 升序。给了 `after_version` 就只要 `version > after_version` 的 | 游标落在已经被裁掉的范围里时，只返回还在的，不提示中间缺了消息（实验：`after_version=2` 时返回 `[100]`） | bus.py:471（节点追读输入时） |
| `latest_port_messages(run_id)` | 用窗口函数取每个（node, port）版本最大的一条，按 version、node、port 排序 | 没有就返回 `()` | bus.py:183（bus 启动时接上版本号）, service.py:294, 378 |

| 情况 | 结果 | 位置 |
|---|---|---|
| 版本连续（bus 的正常情况） | 每个端口留最新的 `port_buffer` 条。`port_buffer` 默认 64，来自 `open_storage(port_buffer=...)`，也就是 `runtime.port_buffer`。删除边界直接算出来，不数行数（决策记录［阶段 5］端口消息保留） | sqlite.py:730-738 |
| 版本有跳跃 | 窗口按版本号算，不按条数，所以留下的可能少于 `port_buffer` 条（实验：buffer 为 3，写完 1..5 后写 100，只剩 `[100]`） | sqlite.py:736 |
| 旧版本晚到 | 照常插入，裁剪按这条的版本算，所以旧版本会重新出现在窗口外（实验：已有 `[3,4,5]` 时写入 1，变成 `[1,3,4,5]`）。下一次正常写入时它会被裁掉。版本号由 bus 在 `_timeline` 锁内递增分配，正常情况下不会晚到 | sqlite.py:724-738 |
| 最新的一条 | 因为 `port_buffer ≥ 1`，永远不会被裁掉，所以 bus 重开时总能用 `latest_port_messages` 接上版本号 | sqlite.py:224, 736 |
| 节点被替换 | 行按逻辑节点 id 存，不按实例。新实例接着旧实例的版本号往下写（bus.py:172-174），所以保留窗口跨越新旧实例 | sqlite.py:727 |
| `version` 为 0 | 模型允许（`ge=0`），照样存（实验） | base.py:127 |
| `load_run_state` 里的端口消息 | 该运行所有端口保留的全部行，按 node、port、version 排序 | sqlite.py:902-904 |

## 接口方法：排队给节点的消息（v2）

主 Agent 发给某个还没启动的实例的消息，先存在这张表里，实例启动时（bus 的 `attach`）按 seq 交给它，然后标为已送达。

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `queue_node_message(run_id, node_id, instance_id, message)` | 插一行，`seq = message.seq`，`delivered=0` | 运行不存在抛 `StorageError`。同一（run, instance, seq）已存在时抛 `IntegrityError`（实验）。不检查实例是否已经启动，也不检查 `node_id` 和实例是否对得上 | bus.py:296（实例进程不在时） |
| `queued_node_messages(run_id, *, instance_id=None, include_delivered=False)` | 按 instance_id、seq 排序。默认只返回未送达的 | — | bus.py:187（包括已送达的，用来接上 seq）, 207（`attach` 时取这个实例未送达的） |
| `mark_node_messages_delivered(run_id, instance_id)` | 把这个实例所有未送达的行标为 `delivered=1`、记下 `delivered_at`，返回行数 | 不查运行。重复调用返回 0（实验） | bus.py:211 |

| 情况 | 结果 | 位置 |
|---|---|---|
| 送达之后 | 只标记，不删行。bus 重开时要用最大 seq 接着编号（决策记录［阶段 7］存储的改动） | sqlite.py:798-805 |
| `load_run_state` | `queued_messages` 只包含未送达的 | sqlite.py:911, 793-794 |

## 接口方法：提问、时间线、接着跑

| 方法 | 作用 | 边界情况 / 报错 | 调用方 |
|---|---|---|---|
| `add_ask(run_id, node_id, instance_id, question)` | 取一个 `ask_seq`，id 是 `ask<N>`（这个对话里统一编号，跨运行；别的对话也从 `ask1` 编起），状态为 pending | 运行不存在抛 `StorageError` | bus.py:449 |
| `answer_ask(ask_id, answer)` | 改成 answered，写入回答和 `answered_at`，返回读回的记录 | 提问不存在抛 `StorageError("ask 'x' does not exist")`。不看当前状态：已作废的会被改成 answered，已回答的会被覆盖（实验）。存储层不拦这两种情况，因为 bus 只对内存里还在等回答的提问调用它，否则抛 `RuntimeControlError`（bus.py:304-306） | bus.py:309 |
| `void_ask(ask_id)` | 只把 pending 改成 void，并把 `answered_at` 记为作废的时间 | 提问不存在抛 `StorageError`。已回答或已作废的保持不变，回答也保留（实验）。返回读回的记录 | bus.py:326（实例结束时）, checkpoint.py:132（打开会话时作废旧实例的提问） |
| `pending_asks(run_id)` | 只要 status 为 pending 的，按 created_at、id 排序 | 没有就返回 `()` | service.py:191, 283, 307；`load_run_state` |
| `next_content_time(run_id)` | 在一个事务里：`timeline:<run_id>` 加一，写回 `runs.content_time`，返回新号 | 运行不存在抛 `StorageError`。第一次调用返回 1 | bus.py:251, 285, 308, 386, 418, 443, 448（都在 bus 的 `_timeline` 锁内） |
| `load_run_state(run_id)` | 返回运行记录、图、节点状态、全部保留的端口消息、待回答的提问、未送达的排队消息 | 运行不存在返回 `None`。没有图时 `graph=None`，调用方会改用空图（checkpoint.py:129）。不包含快照。当前时间线编号在 `run.content_time` 里。由 6 次独立的读组成 | checkpoint.py:127 |

## 列出与删除对话

一个库只装一个对话，所以列出和删除都在库之外、按文件夹做，代码在 agent/session.py，读库用的是 `read_session`（base.py:448-452 转调 sqlite.py:915-939 的 `read_session_summary`）：只读连接，读会话那一行和全部运行，不建任何东西、不写任何东西。

`list_sessions(workspace, *, limit=None)`（session.py:222-228）：取 `sessions/` 下的每个子文件夹，逐个 `read_session`，读得出来的按 `updated_at`、id 倒序排，再取前 `limit` 个。应用打开"最近的对话"和 TUI 的 `/sessions` 都用它（application.py:162；tui/app.py:463）。

| 情况 | 处理 | 位置 |
|---|---|---|
| 工作区里还没有 `sessions/` | 返回 `()` | session.py:225 |
| 文件夹里没有 `.emerge/state.db`（例如旧布局留下的 `sessions/<id>/memory/`） | 不列出 | sqlite.py:921-923 |
| 库的 `user_version` 不是 3 | 记一条 warning，不列出，也不改动它（实验：check_storage.py） | sqlite.py:927-930 |
| 库里还没有会话行（建文件夹、开库之后，`create_session` 之前） | 不列出 | sqlite.py:931-933 |
| 库读不了，或某一行过不了记录类型的校验（比如状态名非法） | 记一条 warning，只是这个对话不列出，别的对话照常（实验：check_bad_row.py） | sqlite.py:936-939 |
| 读的时候这个对话正被打开着写 | 只读连接读已提交的数据，WAL 下不用等写者 | sqlite.py:925 |

`delete_session(workspace, session_id)`（session.py:231-241）删掉一个对话的全部东西：

1. `session_dir(workspace, id)` 算出文件夹。id 为空、含 `/` 或 `\`、或者是 `.`、`..` 时抛 `ValueError`（artifacts.py:49-53）。
2. `read_session` 读不出会话时抛 `StorageError("no session 'x' in <工作区>")`（session.py:234-236）。
3. 有还没结束的运行（`running`、`idle`、`paused`，即状态不在 `ENDED_STATUSES` 里）时抛 `StorageError("session 'x' still has open runs: <id> (<status>), …")`，什么都不删（session.py:237-240）。
4. `shutil.rmtree` 删掉整个文件夹：库、trace、记忆、生成代码、全部运行目录和产物（session.py:241）。

| 情况 | 处理 | 位置 |
|---|---|---|
| 还有 running / idle / paused 的运行 | 拒绝，列出这些运行和它们的状态（实验：check_sessions.py） | session.py:237-240 |
| 运行是 `closed`（会话关闭时留下、可以接着跑的）、`finished`、`stopped` | 允许删除，以后再也不能接着跑。见疑点 9 | session.py:237 |
| 对话正被打开、但没有未结束的运行（例如刚开、还没开 run） | 照样删：打开着的库文件被删掉，之后这个对话的写入失败，由 session.py:199-207 的写库任务记日志。应用层不拦，TUI 也没有删除命令。见疑点 4 | session.py:231-241 |
| 删到一半出错（权限等） | `rmtree` 抛出，留下删了一部分的文件夹 | session.py:241 |
| 不会删的东西 | 工作区的设置（`config.json`、`AGENTS.md`、`USER.md`、`skills/`）和别的对话 | — |

和原来的不同：原来删会话只删库里的行和 `sessions/<id>/`（会话记忆），`runs/<run_id>/` 下的运行目录留着（决策记录［阶段 5］删除会话）；现在运行属于对话，产物和 trace 都在对话文件夹里，跟着一起删。

## 写入值的序列化与校验

| 函数 | 做什么 | 位置 |
|---|---|---|
| `dump_json(value)` | `json.dumps(value, ensure_ascii=False, separators=(",", ":"))`。不能转 JSON 时抛 `TypeError`。`NaN`/`Infinity` 写成非标准 JSON 的 `NaN`。非 str 键转成 str，元组变成列表 | artifacts.py:203-204 |
| `dump_model(model)` | `dump_json(model.model_dump(mode="json"))`，不用 `sort_keys`：要保留服务端给出的字段顺序，同一个模型写两次得到的字节相同（决策记录［阶段 5］序列化：原生 item 要逐字节一致） | artifacts.py:207-213 |
| `load_model(cls, text)` | `cls.model_validate_json(text)` | artifacts.py:216-217 |
| 读回 | `_session` 用 `json.loads` 加 `Usage.model_validate`；快照用 `json.loads`；其他 JSON 列用 `load_model`；图用 `loads_graph`（图那边自己用 `sort_keys=True`） | sqlite.py:945-1016, 714, 652 |

| 情况 | 处理 | 位置 |
|---|---|---|
| 写之前会检查的 | 只有这几项：会话或运行是否存在（部分方法）、`put_port_message` 的参数类型、`port_buffer ≥ 1`、能否转成 JSON | sqlite.py:224, 719-720；sqlite.py:304-310 |
| 状态名、非负数这类取值 | 实现里的参数类型只是 `str` / `int`，写库前不校验。事务提交后构造返回记录时 Pydantic 才校验，抛 `ValidationError`，但这时行已经提交（实验：`update_session(status="bogus")`、`intent_count=-1`、`set_run_status("bogus")`、`set_node_state(state="bogus")`、`append_message(turn=-1)`） | sqlite.py:360-398, 410-421；sqlite.py:593-609, 656-686 |
| 之后再读到这一行 | `get_session`、`get_run`、`runs`、`node_states`、`load_run_state`、`load_messages` 都抛 `ValidationError`。列出对话时 `read_session` 接住它，只是这个对话不列出（实验：check_bad_row.py）。见疑点 3 | sqlite.py:945-1016；sqlite.py:936-939 |
| 实际的调用方 | 阶段 7、8 传入的都是 Literal 类型里的合法值，例如 bus.py:498 的 `str(record.state)` | — |

## artifacts.py：工作区、对话文件夹与路径

布局分两层（artifacts.py:1-21）：工作区里只有所有对话共用的设置和 `sessions/`；一个对话的全部东西在它自己的文件夹里。

```text
<工作区>/
  config.json  AGENTS.md  USER.md  skills/      设置，所有对话共用
  sessions/<session_id>/                         一个对话
    .emerge/state.db                             它的库
    trace.jsonl                                  它在运行之外的事件（例如还没开运行时的主 Agent 事件）
    memory/MEMORY.md、memory/asset/              它的记忆
    code/<节点名>/v<版本>.py                     它生成的节点代码
    runs/<run_id>/                               trace.jsonl、graph.json、nodes/<节点>/<实例>/<端口>/<键>/
```

对话文件夹里的路径都从文件夹算出；对外交出的路径（`copy_memory_asset` 的返回值、`memory_assets.path`、主 Agent 文件工具里的路径）都是相对对话文件夹的 POSIX 字符串，所以工作区或对话文件夹整体搬走后仍然有效。这个模块只管到"键目录"一级，键目录里怎么摆由节点决定。除了会话 id 和生成代码的版本号，它不校验 id 或名字里的字符。

| 函数 | 路径 | 调用方 |
|---|---|---|
| `sessions_dir(ws)` | `<ws>/sessions/` | agent/session.py:224 |
| `session_dir(ws, id)` | `<ws>/sessions/<id>/`。id 为空、含 `/` 或 `\`、或是 `.`、`..` 时抛 `ValueError`（artifacts.py:51-52） | application.py:191, 240；agent/session.py:233 |
| `state_db_path(sd)` | `.emerge/state.db` | sqlite.py:227, 921 |
| `trace_file(sd, run_id)` | 有 run_id 时是 `runs/<run_id>/trace.jsonl`，run_id 为 None 时是 `trace.jsonl` | runtime/service.py:122（交给 `JsonlTracer`） |
| `runs_dir` / `run_dir` | `runs/`、`runs/<run_id>/` | 本模块；tui/app.py:547, 761 |
| `graph_file` | `runs/<run_id>/graph.json` | runtime/run.py:90 |
| `node_dir` | `runs/<run_id>/nodes/<node_id>/<instance_id>/` | service.py:297；tui/app.py:553 |
| `ref_dir(sd, run_id, ref)` | `node_dir(ref.producer, ref.producer_instance)/<ref.port>/<ref.key>/`。实例在路径里，所以新旧实例、或两个不同的节点写同名的键，不会互相覆盖 | runtime/context.py:356, checkpoint.py:201 |
| `memory_dir` / `memory_file` / `memory_asset_dir` | `memory/`、`memory/MEMORY.md`、`memory/asset/` | agent/memory.py:120 |
| `code_dir` / `code_path(sd, name, version)` | `code/`、`code/<name>/v<规范化后的版本>.py` | application.py:259, checkpoint.py:191, agent/tools/graph.py:177 |
| `list_generated_names(sd)` | `code/` 下的类型目录名，排好序；没有 `code/` 时返回 `()` | application.py:256 |
| `ensure_run_dir` / `ensure_node_dir` | 路径同上，并 `mkdir(parents=True, exist_ok=True)` | runtime/run.py:54、runtime/context.py:132 |

| 情况 | 处理 | 位置 |
|---|---|---|
| `ensure_*` 的目标目录已存在 | 不报错 | artifacts.py:116, 122 |
| 路径上某一级是普通文件 | `mkdir` 抛 `FileExistsError` 或 `NotADirectoryError`（实验） | artifacts.py:116, 122 |
| `Ref.key` 是 `.`、`..`，或含 `/`、`\` | 构造 `Ref` 时就抛 `ValidationError`（graph/ports.py:90-96），根本到不了这里 | — |
| `node_id`、`producer_instance` 含 `/` | 不校验，会多出目录层级。节点 id 的规则是 `^[^.\s]+$`（graph/ports.py:13），排除了点和空白，但没有排除 `/`。实例 id 的格式是 `<node_id>@v<图版本>`（graph/graph.py:32-38）。`producer_instance` 只要求非空，实验中给它 `a/../..` 时路径爬出了节点目录（这个值由框架生成） | artifacts.py:80-90 |
| 会话 id 不能当文件夹名（空、含分隔符、`.`、`..`） | `session_dir` 抛 `ValueError`。应用切换对话前先检查，报成 `StartupError`，当前对话不受影响（实验：check_sessions.py） | artifacts.py:51-52；application.py:238-244 |

记忆图片 `copy_memory_asset(session_dir, source)`（artifacts.py:129-144）：

| 情况 | 处理 | 位置 |
|---|---|---|
| 正常情况 | 读入整个文件，用 sha256 加原扩展名（转小写）作文件名，复制进 `memory/asset/`，返回相对对话文件夹的路径 | artifacts.py:136-144 |
| 同一张图复制两次，或扩展名只差大小写（`.PNG` 和 `.png`） | 目标已存在就不再写，返回同一个路径（实验） | artifacts.py:141-143 |
| 内容相同、扩展名不同（`.png` 和 `.jpg`） | 名字不同，存成两个文件（实验） | artifacts.py:141 |
| 源文件没有扩展名 | 文件名只有哈希（实验） | artifacts.py:141 |
| 两个对话记住同一张图 | 各自的对话文件夹里各存一份，互不影响 | artifacts.py:139 |
| 目标文件已存在但内容被改过 | 不比对内容，原样保留：同名就当作同内容（实验） | artifacts.py:142 |
| 源文件不存在 / 是目录 | 抛 `FileNotFoundError` / `IsADirectoryError`（实验） | artifacts.py:137 |
| 源是相对路径 | 按进程的当前目录解析，不是按对话文件夹（实验）。阶段 8 会先把路径解析成对话文件夹内的绝对路径（agent/tools/memory.py:46, 74） | artifacts.py:136 |
| 登记 | 这个函数只复制，不登记。登记由调用方用 `add_memory_asset` 做（agent/memory.py:130-132），两步不在一个事务里 | — |
| 为什么复制而不是记录原路径 | 节点以后可能覆盖自己产出的文件，记忆就会悄悄指向别的内容（artifacts.py:132-134） | — |

生成代码（artifacts.py:150-197）：

| 情况 | 处理 | 位置 |
|---|---|---|
| 版本写成 `"7"`、`"v7"`、`"v007"` | 都规范成 `v7.py`，是同一个文件（实验） | artifacts.py:192-194 |
| 版本为空串，或含 `/`、`\` | 抛 `ValueError("generated code version … cannot name a file")` | artifacts.py:195-196 |
| 其他非数字版本（`"beta"`、`"V2"`、`"-1"`、`".."`） | 原样拼进文件名：`vbeta.py`、`vV2.py`、`v-1.py`、`v...py`（实验） | artifacts.py:197 |
| `name` 含 `/` 或 `..` | 不校验，`"../x"` 得到 `code/../x/v1.py`（实验）。调用方 `register_node_code` 要求名字只含字母、数字和下划线（agent/tools/graph.py:129-130） | artifacts.py:110-111 |
| `write_generated_code` 写一个已有的版本 | 直接覆盖（docstring 里写明了） | artifacts.py:150-155 |
| `read_generated_code` 读的文件不存在 | 抛 `FileNotFoundError`（实验） | artifacts.py:158-159 |
| 读最新版本 | 没有专门的函数，要先取 `list_generated_versions(...)[-1]`，再 `read_generated_code` | — |
| `list_generated_versions` 时类型目录不存在 | 返回 `()` | artifacts.py:176-178 |
| `list_generated_versions` 的排序 | 只看 `v*.py`。匹配 `v<数字>` 的按数值升序（v9 在 v10 前），其余按文本排在后面 | artifacts.py:179-187 |
| 同一个数值有两个文件（手工放进去的 `v10.py` 和 `v010.py`） | 列出两个 `"10"`（实验） | artifacts.py:182-184 |
| 文件名正好是 `v.py` | 列出 `""`，但 `""` 是读不了的版本（会抛 ValueError）（实验） | artifacts.py:186 |
| 两个对话生成了同名同版本的代码 | 各在自己的 `code/` 里，是两个文件；注册时模块名由完整路径、版本和内容算出，同一个进程先后打开两个对话也不会混用（graph/registry.py:278-284） | artifacts.py:110-111 |

## 疑点（待核实）

1. **中文检索的切词有两种情况查不到**。（a）`_TOKEN_RE` 的第二个分支 `[^\s]+` 会把紧跟在非 CJK 字符后面的汉字吞进同一个词，这个词整体当作一个短语去匹配，而索引里汉字是逐字分开的。所以 `UR5机械臂`、`v2发布`、`偏好，颜色`、`（偏好）` 都查不到，尽管条目原文里就有这些串（实验）。（b）只有符号的词（如 `。`、`!`）会变成空短语，和其他词 AND 之后整个查询为空，`偏好。`、`hello !` 也查不到。这和 base.py:280-281 的说明"a query of any length finds a substring"不符。决策记录 HANDOFF.md:364 只写了"只含符号的查询返回空"。证据：sqlite.py:69, 1035-1039, 1046。
2. **`open()` 失败时的连接（2026-11-04 已改）**。原来 `_init_schema` 抛错时不关连接，aiosqlite 的工作线程不是 daemon，进程在退出时卡住。现在先关连接再抛（sqlite.py:241-245）。还剩一点：`PRAGMA journal_mode=WAL` 在版本检查之前执行，被拒绝的库文件也被改成了 WAL（sqlite.py:231, 248-255）。
3. **取值在写之后才校验**。实现中的状态名、轮号、计数这些参数不校验就写进库，构造返回值时才抛 `ValidationError`，而行已提交。之后读这一行的方法全部抛错；列出对话时 `read_session` 会接住，这个对话只是不再出现在列表里（实验：check_bad_row.py）。目前的调用方传的都是合法值。证据：sqlite.py:360-398, 410-421；sqlite.py:593-609, 656-686。
4. **删除对话先检查、后删文件夹，中间没有锁**。`delete_session` 先只读地看一眼运行状态，再 `rmtree`。两步之间另一个进程打开了这个对话、开了运行，它正在用的库照样被删。同一进程里，正打开着、但没有未结束运行的对话也会被删掉，之后它的写入失败只记日志。证据：agent/session.py:231-241。
5. **写完再读回要取两次锁**。7 个方法在写和读回之间可能插进别的调用：返回值可能已经包含别人之后的写；如果这一行在中间被删，`assert session is not None` 之类的检查失败，抛 `AssertionError`（实验：在 `update_session` 上复现）。证据：sqlite.py:396-398, 404-406, 607-609, 625-627, 633-635, 839, 850, 854。
6. **`aclose()` 不取锁**。关闭时如果还有事务在进行，检查点报 `database table is locked`，连接照样被关掉，那个事务丢失，发起它的协程收到 `ValueError`（实验）。目前要靠调用方先把写都做完（例如 `Session.close` 会先 flush 写库队列）。证据：sqlite.py:312-318。
7. **"取号和写盖章消息在同一个事务里"与代码不符**。HANDOFF.md:270、sqlite.py:5-6 和 base.py:423 都说时间线编号和盖了这个号的消息在同一个事务里更新。实际上 `next_content_time` 自己单独提交（sqlite.py:889-894），消息由 bus 随后另起事务写入（runtime/bus.py:251→252、285→296、308→309、386→399、448→449），两步的先后顺序靠 bus 的 `_timeline` 锁保证。结果是只会出现空号、不会出现重号，但两步并不在一个事务里。
8. **`base.py` 里 `put_port_message` 的类型注解写错了**。base.py:367 写的是 `message: Message`，而这个文件里的 `Message` 是 `models.types.Message`（对话消息）。实现要求的是 `graph.ports.Message`，否则抛 `StorageError`（sqlite.py:718-720）。HANDOFF.md:344 写的也是 `graph.ports.Message`。
9. **"还没结束的运行"在两处用词不同**。删除对话时拦的是状态不在 `ENDED_STATUSES`（finished、stopped、closed）里的运行，也就是 running、idle、paused，`closed` 的运行跟着一起删（agent/session.py:237；runtime/run.py:32-33）；`delete_session` 的 docstring 说 "Refused while a run of it is open"。而 sqlite.py 的 `_OPEN_RUN_STATES` 把 `closed` 也算作 open，它只用来决定 `set_run_status` 是否清空 `finished_at`（sqlite.py:63-65, 600）。计划 phase-05-storage.md:92 写的是"会话的运行要先结束才能删"。
10. **决策记录对序号的描述不准**。HANDOFF.md:308 说 `counters` 只存两个全局序号（`session_seq`、`pending_seq`），实际还有 `ask_seq`；现在 `session_seq` 已改名为 `message_seq`，三个计数器都是每个对话一套（sqlite.py:415, 473, 813）。HANDOFF.md:357 说 `pending_inputs` 的序号是每个会话单独编的、删除时必须带会话 id，现在一个对话一个库，这条不再适用。
11. **计划和代码有三处不同**。① phase-05-storage.md:65 说 `runs` 记"当前图版本"，表里没有这一列（sqlite.py:138-146），图版本存在 `graphs.data` 里（graph/serialization.py:31）。② phase-05-storage.md:102 说 `load_run_state` 包含快照，但 `RunState` 没有快照字段（base.py:158-168），`_node_state` 也不读 snapshot 列（sqlite.py:982-992）。③ phase-05-storage.md:127 要求能"读取最新版本"的生成代码，但没有对应的函数（artifacts.py:150-187）。
12. **`foreign_keys=ON` 不起作用，不查存在性的写会留下孤儿行**。外键检查打开了（sqlite.py:233），但没有一张表声明外键。`set_node_state`、`set_node_snapshot`、`put_port_message` 可以写到不存在的运行下面，这些行要等整个对话被删时才随库消失（实验）。证据：sqlite.py:669-676, 699-705, 724-728。
13. **`RunState.port_messages` 的注释写的是"per input port"**（base.py:164）。实际返回的是本运行每个输出端口（source node、port）保留的全部消息，按 node、port、version 排序（sqlite.py:902-904）。

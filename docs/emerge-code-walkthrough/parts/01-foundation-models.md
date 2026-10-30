# 基础层与模型层（阶段 0–2）

## 概览

| 文件 | 职责 |
|---|---|
| `emerge/config.py` | 读取并校验 `<workspace>/config.json`，得到不可变的 `WorkspaceConfig`；`init_workspace()` 在首次运行时复制模板 |
| `emerge/exceptions.py`、`emerge/events.py`、`emerge/__init__.py` | 异常层级；事件记录 `Event` 与类型常量 `EventType`；包顶层导出 |
| `emerge/callbacks/` | 事件扇出（每个 handler 一条队列、一个任务）；`JsonlTracer` 写 `trace.jsonl` |
| `emerge/workspace_template/`、`pyproject.toml`、`README.md` | 随包分发的工作区模板；打包与依赖 |
| `emerge/models/` | 统一消息类型；三种 API 模式；流式为主、自动降级；重试；用量与 token 估算；`ModelClient` |

依赖方向：`models` → `callbacks` → `events`；`config`、`models` → `exceptions`；provider 读取 `config` 中的 `ModelConfig` 和各家 options。`models` 不依赖 `agent`、`graph`、`runtime`。

---

## 配置：`emerge/config.py`

`load_config(workspace)` 读取 `<workspace>/config.json` 并校验成 `WorkspaceConfig`；`init_workspace(workspace)` 在目标不存在时复制模板。各字段的读取方写在下表“含义”列；`load_config`、`init_workspace` 目前在仓库内没有调用方（阶段 9 的 `application.py` 还没实现）。

所有配置块都继承 `_Section`，设置为 `extra="forbid"`、`frozen=True`（`emerge/config.py:13-14`）：多写或写错字段会报错；加载后再给字段赋值会抛 `ValidationError`。

### 字段

| 字段 | 含义 | 约束/默认 | 位置 |
|---|---|---|---|
| `providers` | 只有 `openai`、`anthropic` 两个键；由 `model.provider` 选其中一个 | 两个键都可省略或写 `null` | `emerge/config.py:54-61` |
| `providers.*.base_url` | 服务地址 | 必填，非空；不校验格式 | `emerge/config.py:20` |
| `providers.*.api_key` | 明文 key | 默认 `""`；加载时不检查，`create_provider` 时为空才报错 | `emerge/config.py:21` |
| `providers.*.timeout` | 单位毫秒：连续这么久一个字节都没收到/发不出，就算断线（见“断线判断”） | 默认 600000，`> 0` | `emerge/config.py:25` |
| `providers.openai.options.reasoning_summary` | 只用于 Responses：推理摘要的详细程度 | 默认 `"auto"`；不校验取值；空串时请求里发 `summary: null` | `emerge/config.py:31` |
| `providers.anthropic.options.thinking.type` | `adaptive` / `disabled` | 默认 `adaptive` | `emerge/config.py:36` |
| `providers.anthropic.options.thinking.display` | `summarized` / `omitted` | 默认 `summarized` | `emerge/config.py:39` |
| `model.provider` | 用哪一家（同时决定用哪个 SDK） | 必填，`openai` / `anthropic` | `emerge/config.py:86` |
| `model.api_mode` | `chat_completions` / `responses` / `anthropic_messages` | 必填；它与 provider 的搭配到 `create_provider` 时才检查 | `emerge/config.py:87` |
| `model.name` | 模型名 | 必填，非空 | `emerge/config.py:88` |
| `model.limit.context` / `model.limit.output` | 模型本身的上下文窗口和最长输出；不发给 API | 必填，`> 0` | `emerge/config.py:67-71,89` |
| `model.options.temperature` | 采样温度 | 默认 0.0，范围 [0, 2] | `emerge/config.py:77` |
| `model.options.max_output_tokens` | 单次最长输出 | 默认 16384，`> 0`，且 ≤ `model.limit.output` | `emerge/config.py:78,92-99` |
| `model.options.reasoning_effort` | 推理强度 | 默认 `"medium"`；不校验取值；空串表示不发推理参数 | `emerge/config.py:80` |
| `model.options.speed` | 速度档位 | `standard` / `fast` / `ultrafast`，默认 `standard`；当前 api_mode 支不支持，到 `create_provider` 时才检查 | `emerge/config.py:82` |
| `agent.max_context_tokens` | 主 Agent 打算用的上下文上限（`emerge/agent/agent.py:307`） | 默认 100000，`> 0`，且 ≤ `model.limit.context` | `emerge/config.py:114,135-142` |
| `agent.compact_threshold` | 从多大比例开始压缩（`emerge/agent/agent.py:308`） | 默认 0.8，范围 [0, 1] | `emerge/config.py:115` |
| `agent.max_retries` | 单次模型请求的重试次数（`emerge/models/factory.py:66`） | 默认 3，`≥ 0` | `emerge/config.py:116` |
| `agent.limits.max_iterations` / `max_input_tokens` / `max_output_tokens_total` / `max_tool_calls` | 每轮预算（`emerge/agent/agent.py:319`） | 每项默认 `None` = 不限；写了值须 `> 0`；`"limits": {}` 表示全部不限 | `emerge/config.py:102-108,117` |
| `runtime.stop_timeout` | 单位秒（`emerge/runtime/policies.py:24`） | 默认 30，`> 0` | `emerge/config.py:121` |
| `runtime.port_buffer` | 每个输出端口保留的消息条数（`emerge/runtime/supervisor.py:202`） | 默认 64，`> 0` | `emerge/config.py:123` |
| `nodes` | 节点模块名列表 | 默认 `[]`；目前没有代码读取 | `emerge/config.py:131` |

### 路径属性

| 属性 | 值 | 位置 |
|---|---|---|
| `workspace` | `load_config` 解析出的绝对路径。它存放在私有属性 `_workspace` 里，不是 JSON 字段 | `emerge/config.py:133,144-146` |
| `config_path` | `<ws>/config.json` | `emerge/config.py:148-150` |
| `agents_md`、`user_md` | `<ws>/AGENTS.md`、`<ws>/USER.md` | `emerge/config.py:152-158` |
| `memory_md` | `<ws>/memory/MEMORY.md` | `emerge/config.py:160-162` |
| `code_dir`、`runs_dir`、`skills_dir` | `<ws>/code`、`<ws>/runs`、`<ws>/skills` | `emerge/config.py:164-174` |
| `ProvidersConfig.get(name)` | `name` 是 `openai` 或 `anthropic` 时返回对应条目（可能是 `None`）；其他名字返回 `None` | `emerge/config.py:63-64` |

### `load_config()`

1. 确定目标目录：`workspace` 为 `None` 或空串时用 `~/.emerge/workspace`；否则用 `expanduser().resolve()` 的结果，相对路径按当前目录解析（`emerge/config.py:185,225`）。
2. 按 UTF-8 读取 `config.json`，再 `json.loads`（`emerge/config.py:227-228`）。
3. 调用 `WorkspaceConfig.model_validate`（`emerge/config.py:234`）。
4. 把第 1 步得到的路径存进 `_workspace`（`emerge/config.py:237`）。

校验失败时的报错格式：先是 `invalid config <path>:`，然后每个错误一行 `  <字段路径>: <Pydantic 信息>`。字段路径用 `.` 连接，路径为空时写 `<root>`（`emerge/config.py:177-182`）。

| 配置/情况 | 结果 | 位置 |
|---|---|---|
| `config.json` 不存在 | `ConfigError("config file not found: <path>")` | `emerge/config.py:229-230` |
| JSON 语法错误（UTF-8 BOM 也算） | `ConfigError("invalid JSON in <path>: line L column C: <msg>")` | `emerge/config.py:231-232` |
| 文件不是 UTF-8 编码 | `UnicodeDecodeError` 原样抛出，不是 `ConfigError`（实测） | `emerge/config.py:228` |
| `config.json` 是个目录 / 工作区路径是个普通文件 / 没有读权限 | 分别是 `IsADirectoryError` / `NotADirectoryError` / `PermissionError`，原样抛出（前两种实测） | `emerge/config.py:228` |
| 顶层不是 JSON 对象（例如 `[]`） | `ConfigError`，字段路径写 `<root>` | `emerge/config.py:180,235-236` |
| 任意一层多写了字段：拼写错误、已删除的字段（`agent.max_iterations`、`agent.request_timeout`、`runtime.max_restarts`、`runtime.checkpoint`）、未知供应商（`providers.gemini`）、放错位置的专用参数（`model.options.thinking`、`providers.openai.options.thinking`、`providers.openai.options.include`） | `ConfigError`，报 `<字段路径>: Extra inputs are not permitted`（实测） | `emerge/config.py:14` |
| 缺必填字段（`model`、`model.limit`、`model.provider` / `api_mode` / `name`、`providers.*.base_url`） | `ConfigError`，报 `Field required` | `emerge/config.py:20,86-89,128` |
| `model.name` 或 `base_url` 为空串 | `ConfigError`，报 `String should have at least 1 character` | `emerge/config.py:20,88` |
| 枚举值不在允许范围内（`provider`、`api_mode`、`speed`、`thinking.type`、`thinking.display`） | `ConfigError`，报错里列出允许的值 | `emerge/config.py:36,39,82,86-87` |
| 数值越界（上表任一 `gt` / `ge` / `le` 约束） | `ConfigError`，例如 `providers.openai.timeout: Input should be greater than 0` | 见上表 |
| `max_output_tokens > model.limit.output` | `ConfigError`，字段路径写 `model`，信息为 `Value error, model.options.max_output_tokens (…) is above model.limit.output (…)…` | `emerge/config.py:92-99` |
| `agent.max_context_tokens > model.limit.context` | `ConfigError`，字段路径写 `<root>` | `emerge/config.py:135-142` |
| 上面两项同时超限 | 只报 `model` 那一条：`model` 字段校验失败后，`WorkspaceConfig` 的整体检查不再运行（实测） | `emerge/config.py:92-99,135-142` |
| 刚好等于上限 | 通过 | `emerge/config.py:94,137` |
| 省略了 `agent` 或 `model.options`，而 `limit` 比默认值小（`context` < 100000 或 `output` < 16384） | `ConfigError`：默认值同样要通过上限检查（实测） | `emerge/config.py:78,114` |
| 类型可以宽松转换的值，例如 `"timeout": "1000"`、`"max_output_tokens": 1.0`、`true` | 接受，分别转成 1000.0、1、1；整数字段给 `1.5` 则报错（Pydantic 宽松模式，实测） | — |
| `"limits": null` | `ConfigError`：这里要求对象 | `emerge/config.py:117` |
| `"providers": {}`，或某一家写 `null` | 加载通过；`create_client` 用到时报 `ConfigError` | `emerge/models/factory.py:60-62` |
| provider 与 api_mode 搭配不对（例如 `anthropic` + `responses`） | 加载通过；`create_provider` 时报 `ModelAPIModeError` | `emerge/models/factory.py:31-33` |
| 当前 api_mode 不支持所配的 `speed` | 加载通过；`create_provider` 时报 `ConfigError` | `emerge/models/factory.py:35-37` |
| `api_key` 为空或没写 | 加载通过；`create_provider` 时报 `ConfigError("providers.<name>.api_key is empty")` | `emerge/models/factory.py:38-39` |
| 加载后修改字段 | `ValidationError`（frozen） | `emerge/config.py:14` |
| 不经 `load_config`，直接 `WorkspaceConfig.model_validate(...)` | 校验规则相同；但 `workspace` 和所有路径属性都会抛 `AttributeError`（`_workspace` 没有设置），所以 `create_client` 也会失败（实测） | `emerge/config.py:133,144-146` |

### `init_workspace()`

| 情况 | 结果 | 位置 |
|---|---|---|
| `workspace` 为 `None` 或空串 | 目标是 `~/.emerge/workspace` | `emerge/config.py:211` |
| 给了路径 | 用 `expanduser().resolve()` 后的路径 | `emerge/config.py:211` |
| 目标已存在（目录、空目录或普通文件都算） | 返回 `False`，不做任何改动（实测） | `emerge/config.py:212-213` |
| 目标不存在 | 建目录（包括父目录）；按 `_TEMPLATE_FILES` 逐个用 `shutil.copy2` 复制 8 个文件（保留权限和时间戳）；返回 `True` | `emerge/config.py:188-197,214-221` |
| 复制到一半失败（磁盘满、模板缺文件） | 异常原样抛出；已建的目录和已复制的文件都留着。再调用时目标已存在，直接返回 `False`，不会补齐 | `emerge/config.py:212-220` |
| 模板目录里有、但不在 `_TEMPLATE_FILES` 中的文件 | 不复制 | `emerge/config.py:188-197` |
| 模板目录的位置 | 包内的 `emerge/workspace_template/`；`pyproject.toml` 用 `force-include` 把它打进 wheel | `emerge/config.py:200-202`；`pyproject.toml:28-30` |
| 与 `load_config` 的关系 | `load_config` 不会调用它；工作区不存在时 `load_config` 报 “config file not found” | `emerge/config.py:224-230` |

---

## 异常与包入口：`emerge/exceptions.py`、`emerge/__init__.py`

框架里的异常都继承 `EmergeError`，唯一的例外是 `StopRequested`，它继承 `BaseException`。`emerge/__init__.py` 从顶层导出 `load_config`、`WorkspaceConfig` 和 `exceptions.py` 里的全部 14 个异常类；`init_workspace`、各个配置块类和 `EmptyStream` 不从顶层导出。

| 类 | 父类 | 谁抛 | 含义 |
|---|---|---|---|
| `EmergeError` | `Exception` | 不直接抛 | 框架异常的根 |
| `ConfigError` | `EmergeError` | `config.load_config`（文件缺失、JSON 错误、校验失败）；`models/factory.py`（speed 不支持、`api_key` 为空、provider 没有对应条目） | 配置有问题 |
| `ModelError` | `EmergeError` | `models/retry.py`（把 SDK 错误包装成它）、`models/base.py`、三个 provider、`models/client.py`、`agent/context.py` | 一次模型请求失败 |
| `ModelAPIModeError` | `ModelError` | `models/factory.py`（不支持的搭配）；三个 provider 的 `probe_api_compatibility`（404 / 405 / 422） | 服务或配置的搭配不支持所选 API 模式 |
| `EmptyStream`（定义在 `emerge/models/base.py:32`） | `ModelError` | 三个 provider 的 `_stream_once` 和 `_ping` | 流式请求一个事件都没收到。它只在 `ModelProvider` 内部用来触发降级，不会传到外面 |
| `ToolError` | `EmergeError` | `agent/tool.py`、`agent/tools/*` | 工具调用失败 |
| `GraphValidationError` | `EmergeError` | `graph/` 下的 validation、builder、patch、serialization、edge、spec | 图或图文件不合法 |
| `RegistryError` | `EmergeError` | `graph/registry.py` | 注册或查找节点类型失败 |
| `NodeError` | `EmergeError` | `graph/node.py`（检查节点类定义）、`runtime/process.py`、`runtime/context.py` | 节点定义错误或节点运行出错 |
| `RuntimeControlError` | `EmergeError` | `runtime/` 多处、`agent/agent.py` | 不允许的运行控制操作 |
| `StorageError` | `EmergeError` | `storage/sqlite.py`、`agent/session.py` | 存储层错误 |
| `CheckpointError` | `EmergeError` | `runtime/checkpoint.py` | 这次运行不能接着跑 |
| `StopRequested` | **`BaseException`** | `runtime/context.py`、`runtime/bus.py` | 节点在阻塞等待时被要求停止。`except Exception` 拦不住它，需要按名字捕获、清理后重新抛出 |
| `WaitTimeout` | `EmergeError` | `runtime/context.py` | `NodeContext` 的等待在超时前什么都没等到 |
| `InputClosed` | `EmergeError` | `runtime/context.py` | 这个输入不会再收到消息：源节点已结束、被删除，或运行结束。源节点被替换不算 |

---

## 事件：`emerge/events.py`

`Event` 是各模块共用的事件记录（`@dataclass(slots=True)`），`EventType` 是 `StrEnum` 常量。callbacks 分发、bus 跨进程转发（`to_dict`）、tracer 落盘用的都是它。

| 字段/方法 | 含义 | 约束/默认 | 位置 |
|---|---|---|---|
| `type` | 事件类型：`EventType` 成员或任意字符串 | 必填 | `emerge/events.py:36` |
| `timestamp` | Unix 时间戳（秒） | 默认取创建时的 `time.time()` | `emerge/events.py:37` |
| `run_id`、`node_id`、`irreversible` | 所属运行、节点，是否来自不可逆节点 | 默认 `None`；由视图、主 Agent 的 `_RunEvents` 或 bus 补填 | `emerge/events.py:38-39,46` |
| `instance_id` | 产生事件的节点实例。节点被替换后 id 不变，靠这个字段丢弃旧实例的迟到事件 | 默认 `None` | `emerge/events.py:42` |
| `content_time` | 运行内全图时间线上的位置。bus 只给节点与主 Agent 之间传的内容盖这个戳；模型 token 之类的过程事件为 `None` | 默认 `None` | `emerge/events.py:45` |
| `payload` | 任意 dict | 默认 `{}`；不检查能否 JSON 序列化 | `emerge/events.py:47` |
| `to_dict()` | 用 `dataclasses.asdict` 转成 dict（`payload` 会被深拷贝），`type` 转成 `str` | — | `emerge/events.py:49-52` |
| `from_dict(d)` | 直接 `cls(**d)`。有多余的键或缺少 `type` 时抛 `TypeError`；`type` 保持为普通字符串，不转回 `EventType`，但与成员比较仍然相等（实测） | — | `emerge/events.py:54-56` |

| 常量 | 值 | 发出位置 |
|---|---|---|
| `RUN_STARTED` / `RUN_FINISHED` | `run.started` / `run.finished` | `emerge/runtime/service.py:160` / `:340`；`JsonlTracer` 收到 `run.finished` 后关闭这个 run 的文件 |
| `NODE_STATE` | `node.state` | `emerge/runtime/bus.py:254` |
| `NODE_REPORT` | `node.report` | `emerge/runtime/bus.py:420` |
| `NODE_CUSTOM` | `node.custom` | 节点调 `emit_event` 发出（`emerge/runtime/context.py:326-336`），经 bus 盖时间戳后转发（`emerge/runtime/bus.py:438-444`） |
| `NODE_ASK` / `NODE_ANSWER` | `node.ask` / `node.answer` | `emerge/runtime/bus.py:452` / `:311` |
| `NODE_MAIN_AGENT_MESSAGE` | `node.main_agent_message` | `emerge/runtime/bus.py:292` |
| `PORT_PUBLISHED` | `port.published` | `emerge/runtime/bus.py:408` |
| `MODEL_STARTED` / `MODEL_TOKEN` / `MODEL_FINISHED` | `model.started` / `model.token` / `model.finished` | `emerge/models/client.py:74` / `:53,55` / `:60,79`（payload 见“客户端”一节） |
| `MODEL_USAGE` | `model.usage` | `emerge/models/usage.py:80` |
| `TOOL_STARTED` / `TOOL_FINISHED` | `tool.started` / `tool.finished` | `emerge/agent/loop.py:236` / `:243` |
| `GRAPH_PATCHED` | `graph.patched` | `emerge/runtime/mutations.py:175` |
| `AGENT_MESSAGE` | `agent.message` | `emerge/agent/loop.py:95,115,177` |
| 不在 `EventType` 里的类型 | `node.stale`、`run.status`、`run.idle`、`agent.turn`、`agent.inbox`、`agent.fork`、`agent.control` | `emerge/runtime/bus.py:41`、`emerge/runtime/run.py:28,30`、`emerge/agent/agent.py:70-76`。它们没有对应的具名钩子，只能在 `on_event` 里收到 |

---

## 事件分发：`emerge/callbacks/`

`CallbackManager` 把事件扇出给已注册的 handler：每个 handler 有自己的无上限 `asyncio.Queue` 和一个消费任务，`emit` 只负责入队。`RuntimeService.open` 会注册 tracer、feedback、stream 三个 handler（`emerge/runtime/service.py:135-137`）。`ModelClient`、`AgentLoop`、bus 通过 `EventEmitter` 协议发事件。计划里的 `add` / `remove` / `child()` 已改名为 `add_handler` / `remove_handler` / `with_event_defaults`（`EMERGE-Policy-2.0-Plan/refactor-api-naming.md:121-123`）。

### 分发顺序

1. `add_handler(h)`：第一次调用时绑定当前正在运行的事件循环，之后必须是同一个循环；按 `id(h)` 去重；为 `h` 建一个 `_Worker`，即一条队列加一个名为 `callback:<类名>` 的任务（`emerge/callbacks/manager.py:56-59,26-29`）。
2. `emit_nowait(e)`：如果在绑定的循环线程里调用，先把 `_pending` 里其他线程发来的事件全部分发，再分发 `e`；如果在其他线程或其他循环里调用，就把 `e` 放进 `_pending`，再 `call_soon_threadsafe(_flush_pending)`（`emerge/callbacks/manager.py:69-83`）。
3. `_dispatch(e)`：manager 已关闭时记一条 warning 并丢弃；否则按注册顺序，对每个 `accepts(e)` 为真的 handler 执行 `put_nowait`（`emerge/callbacks/manager.py:110-116`）。
4. 每个 worker 按顺序取事件，执行 `await handler.on_event(e)`；取到 `_STOP` 后退出循环，再调用一次 `handler.close()`（`emerge/callbacks/manager.py:31-43`）。
5. 默认的 `on_event` 用 `_HOOKS` 查表，把 `node.state` 这类类型交给对应的 `on_node_state` 等具名钩子（`emerge/callbacks/base.py:6,16-19`）。

### `CallbackHandler`（`base.py`）

| 成员/情况 | 处理 | 位置 |
|---|---|---|
| `event_types` 类属性 | `None` 表示接收所有类型；否则 manager 只投递集合里的类型。写 `EventType` 成员或字符串都能匹配，因为 `StrEnum` 与 `str` 的相等和哈希一致 | `emerge/callbacks/base.py:11,13-14` |
| 事件类型是 `EventType` 里的值 | 默认 `on_event` 调用对应的具名钩子（17 个，默认都什么也不做） | `emerge/callbacks/base.py:17-19,24-73` |
| 事件类型不在 `EventType` 里（例如 `run.status`） | 默认 `on_event` 直接返回；只有覆盖了 `on_event` 的 handler 才能看到它 | `emerge/callbacks/base.py:17-18` |
| 类型字符串恰好是 `"event"` 这样的任意值 | 不会被路由到 `on_event` 自己，因为 `_HOOKS` 只包含已知类型 | `emerge/callbacks/base.py:5-6` |
| 子类覆盖了 `on_event` 且不调用 `super()`（例如 `JsonlTracer`） | 具名钩子不再被调用 | `emerge/callbacks/tracers.py:20` |
| `close()` | worker 停止时调用一次（handler 被移除或 manager 关闭时） | `emerge/callbacks/base.py:21-22` |

### `CallbackManager`（`manager.py`）

| 情况 | 处理 | 位置 |
|---|---|---|
| 还没有调用过任何 `add_handler` 就 `emit_nowait` | `RuntimeError("CallbackManager has no event loop yet; …")` | `emerge/callbacks/manager.py:71-73` |
| 在没有运行事件循环的地方调用 `add_handler` | `asyncio.get_running_loop()` 抛 `RuntimeError("no running event loop")`（实测） | `emerge/callbacks/manager.py:100` |
| 在另一个事件循环里调用 `add_handler` | `RuntimeError("CallbackManager is bound to a different event loop")` | `emerge/callbacks/manager.py:103-104` |
| 同一个 handler 添加两次 | 第二次被忽略（按 `id` 判断） | `emerge/callbacks/manager.py:58-59` |
| handler 的 `on_event` 抛出 `Exception` | `logger.exception` 记录后继续处理下一个事件，不影响其他 handler 和调用方 | `emerge/callbacks/manager.py:36-39` |
| handler 的 `close()` 抛出 `Exception` | 记录下来，不往外抛 | `emerge/callbacks/manager.py:40-43` |
| handler 抛出 `BaseException`（例如 `CancelledError`） | 不捕获。这个 worker 任务结束，后续事件留在它的队列里不再处理，`close()` 也不会被调用；`aclose()` 里的 `gather` 会把 `CancelledError` 抛给调用方（实测） | `emerge/callbacks/manager.py:36-38,97` |
| handler 处理得慢 | 只会拖慢它自己的队列；队列没有上限，积压会占内存（决策记录［阶段 1］，HANDOFF.md:126） | `emerge/callbacks/manager.py:28` |
| 事件顺序 | 同一个 handler 按 FIFO 收到；不同 handler 之间没有先后保证 | `emerge/callbacks/manager.py:28-37` |
| 多个 handler 收到的 `Event` | 是同一个对象，不复制（经视图填充时才会生成新对象）。一个 handler 改了 `payload`，其他 handler 也会看到（实测） | `emerge/callbacks/manager.py:114-116` |
| `emit()`（async 版本） | 与 `emit_nowait` 相同，不等待 handler 处理完 | `emerge/callbacks/manager.py:66-67` |
| 在其他线程调用 `emit_nowait` | 先进入 `_pending`（`deque`），再由循环线程按顺序取出。循环线程下一次 `emit_nowait` 和 `aclose()` 都会先清空 `_pending`，从而保持先后顺序（实测） | `emerge/callbacks/manager.py:78-83,91,106-108` |
| 事件循环已关闭后，在其他线程调用 `emit_nowait` | `call_soon_threadsafe` 抛 `RuntimeError("Event loop is closed")`，事件留在 `_pending` 里（实测） | `emerge/callbacks/manager.py:82-83` |
| `remove_handler(h)` | 从表中移除 `h`，并往它的队列放一个 `_STOP`：已入队的事件照常处理完，然后调用 `close()`。这个方法不 await；之后的事件不再投递给 `h`（实测） | `emerge/callbacks/manager.py:61-64` |
| `remove_handler` 一个没有注册的 handler | 什么都不做 | `emerge/callbacks/manager.py:62-63` |
| `aclose()` | 依次：清空 `_pending` → 标记为已关闭 → 给每个 worker 放 `_STOP` → `gather` 等所有 worker 处理完已入队事件并调用 `close()` | `emerge/callbacks/manager.py:90-97` |
| 第二次 `aclose()` | worker 表已为空，立即返回 | `emerge/callbacks/manager.py:93-97` |
| 关闭后再 `emit_nowait` | 记一条 warning（`dropping … emitted after CallbackManager was closed`）并丢弃，不抛异常 | `emerge/callbacks/manager.py:111-113` |
| 关闭后再 `add_handler` | 照样建 worker，但所有事件都被丢弃；它的任务会一直挂着，直到 `remove_handler`（实测） | `emerge/callbacks/manager.py:56-59,111-113` |

### 默认字段视图：`with_event_defaults()` / `CallbackView`（即计划里的 `child()`）

| 情况 | 处理 | 位置 |
|---|---|---|
| `manager.with_event_defaults(run_id=…, node_id=…, irreversible=…)` | 返回一个 `CallbackView`；值为 `None` 的参数不计入默认值 | `emerge/callbacks/manager.py:85-88,124` |
| 通过视图发事件 | 只填事件里为 `None` 的 `run_id` / `node_id` / `irreversible`。用 `dataclasses.replace` 生成新对象，原事件不变；没有需要填的字段时原样转发 | `emerge/callbacks/manager.py:139-141` |
| 事件本身已带值 | 保留，不被默认值覆盖（实测） | `emerge/callbacks/manager.py:140` |
| 在视图上再调用 `with_event_defaults` | 父视图的默认值加上新给的非 `None` 值，新值优先。传 `None` 不能清掉继承来的默认值（实测） | `emerge/callbacks/manager.py:132-137` |
| 视图的 `emit` / `emit_nowait` | 都转给 manager 的 `emit_nowait` | `emerge/callbacks/manager.py:126-130` |
| `instance_id`、`content_time` | 视图不填这两个字段 | `emerge/callbacks/manager.py:135` |
| `EventEmitter` 协议 | 包含 `emit`、`emit_nowait`、`with_event_defaults` 三个方法。实现它的有：manager、视图、主 Agent 的 `_RunEvents`（`emerge/agent/agent.py:144-160`，每个事件的 `run_id` 都改成当前运行）、节点进程里的 `_BusEmitter`（`emerge/runtime/context.py:103-117`，把事件经管道发给 bus，由 bus 填写各字段） | `emerge/callbacks/manager.py:15-22` |

### `JsonlTracer`（`tracers.py`）

把每个事件写成一行 JSON，追加到 `<runs_dir>/<run_id>/trace.jsonl`。由 `RuntimeService` 创建（`emerge/runtime/service.py:120`）。

| 情况 | 处理 | 位置 |
|---|---|---|
| 收到任意事件 | 用 `json.dumps(event.to_dict(), ensure_ascii=False)` 序列化，再经 `asyncio.to_thread` 写入。同一个 handler 的事件是串行处理的，所以写文件不会并发 | `emerge/callbacks/tracers.py:20-22` |
| `run_id` 为 `None` 或空串 | 写到 `<runs_dir>/_session/trace.jsonl`（实测） | `emerge/callbacks/tracers.py:22` |
| 某个 key 第一次出现 | 先建目录（包括父目录），再以追加模式 `"a"`、UTF-8 编码打开文件，并缓存句柄 | `emerge/callbacks/tracers.py:28-32` |
| 每写一行 | `write` 后立即 `flush`（不做 fsync） | `emerge/callbacks/tracers.py:33-34` |
| 事件是 `run.finished` | 写完这一行后，关闭该 key 的文件并移出缓存 | `emerge/callbacks/tracers.py:35-36` |
| `run.finished` 之后同一个 run 又来了事件 | 以追加模式重新打开文件（实测） | `emerge/callbacks/tracers.py:28-32` |
| `run.finished` 的 `run_id` 为 `None` | 被关闭的是 `_session` 文件 | `emerge/callbacks/tracers.py:22,35-36` |
| `payload` 不能序列化成 JSON（例如含 `Path`） | 抛 `TypeError`，由 worker 记录；这一条不写入，后续事件照常写（实测） | `emerge/callbacks/tracers.py:21`；`emerge/callbacks/manager.py:38-39` |
| 打开或写入失败（权限不足、磁盘满） | 异常同样由 worker 记录；如果是打开失败，下一个事件会再尝试打开 | `emerge/callbacks/tracers.py:28-33` |
| `close()`（handler 被移除或 manager 关闭时） | 在线程里关闭所有句柄 | `emerge/callbacks/tracers.py:24-25,38-41` |
| 读回 | 每一行都能用 `Event.from_dict(json.loads(line))` 还原（实测） | `emerge/events.py:54-56` |

---

## 工作区模板与打包：`emerge/workspace_template/`、`pyproject.toml`、`README.md`

模板随包一起分发，`init_workspace()` 把 `_TEMPLATE_FILES` 列出的 8 个文件复制到运行时工作区。

| 文件 | 内容 |
|---|---|
| `config.json` | 两家 providers 都已配好，`api_key` 是占位串 `your-openai-key` / `your-anthropic-key`。占位串不为空，所以 `create_provider` 不会报 “key 为空”，真实请求会返回 401。`model` 为 openai / responses / `gpt-6.1-sol`，`limit` 为 1050000 / 128000；options：temperature 0.0、max_output_tokens 128000（等于上限，可以通过）、reasoning_effort `xhigh`、speed `standard`。agent：max_context_tokens 1000000、compact_threshold 0.8、max_retries 3、`limits` 为 `{}`。runtime：stop_timeout 30、port_buffer 64。`nodes` 为 `[]`。没有写 `timeout`，取默认值 600000 ms |
| `AGENTS.md`、`USER.md`、`memory/MEMORY.md` | 各一两行，说明文件用途 |
| `code/.gitkeep`、`runs/.gitkeep` | 占位文件，保证目录存在 |
| `skills/sense-before-act.md` | front matter 含 `name`、`description`。正文：执行不可逆动作前，先在上游放一个感知节点确认前提，前提不成立就改图，不在一个节点里“先修正再执行” |
| `skills/recovery-after-irreversible-failure.md` | 不可逆节点没有达到效果时：先用感知节点确认现场 → 判断属于四种情况中的哪一种 → 向前修正；不自动重试，不设计回滚 |

| 项 | 内容 | 位置 |
|---|---|---|
| 构建 | hatchling；wheel 的 `packages = ["emerge"]`；`force-include` 把 `emerge/workspace_template` 和 `emerge/agent/prompts` 打进 wheel；sdist 包含 `emerge`、README 和 pyproject | `pyproject.toml:1-3,23-33` |
| Python | `>=3.12` | `pyproject.toml:10` |
| 依赖 | pydantic 2.x、openai 1.x（≥ 1.35）、anthropic 1.x（≥ 1.15）、prompt_toolkit 3、rich 13、aiosqlite。`httpx` / `httpx2` 没有声明，由 openai / anthropic 带进来，但 `models/retry.py` 直接 import 它们。`tiktoken` 是可选依赖，没有声明，当前环境也没有安装。当前环境的版本：openai 1.109.1、anthropic 1.15.0、httpx 0.28.1、httpx2 2.13.1、pydantic 2.14.1 | `pyproject.toml:11-18`；`emerge/models/retry.py:10-11` |
| 入口 | `emerge = "emerge.cli:main"`。`emerge/cli.py` 和 `emerge/__main__.py` 还不存在（阶段 9），所以 README 里写的 `emerge` / `python -m emerge` 目前不能运行 | `pyproject.toml:20-21`；`README.md:13-19` |
| `.gitignore` | 忽略本地的 `workspace/`（里面有 key）、`*.sqlite`、`*.db` | `.gitignore:9-12` |

---

## 模型层类型：`emerge/models/types.py`

三种 API 模式共用的消息、请求、响应和流事件类型。它们都是 `extra="forbid"` 的 Pydantic 模型（不是 frozen），可以 JSON 往返；Agent 历史和会话存储里存的都是 `Message`。`emerge.models` 导出这些类型以及 `ModelClient`、`ModelProvider`、`create_client`、`create_provider`、`RetryPolicy`、`RateLimiter`、`UsageTracker`、`count_text_tokens`、`estimate_tokens`（`emerge/models/__init__.py:1-49`）。

| 类型.字段 | 含义 | 约束/默认 | 位置 |
|---|---|---|---|
| `TextPart.text` | 文字 | 必填 | `emerge/models/types.py:15-17` |
| `ImagePart.path` | 工作区相对路径或绝对路径；发请求时才读文件并做 base64 | 必填 | `emerge/models/types.py:20-23` |
| `ImagePart.detail` | `auto` / `low` / `high` | 默认 `None`。OpenAI 两种模式按 `auto` 发送；Anthropic 不使用这个字段 | `emerge/models/types.py:24` |
| `ReasoningPart.text` | 模型思考内容的可读摘要；服务端没给时为空串 | 默认 `""`；只允许出现在 assistant 消息里；从不发给 API | `emerge/models/types.py:27-38` |
| `Part` | 以上三种，按 `type` 区分 | — | `emerge/models/types.py:41` |
| `ToolCall.id` / `name` | 调用 id、工具名 | 必填 | `emerge/models/types.py:47-49` |
| `ToolCall.arguments` | 参数 dict；模型给的参数不是 JSON 对象时，存为 `{"_raw": 原文}` | 默认 `{}` | `emerge/models/types.py:50-51` |
| `ToolSpec` | `name`、`description`（默认 `""`）、`parameters`（JSON Schema） | `parameters` 默认 `{"type": "object", "properties": {}}` | `emerge/models/types.py:54-57` |
| `NativeItem` | `provider`（任意字符串）、`api_mode`（三种之一）、`data`（原样保存的 dict） | 不解析，不改写 | `emerge/models/types.py:60-65` |
| `Message.role` | `system` / `user` / `assistant` / `tool` | 必填 | `emerge/models/types.py:69` |
| `Message.content` | Part 列表，按模型给出的先后顺序排列 | 默认 `[]` | `emerge/models/types.py:70` |
| `Message.tool_calls` / `tool_call_id` | assistant 发起的调用 / tool 消息对应的调用 id | 默认 `[]` / `None` | `emerge/models/types.py:71-72` |
| `Message.native_items` | 产生这条 assistant 消息的原生条目。下一次请求的 provider 和 api_mode 都与它相同时，原样送回它们，代替按 content 重建 | 默认 `[]` | `emerge/models/types.py:74` |
| `Usage` | `input_tokens` / `output_tokens` / `total_tokens`（默认 0）、`cached_tokens` / `reasoning_tokens`（默认 `None`）、`speed`（服务端报告的档位原值） | — | `emerge/models/types.py:112-119` |
| `ModelRequest.messages` / `tools` | 消息、工具 | `tools` 默认 `[]` | `emerge/models/types.py:136-137` |
| `ModelRequest.native_items` | 原样追加在转换后的消息末尾；不按 provider / api_mode 过滤。目前仓库里没有调用方设置它 | 默认 `[]` | `emerge/models/types.py:138-139` |
| `ModelRequest.tool_choice` | `auto` / `none` / `required` | 默认 `None`（不发送）；主 Agent 回答节点提问时用 `none`（`emerge/agent/agent.py:489`） | `emerge/models/types.py:140` |
| `ModelRequest.temperature` / `max_output_tokens` / `reasoning_effort` / `speed` | 按请求覆盖 `model.options` | 默认 `None`，表示用配置值；请求级的数值不做范围校验 | `emerge/models/types.py:141-145` |
| `ModelRequest.extra` | 原样并入 SDK 调用参数；主 Agent 对 openai 用它传 `prompt_cache_key`（`emerge/agent/agent.py:140-141,311`） | 默认 `{}` | `emerge/models/types.py:146-147` |
| `ModelResponse` | `message`、`native_items`（与 `message.native_items` 内容相同）、`response_id`、`usage`、`finish_reason` | `finish_reason` 默认 `stop`，取值为 `stop` / `tool_calls` / `length` / `content_filter` / `other` | `emerge/models/types.py:150-158` |
| `TextDelta` / `ReasoningDelta` | `part`（这个片段属于 `content` 里的第几项，≥ 0）、`text`。同一个 `part` 的片段拼起来，就是那一项的最终文字 | `part` ≥ 0 | `emerge/models/types.py:165-174` |
| `ToolCallDelta` | `index`（第几个工具调用）、`id` / `name`（该调用第一次出现时给出）、`arguments`（参数文本片段） | `id` / `name` 默认 `None`，`arguments` 默认 `""` | `emerge/models/types.py:177-182` |
| `StreamDone` | 携带完整的 `ModelResponse`，是流的最后一个事件 | — | `emerge/models/types.py:185-187` |
| `StreamEvent` | 以上四种，按 `type`（`text_delta` / `reasoning_delta` / `tool_call_delta` / `done`）区分 | — | `emerge/models/types.py:190` |

| 情况 | 处理 | 位置 |
|---|---|---|
| 非 assistant 消息里含有 `ReasoningPart` | 校验失败，抛 `ValidationError`（`a user message cannot hold reasoning`） | `emerge/models/types.py:76-80` |
| dict 里有未知的键（例如读到旧数据） | `ValidationError`（`extra_forbidden`） | `emerge/models/types.py:11-12` |
| `Message.text` | 把所有 `TextPart` 直接拼接，不加分隔符 | `emerge/models/types.py:82-84` |
| `Message.reasoning` | 把非空的 `ReasoningPart` 用 `"\n\n"` 连起来，空的跳过 | `emerge/models/types.py:86-89` |
| `Message.images` | 所有 `ImagePart` | `emerge/models/types.py:91-93` |
| `Message.system(text)` | 生成一个 `TextPart`，空串也放进去 | `emerge/models/types.py:95-97` |
| `Message.user(text, images, detail)` | `text` 非空才放 `TextPart`；每张图一个 `ImagePart`，共用同一个 `detail`；两样都没有时 `content` 为 `[]` | `emerge/models/types.py:99-103` |
| `Message.tool(id, text, images)` | 总会放一个 `TextPart`（空串也放），再加上 `ImagePart`（`detail` 为 `None`） | `emerge/models/types.py:105-109` |
| `Usage + Usage` | 三个计数相加。`cached_tokens` / `reasoning_tokens`：两边都是 `None` 时结果为 `None`，否则把 `None` 当 0 相加。`speed` 不保留，结果为 `None`（实测） | `emerge/models/types.py:121-132` |

---

## Provider 基类与“流式为主、自动降级”：`emerge/models/base.py`

`ModelProvider` 表示“一家供应商的一种 API 模式”。基类实现 `stream()`、`generate()`、降级逻辑和启动探测；子类只实现 `_stream_once`、`_generate_once`、`_refuses_streaming`、`probe_api_compatibility`（一般就是调用 `_probe_streaming_first(self._ping)`）和 `aclose`。仓库里只有 `ModelClient` 调用 `provider.stream()`；`ModelProvider.generate()` 没有调用方。

| 成员 | 含义 | 位置 |
|---|---|---|
| `provider` / `api_mode` | 类属性，标识这是哪一家的哪种模式 | `emerge/models/base.py:44-45` |
| `speeds` | 除 `standard` 外支持的档位，以及每个档位要并入请求的参数 | `emerge/models/base.py:47` |
| `streaming` | 类属性默认为 `True`；降级后在实例上改为 `False`，之后不会再改回来（没有提供恢复的接口） | `emerge/models/base.py:49,124` |
| `stream(request)` | 依次交出增量，最后交出一个 `StreamDone`（见下文） | `emerge/models/base.py:51-74` |
| `generate(request)` | 把 `stream()` 跑完，取最后一个 `StreamDone` 里的响应；如果没有，抛 `ModelError("<provider> / <api_mode> stream ended without a final response")` | `emerge/models/base.py:76-84` |

### 一次请求的路径

1. `streaming` 为 `True` 时，先发流式请求（`_stream_once`），每收到一个增量就立即交出（`emerge/models/base.py:58-64`）。
2. 如果流式请求在交出第一个事件之前就失败，并且属于“拒绝流式”（`EmptyStream`，或 provider 自己判定为拒绝），就在同一次调用里改发一次非流式请求（`emerge/models/base.py:65-69`）。
3. 非流式请求成功后调用 `_stop_streaming()`：把这个实例的 `streaming` 设为 `False`，并记一条 warning（`emerge/models/base.py:70,123-131`）。
4. 用 `events_of_response()` 把完整响应拆成片段交出：每个有文字的 content 项一个增量，每个工具调用一个 `ToolCallDelta`，最后一个 `StreamDone`（`emerge/models/base.py:73-74,139-150`）。
5. `streaming` 已为 `False` 时，直接发非流式请求，再按第 4 步拆成片段交出（`emerge/models/base.py:71-74`）。

| 情况 | 处理 | 位置 |
|---|---|---|
| 流式正常 | 原样交出 provider 产生的增量，最后交出 `StreamDone` | `emerge/models/base.py:58-64` |
| 返回 200，但一个 SSE 事件都没有（服务端没理会 `stream`，回了普通 JSON） | provider 抛 `EmptyStream`，触发降级。实测 OpenAI 两种模式和 Anthropic 遇到这种回包时，SDK 给出的事件数都是 0 | `emerge/models/base.py:120-121`；R:96-97、C:113-114、A:92-93 |
| 流式请求被拒绝（判定规则见“三种 API 模式对照”） | 降级 | `emerge/models/base.py:65-69` |
| 已经交出至少一个增量后出错 | 原样上抛，不降级 | `emerge/models/base.py:66-67` |
| 还没交出增量就出错，但不属于“拒绝流式”：401、429、5xx、与 stream 无关的 400、网络错误、读图失败的 `ModelError`、`response.failed` 等 | 原样上抛，不降级；是否重试由 `retry.py` 决定 | `emerge/models/base.py:66-67` |
| 降级后的非流式请求也失败 | 异常原样上抛，`streaming` 仍为 `True`。如果 `retry.py` 重试，下一次还会先试流式（实测） | `emerge/models/base.py:69-70` |
| 降级成功 | 这个 provider 实例从此只发非流式请求，并记 warning：`<provider> / <api_mode> refused to stream (<异常>); its requests go without streaming from now on` | `emerge/models/base.py:123-131` |
| 非流式响应体不是 JSON（SDK 把它当 `str` 交回，例如中转对非流式请求回 SSE） | `ModelError("<base_url> answered a request without streaming with something other than JSON (it starts '<前 80 字符>')")`（实测） | `emerge/models/base.py:153-159` |
| “降级”状态的作用范围 | 按 provider 实例记录：主 Agent 有一个；每个节点进程在第一次 `get_model_client()` 时各建一个。新进程重新从流式开始 | `emerge/agent/agent.py:191`；`emerge/runtime/context.py:365-371` |
| 并发请求共用一个 provider | 每个请求在开始时读取 `streaming`；某个请求降级，不影响已经在流式进行中的请求 | `emerge/models/base.py:58` |

### 启动探测：`_probe_streaming_first(ping)`

| 情况 | 处理 | 位置 |
|---|---|---|
| `streaming` 为 `True`，流式 ping 成功（读完全部事件） | 通过 | `emerge/models/base.py:107-110` |
| 流式 ping 被拒，或一个事件都没有 | 改发非流式 ping；成功后同样切换到非流式（实测） | `emerge/models/base.py:111-116` |
| 流式 ping 出了其他错误 | 原样上抛；provider 会把其中的 404 / 405 / 422 换成 `ModelAPIModeError` | `emerge/models/base.py:112-113` |
| `streaming` 已为 `False` | 只发非流式 ping | `emerge/models/base.py:117-118` |
| ping 的结果检查 | 流式 ping 只数事件个数，非流式 ping 不检查回包是否为 JSON。因此服务端对非流式请求回 SSE 文本时，探测也能通过（实测） | R:129-138、C:156-169、A:124-132 |

### 共用函数

| 函数 | 输入 → 结果 | 位置 |
|---|---|---|
| `mentions_stream(text)` | 正则 `\bstream`，忽略大小写：`stream`、`Streaming`、`stream_options`、`non-stream` → `True`；`upstream`、`include_stream` → `False`；空串或 `None` → `False`（实测） | `emerge/models/base.py:29,134-136` |
| `events_of_response(resp)` | 按 content 顺序处理：有文字的 `ReasoningPart` → 一个 `ReasoningDelta`；有文字的 `TextPart` → 一个 `TextDelta`；空的项不发增量，但 `part` 编号仍按它在 content 里的原位置算。接着每个工具调用一个 `ToolCallDelta`（`id`、`name`、`arguments`）：参数只有 `_raw` 一个键时用原文，否则用 `json.dumps(arguments)`，所以空白可能与服务端原文不同（实测）。最后是 `StreamDone` | `emerge/models/base.py:139-150` |
| `require_parsed(resp, base_url)` | `resp` 是 `str` 时抛上表那条 `ModelError` | `emerge/models/base.py:153-159` |
| `without_read_limit(timeout)` | 生成一个同类型的 Timeout：`read=None`，`connect` / `write` / `pool` 不变。所有非流式请求（包括 `_ping(False)`）都用它 | `emerge/models/base.py:162-169` |
| `speed_params(provider, speed)` | `standard` → `{}`；不在 `speeds` 里 → `ModelError("<provider> / <api_mode> does not support speed '<speed>'")`；否则返回该档位的参数 | `emerge/models/base.py:172-177` |
| `read_image(image, workspace)` | 先 `expanduser`。相对路径且给了 workspace 时拼到 workspace 下；workspace 为 `None` 时相对当前目录。读文件出现 `OSError` → `ModelError("cannot read image <path>: …")`（实测）。MIME 按扩展名猜，猜不出时用 `image/png`。不检查文件内容是不是图片，例如 `.txt` 会以 `text/plain` 发出 | `emerge/models/base.py:180-190` |
| `image_data_url(image, workspace)` | 返回 `data:<mime>;base64,<数据>` | `emerge/models/base.py:193-195` |
| `parse_arguments(raw)` | `None` 或空串 → `{}`；JSON 解析失败 → `{"_raw": raw}`；解析结果不是对象（数组、数字、字符串、`null`）→ `{"_raw": raw}`（实测） | `emerge/models/base.py:198-205` |

---

## 三种 API 模式对照：`emerge/models/providers/`

本节表格里的位置缩写：R = `emerge/models/providers/openai/responses.py`，C = `emerge/models/providers/openai/chat_completions.py`，A = `emerge/models/providers/anthropic/messages.py`。每个 provider 持有一个 SDK 客户端 `self.client`、`ModelConfig`、R 和 A 各自的 `provider_options`，以及用来解析相对图片路径的 `workspace`。消息转换放在 `asyncio.to_thread` 里执行，读图和 base64 编码不会阻塞事件循环（R:144、C:175、A:136）。

### 请求参数

| 项 | Responses（R） | Chat Completions（C） | Anthropic Messages（A） |
|---|---|---|---|
| SDK 调用 | `client.responses.create`（流式时加 `stream=True`） | `client.chat.completions.create`（流式时加 `stream=True` 和 `stream_options={"include_usage": True}`，C:63） | 流式用 `client.messages.stream(...)` 再调 `get_final_message()`；非流式用 `client.messages.create` |
| 固定参数 | `store: false`，`include: ["reasoning.encrypted_content"]`（R:147-148） | 无 | 顶层 `cache_control: {"type": "ephemeral"}`（A:142） |
| 最长输出 | `max_output_tokens`（R:149） | `max_completion_tokens`（C:176） | `max_tokens`（A:139） |
| 上一行的取值 | 都是 `request.max_output_tokens or options.max_output_tokens` | 同左 | 同左 |
| temperature | 顶层参数；请求值为 `None` 时用配置值（R:150） | 同左（C:177） | `thinking.type=adaptive` 时不发送，请求里给的 temperature 也被忽略；`disabled` 时放进 `extra_body.temperature`，因为 SDK 1.x 的方法签名里没有 temperature（A:148-156） |
| 推理强度（effort 取 `request.reasoning_effort or options.reasoning_effort`） | effort 非空时发 `reasoning: {effort, summary}`，`summary` 取 `reasoning_summary`，空串时为 `None`（R:152-154） | effort 非空时发 `reasoning_effort: effort`（C:179-181） | `adaptive` 时发 `thinking: {type: "adaptive", display}`，effort 非空时再加 `output_config: {effort}`；`disabled` 时两者都不发，effort 也被忽略（A:147-153） |
| tools | `{"type": "function", "name", "description", "parameters", "strict": false}`（R:155-159） | `{"type": "function", "function": {name, description, parameters}}`（C:182-186） | `{"name", "description", "input_schema"}`（A:157-160） |
| tool_choice | 原样发送字符串（R:160-161） | 原样发送字符串（C:187-188） | `required` → `{"type": "any"}`，`auto` / `none` → `{"type": "auto"}` / `{"type": "none"}`（A:161-162）。决策记录说较新的模型对 `any` 返回 400（HANDOFF.md:144），代码没有拦截 |
| speed 档位（`standard` 什么都不发） | `fast` → `service_tier: "fast"`；`ultrafast` → `service_tier: "ultrafast"`（R:50） | `fast` → `service_tier: "priority"`；不支持 `ultrafast`（C:54） | `fast` → `speed: "fast"`；不支持 `ultrafast`（A:54） |
| 缓存 | 模型层不发任何缓存参数。主 Agent 在 provider 为 openai 时，经 `extra` 加上 `prompt_cache_key`（`emerge/agent/agent.py:140-141,311`；HANDOFF.md:826） | 同左 | 每个请求都带 `cache_control`。不能经 `extra` 传 `prompt_cache_key`：SDK 不认这个参数，会报 `TypeError`（实测） |
| `extra` | 与 params 合并，`extra` 覆盖同名的键，包括 `model`、`store`、`include`（R:163） | 同左（C:190） | 先把算出来的 `extra_body` 与 `request.extra["extra_body"]` 合并（请求里的值优先），再与 params 合并（A:164-167） |
| `extra` 里有 SDK 不认的键 | `TypeError: … got an unexpected keyword argument`；与内部显式传入的参数重名（例如 `stream`）时报 `got multiple values`（实测） | 同左；`stream_options` 也会重名 | 同左；`temperature`、`prompt_cache_key` 也属于不认的键（实测） |

### 消息转换

| 消息 | Responses（R） | Chat Completions（C） | Anthropic Messages（A） |
|---|---|---|---|
| system | 作为 input 里的 `{"role": "system", "content": [input_text / input_image…]}`，保留原来的位置，图片也会发出（实测，R:191-192） | `{"role": "system", "content": 文字}`，保留原来的位置，只取文字，图片丢弃（C:220-221） | 所有 system 消息的文字用 `"\n\n"` 连成顶层 `system` 字符串，原来的位置信息丢失，图片丢弃；没有 system 消息时不发这个参数（A:144-145,188-189,209） |
| user | `content` 是 `input_text` / `input_image` 列表；没有任何内容时为 `[]`（R:191-192,198-201） | 有图片时 `content` 是 `text` / `image_url` 列表，没有图片时是字符串；`Message.user("")` 发成 `""`（C:222-226） | text / image 块列表；与前一条同角色消息合并；没有任何内容时为 `[]`（A:173-178,203-204） |
| 图片 | `{"type": "input_image", "image_url": <data URL>, "detail": detail 或 "auto"}`（R:203-204） | `{"type": "image_url", "image_url": {"url": <data URL>, "detail": detail 或 "auto"}}`（C:237-238） | `{"type": "image", "source": {"type": "base64", "media_type", "data"}}`，不带 detail（A:211-215） |
| 工具结果 | `{"type": "function_call_output", "call_id", "output": 文字}`（R:188-189） | `{"role": "tool", "tool_call_id", "content": 文字}`（C:217-218） | user 消息里的 `{"type": "tool_result", "tool_use_id", "content": 文字}` 块；连续的工具结果合进同一条 user 消息（A:200-201） |
| 工具结果里的图片 | 先暂存，等遇到下一条非 tool 消息或到达末尾时，作为一条只含图片的 user 消息插入（R:167-172,193-194） | 同左（C:194-199,227-228） | 作为 image 块追加到同一条 user 消息里，排在 tool_result 之后；下一条 user 消息的文字也会并进来（实测，A:180-186,205-206）。原因：实测中转会丢掉 `tool_result.content` 里的图片（HANDOFF.md:142） |
| assistant，有 provider 和 api_mode 都相同的 native item | 把所有 `data` 依次放进 input（R:175-178） | 放入 `data`，即整条原始 assistant 消息（C:202-205） | 把 `data`（content 块）作为一条 assistant 消息（A:191-194） |
| 上一行的补充 | 三家都完全不看这条消息的 content 和 tool_calls | 同左 | 同左 |
| assistant，没有匹配的 native item（需要重建） | 有文字时发 `{"role": "assistant", "content": 文字}`；每个工具调用发一个 `function_call` item，`arguments` 为 `json.dumps` 的结果（R:179-187） | `{"role": "assistant", "content": 文字或 None}`，有工具调用时加 `tool_calls`（C:206-216） | 有文字时一个 text 块，再加 `tool_use` 块；`input` 就是 `arguments` 这个 dict，含 `_raw` 时也照样发送（A:195-199） |
| 上一行的补充 | 三家都丢弃 `ReasoningPart`，不转成文字（HANDOFF.md:532） | 同左 | 同左 |
| assistant 既没有文字也没有工具调用（例如只剩别家的思考） | 什么都不发 | `{"role": "assistant", "content": null}` | `{"role": "assistant", "content": []}`（三种结果均为实测） |
| `request.native_items` | 追加在 input 末尾，不检查 provider / api_mode（R:195） | 追加在 messages 末尾，不检查（C:229） | 作为一条 assistant 消息追加，可能与前一条 assistant 合并（A:207-208） |
| 相邻的同角色消息 | 不合并 | 不合并 | 合并，因为 Anthropic 要求 user / assistant 交替（A:173-178） |

### 响应解析与流事件映射

| 项 | Responses（R） | Chat Completions（C） | Anthropic Messages（A） |
|---|---|---|---|
| 怎样算“收到过事件” | SDK 给出任何事件都算，包括 `response.created`（R:69-71） | 收到任何 chunk 都算，包括没有 choices 的用量 chunk（C:83-85） | SDK 给出任何事件都算（A:72-74）；`ping` 事件 SDK 不交出来，所以不算 |
| 一个事件都没有 | `EmptyStream`（R:96-97） | `EmptyStream`（C:113-114） | 在调用 `get_final_message()` 之前就抛 `EmptyStream`，避免触发 SDK 的断言错误（A:91-93） |
| content 片段怎样编号 | 按 `output_index` 分配：每个 reasoning 或 message item 占一个 part。通常在 `output_item.added` 时分配；如果增量先到，就在增量处用 `setdefault` 分配（R:65,73-75,80-85） | 在“思考”和“回答”之间每切换一次就开一个新 part，`content` 和 `refusal` 都算回答（实测，C:69,77-81） | 按 block index 分配：每个 thinking、redacted_thinking 或 text 块占一个 part（A:69,76-79） |
| 思考增量 | `reasoning_summary_text.delta` → `ReasoningDelta`；`reasoning_summary_part.added` 且 `summary_index > 0` 时，先发一个 `"\n\n"`（实测，R:79-83） | delta 里的非标准字段 `reasoning_content`（取自 `delta.model_extra`）→ `ReasoningDelta`（C:93-97） | `thinking_delta` 且文字非空 → `ReasoningDelta`；`signature_delta` 忽略（A:87-88） |
| 文字增量 | `output_text.delta`、`refusal.delta` → `TextDelta`（R:84-85） | `delta.content`、`delta.refusal` → `TextDelta`（C:99-102） | `text_delta` → `TextDelta`（A:85-86） |
| 工具调用增量 | function_call 的 `output_item.added` → `ToolCallDelta(index, id=call_id, name)`；`function_call_arguments.delta` → 参数片段（R:76-78,86-88） | 按 `tc.index` 累积 id、name、arguments，同时原样转发为 `ToolCallDelta`。所有 choice 的增量都并进同一条消息（请求没有设 `n`，正常只有一个 choice）（C:90,103-112） | `content_block_start`（tool_use）→ `ToolCallDelta(index, id, name)`；`input_json_delta` → 参数片段，只处理已登记的 tool_use 块（A:80-82,89-90） |
| 其他类型的 item / 块（例如服务端工具） | 不进 content，只保留在 native 里（R:44,213-228） | — | 不进 content，只保留在 native 里（A:224-241） |
| 正常结束 | `response.completed` 或 `response.incomplete` 给出最终响应（R:89-90） | 流读完就结束；`finish_reason` 取最后一个非空值，用量取带 `usage` 的那个 chunk（C:86-92） | `get_final_message()`（A:94） |
| 流中途报错 | `response.failed` → `ModelError("response failed: <message>")`；`error` 事件 → `ModelError("response stream error: <message>")`（实测，R:91-95） | 带 `error` 键的 SSE 由 SDK 抛 `openai.APIError`（实测） | `event: error` 由 SDK 抛 `APIStatusError`，状态码用的是这次流响应的 200（实测） |
| 流结束了，但没有收到完成标志 | `ModelError("response stream ended without a final response")`；即使已经交出过增量也一样（实测，R:98-99） | 当作正常完成：`finish_reason` 按 `stop` 处理；如果没有用量 chunk，用量全部为 0（实测，C:268,272-274） | 看 SDK 的快照：有内容就照常处理，`stop_reason` 缺失时按 `end_turn`，即 `stop`；既没有内容也没有 `stop_reason` 时抛 `ModelError("anthropic response has no content")`（实测，A:221-222,247） |
| 最终的 content | 每个 reasoning item 一个 `ReasoningPart`，多段摘要用 `"\n\n"` 连接，没有摘要时为空；每个 message item 一个 `TextPart`，output_text 与 refusal 直接拼接；每个 function_call 一个 `ToolCall`（R:216-228） | 流式时按到达顺序排列各 part。非流式时先放 `ReasoningPart(reasoning_content)`，再放 `TextPart(content 与 refusal 拼接)`，哪一项为空就省略（实测，C:122-125,256-260） | thinking → `ReasoningPart(thinking)`；redacted_thinking → `ReasoningPart("")`；text → `TextPart`；tool_use → `ToolCall`，`input` 不是 dict 时存为 `{"_raw": str(input)}`（A:228-241） |
| `native_items` | 每个 output item 的 `model_dump(mode="json", exclude_unset=True)`，所有类型都存，包括 reasoning（含 `encrypted_content`）、message、function_call（R:215） | 只有一条。流式时由增量拼成 `{"role": "assistant", "content": 文字或 None}`，再按需加上 `refusal`、`reasoning_content`、`tool_calls`；非流式时取 `choice.message.model_dump(…)`（实测，C:115-121,134,261） | 每个 content 块的 `model_dump(…)`；thinking 块带 `signature`（A:224-227） |
| 推理续接 | 下一轮把 reasoning item 连同 `encrypted_content` 原样放回 input；服务端不保存任何内容（`store: false`），不用 `previous_response_id`（HANDOFF.md:597-602） | 原样送回原始 assistant 消息，包括兼容服务给出的 `reasoning_content`；这种模式没有加密推理 | thinking 块带着 `signature`、redacted 块带着 `data` 原样送回 |
| finish_reason | `status` 为 `incomplete` 时：原因是 `max_output_tokens` → `length`，`content_filter` → `content_filter`，其他 → `other`。否则有工具调用 → `tool_calls`，没有 → `stop`；`status` 为 failed / cancelled 时也是 `stop`（实测，R:239-243） | `stop` → stop；`length` → length；`tool_calls` / `function_call` → tool_calls；`content_filter` → content_filter；`None` → stop；其他 → other（C:41-47,268） | `end_turn` / `stop_sequence` → stop；`tool_use` → tool_calls；`max_tokens` → length；`refusal` → content_filter；`None` → stop；其他（例如 `pause_turn`）→ other（A:42-48,247） |
| `response_id` | `response.id` | 第一个 chunk 的 `id`；非流式时取 `completion.id` | `message.id` |
| 非流式的回包形状 | — | 取 `completion.choices[0]`；`choices` 为空时 `IndexError` 原样抛出（实测，C:133） | — |

### 用量（`Usage`）映射

| 字段 | Responses（R:246-258） | Chat Completions（C:272-284） | Anthropic Messages（A:251-263） |
|---|---|---|---|
| `input_tokens` | `usage.input_tokens`，缺失时为 0 | `prompt_tokens`，缺失时为 0 | `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`。Anthropic 的 `input_tokens` 不含缓存读写，这里补上，口径与 OpenAI 一致（HANDOFF.md:144） |
| `output_tokens` | `output_tokens` | `completion_tokens` | `output_tokens` |
| `total_tokens` | `total_tokens` | `total_tokens` | 自己计算 input + output |
| `cached_tokens` | `input_tokens_details.cached_tokens`，缺失时为 `None` | `prompt_tokens_details.cached_tokens`，缺失时为 `None` | `cache_read_input_tokens`，缺失时为 0（总是整数） |
| `reasoning_tokens` | `output_tokens_details.reasoning_tokens` | `completion_tokens_details.reasoning_tokens` | 不填（`None`） |
| `speed` | `response.service_tier` 的原值（例如 `default`） | 流式时取 chunk 上最后一个非空的 `service_tier`，非流式时取 `completion.service_tier` | `usage.speed` |
| 响应里没有 usage | `Usage(speed=service_tier)`，各计数为 0 | 同左 | — |

### 拒绝流式的判定与启动探测

| 项 | Responses（R） | Chat Completions（C） | Anthropic Messages（A） |
|---|---|---|---|
| 怎样算“拒绝流式” | `BadRequestError` / `UnprocessableEntityError`（400 / 422），并且 `param == "stream"` 或报错信息里出现 stream 这个词（R:109-114） | 同左，`param` 还认 `stream_options`；所以只是不接受 `stream_options` 的兼容服务，也会被永久切到非流式（实测，C:137-141） | 400 / 422，并且报错信息里出现 stream 这个词（A:105-109） |
| 不算拒绝的错误 | 401、403、429、5xx、与 stream 无关的 400，以及所有非状态错误（HANDOFF.md:518） | 同左 | 同左 |
| 探测请求 | `model`，`input="ping"`，`max_output_tokens=16`，`store=False`（R:129-138） | `model`，`messages=[ping]`，`max_completion_tokens=16`；流式探测不带 `stream_options`（C:156-169） | `model`，`max_tokens=16`，`messages=[ping]`（A:124-132） |
| 探测遇到 404、422（与 stream 无关）或 405 | `ModelAPIModeError("<base_url> does not support the responses API: …")`（实测，R:119-123） | 同样处理，信息里写 `chat completions API`（C:146-150） | 同样处理，信息里写 `anthropic messages API`（A:114-118） |
| 探测遇到其他状态错误或连接错误 | 原样上抛，交给 `ModelClient.probe_api_compatibility` 里的 `with_retry` 分类：可重试的重试，不可重试的包装成 `ModelError`（实测） | 同左 | 同左 |

### 不支持的搭配

| 情况 | 结果 | 位置 |
|---|---|---|
| `(openai, anthropic_messages)`、`(anthropic, responses)`、`(anthropic, chat_completions)` | `ModelAPIModeError("unsupported provider / api_mode: <provider> / <api_mode>")`。这项检查排在 speed 和 key 检查之前（实测） | `emerge/models/factory.py:31-33` |
| 配置的 `speed` 不被支持（C、A 的 `ultrafast`） | `ConfigError("model.options.speed '<speed>' is not supported by …")` | `emerge/models/factory.py:35-37` |
| 请求级 `speed` 不被支持 | `ModelError`（实测） | `emerge/models/base.py:175-176` |

---

## 工厂与超时：`emerge/models/factory.py`

`create_client(config, callbacks, limiter)` 把配置组装成 `ModelClient`。主 Agent（`emerge/agent/agent.py:191`）和节点进程的 `ctx.get_model_client()`（`emerge/runtime/context.py:365-371`，第一次调用时才创建，import 也推迟到这时）各建一个。

### `create_provider(model, provider, workspace)` 的步骤

1. 检查搭配是否支持，不支持就抛 `ModelAPIModeError`（`emerge/models/factory.py:31-33`）。
2. 选实现类：anthropic 用 `AnthropicMessagesProvider`，否则从 `_OPENAI` 里按 api_mode 选（`emerge/models/factory.py:21-24,34`）。
3. 检查 speed：不是 `standard` 且不在 `cls.speeds` 里就抛 `ConfigError`（`emerge/models/factory.py:35-37`）。
4. 检查 `api_key`：为空就抛 `ConfigError`（`emerge/models/factory.py:38-39`）。
5. 算超时：`silence = timeout / 1000` 秒；`connect = 30`，`read`、`write`、`pool` 都等于 `silence`（`emerge/models/factory.py:19,42-43`）。
6. anthropic：先去掉 `base_url` 末尾的 `/`，再去掉一个 `/v1` 后缀（区分大小写，`/V1`、`/v1beta` 保持不变，实测）；然后建 `AsyncAnthropic(timeout=anthropic.Timeout(...), max_retries=0)`，交给 `AnthropicMessagesProvider(client, model, provider.options, workspace)`（`emerge/models/factory.py:44-49`）。
7. openai：`base_url` 原样使用（需要自己带上 `/v1`）；建 `AsyncOpenAI(timeout=openai.Timeout(...), max_retries=0)`。Responses 会收到 `provider.options`，Chat Completions 不收任何 options（`emerge/models/factory.py:50-54`）。

| 情况 | 处理 | 位置 |
|---|---|---|
| `create_client` 时 `providers.<model.provider>` 为 `None` | `ConfigError("model.provider '<name>' has no entry in providers")`（实测） | `emerge/models/factory.py:60-62` |
| `create_client` 收到的 config 不是由 `load_config` 得到的 | 读 `config.workspace` 时抛 `AttributeError`（实测） | `emerge/models/factory.py:64` |
| 重试策略 | `RetryPolicy(max_retries=config.agent.max_retries)`，其余用默认值（`base_delay` 1 s、`max_delay` 30 s） | `emerge/models/factory.py:66` |
| limiter | 默认是 `None`，`ModelClient` 会自己建一个不限并发的；仓库里没有调用方传入 limiter | `emerge/models/factory.py:67`；`emerge/models/client.py:27` |
| SDK 自带的重试 | 关闭（`max_retries=0`），只由 `retry.py` 负责重试 | `emerge/models/factory.py:48,50` |
| 两个 SDK 的 Timeout 类 | 各用各的：Anthropic SDK 1.15 用它自带的 httpx2，只认 `anthropic.Timeout`（它就是 `httpx2.Timeout`，实测）；OpenAI 用 `openai.Timeout`（即 `httpx.Timeout`） | `emerge/models/factory.py:3-6,47-50` |

### 断线判断（按静默时长）

| 阶段 | 限制 | 位置 |
|---|---|---|
| 建立连接 | 30 秒（`CONNECT_TIMEOUT`）。建连不涉及模型思考，短一些不会误伤 | `emerge/models/factory.py:18-19` |
| 流式读取 | 连续 `timeout / 1000` 秒一个字节都没收到，就抛 `httpx.ReadTimeout` / `httpx2.ReadTimeout`。SSE 注释心跳和 Anthropic 的 `ping` 都算字节，所以模型思考多久都可以。实测：上限 0.8 s，每 0.3 s 一次心跳、持续 1.8 s，请求成功；完全不发数据时，0.8 s 后超时 | `emerge/models/factory.py:40-43` |
| 写入、等待连接池 | 同样是 `timeout / 1000` 秒 | `emerge/models/factory.py:43` |
| 非流式读取（降级后的请求、`_ping(False)`） | 不限（`read=None`）；connect、write、pool 照旧受限。实测：上限 0.8 s，非流式请求 1.6 s 后才返回，仍然成功 | `emerge/models/base.py:162-169`；R:104、C:130、A:100 |
| 一次调用的总时长 | 不限：`RetryPolicy` 没有自己的计时（`request_timeout` 已删除，HANDOFF.md:542） | `emerge/models/retry.py:23-24` |
| 超时之后 | 还没交出增量 → 按可重试的网络错误处理；已经交出过 → `ModelError`（见“重试”一节） | `emerge/models/retry.py:110-112` |
| 已知的边界情况 | 中转一直发心跳，但它背后的模型卡住了 → 会一直等；降级成非流式后连接悄悄断了 → 只能等代理或系统关闭连接（HANDOFF.md:549-551） | — |

---

## 重试与限流：`emerge/models/retry.py`

`retry_stream` 包住 `provider.stream()`，`ModelClient` 的 `generate()` 和 `stream()` 都经过它；`with_retry` 包住一个协程，供 `probe_api_compatibility` 使用。两者都只针对单次模型请求，不涉及节点重启。

### 错误分类

| 错误 | 典型来源 | 是否重试 | 等待 | 位置 |
|---|---|---|---|---|
| `openai.APIConnectionError` / `anthropic.APIConnectionError`（包括子类 `APITimeoutError`） | 建连失败；SDK 在发请求阶段包装的超时 | 是 | 退避 | `emerge/models/retry.py:61-62` |
| `httpx.TransportError` / `httpx2.TransportError`（`ReadTimeout`、`RemoteProtocolError` 等） | 读流过程中静默超时或断线，SDK 不包装、直接抛出（HANDOFF.md:553） | 是（实测） | 退避 | `emerge/models/retry.py:55,61-62` |
| 状态码为 408 / 409 / 429 / ≥ 500 的 `APIStatusError`（包括 Anthropic 的 529） | HTTP 状态错误 | 是（实测 408、409、500、503、529） | 有数字形式的 `retry-after` 就用它，否则退避 | `emerge/models/retry.py:63-64` |
| 其他状态码的 `APIStatusError`（400、401、403、404、413、422…） | — | 否（实测 401） | — | `emerge/models/retry.py:63-65` |
| Anthropic 流中途的 `event: error`（例如 `overloaded_error`） | SDK 用流响应的状态码 200 构造 `APIStatusError` | 否：200 不在可重试的状态码里（实测） | — | `emerge/models/retry.py:64` |
| OpenAI 流中途带 `error` 键的 SSE | SDK 抛 `openai.APIError`（不是状态错误） | 否（实测） | — | `emerge/models/retry.py:65` |
| 其他 SDK 异常（例如 `APIResponseValidationError`） | — | 否 | — | `emerge/models/retry.py:65` |
| `ModelError` 及其子类（provider 自己抛的：`response.failed`、流没有最终响应、非 JSON 回包、读图失败、请求级 speed 不支持、`ModelAPIModeError`） | — | 否；原样上抛，不再包装 | — | `emerge/models/retry.py:77-78,107-108` |
| 非 SDK 异常（`TypeError`、`IndexError`、`AssertionError`、`ValidationError`…） | — | 不捕获；原样上抛，不包装成 `ModelError` | — | `emerge/models/retry.py:79,109` |
| `CancelledError`、`GeneratorExit` | 被取消，或调用方提前停止读取 | 不捕获（它们是 `BaseException`）；`finally` 会关闭 SDK 流 | — | `emerge/models/retry.py:105-106` |
| 可重试的错误，但已经交出过事件（仅 `retry_stream`） | — | 否 → `ModelError`（实测） | — | `emerge/models/retry.py:110-111` |
| 可重试的错误，但 `attempt >= max_retries` | — | 否 → `ModelError("model request failed after N attempt(s): <描述>")`，`__cause__` 是原始异常 | — | `emerge/models/retry.py:80-81,110-111,140-141` |

`<描述>` 的格式：超时（httpx / httpx2 的 `TimeoutException`）写 `<类名>: no data from the service within the time limit`；状态错误写 `HTTP <状态码>: <message>`；其他写 `<类名>: <str(error)>`（`emerge/models/retry.py:132-137`）。

### 退避

| 情况 | 等待时长 | 位置 |
|---|---|---|
| 状态错误带有数字形式的 `retry-after` | `min(值, max_delay=30)` 秒（实测 `retry-after: 0.01`） | `emerge/models/retry.py:30-32,122-129` |
| `retry-after` 是 HTTP 日期、解析失败、没有这个头，或者错误不是状态错误 | 在 `0` 到 `min(30, 1 × 2^attempt)` 之间均匀随机取值：第 1 次重试前 0–1 s，第 2 次 0–2 s，第 3 次 0–4 s……最多 30 s | `emerge/models/retry.py:33,123-129` |
| `retry-after-ms` 头 | 不读取 | `emerge/models/retry.py:125` |
| 每次退避 | 记一条 warning：`model request failed (<描述>), retry i/N in X.Xs` | `emerge/models/retry.py:116-119` |
| 总尝试次数 | `max_retries + 1`。`agent.max_retries` 默认为 3，即最多 4 次；`max_retries=0` 时只试 1 次（实测：`max_retries=2` 时 500 出现 3 次后报错） | `emerge/models/retry.py:80,110`；`emerge/models/factory.py:66` |
| 更快档位遇到 429 | 同样按 `retry-after` 重试，不会自动降回 `standard`（HANDOFF.md:258） | — |

### `retry_stream` 的流程

1. 占用一个 limiter 名额，调用 `open_stream()` 得到 provider 的流（`emerge/models/retry.py:95-96`）。
2. 反复 `anext`：拿到事件就把 `yielded` 设为真并交出；遇到 `StopAsyncIteration` 就结束（`emerge/models/retry.py:98-104`）。
3. 无论成功还是失败，都在 `finally` 里 `aclose()` provider 的流（释放 HTTP 连接），然后释放名额（`emerge/models/retry.py:105-106`）。
4. 出错时按上面的表分类；需要重试就先退避，再回到第 1 步。每次重试都会重新构造请求、重新读图（`emerge/models/retry.py:107-113`）。

`with_retry` 的逻辑相同，只是包住的是一次 `await call()`（`emerge/models/retry.py:68-83`）。

### 限流：`RateLimiter`

| 情况 | 处理 | 位置 |
|---|---|---|
| `RateLimiter()`，或 `max_concurrency` 为 `None` / 0 | 不限并发 | `emerge/models/retry.py:39-40,44-46` |
| 设了上限 | 用 `asyncio.Semaphore`；只在同一进程内、共用同一个 limiter 对象的请求之间生效 | `emerge/models/retry.py:40,47-48` |
| 名额被占用多久 | `retry_stream` 从发请求一直占到流读完，调用方处理每个增量的时间也算在内，所以消费慢的调用方会一直占着名额（实测：上限为 1 时，第二个请求要等第一个流被读完才开始）。退避等待期间不占名额 | `emerge/models/retry.py:95-106` |
| 默认情况 | 每个 `ModelClient` 自己建一个不限并发的 limiter；仓库里没有调用方传入共享的 limiter | `emerge/models/client.py:27` |

---

## 客户端与事件：`emerge/models/client.py`

`ModelClient` 把 provider、重试、limiter 和用量统计组合起来，对外提供 `generate()`、`stream()`、`probe_api_compatibility()`、`estimate_tokens()` 和 `aclose()`。调用方是 `emerge/agent/context.py:195-205` 的 `call_model()`：`stream=False` 时调 `generate()`，否则调 `stream()`。

### `generate()` 与 `stream()` 的对比

| 项 | `generate()` | `stream()` |
|---|---|---|
| 实际发出的请求 | 与 `stream()` 完全相同：都走 `provider.stream()`，先试流式（`emerge/models/client.py:34`） | `emerge/models/client.py:42` |
| `model.started` 的 `stream` 字段 | `False`（只表示调用了哪个方法） | `True` |
| `model.token` | 照样实时发出 | 发出 |
| 返回什么 | 最终的 `ModelResponse` | 依次交出所有 `StreamEvent`，包括 `ToolCallDelta` 和 `StreamDone` |

### 事件的 payload

所有事件都带 `call_id`（uuid4 十六进制的前 12 位）、`provider`、`api_mode`、`model`（配置里的模型名，未传时为 `""`）（`emerge/models/client.py:73,90-91`）。事件本身的 `run_id` / `node_id` 都是 `None`，由注入的 emitter 补填：主 Agent 的 `_RunEvents` 写入当前运行（`emerge/agent/agent.py:153-154`）；节点进程的 `_BusEmitter` 经管道发给 bus，由 bus 填写 run、node、instance 和 irreversible，不盖 `content_time`（`emerge/runtime/bus.py:428-441`）。

| 事件 | 额外的 payload | 什么时候发 | 位置 |
|---|---|---|---|
| `model.started` | `stream`、`messages`（消息条数）、`tools`（工具个数） | 每次调用开始时发一次，重试不会再发 | `emerge/models/client.py:72-75` |
| `model.token` | `kind`（`text` / `reasoning`）、`part`、`text` | 每个 `TextDelta` / `ReasoningDelta` 都发；先发事件，再交给调用方 | `emerge/models/client.py:52-55` |
| `model.finished`（成功） | `response_id`、`finish_reason`、`tool_calls`（工具名列表）、`usage`（dict，含 `speed`）、`streamed`（此刻 `provider.streaming` 的值；降级那次为 `False`，实测） | 收到 `StreamDone` 时，在把它交给调用方之前 | `emerge/models/client.py:77-87` |
| `model.usage` | `usage`（这一次的） | 紧跟在成功的 `model.finished` 之后 | `emerge/models/client.py:88`；`emerge/models/usage.py:76-80` |
| `model.finished`（失败） | `error`（`ModelError` 的文本） | 捕获到 `ModelError` 时发出，然后重新抛出 | `emerge/models/client.py:59-61` |

### 情况表

| 情况 | 处理 | 位置 |
|---|---|---|
| `callbacks` 为 `None` | 不发任何事件；`UsageTracker` 照常累计 | `emerge/models/client.py:93-95`；`emerge/models/usage.py:79` |
| `ToolCallDelta` | 不发事件，只交给 `stream()` 的调用方 | `emerge/models/client.py:52-58` |
| 发生了重试 | `model.started` 只有一次；失败的那几次尝试不发事件，只有 `retry.py` 的 warning 日志（实测） | `emerge/models/client.py:49-51` |
| 已经交出过增量后失败 | 已发出的 `model.token` 不会撤回；接着发 `model.finished(error)`（实测） | `emerge/models/client.py:46-48,59-61` |
| 抛出的不是 `ModelError`（例如 `IndexError`、`TypeError`） | 原样上抛，不发 `model.finished`（实测：只有 `model.started`） | `emerge/models/client.py:59` |
| `stream()` 的调用方提前停止（`break` 或被取消） | 不发 `model.finished`，也不发 `model.usage`（实测） | `emerge/models/client.py:41-43` |
| `generate()` 读完了流却没有 `StreamDone` | `ModelError("model stream ended without a response")`，这时不发 `model.finished`。内置的三个 provider 不会出现这种情况 | `emerge/models/client.py:37-38` |
| `probe_api_compatibility()` | 用 `with_retry` 包住 `provider.probe_api_compatibility`；不发事件；仓库里还没有调用方 | `emerge/models/client.py:63-64` |
| `estimate_tokens(request)` | 转给 `usage.estimate_tokens`。仓库里没有调用方：`agent/context.py` 直接用 `usage.estimate_tokens` | `emerge/models/client.py:66-67` |
| `aclose()` | 关闭 provider 的 SDK 客户端。节点进程退出时会调用（`emerge/runtime/context.py:375`）；主 Agent 的客户端在仓库里没有地方关闭 | `emerge/models/client.py:69-70` |
| 并发调用同一个 client | 没有加锁；各次调用共用 provider（包括 `streaming` 标志）和 limiter | — |

---

## 用量与 token 估算：`emerge/models/usage.py`

请求前用 `estimate_tokens` 粗估 token 数，`agent/context.py` 用它判断是否需要压缩（`emerge/agent/context.py:144-146,166`）。`UsageTracker` 记录每次响应的用量，并发出 `model.usage` 事件。

| 函数/情况 | 估算方式 | 位置 |
|---|---|---|
| `_encoding()` | 尝试 `tiktoken.get_encoding("o200k_base")`。出现任何异常（没安装，或下载编码文件失败）都返回 `None`。结果由 `@cache` 缓存，`None` 也会被缓存，所以本进程之后不再尝试。当前环境没有安装 tiktoken | `emerge/models/usage.py:17-24` |
| `count_text_tokens("")` | 0 | `emerge/models/usage.py:28-29` |
| 有 tiktoken | `len(encode(text, disallowed_special=()))`，特殊记号按普通文字计 | `emerge/models/usage.py:30-32` |
| 没有 tiktoken | `ASCII 字符数 // 4 + 非 ASCII 字符数 + 1`。例如 `"hello world"` 算 3，`"中文字符"` 算 5（实测） | `emerge/models/usage.py:34-35` |
| `_native_tokens(item)` | 复制 `data`，去掉顶层的 `encrypted_content`；剩下的部分 `json.dumps` 后计 token，再加上 `len(encrypted_content) // 4`。Anthropic 的 `signature`、redacted 块的 `data` 不去掉，按普通文字计 | `emerge/models/usage.py:38-42` |
| 消息带有 `native_items` | `4 + 各 native 的估算之和`，不看 content 和 tool_calls；也不管这些 native 这次会不会真正发出（换了 provider 时不会发） | `emerge/models/usage.py:46-47` |
| 消息没有 `native_items` | `4 + 各 TextPart 的估算 + 图片 + 每个工具调用（名字 + json(arguments)）`。图片 `low` 算 85，`auto`、`high`、`None` 都算 765；`ReasoningPart` 和 `tool_call_id` 不计 | `emerge/models/usage.py:13-14,48-56` |
| `estimate_tokens(messages, tools, native_items)` | 各消息的估算，加上每个 `ToolSpec` 的 `json.dumps(model_dump())`，再加上 `request.native_items` | `emerge/models/usage.py:59-65` |
| `UsageTracker.record(usage, **payload)` | `total = total + usage`（`speed` 被丢掉）；`requests += 1`；有 callbacks 时发 `model.usage`，payload 是调用方传入的字段加上 `usage`（本次的 `model_dump()`，含 `speed`） | `emerge/models/usage.py:76-80` |
| `UsageTracker.total` / `requests` | 只在本进程累计，仓库里没有代码读取。总量由主进程汇总事件得到；主 Agent 另外在 `emerge/agent/agent.py:556-559` 自己累加 | `emerge/models/usage.py:73-74` |

---

## 疑点（待核实）

| # | 疑点 | 证据 |
|---|---|---|
| 1 | Anthropic 在流中途发来的错误不会重试。`overloaded_error` 这类以 SSE `event: error` 形式到达的错误，SDK 按流响应的状态码 200 构造 `APIStatusError`；`is_retryable` 只认 408 / 409 / 429 / ≥ 500，所以即使一个增量都还没交出，也只试 1 次就报 `ModelError`。决策记录写的是 5xx / 429 可重试（HANDOFF.md:132） | `emerge/models/retry.py:63-64`；用模拟流实测，报错为 `failed after 1 attempt(s): HTTP 200: {… overloaded_error …}` |
| 2 | 非 `ModelError` 的异常会绕过统一处理：不发 `model.finished`，不包装，不重试。例如 Chat 非流式回包的 `choices` 为空时抛 `IndexError`；`extra` 里有 SDK 不认的键时抛 `TypeError`；回包缺字段时可能抛 `AttributeError` 或 `ValidationError`。HANDOFF.md:498 记录过同类现象，当时只修了“回包是字符串”这一种情况 | `emerge/models/client.py:59`；`emerge/models/providers/openai/chat_completions.py:133`；实测 `IndexError` 时只有 `model.started` 事件 |
| 3 | 空 `api_key` 的处理，计划与代码不一致：phase-02-models.md:89 写的是“为空字符串时不传给 SDK（本地模型）”，代码则抛 `ConfigError`（与 HANDOFF.md:130 一致）。按计划写法配置本地模型会被拒绝 | `emerge/models/factory.py:38-39` |
| 4 | `Retry-After` 没有完全遵守：等待时长被截到 30 s；HTTP 日期格式不解析，退回指数退避；`retry-after-ms` 不读。决策记录写的是“遵守 Retry-After”（HANDOFF.md:132） | `emerge/models/retry.py:30-33,122-129` |
| 5 | 无法按请求关闭推理：`request.reasoning_effort=""` 会因为 `or` 回落到配置值。这与“为 None 时取配置”的说明不符（`emerge/models/types.py:141`、HANDOFF.md:136）。`max_output_tokens` 也用了 `or`，不过 0 本身就不合法 | R:152、C:179、A:151（实测：请求给 `""`、配置为 `medium` 时仍发 `medium`） |
| 6 | 流在没有完成标志时结束，三家处理不同：Responses 报 `ModelError`，即使一个增量都没交出也不重试；Chat 当作正常的 `stop`，内容被截断也察觉不到；Anthropic 有内容就照常返回 | R:98-99；C:268；A:221-222,247（均有实测） |
| 7 | Responses 的非流式路径不检查 `status`：`failed` / `cancelled` 的响应会被当成 `stop` 正常返回，而流式路径遇到 `response.failed` 会报错。尚未确认真实服务会不会对非流式请求返回 200 + `status: failed` | R:239-243 对比 R:91-93（模拟回包实测：finish_reason 为 `stop`，content 为空） |
| 8 | 换了 provider 之后，只含思考的 assistant 消息会被重建成空消息：Chat 发出 `{"role": "assistant", "content": null}`，Anthropic 发出 `content: []`，两家 API 很可能拒绝；Responses 则直接跳过这条消息。未用真实 API 验证 | C:206-216；A:195-199（转换结果实测） |
| 9 | `model.finished.streamed` 读的是 provider 此刻的标志，而不是这一次请求实际走的路径。同一个 client 上有并发请求、其中一个降级时，另一个已经流式完成的请求也会报 `streamed: false` | `emerge/models/client.py:86` |
| 10 | callbacks 的异常隔离只覆盖 `Exception`。handler 抛出 `CancelledError` 这类 `BaseException` 时，它的 worker 会退出，之后的事件在队列里堆积，`close()` 不被调用，`aclose()` 还会把 `CancelledError` 抛给调用方（实测）。计划要求“不影响调用方”（phase-01-callbacks.md:36） | `emerge/callbacks/manager.py:36-39,97` |
| 11 | `load_config` 只把“文件缺失、JSON 错误、校验失败”转成 `ConfigError`。文件不是 UTF-8、`config.json` 是目录、工作区路径是文件、没有读权限时，抛出的是原始的 `UnicodeDecodeError` / `IsADirectoryError` / `NotADirectoryError` / `PermissionError`（前三种实测） | `emerge/config.py:227-232` |
| 12 | 文档过时：phase-02-models.md:134-201 是旧版“要求 / 完成标准”的重复段落，里面还有 `previous_response_id`、`api_key_env`、`request_timeout`。HANDOFF.md:130（“目前只有 openai”）、:131（`request_timeout`）、:143 和 :146（`model.options.thinking` 的旧位置、旧字段）没有标注已被后来的决策取代。phase-00、phase-01 里还有已被取代的写法：`env.action` / `on_env_action` 和 `RuntimeConfig.max_restarts` 已按 HANDOFF.md:213 删除；`RuntimeConfig.checkpoint` 已按 :261 删除；“从仓库 `workspace/` 复制 6 个文件”已按 :120、:160 改为从 `emerge/workspace_template/` 复制 8 个文件 | 见左列行号 |

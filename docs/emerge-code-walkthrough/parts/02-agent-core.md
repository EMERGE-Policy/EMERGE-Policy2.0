# Agent 通用模块（阶段 3）

## 概览

| 文件 | 职责 |
|---|---|
| `messages.py` | 按轮组织的对话历史 `History` / `Turn`、监听者协议 `HistoryListener`、去掉 thinking |
| `tool.py` | `@tool` → `Tool`（参数模型、JSON schema、`invoke`），`Toolset`，`ToolResult`，`error_result` |
| `middleware.py` | 中间件基类 `AgentMiddleware`、洋葱链 `MiddlewareChain`、`CompactionMiddleware` |
| `state.py` | 每轮预算 `RunLimits`、每轮运行上下文 `RunContext` |
| `context.py` | system 组装与缓存、运行信息追加、请求组装 `RequestBuilder`、压缩 `Compactor`、`call_model`、`transcript` |
| `loop.py` | `AgentLoop`：一轮内的模型/工具循环，以及取消、关闭、续跑和事件 |
| `skills.py`、`prompts/compact.md` | 读取工作区 skill；压缩摘要用的提示词 |

文中"实测"指用假 provider 在 `EmergePolicy2.0` 环境里跑过，脚本在 `/data/yuqingchi/tmp/emerge-code-walkthrough/scratch/`，没有调用真实模型。

## messages.py

对话历史按"轮"存放：一轮从一条输入开始，后面跟 assistant 回复、工具结果、运行信息，以及 boundary hook 追加的消息。`AgentLoop`、`ContextBuilder`、`Compactor` 读写它；主 Agent 的 `Session` 作为监听者把每次改动写进存储。

### 结构

| 字段/方法 | 含义 | 约束/默认 |
|---|---|---|
| `Turn.messages` | 本轮全部消息，按发生顺序 | 默认 `[]`；`extra="forbid"` |
| `Turn.summary` | 是否是压缩生成的摘要轮 | 默认 `False`，只有 `replace_oldest_turns_with_summary` 会置为 `True` |
| `History.turns` | 轮列表，是公开属性 | 直接改它不会通知监听者；`session.history_from_rows` 就是这样重建历史的（`emerge/agent/session.py:210-219`） |
| `History._listener` | 监听者 | 构造时为 `None` |
| `len(history)` | 轮数 | |
| `set_listener(listener \| None)` | 挂上 / 解除监听者 | |
| `start_turn(message)` | 新开一轮，`message` 是首条 | |
| `append_messages_to_current_turn(*messages)` | 追加到最后一轮 | |
| `flatten_messages()` | 全部消息按顺序展平 | `RequestBuilder.build` 每次请求都调用 |
| `native_items()` | 汇总所有消息上的原生 item | 目前没有调用方。原生 item 跟着产生它的 assistant 消息送回，`ModelRequest.native_items` 保持为空 |
| `truncate_to_turn_count(count)` | 只保留前 `count` 轮 | 用于回滚没完成的轮 |
| `compact(count, summary)` | 前 `count` 轮换成一个摘要轮，然后对所有轮去 thinking | 只用于压缩 |
| `replace_oldest_turns_with_summary(count, summary)`、`strip_thinking()` | `compact` 拆开的两步 | 单独调用时不通知监听者 |
| `SUMMARY_PREFIX` | `"[Earlier conversation, compacted]\n\n"` | 摘要轮里只有一条 user 消息 `SUMMARY_PREFIX + summary.strip()`，永远是第 0 轮 |

### 监听者 `HistoryListener`

它是 `Protocol`，监听者不需要继承它。四个方法都在修改**完成之后**同步调用，所以不能阻塞；主 Agent 的 `Session` 在这里只把写库操作排进队列（`emerge/agent/session.py:139-153`）。

| 方法 | 何时调用 | 参数 |
|---|---|---|
| `turn_started(turn, message)` | `start_turn` 之后 | 新轮下标、首条消息 |
| `messages_appended(turn, messages)` | `append_messages_to_current_turn` 之后，且 `messages` 不为空 | 最后一轮的下标、追加的消息 |
| `turns_dropped(start, stop)` | `truncate_to_turn_count` 真的删掉了轮 | 删掉的是第 `start` 到 `stop-1` 轮 |
| `compacted(summary, kept)` | `compact` 做完后调用一次 | `summary` 是新的第 0 轮消息；`kept` 是第 1 轮起每条消息的 `(压缩后的轮下标, 消息)`，已经去掉 thinking |

### 情况表

| 情况 | 处理 | 位置 |
|---|---|---|
| 历史为空时调用 `append_messages_to_current_turn(m)` | 先建一个空 `Turn` 再追加；监听者只收到 `messages_appended(0, …)`，收不到 `turn_started`（实测） | `emerge/agent/messages.py:63-67` |
| 调用 `append_messages_to_current_turn()` 时不带消息 | 不通知；如果历史为空，会留下一个空轮 | `emerge/agent/messages.py:63-67` |
| `truncate_to_turn_count(count)` 且 `count` ≥ 当前轮数 | 什么都不删，也不通知 | `emerge/agent/messages.py:78-81` |
| `replace_oldest_turns_with_summary(count)` 且 `count` ≤ 0 或大于轮数 | 抛 `ValueError("cannot replace N of M turns")` | `emerge/agent/messages.py:98-99` |
| 同上，`count` 等于轮数 | 允许，所有轮合成一个摘要轮。`Compactor` 不会这样调用，它至少保留最新 1 轮 | `emerge/agent/messages.py:98-101` |
| 摘要文字带首尾空白 | `strip()` 后接在前缀后面 | `emerge/agent/messages.py:100` |
| 对非 assistant 消息或没有原生 item 的消息调 `strip_thinking(message)` | 返回原对象 | `emerge/agent/messages.py:117-118` |
| 原生 item 满足 `provider == "anthropic"` 且 `data["type"]` 为 `thinking` / `redacted_thinking` | 删除。其他原生 item（Anthropic 的 text、tool_use，OpenAI 带 `encrypted_content` 的 reasoning）保留（实测） | `emerge/agent/messages.py:119-123` |
| 没有可删的 item | 返回同一个对象（用 `is` 比较也相同） | `emerge/agent/messages.py:124-125` |
| 某一轮有消息被改 | 用 `model_copy` 替换整个 `Turn`，`summary` 标记保留；没改的轮还是原来的对象 | `emerge/agent/messages.py:105-108` |
| `content` 里的 `ReasoningPart` | 不处理，它本来就不会发给 API | `emerge/agent/messages.py:111-126` |
| 监听者方法抛异常 | 内存里的修改已经做完（先改后通知），异常原样抛给调用方 | `emerge/agent/messages.py:56-58, 65-67, 79-81, 88-92` |

### 序列化

`History` 是普通类，不是 Pydantic 模型，没有 `model_dump_json` / `model_validate_json`。`Turn` 仍是 Pydantic 模型（`extra="forbid"`，传入未知字段抛 `ValidationError`），去 thinking 时用它的 `model_copy`。

| 情况 | 结果 | 位置 |
|---|---|---|
| 主 Agent 怎么持久化 | 不整体序列化 History。监听者逐条写库；打开会话时按行重建，`turn` 号变了就开新轮，`summary` 跟着行恢复 | `emerge/agent/session.py:139-153, 210-219` |

## tool.py

`@tool` 把一个同步或异步函数包装成 `Tool`：用签名和 docstring 生成 Pydantic 参数模型和 JSON schema；`invoke` 负责校验参数、调用函数，并把返回值统一成 `ToolResult`。`AgentLoop._tool_call` 调用 `invoke`；主 Agent 的工具（`emerge/agent/tools/`）都用它定义。

### 结构

| 字段/方法 | 含义 | 约束/默认 |
|---|---|---|
| `ToolResult.content` | 发给模型的文字 | 默认 `""` |
| `ToolResult.images` | 图片路径（相对路径或绝对路径；主 Agent 的相对路径相对对话文件夹，由模型客户端的 `image_root` 读） | 默认 `[]` |
| `ToolResult.is_error` | 是否错误 | 默认 `False`。只体现在 `tool.finished` 事件里，发给模型的消息里没有这个字段 |
| `ToolResult.to_message(call_id)` | 转成 `role="tool"` 的消息 | `content` 为空时文字写 `"(no output)"`；图片作为 `ImagePart` 跟在文字后面 |
| `Tool.name` | 工具名 | `name` 参数是 `None` 或 `""` 时用 `fn.__name__` |
| `Tool.description` | 工具描述 | `description` 参数是 `None` 时取 docstring 的第一段；显式传 `""` 就是空描述 |
| `Tool.args_model` | 参数模型 | 模型名为 `<name>_args`，`extra="forbid"` |
| `Tool.spec` | `ToolSpec(name, description, parameters=args_model.model_json_schema())` | 构造时生成一次 |
| `Tool.fn` | 原函数 | `@tool` 返回的是 `Tool` 对象，它没有 `__call__`，不能直接调用；要调原函数用 `.fn` |
| `async Tool.invoke(arguments: dict)` | 调用协议：校验 → 调用 → 统一返回值 | 总是返回 `ToolResult`；只有 `BaseException`（取消、停止）会抛出来 |

### 定义工具时

| 情况 | 处理 | 位置 |
|---|---|---|
| 用 `@tool`（不带括号）或 `@tool(name=..., description=...)` | 都得到 `Tool` | `emerge/agent/tool.py:99-107` |
| 写成 `@tool("x")`，用位置参数传字符串 | 字符串被当成函数处理，抛 `AttributeError: 'str' object has no attribute '__name__'`（实测） | `emerge/agent/tool.py:39, 107` |
| 函数有 `*args` / `**kwargs` | 抛 `ToolError("... cannot use *args or **kwargs")` | `emerge/agent/tool.py:119-120` |
| 参数没有类型注解（包括 `lambda`） | 抛 `ToolError("... parameter 'x' needs a type annotation")` | `emerge/agent/tool.py:121-123` |
| 注解写成 `None` | 同上：`hints.get` 拿到 `None`，被当成没有注解 | `emerge/agent/tool.py:121-123` |
| 传入 `functools.partial` | 不给 `name` 时，取 `fn.__name__` 就抛 `AttributeError`；给了 `name`，也因为拿不到注解抛"needs a type annotation"（实测） | `emerge/agent/tool.py:39, 113, 121-123` |
| 注解无法求值（前向引用了不存在的名字） | 抛 `ToolError("... has unresolvable type annotations: ...")` | `emerge/agent/tool.py:112-115` |
| 注解是 Pydantic 不认识的类（任意自定义类） | `create_model` 抛 `PydanticSchemaGenerationError` 并原样传出，不会转成 `ToolError`（实测） | `emerge/agent/tool.py:129-133` |
| 注解能校验但生成不了 JSON schema（如 `Callable`） | `model_json_schema()` 抛 `PydanticInvalidForJsonSchema` 并原样传出（实测） | `emerge/agent/tool.py:44` |
| 参数名以下划线开头（如 `_x`） | Pydantic 把它当私有属性，schema 里没有这个参数，定义时不报错。之后每次调用都失败：模型传了 `_x` 就报 "Extra inputs are not permitted"，没传就报 `TypeError: missing ... argument`（实测，见疑点 7） | `emerge/agent/tool.py:129-133, 54-56` |
| 有仅位置参数（`def f(x: int, /)`） | 定义成功，但调用时 `fn(**kwargs)` 抛 `TypeError`，每次都得到错误结果（实测，见疑点 7） | `emerge/agent/tool.py:54-56` |
| 仅关键字参数；绑定方法（`self` 不在签名里） | 正常 | `emerge/agent/tool.py:111` |
| 参数有默认值 | 作为字段默认值，不进 `required`；没有默认值的参数进 `required` | `emerge/agent/tool.py:124` |
| docstring 的 `Args` 段描述了这个参数 | 字段带 `description`；没描述就不带 | `emerge/agent/tool.py:125-128` |

### 注解 → JSON schema（实测，Pydantic 2.14）

顶层固定为 `{"type": "object", "additionalProperties": false, "title": "<name>_args", "properties": …}`；有必填参数时才有 `"required": […]`，有嵌套类型时才有 `$defs`。每个属性都带 Pydantic 生成的 `title`。

| 注解 | schema |
|---|---|
| `int` / `float` / `str` / `bool` | `integer` / `number` / `string` / `boolean` |
| `list[int]` | `array`，带 `items` |
| `dict[str, Any]` | `object`，`additionalProperties: true` |
| `tuple[int, int]` | `array`，带 `prefixItems`，`minItems` 和 `maxItems` 都是 2 |
| `set[str]` | `array`，`uniqueItems: true` |
| `Literal["x", "y"]` | `enum` |
| `Enum` 子类、嵌套的 `BaseModel` | 定义放进顶层 `$defs`，属性用 `$ref` 引用 |
| `int \| None = None`、`int \| str` | `anyOf` |
| `datetime` / `Path` / `bytes` | `string`，`format` 分别为 `date-time` / `path` / `binary` |
| `Any` | `{}`，不限类型 |

### docstring 解析

只认 Google 风格，不引入解析库（决策记录［阶段 3］`@tool`，HANDOFF.md:149）。

| 情况 | 处理 | 位置 |
|---|---|---|
| 工具描述怎么来 | 取第一段：从第一行到第一个空行或节标题为止，每行 strip 后用空格连起来 | `emerge/agent/tool.py:136-143` |
| docstring 以节标题开头（如 `Args:`），或者没有 docstring | 描述为空串 | `emerge/agent/tool.py:41, 140-141` |
| 什么算节标题 | 整行是 `args/arguments/parameters/return(s)/raises/yields/example(s)/note(s)` 加冒号，不区分大小写，冒号后面不能有内容 | `emerge/agent/tool.py:19` |
| `Args:` / `Arguments:` / `Parameters:` 段里的 `name (type): desc` | `desc` 作为该参数的描述；括号里的类型忽略；`*name` / `**name` 去掉星号 | `emerge/agent/tool.py:20, 158-161` |
| 段内不符合条目格式的行 | 接到上一个参数的描述后面。每行先 strip，所以缩进不起作用 | `emerge/agent/tool.py:162-163` |
| 段内的空行 | 跳过，不结束这一段；空行后面的普通句子仍会接到最后一个参数上（实测） | `emerge/agent/tool.py:156-157` |
| 续行写成 `Word: text`（如 `Note: be careful`） | 被当成名为 `Note` 的新条目，原参数的描述到这里就断了（实测） | `emerge/agent/tool.py:158-161` |
| 遇到别的节标题（`Returns:` 等） | 参数段结束 | `emerge/agent/tool.py:152-155` |
| numpy 风格（`Parameters` 下一行是 `----------`） | 标题没有冒号，不识别，参数都没有描述（实测） | `emerge/agent/tool.py:19` |
| 描述了签名里没有的参数 | 忽略 | `emerge/agent/tool.py:125` |

### 调用 `invoke`

| 情况 | 处理 | 位置 |
|---|---|---|
| `arguments` 刚好是 `{"_raw": 文本}`（模型层解析 JSON 失败时的约定，`emerge/models/base.py:198-205`） | 不做校验，直接返回错误结果 `"arguments are not valid JSON: <文本>"` | `emerge/agent/tool.py:47-48` |
| 缺必填参数、类型不对、有多余字段 | 返回错误结果 `"invalid arguments"`，带 `details: [{loc, msg}]`；`loc` 用 `.` 连接嵌套路径，如 `p.x` | `emerge/agent/tool.py:49-53` |
| 值能宽松转换（给 `int` 传 `"3"`、`1.0`、`true`） | Pydantic 默认是宽松模式，转换后照常调用；给 `int` 传 `1.5` 会报错（实测） | `emerge/agent/tool.py:50` |
| 参数是嵌套模型 | 函数收到的是模型实例；没传的参数用默认值 | `emerge/agent/tool.py:54` |
| 同步函数 | 直接在事件循环线程里调用，不放进线程池；执行期间整个事件循环都在等它 | `emerge/agent/tool.py:56` |
| 异步函数，或同步函数返回了 awaitable | 先 `await`，再统一返回值 | `emerge/agent/tool.py:57-58` |
| 函数抛出任何 `Exception`（包括 `ToolError`） | 返回错误结果 `"<异常类型>: <消息>"` | `emerge/agent/tool.py:60-61` |
| 函数抛出 `CancelledError` / `StopRequested`（都是 `BaseException`） | 不捕获，交给循环处理（实测） | `emerge/agent/tool.py:60` |
| 返回值类型不支持 | 统一返回值时抛 `TypeError`，它在 `try` 里，被转成错误结果 | `emerge/agent/tool.py:59, 182-186` |
| 参数校验失败的调用 | 也算一次工具调用：计入 `max_tool_calls` 和 `TurnResult.tool_calls`，因为计数在调用前就加了 | `emerge/agent/loop.py:193-194` |

错误结果都由 `error_result(tool_name, message, details=None)` 生成：`ToolResult(content=json.dumps({"tool", "error"[, "details"]}, ensure_ascii=False), is_error=True)`，`details` 为空时不写这个键（`emerge/agent/tool.py:189-194`）。用到它的地方有：`invoke`、循环里的未知工具（`emerge/agent/loop.py:238-239`）、主 Agent 的 `StaleCallGuard`（`emerge/agent/agent.py:123`）。

### 返回值 → `ToolResult`（均实测）

| 返回值 | 结果 |
|---|---|
| `ToolResult` | 原样返回，可以带 `is_error=True`，也可以带多张图片 |
| `str` | `content` 就是这个字符串 |
| `""` 或 `None` | `content=""`，消息文字是 `"(no output)"` |
| `ImagePart` | `content="Image: <path>"`，`images=[path]` |
| Pydantic 模型 | `content=model_dump_json()` |
| `bool` / `int` / `float` / `dict` / `list` / `tuple` | `json.dumps(ensure_ascii=False, default=str)`：`Path`、`datetime` 等转成 `str()`；`int` 键转成字符串键；`NaN` 输出为 `NaN` |
| `dict` 的键是 `tuple` 等类型 | `json.dumps` 抛 `TypeError`，转成错误结果 |
| `list[ImagePart]` | 按 list 处理，每个 `ImagePart` 变成 `str()` 文字，**不会附上图片**；要返回多张图，得返回 `ToolResult(images=[...])` |
| `set`、`bytes`、异步生成器等 | `TypeError("tool returned set; expected ...")`，转成错误结果 |
| 图片路径不存在或读不出来 | 工具这里不检查。要到下一次组装模型请求、provider 读图时才抛 `ModelError("cannot read image ...")`（`emerge/models/base.py:185-188`，实测），这一轮因此失败（见疑点 8） |

### `Toolset`

| 情况 | 处理 | 位置 |
|---|---|---|
| `add` 时重名（和已注册的重名，或和同一次 `add` 里前面的重名） | 抛 `ToolError("duplicate tool name: x")`；同一次调用里排在它前面的工具已经注册了，不会撤销（实测） | `emerge/agent/tool.py:74-78` |
| `remove` 一个不存在的名字 | 抛 `KeyError` | `emerge/agent/tool.py:80-81` |
| `get` 一个不存在的名字 | 返回 `None` | `emerge/agent/tool.py:83-84` |
| `specs()` | 按注册顺序返回 `ToolSpec` 列表，这就是发给模型的顺序 | `emerge/agent/tool.py:86-87` |
| `RequestBuilder` 建好之后再增删工具 | `RequestBuilder` 构造时拷贝了 specs，发给模型的工具列表不会变；循环却按名字实时查找。所以删掉的工具被调用时返回 unknown tool 错误，新加的工具模型看不到 | `emerge/agent/context.py:89`、`emerge/agent/loop.py:237` |

## middleware.py

中间件包在模型调用和工具调用外面，另外还有一轮开始和结束时的通知。`AgentLoop` 持有一个 `MiddlewareChain`。主 Agent 的链从外到内依次是 `TurnControl`、`StaleCallGuard`、`PromptCacheKey`、`CompactionMiddleware`（`emerge/agent/agent.py:346`）。

### 钩子

| 钩子 | 何时调用 | 返回值的含义 |
|---|---|---|
| `on_turn_start(ctx)` | 每次 `run_turn` / `continue_turn` 开始时，按注册顺序，每个调一次 | 忽略 |
| `on_turn_end(ctx, result)` | 一轮以 stop、触发限额、closed 或 cancelled（包括任务被取消、`StopRequested`）结束时，按注册顺序，每个调一次；以其他异常（如 `ModelError`）结束时不调 | 忽略 |
| `wrap_model_call(ctx, request, handler)` | 每次模型调用都调；先注册的在外层 | 返回值就是这次的回复。可以改了 request 再调 `handler`，可以不调（短路），也可以调多次 |
| `before_model(ctx, request)` | 在所有 `wrap_model_call` 里面、真正调用之前，按注册顺序 | `None` 表示不改；返回一个 `ModelRequest` 就替换掉原请求，后面的 `before_model` 和真正的调用都用新的 |
| `after_model(ctx, response)` | 真正调用返回之后，按注册的**逆序** | `None` 表示不改；返回一个 `ModelResponse` 就替换掉原回复 |
| `wrap_tool_call(ctx, call, handler)` | 每个真正要执行的工具调用都调（因预算耗尽或会话关闭而补结果的调用不经过这里）；先注册的在外层 | 返回值就是工具结果；不调 `handler` 就是拦截 |

### 洋葱顺序（中间件 `[A, B]`，实测）

1. `A.wrap_model_call` 中调用 `handler` 之前的部分
2. `B.wrap_model_call` 中调用 `handler` 之前的部分
3. `A.before_model`
4. `B.before_model`
5. 真正调用 `call_model`
6. `B.after_model`
7. `A.after_model`
8. `B.wrap_model_call` 中 `handler` 返回之后的部分
9. `A.wrap_model_call` 中 `handler` 返回之后的部分

工具调用的顺序是：`A.wrap_tool_call` → `B.wrap_tool_call` → 核心 `AgentLoop._tool_call`（发 `tool.started` → 查找工具并 `invoke` → 发 `tool.finished`）→ 结果依次经过 B、A 返回。

### 情况表

| 情况 | 处理 | 位置 |
|---|---|---|
| `wrap_model_call` 不调 `handler`，直接返回一个回复 | 内层中间件的 wrap、所有 `before_model` / `after_model` 和 provider 都不执行；返回的回复照常写入历史，计一次迭代，也计入用量（实测） | `emerge/agent/middleware.py:59-75`、`emerge/agent/loop.py:169-174` |
| `wrap_model_call` 多次调用 `handler`（比如自己做重试） | 每次都重新执行全部 `before_model`（包括压缩检查）、真正的调用和 `after_model` | `emerge/agent/middleware.py:60-70` |
| `wrap_tool_call` 不调 `handler`（拦截） | 核心逻辑不执行，不发 `tool.started` / `tool.finished`；结果照常写入历史；这次调用已经计入 `max_tool_calls` 和 `TurnResult.tool_calls`（实测） | `emerge/agent/middleware.py:77-81`、`emerge/agent/loop.py:193-196` |
| 任意钩子抛异常 | 链本身不捕获，异常沿洋葱向外传。外层的 `wrap_*` 可以自己 `try`；没人接住就从 `run_turn` 抛出，后果见 loop.py 的"一轮怎么结束" | `emerge/agent/middleware.py:51-81` |
| "只有 `CompactionMiddleware` 可以改前缀" | 只是 docstring 里的约定，代码不检查 | `emerge/agent/middleware.py:101` |
| 构造 `MiddlewareChain(列表)` | 构造时拷贝列表，之后改原列表不影响链 | `emerge/agent/middleware.py:48-49` |
| 各钩子在哪个 task 里跑 | 模型调用这条链在循环专门为它建的 task 里（这样 `close_turn` 能只取消回复）；工具调用和整轮通知在本轮自己的 task 里 | `emerge/agent/loop.py:204, 195, 131` |

`CompactionMiddleware` 放在下面"压缩"一节讲。

## state.py

`RunLimits` 是每轮的预算，`RunContext` 是一轮的运行上下文，两者都不发给模型。主 Agent 从配置读出 `RunLimits`（`emerge/agent/agent.py:327`），并且每轮新建一个 `RunContext`（`emerge/agent/agent.py:401`）。

### `RunLimits`

| 字段/方法 | 含义 | 约束/默认 |
|---|---|---|
| `max_iterations` | 本轮最多调用几次模型 | `None` 表示不限 |
| `max_input_tokens` | 本轮累计 input token 的上限 | `None` |
| `max_output_tokens_total` | 本轮累计 output token 的上限 | `None` |
| `max_tool_calls` | 本轮最多执行几个工具调用 | `None` |
| `from_config(limits)` | 有 `model_dump()` 就用它，否则用 `dict(limits)`；每个字段取 `data.get(name)`，缺的为 `None`，多出的键忽略（实测） | `emerge/agent/state.py:20-24` |

`RunLimits` 是 frozen dataclass，自己不校验取值：设成 `0` 或负数，对应的限额在第一次检查时就会触发。要求 `> 0` 的是配置层的 `config.RunLimits`（`emerge/config.py:105-108`）。

### 预算

| 预算 | 何时检查 | 超出的结果 |
|---|---|---|
| `max_iterations` | 每次调用模型前（迭代开头，在 checkpoint 和关闭检查之后）。`iterations` 是本次 `_run` 里已完成的模型调用次数 | `iterations >= 上限` 时不再调用模型，返回 `finish_reason="max_iterations"`，`text` 是本轮最后一条非空回复的文字；本轮保留在历史里，`on_turn_end` 照常调用。上限为 0 时一次模型都不调，历史里只有本轮的输入（实测） |
| `max_input_tokens` | 同上；比较的是 `ctx.usage.input_tokens`，即本轮各次模型调用加上压缩摘要调用的累计 | `>= 上限` 时返回 `"max_input_tokens"`。只在调用前检查，所以单次调用可以越过上限，上一次回复里的工具也会照样执行完（实测：上限 150、每次调用 100，第 2 次调用之后才结束） |
| `max_output_tokens_total` | 同上；比较的是 `ctx.usage.output_tokens` | `>= 上限` 时返回 `"max_output_tokens"` |
| `max_tool_calls` | 每个工具调用执行前；`executed` 是本次 `_run` 里已执行的调用数，被拦截、工具不存在、参数错误的调用都算 | `executed >= 上限` 时，这个调用和同一回复里剩下的调用各补一条 `"not executed: tool call budget exhausted"`（这些调用前不再调 checkpoint），补完返回 `"max_tool_calls"`。刚好用完时不会结束，要等模型下一次还要调工具才结束（实测） |

- 同一个边界上几个条件同时成立时，优先级是：checkpoint 返回 `False`（cancelled）→ 已请求关闭（closed）→ `max_iterations` → `max_input_tokens` → `max_output_tokens`（`emerge/agent/loop.py:160-165, 216-226`）。
- 因限额结束时历史是完整的：每个 tool_call 都有结果。
- `continue_turn` 会重新进入 `_run`，`iterations` 和 `executed` 从 0 开始计。`ctx.usage` 是否从 0 开始，取决于调用方传进来的 `RunContext`。

### `RunContext`

| 字段/方法 | 含义 | 约束/默认 |
|---|---|---|
| `run_id` | 本轮事件带的 `run_id` | `create()` 没给时用 `uuid4().hex[:12]`。主 Agent 传入会话 id，它的事件包装层再换成当前运行的 id（`emerge/agent/agent.py:157, 401`） |
| `workspace` | 这一轮工作的目录，循环本身不用它；主 Agent 传的是对话文件夹（`emerge/agent/agent.py:418`） | 必填 |
| `intent_counter` | 意图计数 | 默认 0。主 Agent 的 boundary hook 在每个边界更新它（`emerge/agent/agent.py:459`），`StaleCallGuard` 读取它 |
| `usage` | 本轮累计用量 | 默认 `Usage()`。循环在每次模型响应后累加（`emerge/agent/loop.py:171`），压缩的摘要调用也累加（`emerge/agent/context.py:190-191`） |
| `record_usage(usage)` | `self.usage = self.usage + usage` | `Usage.__add__` 不保留 `speed`；`cached_tokens` / `reasoning_tokens` 两边都是 `None` 时结果仍是 `None`（`emerge/models/types.py:121-132`） |

## context.py

这个文件负责让请求前缀保持稳定：`ContextBuilder` 组装并缓存 system，并负责追加运行信息；`RequestBuilder` 是唯一组装请求的地方；`Compactor`（见下一节）是唯一会改写前缀的操作。`AgentLoop` 每次调用模型前都用到它们，主 Agent 处理节点提问的旁路请求也用 `RequestBuilder.build()`（`emerge/agent/agent.py:495`）。

### `ContextBuilder`

| 字段/方法 | 含义 | 约束/默认 |
|---|---|---|
| `instructions` | 按顺序排列的指令片段，每段是 `str` 或返回 `str` 的回调 | 拷贝成 list |
| `runtime_info` | 运行信息回调 | 默认 `None` |
| `system`（构造参数） | 直接作为缓存的 system 文本，不再组装 | 默认 `None`。主 Agent 传入会话记录里存的 system（`emerge/agent/agent.py:283-285`） |
| `get_system_prompt()` | 缓存为 `None` 时组装一次，之后一直返回缓存 | |
| `rebuild_system_prompt()` | 清空"上次追加的运行信息"，然后重新组装：所有片段回调都会重新求值，比如重读 `MEMORY.md` | 只有 `Compactor.compact` 调用它（`emerge/agent/context.py:183`） |
| `append_runtime_info(history)` | 见下面的运行信息表 | 返回这次是否追加了 |

### system 组装

| 情况 | 处理 | 位置 |
|---|---|---|
| 怎么组装 | 按顺序求值（回调就调用一次），每段 `strip()`，空段跳过，段之间用 `"\n\n"` 连接（实测：`"  A  "`、`""`、`" B "` 得到 `"A\n\nB"`） | `emerge/agent/context.py:70-76` |
| 第二次及以后的请求 | 直接用缓存，片段回调不再被调用（实测只调了一次） | `emerge/agent/context.py:44-47` |
| 构造时给了 `system`（包括空串 `""`） | 直接使用，不组装，直到压缩后重建；给的是 `""` 时请求里没有 system 消息（实测） | `emerge/agent/context.py:41, 45, 94-95` |
| 组装出来是空串 | 请求里没有 system 消息 | `emerge/agent/context.py:94-95` |
| 某个片段回调抛异常 | 异常传给调用方。如果发生在第一次请求，这一轮失败；如果发生在压缩时，历史已经压缩了但 system 没更新，这一轮也失败 | `emerge/agent/context.py:52, 73, 180-183` |

### 运行信息

| 情况 | 处理 | 位置 |
|---|---|---|
| 什么时候调用 | 每次调用模型前，在 checkpoint、关闭和限额检查之后，`build()` 之前 | `emerge/agent/loop.py:167` |
| `runtime_info` 为 `None` | 不追加，返回 `False` | `emerge/agent/context.py:61-62` |
| 回调返回空串或全是空白 | 不追加，`_last_info` 保持不变 | `emerge/agent/context.py:63-64` |
| 和上次追加的文字相同 | 不追加 | `emerge/agent/context.py:64` |
| 和上次不同 | 记为 `_last_info`，作为一条 user 消息追加到当前（最后一）轮末尾，监听者收到 `messages_appended` | `emerge/agent/context.py:65-68` |
| 回调依次返回 `A`、`""`、`A` | 第二个 `A` 不追加，因为 `_last_info` 还是 `A`（实测） | `emerge/agent/context.py:63-66` |
| 新建了 `ContextBuilder`（比如重开会话） | `_last_info` 是 `None`，第一次一定追加，即使历史末尾已经有相同的文字（实测） | `emerge/agent/context.py:42` |
| 压缩之后 | `_last_info` 被清空，下一次请求会再追加一次。当前轮总会被保留下来，所以如果本轮刚追加过同样的内容，当前轮里会出现两条相同的运行信息（实测，见疑点 3） | `emerge/agent/context.py:50-51` |
| 回调抛异常 | 异常传出，这一轮失败，本轮的输入留在历史里（实测） | `emerge/agent/loop.py:167` |
| 事件 | 不发 `agent.message` | `emerge/agent/context.py:67` |

### `RequestBuilder`

| 字段/方法 | 含义 | 约束/默认 |
|---|---|---|
| `tools` | 构造时拷贝的 `ToolSpec` 列表 | 之后不再变化（见 tool.py 的 `Toolset` 表） |
| `build()` | 消息 = `[Message.system(system)]`（system 非空时才有）+ `history.flatten_messages()`；`tools` 每次是新列表，但里面是同一批 `ToolSpec` 对象 | `native_items` 留空，原生 item 跟着 assistant 消息送回 |
| `rebuild(request)` | 重新 `build()` 一次，只替换 `messages` 和 `tools`，`tool_choice`、`temperature`、`cache_key`、`extra` 等保持不变（实测） | 压缩后由 `CompactionMiddleware.before_model` 调用 |

### 前缀稳定靠什么

| 前缀部分 | 怎么保持不变 | 唯一的例外 |
|---|---|---|
| system | 缓存起来，只在压缩后重建 | 压缩 |
| 工具列表 | 在 `RequestBuilder` 构造时固定，顺序就是注册顺序 | 无 |
| 消息 | 两次压缩之间 History 只在末尾追加；会变化的内容（运行信息）作为新消息追加，不放进 system | 压缩（折叠旧轮、去 thinking）；取消回滚只删除末尾的轮 |
| 调用方追加的消息 | boundary hook 用 `append_messages_to_current_turn` 追加到末尾；循环不检查它追加在什么位置 | — |

### `call_model(client, request, *, stream=True)`

| 情况 | 处理 | 位置 |
|---|---|---|
| `stream=False` | 调 `client.generate(request)` | `emerge/agent/context.py:197-198` |
| `stream=True` | 遍历 `client.stream(request)`，取 `StreamDone` 里的完整响应；有多个时取最后一个 | `emerge/agent/context.py:199-202` |
| 流结束了还没有 `StreamDone` | 抛 `ModelError("model stream ended without a response")` | `emerge/agent/context.py:203-204` |
| 两种方式有什么区别 | `ModelClient` 两种方式发的是同一个流式请求，发的 `model.token` 事件也一样，只有 `model.started` 的 `stream` 字段不同（实测） | `emerge/models/client.py:31-61` |

## 压缩：`Compactor`（context.py）、`CompactionMiddleware`（middleware.py）、prompts/compact.md

`Compactor` 把较早的若干轮交给模型写成摘要，再用一个摘要轮替换它们；`CompactionMiddleware` 在每次调用模型前判断要不要压缩，并维护精确的 token 计数。主 Agent 从配置读 `max_context_tokens` 和 `compact_threshold`，`keep_ratio` 用默认值 0.5（`emerge/agent/agent.py:310-317`）。

### 参数

| 参数 | 含义 | 默认 |
|---|---|---|
| `max_context_tokens` | 上下文预算 | 必填；配置里默认 100000（`emerge/config.py:114`） |
| `compact_threshold` | 触发压缩的比例 | 0.8；配置允许 0 到 1 |
| `budget` | `int(max_context_tokens * compact_threshold)` | 估算值**严格大于**它才压缩 |
| `keep_ratio` | 保留下来的部分最多占 `budget` 的多大比例 | 0.5，即保留预算为 `max(1, int(budget * 0.5))` |
| `prompt` | 摘要提示词 | `prompts/compact.md`，构造时读一次 |
| `stream` | 摘要调用是否走流式 | `True`。`CompactionMiddleware` 不传这个参数，所以它不跟随 `AgentLoop(stream=...)` |

### `CompactionMiddleware`

| 方法 | 做什么 | 位置 |
|---|---|---|
| `__init__` | 建一个 `Compactor(client, context, max_context_tokens, compact_threshold, keep_ratio)`，保存 `history` 和 `builder`；`_sent_messages` 和 `_input_tokens` 初始为 0 | `emerge/agent/middleware.py:105-126` |
| `before_model` | 估算超出预算，并且 `compact()` 返回 `True` 时，用 `builder.rebuild(request)` 重建请求；记下 `_sent_messages`；不管压没压缩都返回 request | `emerge/agent/middleware.py:128-133` |
| `after_model` | 记下 `_input_tokens`，返回 `None` | `emerge/agent/middleware.py:135-137` |
| `on_turn_end` | 调 `record_usage(...)`，见下 | `emerge/agent/middleware.py:139-141` |

在主 Agent 里它注册在最后（最内层），所以它的 `before_model` 最后执行，拿到的是其他中间件改过的请求；`rebuild` 会保留这些改动，比如 `PromptCacheKey` 设置的 `cache_key`。

### 精确 token 计数怎么来

1. `before_model` 记下这次实际发送的消息条数 `_sent_messages`，其中包括 system 消息（`emerge/agent/middleware.py:131`）。
2. `after_model` 记下这次响应的 `usage.input_tokens`；每次调用都会覆盖，以最后一次为准（`emerge/agent/middleware.py:136`）。
3. `on_turn_end` 调用 `record_usage(_sent_messages, _input_tokens or result.usage.input_tokens)`；`input_tokens ≤ 0` 时不记（`emerge/agent/middleware.py:141`、`emerge/agent/context.py:154-155`）。
4. 之后估算时：如果有记录，并且这次的消息条数不少于记录的条数，就用"记录的 token 数 + `estimate_tokens(新增的消息)`"；否则对整个请求（消息、工具、`native_items`）做估算（`emerge/agent/context.py:142-146`）。
5. 压缩成功后清空记录（`emerge/agent/context.py:182`）。

- 精确值只在每轮结束时写入。所以同一轮里第 2、3… 次调用模型时，用的仍是上一轮最后一次调用的精确值，加上之后新增的所有消息的估算。
- 用最后一次响应的 `input_tokens` 而不是 `TurnResult.usage`：后者是整轮多次调用的总和，会成倍高估（决策记录［阶段 3］精确 token 计数，HANDOFF.md:153）。按阶段 2 的口径，Anthropic 的 `input_tokens` 已经包含缓存读写，所以等于整段前缀的大小。
- 估算由 `emerge/models/usage.py` 完成：装了 `tiktoken` 就用 `o200k_base`；没装时粗估为每 4 个 ASCII 字符算 1 token、每个非 ASCII 字符算 1 token，每条消息另加 4。`tiktoken` 不在 `pyproject.toml` 的依赖里，当前 conda 环境也没装。图片按 detail 计 85 或 765。

### 折叠哪些轮（`Compactor.compact`）

1. 历史只有 0 或 1 轮：不压缩，返回 `False`（`emerge/agent/context.py:160-161`）。
2. 从最新一轮往前累计每轮的估算 token：最新一轮无条件保留；更早的轮只要累计值不超过保留预算就保留，碰到第一个放不下的轮就停。所以保留的总是连续的最新若干轮（`emerge/agent/context.py:163-170`）。
3. 如果所有轮都放得下，仍然折叠最早的 1 轮（`emerge/agent/context.py:171-172`）。
4. 如果要折叠的只有第 0 轮，而它本身已经是摘要轮：不压缩（守卫，`emerge/agent/context.py:173-176`）。
5. 把要折叠的轮转成文字记录，请模型写摘要；这次调用的用量计入 `RunContext.usage`（`emerge/agent/context.py:186-192`）。
6. 摘要 strip 后为空：不压缩（`emerge/agent/context.py:178-179`）。
7. 调 `history.compact(index, summary)`：折叠的轮换成一个摘要轮，所有轮去掉 Anthropic thinking，监听者收到一次 `compacted`（`emerge/agent/messages.py:84-93`）。
8. 清空精确计数，重建 system（`emerge/agent/context.py:182-183`）。
9. 回到 `before_model`，执行 `builder.rebuild(request)`，这一次调用就发出新的前缀（`emerge/agent/middleware.py:129-130`）。

### 情况表

| 情况 | 处理 | 位置 |
|---|---|---|
| 估算值 ≤ budget | 不压缩，发送原请求 | `emerge/agent/middleware.py:129`、`emerge/agent/context.py:137-138` |
| 历史只有 1 轮（不管它多大） | 不压缩，超预算的请求照样发出（实测） | `emerge/agent/context.py:160-161` |
| 当前轮自己就超过保留预算 | 只保留当前轮，之前的轮全部折叠 | `emerge/agent/context.py:166-170` |
| 当前轮自己就超过模型的上下文 | 当前轮不能拆开，压缩解决不了；请求照样发出，会不会被拒由服务端决定（被拒时抛 `ModelError`，这一轮失败） | `emerge/agent/context.py:163-170` |
| 历史是 `[摘要轮, 很大的最新轮]`，能折叠的只有旧摘要 | 守卫生效，不调模型、不压缩（实测摘要调用 0 次）。这一轮之后每次调模型都会再判断一次，每次都被守卫挡住 | `emerge/agent/context.py:173-176` |
| 历史是 `[摘要轮, 普通轮, 很大的最新轮]` | 守卫不管：旧摘要和普通轮一起折叠成新摘要（实测） | `emerge/agent/context.py:173` |
| 所有轮都放得下保留预算，但请求仍超预算（比如 system 很大） | 折叠最早的 1 轮（实测）；下一次再超预算时，多半会被守卫挡住 | `emerge/agent/context.py:171-176` |
| 摘要为空或全是空白 | 返回 `False`，超预算的原请求照发；下一次调模型时会再试一次，再花一次摘要调用 | `emerge/agent/context.py:178-179` |
| 摘要调用抛 `ModelError` | 异常从 `before_model` 传出，这一轮以 `ModelError` 失败；历史没有压缩，本轮输入留在历史里（实测） | `emerge/agent/context.py:189`、`emerge/agent/loop.py:129-151` |
| 摘要调用期间，这一轮被取消或被 `close_turn` | 被取消：整轮丢弃；被关闭：以 `closed` 结束。两种情况历史都没有压缩（实测） | `emerge/agent/loop.py:202-214` |
| 本轮压缩之后，这一轮又被取消 | 如果折叠了 2 轮或更多，回滚会失效（见疑点 1） | `emerge/agent/loop.py:93, 136, 142` |
| 被折叠的轮里的图片 | 不保留，在文字记录里写成 `[image: 路径]` | `emerge/agent/context.py:215, 226` |
| 保留下来的轮里的图片 | 原样保留 | `emerge/agent/messages.py:104-109` |
| 保留下来的轮里的 Anthropic `thinking` / `redacted_thinking` | 删除，其他原生 item 保留（实测）。原因：thinking 的签名覆盖它之前的前缀，前缀改写后再送回会被拒（决策记录［阶段 3］压缩保留，HANDOFF.md:154） | `emerge/agent/messages.py:104-127` |
| 新的 system | 所有片段重新求值，会话中改过的 `MEMORY.md` 等这时才生效（实测：压缩后发出的是新 system） | `emerge/agent/context.py:183` |
| 摘要调用的用量 | 计入 `RunContext.usage`（实测 150 = 主调用 100 + 摘要 50），因此也计入 `max_input_tokens` / `max_output_tokens_total`；但不计入 `_input_tokens` | `emerge/agent/context.py:190-191` |
| 直接调 `Compactor.compact(history)`，不传 `run_context` | 摘要调用的用量不记到任何 `RunContext`（`ModelClient` 自己的 `model.usage` 事件照发） | `emerge/agent/context.py:190` |
| 监听者收到通知的时机 | `compacted` 在 `rebuild_system_prompt()` 之前发出。主 Agent 的 `Session` 用延迟求值的回调取 system，真正写库时拿到的已经是重建后的文本 | `emerge/agent/context.py:180-183`、`emerge/agent/session.py:150-153` |
| `compact_threshold = 0` | budget 为 0，每次调模型都会尝试压缩（仍受"只有 1 轮"和守卫的限制） | `emerge/agent/context.py:135` |

### 摘要请求与文字记录

摘要请求只有一条 user 消息：把 `compact.md` 里的 `{conversation}` 用 `str.replace` 换成下面的文字记录。请求不带 system，也不带工具，不经过中间件链，直接调 `call_model`（`emerge/agent/context.py:186-189`）。

| 消息 | 在文字记录里写成 |
|---|---|
| system | 跳过 |
| user | `user: <文字>`；没有文字时写 `user:` |
| assistant | 有文字时写 `assistant: <文字>`；每个工具调用一行 `assistant calls <name>(<JSON 参数>)` |
| tool | `tool result: <文字>`；文字为空时写 `tool result: (empty)` |
| 任何消息里的图片 | 跟在该消息的行后面，每张一行 `[image: <path>]` |
| 旧的摘要轮 | 作为 `user: [Earlier conversation, compacted] …` 写进去，由新摘要接替 |
| 推理文字、原生 item、`tool_call_id` | 不出现 |

### `prompts/compact.md`

| 部分 | 内容 |
|---|---|
| 开头 | 告诉模型：这段对话会被摘要替换，只有摘要能留下来。要写的是给接手助手的交接说明；不要继续对话，不要回答最后一条消息，不要评论任务 |
| 按顺序保留 | 1 用户想要什么：意图、偏好、更正、约束，方向改变以及被替换掉的内容；2 已完成的事：带具体值（路径、标识符、命令、数量、报错）；3 没完成的事：待解决的问题、待做的决定、下一步；4 已经发生的环境动作：按发生顺序，写明确切目标，这些不可撤销；5 关键事实：文件位置、接口、配置和结论 |
| 规则 | 用散文和短列表，不要写成对话记录；名称、路径、数字和报错原文照抄；去掉闲聊、重复解释和被取代的计划；图片留不下来，重要的话写出图里是什么；不确定的地方明说 |
| 结尾 | `Conversation to summarize:` 后面接 `{conversation}` |

## loop.py

`AgentLoop` 跑一轮：调用模型 → 依次执行工具 → 把结果交回模型，直到回复里没有工具调用、触发限额、被取消或被关闭为止。它不知道 inbox 和控制通道，调用方通过 `boundary_hook` 在边界上插入消息或结束本轮。主 Agent 每轮用一个新 task 调用 `run_turn` 或 `continue_turn`，睡眠时攒下的多条消息一次传入（`emerge/agent/agent.py:411-427`）。

### 构造参数

| 参数 | 含义 | 默认 |
|---|---|---|
| `client` | `ModelClient` | 必填 |
| `tools` | `Toolset`，执行时按名字查找工具 | 必填 |
| `builder` | `RequestBuilder`；运行信息通过 `builder.context` 追加 | 必填 |
| `history` | 写入的 History | 必填；应当和 `builder.history` 是同一个对象，代码不检查 |
| `middleware` | `MiddlewareChain` | `None` 时用空链 |
| `limits` | `RunLimits` | `None` 时全部不限 |
| `callbacks` | 事件发射器 | `None` 时不发事件 |
| `stream` | 调模型走 `stream()` 还是 `generate()` | `True` |

### `TurnResult`

| 字段 | 含义 | 约束/默认 |
|---|---|---|
| `text` | 本次 `_run` 里最后一条非空的 assistant 文字；最后一条回复没有文字时，保留更早的那条（实测） | cancelled 时为 `""`；`continue_turn` 只算续跑的部分 |
| `tool_calls` | 本次 `_run` 里进入执行的调用，包括被中间件拦截、工具不存在、参数错误的；补了 "not executed" 的不算 | cancelled 时为 `[]` |
| `usage` | 即 `ctx.usage`：本轮已经发生的全部用量，包括摘要调用；被取消时也照算 | |
| `finish_reason` | `stop` / `max_iterations` / `max_input_tokens` / `max_output_tokens` / `max_tool_calls` / `cancelled` / `closed` | 和 `models.types.FinishReason`（模型自己的结束原因）同名但不是同一个类型；模型的 `length`、`content_filter` 不会反映在这里 |

### 一轮的步骤（`run_turn`）

1. 输入可以是 `str`、一条 `Message` 或一组 `Message`（`TurnInput`）：`str` 转成 `Message.user`，`Message` 原样使用、不检查 role，一组消息按顺序使用，空列表抛 `ValueError`（`emerge/agent/loop.py:29, 265-272`）。
2. 记下 `turn_index = 当前轮数`，用第一条 `start_turn` 新开一轮，其余用 `append_messages_to_current_turn` 追加到同一轮；所以它们都在第一次模型调用之前进入历史（`emerge/agent/loop.py:96-100`）。
3. 每条输入各发一次 `agent.message`，`role="user"`，只带文字（`emerge/agent/loop.py:101-102`）。
4. 依次调用每个中间件的 `on_turn_start`（`emerge/agent/loop.py:131`）。
5. 每次迭代开头依次检查：boundary hook → 是否已请求关闭 → 限额（`emerge/agent/loop.py:169-174`）。
6. 追加运行信息，`builder.build()`（`emerge/agent/loop.py:167-168`）。
7. 在单独的 task 里跑模型调用的中间件链（包括压缩）和 `call_model`（`emerge/agent/loop.py:169, 202-214`）。
8. `iterations += 1`，`ctx.record_usage`；把 assistant 消息写入历史，有文字就发 `agent.message`（`emerge/agent/loop.py:170-177`）。
9. 回复里没有工具调用：以 `stop` 结束（`emerge/agent/loop.py:178-179`）。
10. 按回复里的顺序逐个处理工具调用：预算检查 → boundary hook → 关闭检查 → 计数 → 工具中间件链加 `_tool_call` → 把结果写入历史（`emerge/agent/loop.py:191-205`）。
11. 有因预算耗尽而补了结果的调用：以 `max_tool_calls` 结束；否则回到第 5 步（`emerge/agent/loop.py:197-198`）。
12. 结束时调用 `on_turn_end`（以异常结束时除外）；`finally` 里清除关闭标记（`emerge/agent/loop.py:134-151`）。

### 工具执行

| 情况 | 处理 | 位置 |
|---|---|---|
| 一次回复里有多个工具调用 | 串行执行，按模型给出的顺序，前一个完成才开始下一个；每个调用前都经过一次 checkpoint；每个结果一出来就写入历史，监听者逐条收到 | `emerge/agent/loop.py:182-196` |
| 工具名不存在 | 返回错误结果 `"unknown tool; available: a, b"`（一个工具都没有时写 `none`）；`tool.started` / `tool.finished` 照常发 | `emerge/agent/loop.py:238-239` |
| 工具执行中被取消 | 不发 `tool.finished` | `emerge/agent/loop.py:241-249` |
| 回复因长度被截断（`ModelResponse.finish_reason="length"`），且没有工具调用 | 仍以 `stop` 结束 | `emerge/agent/loop.py:178-179` |
| 截断导致工具参数的 JSON 不完整 | 模型层存成 `{"_raw": …}`，工具返回"参数不是合法 JSON"的错误结果，模型下一次可以重试 | `emerge/agent/tool.py:47-48` |
| 回复既没有文字也没有工具调用 | 空的 assistant 消息照样写入历史，以 `stop` 结束；`text` 是更早的文字，没有就是 `""` | `emerge/agent/loop.py:174-179` |
| 同一个 `AgentLoop` 同时跑两轮 | 代码没有加锁，也不检查；两轮共用 `_closing`、`_inflight` 和历史。docstring 写的是一次只跑一轮，主 Agent 靠 serve 循环保证串行（`emerge/agent/agent.py:386-397`） | `emerge/agent/loop.py:82-83` |

### 流式与非流式

| `AgentLoop(stream=)` | 行为 |
|---|---|
| `True`（默认） | `call_model(..., stream=True)`，走 `client.stream()`；循环只用最后 `StreamDone` 里的完整响应 |
| `False` | 走 `client.generate()`，发出的请求和事件与流式相同 |
| 两者共同点 | 文字和思考的增量由 `ModelClient` 用它自己的 callbacks 以 `model.token` 事件发出；历史只写完整的回复，回复中途被取消时什么都不写 |

### 边界上的新消息（`boundary_hook`）

| 边界 | 位置 | hook 返回 `False` 时 |
|---|---|---|
| 每次调用模型前（每次迭代开头，包括第一次） | `emerge/agent/loop.py:169` | 整轮取消并回滚 |
| 每个工具调用前（因预算耗尽而补结果的调用除外） | `emerge/agent/loop.py:197` | 整轮取消并回滚，已执行工具的结果一起丢掉 |

| 情况 | 处理 | 位置 |
|---|---|---|
| hook 返回 `None`、`True`、`0` 等不是 `False` 的值 | 继续。判断用的是 `is False`，实测返回 `0` 不会取消 | `emerge/agent/loop.py:238` |
| hook 在边界上往历史末尾追加消息 | 循环不管追加在哪，下一次请求会带上（实测：追加的 user 消息成了第 2 次请求的最后一条） | `emerge/agent/loop.py:169, 177` |
| hook 在两个工具调用之间追加消息 | 循环不阻止，消息会夹在同一回复的工具结果中间。主 Agent 的 hook 在上一条回复还有工具调用没得到结果时不追加（`emerge/agent/agent.py:453-455`），见疑点 10 | `emerge/agent/loop.py:197` |
| hook 抛异常 | 不回滚，异常向上抛（见"一轮怎么结束"） | `emerge/agent/loop.py:237-239` |
| 回滚的范围 | 只回滚历史；已经执行的工具在外部产生的效果不会撤销，下一轮的模型也看不到这些调用 | `emerge/agent/loop.py:145, 151` |
| 主 Agent 怎么用（交接点） | 用户要求中断时返回 `False`；否则把 inbox 里的消息追加到当前轮，并把 `ctx.intent_counter` 更新为最新的意图数 | `emerge/agent/agent.py:449-460` |

### 一轮怎么结束

| 结束方式 | 触发条件 | 历史 | `on_turn_end` | 调用方得到 |
|---|---|---|---|---|
| `stop` | 回复里没有工具调用 | 保留 | 调用 | `TurnResult` |
| 限额（4 种） | 见 state.py 的预算表 | 保留，每个 tool_call 都有结果 | 调用 | `TurnResult` |
| `closed` | 调了 `close_turn()` | 保留已经完成的部分（见下表） | 调用 | `TurnResult` |
| `cancelled`（hook 触发） | boundary hook 返回 `False` | 执行 `truncate_to_turn_count(turn_index)`，监听者收到 `turns_dropped`（本轮压缩过时可能删不掉，见疑点 1） | 调用 | `TurnResult(text="", tool_calls=[])` |
| `cancelled`（取消或停止） | 任务被 `cancel()`，或者工具、中间件抛了 `StopRequested` | 同上 | 调用；`on_turn_end` 自己又被取消时会吞掉这个取消 | 原来的异常重新抛出（`CancelledError` / `StopRequested`） |
| 其他异常 | `ModelError`，或者中间件、hook、运行信息回调、指令片段抛出的异常 | **不回滚**，本轮已写入的内容保留 | 不调用 | 异常抛出 |

| 实测情况 | 结果 | 位置 |
|---|---|---|
| 模型调用中 `task.cancel()` | 历史回滚为空，调用 `on_turn_end(cancelled)`，然后抛出 `CancelledError` | `emerge/agent/loop.py:139-146` |
| 工具执行中 `task.cancel()` | 工具被打断，整轮回滚 | `emerge/agent/loop.py:139-146` |
| 工具抛 `StopRequested` | 整轮回滚，然后抛出 `StopRequested` | `emerge/agent/loop.py:139-146` |
| 第 2 次模型调用抛 `ModelError` | 历史保留 `U, A(calls), T`，不调用 `on_turn_end` | `emerge/agent/loop.py:129-151` |
| 中间件的 `wrap_tool_call` 在 3 个调用中的第 2 个抛异常 | 历史保留 `A(3 个调用), T[c1]`，c2、c3 没有结果（疑点 4） | `emerge/agent/loop.py:195` |
| `on_turn_start` 里被取消或抛异常 | 不回滚，本轮输入留在历史里（疑点 6） | `emerge/agent/loop.py:131` |
| 本轮压缩之后被取消 | 见疑点 1 | `emerge/agent/loop.py:93, 136, 142` |
| 被取消之后循环自身的状态 | `_closing=False`，`_inflight=None`，`ctx.usage` 里保留已完成调用的用量 | `emerge/agent/loop.py:150-151, 213-214` |

### `close_turn()`

决策记录［阶段 8］（HANDOFF.md:779）要求关闭时保留这一轮已经做完的部分。代码的做法是把模型调用放进单独的 task，`close_turn` 只取消这个 task；再用 `current_task().cancelling()` 区分"关闭"和"整轮被取消"。

| 调 `close_turn()` 时循环在做什么 | 结果 | 位置 |
|---|---|---|
| 正在生成模型回复（包括正在做压缩摘要调用） | 取消那个单独的模型调用 task；回复不写入；以 `closed` 结束（两种都实测）；摘要调用被取消时历史不压缩 | `emerge/agent/loop.py:126-127, 208-211` |
| 回复已写入，但第一个工具还没开始 | 在第一个工具前发现关闭标记，回复里的所有调用都补一条 `"not executed: session closed"`；以 `closed` 结束 | `emerge/agent/loop.py:188-192` |
| 某个工具正在执行，后面还有调用 | 正在执行的工具做完，结果照常写入；同一回复里还没执行的调用各补一条 `"not executed: session closed"`；以 `closed` 结束，不再调模型（实测 3 个调用：c1 的结果保留，c2、c3 补了结果） | `emerge/agent/loop.py:188-192` |
| 正在执行回复里的最后一个工具 | 工具做完，结果写入；下一次迭代开头发现关闭标记，以 `closed` 结束，不调模型（实测） | `emerge/agent/loop.py:161-162` |
| boundary hook 正在执行 | hook 返回后再检查关闭标记；如果 hook 返回了 `False`，按取消处理 | `emerge/agent/loop.py:169-171` |
| 同时还有 `task.cancel()` | 取消优先（`current_task().cancelling() > 0`）：整轮丢弃，抛出 `CancelledError`（实测） | `emerge/agent/loop.py:209-212` |
| 当时没有正在进行的轮 | 关闭标记一直留到下一轮结束，下一轮在第一个边界就以 `closed` 结束，一次模型都不调（实测，疑点 5） | `emerge/agent/loop.py:125, 150-151` |
| 重复调用 | 没有额外效果 | `emerge/agent/loop.py:125-127` |

### `continue_turn()`

| 情况 | 处理 | 位置 |
|---|---|---|
| 历史为空 | 和 `run_turn` 一样 | `emerge/agent/loop.py:110-111` |
| 历史不为空 | 把输入（一条或一组，同 `run_turn`）追加到最后一轮末尾（监听者收到 `messages_appended`），每条发一次 `agent.message(user)`，令 `turn_index = 最后一轮的下标`，然后接着跑 | `emerge/agent/loop.py:112-116` |
| 预算 | `iterations` 和 `executed` 从 0 开始计 | `emerge/agent/loop.py:156-157` |
| 续跑被取消 | 整个最后一轮都丢掉，包括续跑之前的部分（实测） | `emerge/agent/loop.py:136, 142` |
| 续跑被关闭 | 全部保留 | `emerge/agent/loop.py:199-200` |
| 用在哪里（交接点） | 主 Agent 重开会话时，用它继续上次被关闭的那一轮 | `emerge/agent/agent.py:350-353, 388, 402` |

### 事件

| 事件 | 时机 | payload 字段 |
|---|---|---|
| `agent.message` | `run_turn` / `continue_turn` 把输入写入历史之后，每条输入一次 | `role="user"`，`text`（只有文字，图片不在事件里） |
| `agent.message` | 每次回复写入历史之后，且回复有文字时 | `role="assistant"`，`text` |
| `tool.started` | 在工具中间件链的最内层，查找工具之前 | `tool`、`call_id`、`arguments` |
| `tool.finished` | `invoke` 返回之后（工具不存在、参数错误、工具抛异常时也发） | `tool`、`call_id`、`is_error`、`content`（不含图片） |

- 事件的 `run_id` 取自 `ctx.run_id`；`callbacks=None` 时不发事件（`emerge/agent/loop.py:252-254`）。
- 不发事件的有：运行信息、hook 追加的消息、补的 `not executed` 结果、被中间件拦截的工具调用、压缩。
- `model.started` / `model.token` / `model.finished` / `model.usage` 由 `ModelClient` 用它自己的 callbacks 发出；表示整轮开始和结束的 `agent.turn` 事件由主 Agent 发出（`emerge/agent/agent.py:410, 430`）。

## skills.py

`SkillStore` 在构造时读入工作区 `skills/` 目录下的全部 skill，之后不再访问磁盘。主 Agent 在 attach 时建一个（`emerge/agent/agent.py:282`）。

### 结构

| 字段/方法 | 含义 | 约束/默认 |
|---|---|---|
| `Skill.name` | 名字，按名查找时用 | 取 frontmatter 的 `name`；缺失或为空时用文件名（去掉 `.md`） |
| `Skill.description` | 什么时候该用这个 skill | 取 frontmatter 的 `description`；缺失时为 `""` |
| `Skill.body` | 正文：frontmatter 后面的部分，strip 过 | |
| `Skill.summary()` | 一个名字和描述相同、`body=""` 的副本 | `Skill` 是 frozen dataclass |
| `SkillStore(skills_dir)` | 构造时一次读完 | |
| `list()` | 所有 skill 的 `summary()`，按注册顺序（也就是文件名顺序） | |
| `get(name)` | 返回完整的 skill，找不到返回 `None` | 精确匹配，区分大小写（实测 `get("ALPHA")` 返回 `None`） |
| `in` / `len()` | 是否存在 / 一共几个 | |

### 加载

| 情况 | 处理 | 位置 |
|---|---|---|
| `skills_dir` 不存在，或者不是目录 | 得到空的 store，不报错 | `emerge/agent/skills.py:35-36` |
| 扫描范围 | `skills_dir.glob("*.md")`，按路径排序。不进子目录；`.MD` 不匹配（Linux 区分大小写）；以点开头的 `.hidden.md` 会被读入（实测） | `emerge/agent/skills.py:37` |
| 有一个名叫 `x.md` 的目录 | 读取时抛 `IsADirectoryError`（属于 `OSError`），记 warning 后跳过（实测） | `emerge/agent/skills.py:60-64` |
| 文件不是 UTF-8 | 抛 `UnicodeDecodeError`，记 warning 后跳过（实测） | `emerge/agent/skills.py:60-64` |
| 文件不是以 `---` 开头（前面有空行，或者带 UTF-8 BOM） | 记 "has no YAML frontmatter" warning 后跳过（实测） | `emerge/agent/skills.py:15, 65-68` |
| frontmatter 是空的（`---` 下一行紧接着 `---`） | 正则要求中间至少有一个换行，匹配不上，当作没有 frontmatter，跳过（实测） | `emerge/agent/skills.py:15` |
| 结尾的 `---` 后面有空格，或者在文件末尾且没有换行；CRLF 换行 | 都能识别（实测） | `emerge/agent/skills.py:15` |
| 名字重复（区分大小写） | 记 warning，保留文件名排在前面的那个 | `emerge/agent/skills.py:41-43` |
| 加载之后磁盘上的文件变了 | 不会重新读，只有新建 `SkillStore` 才读得到 | `emerge/agent/skills.py:32-44` |

### frontmatter 解析（手写的最小解析，不是 YAML）

| 写法 | 解析结果 | 位置 |
|---|---|---|
| `key: value` | 在第一个冒号处切开，`value` 做 strip，所以值里可以有冒号（实测 `"Quoted: with colon"`） | `emerge/agent/skills.py:85-89` |
| 值两端是成对的 `"` 或 `'` | 去掉这对引号（实测），里面的转义不处理 | `emerge/agent/skills.py:93-96` |
| 缩进的行，且前面已经有 key | 用空格接到上一个 key 的值后面（实测） | `emerge/agent/skills.py:82-84` |
| 第一行就缩进（前面还没有 key） | 当成普通的 `key: value` | `emerge/agent/skills.py:82, 85-89` |
| `description: >` 后面跟缩进的多行（YAML 折叠块） | 不支持，得到的值是 `"> first line second line"`，`>` 保留在里面（实测） | `emerge/agent/skills.py:82-89` |
| 列表（`tags:` 后面跟 `  - x`） | 拼成 `"- x - y"`；除了 `name` 和 `description`，其他键都被忽略（实测） | `emerge/agent/skills.py:82-84` |
| 空行，或者以 `#` 开头的行 | 跳过（实测） | `emerge/agent/skills.py:80-81` |
| 行尾的 `# 注释` | 不去掉，成为值的一部分（实测得到 `name: trailing-spaces # inline comment`） | `emerge/agent/skills.py:88-89` |
| 不缩进、也没有冒号的行 | 跳过（实测）；之后的缩进行仍然接到上一个 key 上 | `emerge/agent/skills.py:85-87` |
| 同一个 key 写了两次 | 后面的覆盖前面的 | `emerge/agent/skills.py:89` |
| 带引号的值被续行续接 | 去引号只在第一行做（第一行的引号不成对就保留），续接之后不再去 | `emerge/agent/skills.py:83, 89` |

交接点：主 Agent 不把 skill 正文放进 system，也不放进运行信息。只要工作区有 skill，`emerge/agent/tools/catalog.py` 就注册 `use_skill` 工具，工具描述里列出 `list()` 返回的名字和描述；调用时用 `get(name)` 返回 `"Skill <name>:\n\n<body>"`，名字不存在时抛 `ToolError`，变成错误结果（`emerge/agent/tools/catalog.py:48-67`）。一个 skill 都没有时不注册这个工具。

## `emerge/agent/__init__.py` 的导出

| 来源 | 从 `emerge.agent` 导出的名字 |
|---|---|
| `context.py` | `ContextBuilder`、`Compactor`、`RequestBuilder`、`call_model` |
| `loop.py` | `AgentLoop`、`FinishReason`、`TurnResult` |
| `messages.py` | `History`、`HistoryListener`、`Turn` |
| `middleware.py` | `AgentMiddleware`、`CompactionMiddleware`、`MiddlewareChain` |
| `skills.py` | `Skill`、`SkillStore` |
| `state.py` | `RunContext`、`RunLimits` |
| `tool.py` | `Tool`、`ToolResult`、`Toolset`、`error_result`、`tool` |

- 上面这些在 `import emerge.agent` 时立即导入。主 Agent 的名字（`MainAgent`、`Session`、`Inbox` 等）通过模块级 `__getattr__` 在第一次访问时才导入，导入后存进模块全局，之后不再经过 `__getattr__`（`emerge/agent/__init__.py:29-53`）。实测 `import emerge.agent` 之后，`emerge.runtime`、`emerge.storage`、`emerge.graph`、`emerge.agent.agent` 都没有被加载；访问不认识的名字抛 `AttributeError`。
- 没有从包导出、要从子模块导入的名字：`SUMMARY_PREFIX`、`strip_thinking`、`NOT_EXECUTED_CLOSED`、`BoundaryHook`、`ModelHandler`、`ToolHandler`、`Instruction`、`transcript`、`COMPACT_PROMPT`。
- 依赖方向：这七个模块只 import `emerge.models`、`emerge.callbacks`、`emerge.events`、`emerge.exceptions` 以及彼此（用 grep 核对过），符合计划的硬性要求。

## 疑点（待核实）

### 可能的 bug 和没处理的情况（1–7 都实测过）

1. **本轮压缩折叠了 2 轮或更多之后再取消，回滚不起作用。** `turn_index` 是在压缩之前记下的（`emerge/agent/loop.py:93, 113`），压缩让轮数减少了"折叠的轮数 − 1"（`emerge/agent/context.py:180`）；取消时调用的 `truncate_to_turn_count(turn_index)`（`emerge/agent/loop.py:136, 142`）在 `turn_index ≥ 当前轮数` 时什么都不删，监听者也收不到 `turns_dropped`（`emerge/agent/messages.py:80-82`）。实测有两种结果：模型调用中取消，历史里留下 `turn2: U('cancel me')`；checkpoint 在工具前返回 `False`，历史里留下 `A(calls=['fast'])` 却没有对应的结果，之后每次请求都会带着这个没答复的 tool_call（`emerge/agent/loop.py:184` 的注释说每个 tool call 都需要结果）。只折叠 1 轮时下标不变，回滚正常（实测）。在主 Agent 里，这对应的情形是：某次模型调用前做了压缩，之后用户中断；如果中断时有工具在执行，`TurnControl` 不会取消任务，而是等到下一个边界由 hook 返回 `False`（`emerge/agent/control.py:75-78`、`emerge/agent/agent.py:435-436`）。
2. **压缩之后，这次模型调用没完成（被取消或关闭）时，精确计数记下的是一对对不上的值。** `before_model` 已经记下压缩后的消息条数（`emerge/agent/middleware.py:131`），但 `after_model` 没有执行，`_input_tokens` 还是压缩前那次响应的值；它为 0 时会退回用 `result.usage.input_tokens`，而这个值包含摘要调用（`emerge/agent/middleware.py:141`）。实测记下的是 `(5, 50)`，其中 50 是摘要调用的 input。这之后的估算会偏高或偏低，直到下一次调用成功为止。
3. **压缩之后，同一轮里会重复追加运行信息。** `rebuild_system_prompt` 的注释说压缩去掉了旧的运行信息（`emerge/agent/context.py:50-51`），但当前轮总会被保留（`emerge/agent/context.py:163-170`），本轮刚追加的那条还在。`_last_info` 被清空后，下一次请求又追加了一条相同的。实测当前轮是 `U('q') U('RUN INFO v1') A(calls) T U('RUN INFO v1') A('done')`。前缀没有被破坏，只是多了一条消息。
4. **在两个工具调用之间发生的非取消类异常，会在历史里留下没有结果的 tool_call。** `_drive` 只处理 `_Cancelled`、`CancelledError` 和 `StopRequested`（`emerge/agent/loop.py:134-148`）。如果中间件的 `wrap_tool_call` 或 boundary hook 在同一回复的两个调用之间抛异常，实测历史为 `A(calls=[fast, fast, fast]) T[c1]`。主 Agent 把这样的轮记为 `error`，会话回到 idle（`emerge/agent/agent.py:415-427`），之后的请求都会带着这两个没答复的调用。主 Agent 里有一个能触发的地方：意图变化之后，`StaleCallGuard.wrap_tool_call` 会检查 `write_file` / `edit_file` 的 `path`，而 `is_graph_file` 遇到 `~不存在的用户/...` 或含 NUL 的路径会抛 `RuntimeError` / `ValueError`（实测，`emerge/agent/agent.py:122`、`emerge/agent/tools/files.py:50-51`）。`ModelError` 不会造成这种不配对，因为它在调模型时抛出，那时所有工具结果都已经写入了。
5. **没有轮在进行时调用 `close_turn()`，会影响下一轮。** `_closing` 只在 `_drive` 的 `finally` 里清除（`emerge/agent/loop.py:125, 150-151`）。实测下一轮一次模型都没调就以 `closed` 结束。主 Agent 只在本轮 task 还没结束时才调用它（`emerge/agent/agent.py:364-365`），所以目前不会触发。
6. **`on_turn_start` 中被取消或抛异常时不回滚。** `notify_turn_start` 在内层 `try` 外面（`emerge/agent/loop.py:131`）。实测用户消息留在历史里，`on_turn_end` 也不调用。现有的中间件都没有覆盖 `on_turn_start`，所以目前不会触发。
7. **仅位置参数，以及以下划线开头的参数，在定义工具时不报错，但这个工具永远调用失败。** `_args_model` 不检查参数的种类和名字（`emerge/agent/tool.py:118-128`）；对仅位置参数，`fn(**kwargs)` 会抛 `TypeError`（`emerge/agent/tool.py:56`）；`_x` 被 Pydantic 当成私有属性，schema 里没有它。
8. **工具返回的图片路径不存在时，会让历史"中毒"（只实测了读图这一步会报错）。** `ImagePart` 的路径在工具这一层不检查（`emerge/agent/tool.py:173-174`）。下一次组装请求时，provider 读图抛 `ModelError`（`emerge/models/base.py:185-188`）；`retry_stream` 对 `ModelError` 不重试，这一轮失败。这条工具结果已经在历史里了，按代码推断，之后每次请求都会再失败一次，直到它被压缩折叠掉。

### 与计划、决策记录不一致的地方

9. skill 正文的注入方式：计划（`phase-03-agent-core.md:54`）和决策记录［阶段 3 补充］（HANDOFF.md:160）都说通过 `context.py` 的运行信息回调追加；代码里是 `use_skill` 工具返回正文（`emerge/agent/tools/catalog.py:48-67`），`skills.py` 只提供 `list` 和 `get`。阶段 8 的记录说明了这一改动（HANDOFF.md:821）。
10. 计划写 checkpoint hook（代码里现名 boundary hook）"在每次调用模型前和每个工具执行前调用，用来在末尾追加新消息"（`phase-03-agent-core.md:98`）。但如果在同一回复的工具结果之间插入消息，tool_call 和结果就不再相邻了（Anthropic 要求 tool_result 排在 user 消息的最前面，见 `emerge/models/providers/anthropic/messages.py:185-186` 的注释）。循环不检查这一点（`emerge/agent/loop.py:197`），主 Agent 的 hook 自己避开了（`emerge/agent/agent.py:453-455`）。
11. 精确计数的来源：计划写的是在 `on_turn_end` 里用 `TurnResult.usage` 更新（`phase-03-agent-core.md:74`）；代码用的是最后一次响应的 `input_tokens`，`TurnResult.usage` 只在它为 0 时作为后备（`emerge/agent/middleware.py:136, 141`）。决策记录（HANDOFF.md:153）说明了原因。
12. 决策记录里的方法名和代码对不上：HANDOFF.md:148 写的是 `append` / `truncate` / `replace_prefix`，HANDOFF.md:155 写的是 `regenerate()`；代码里分别是 `append_messages_to_current_turn` / `truncate_to_turn_count` / `replace_oldest_turns_with_summary`（`emerge/agent/messages.py:62, 77, 97`）和 `rebuild_system_prompt()`（`emerge/agent/context.py:49`）。

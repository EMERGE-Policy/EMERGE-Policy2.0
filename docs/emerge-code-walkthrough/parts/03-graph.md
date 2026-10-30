# 图层（阶段 4）

## 概览

| 文件 | 职责 | 主要调用方 |
|---|---|---|
| `node.py`、`ports.py` | 节点基类、结局 `Outcome`、节点运行期接口 `NodeContext`（这里只定签名）；端口、消息、引用、冻结 JSON | 节点作者；`runtime/process.py`、`runtime/context.py`、`runtime/bus.py`、`storage/` |
| `edge.py`、`graph.py` | 订阅与启动边；不可变的 `Graph`、结构查询、`start_status` | 所有上层 |
| `registry.py`、`builder.py` | 节点类型注册、生成代码按版本加载；流式建图 | `agent/tools/graph.py`、`runtime/process.py`、`runtime/checkpoint.py`；`spec.py` |
| `validation.py`、`patch.py` | 结构校验（返回 issue）；改图：edit → 状态检查 → 整体校验 → 新版本，以及 `diff_graphs` | `runtime/supervisor.py`、`runtime/mutations.py` |
| `serialization.py`、`spec.py`、`visualization.py` | 存储用的完整 JSON（schema 3）；主 Agent 编辑的图文件，以及从文件算出 patch；文本 / Mermaid 输出 | `storage/sqlite.py`；`runtime/mutations.py`、`runtime/checkpoint.py`；`agent/tools/graph.py` |

除 `registry.py` 要 import 或 exec 代码文件以外，这一层都是纯数据和纯函数：不起进程、不读写文件、不等待，也不保存运行状态，节点状态和结局由调用方通过 `states` / `ended` 参数传入。`__init__.py` 统一导出公开名字；`NodeChange`、`SubscriptionChange`、`GraphSpecNode`、`StartCondition`、`StartWhen`、`Catalog`，以及 `ports.py` 里的 `freeze`、`thaw`、`NAME_PATTERN` 不在导出之列，要从子模块导入。下文的"位置"都相对于仓库根；"实验"指 `/data/yuqingchi/tmp/emerge-code-walkthrough/scratch/graph/` 下的脚本（`e*.py`），示例节点类定义在 `fx.py`。

## node.py

本文件定义节点类型的基类和结局 `Outcome`，以及节点进程里可以调用的 `NodeContext` 协议（实现在 `runtime/context.py`）。图里只保存类型名：`registry.py` 负责注册这些类，`runtime/process.py` 在子进程里实例化节点，再依次调用 `setup` / `run` / `teardown`。

### Node 的类属性

| 属性 | 含义 | 默认 | 何时检查 |
|---|---|---|---|
| `name` | 注册名，就是 `NodeSpec.type` 里写的值 | `""` | 定义期不查；注册时为空或全是空白则报 `RegistryError`（`emerge/graph/registry.py:173`） |
| `description` | 说明这一类节点做什么，用于目录展示。实例自己的那句话是 `NodeSpec.description` | `""` | 不查 |
| `inputs` / `outputs` | `Port` 元组 | `()` | 定义期转成 tuple。同侧重名允许，输入和输出同名也允许；元素是否为 `Port` 不查 |
| `irreversible` | 节点是否作用于环境。它只是个标记：随事件发出，界面上醒目显示，不影响状态怎么记（决策［去掉 unknown 状态］） | `False` | 不查 |
| `outcomes` | `run()` 可能给出的结局名 | `()` | 定义期检查名字（见下表）；注册时为空则报 `RegistryError`（`emerge/graph/registry.py:177-180`） |
| `reads` | 节点直接读取的活源，格式为 `(名字, 说明)`。不进图、不参与调度，只用于目录展示和影响分析 | `()` | 定义期转成 tuple，名字不能重复 |
| `Params` | 实例参数的 Pydantic 模型；为 `None` 时不接受参数 | `None` | 注册时据此生成 `params_schema`，校验时用它验证 `NodeSpec.params` |

### 定义期校验（`__init_subclass__`，`emerge/graph/node.py:223-233`）

| 情况 | 处理 | 位置 |
|---|---|---|
| `outcomes = ("done")`：少了逗号，实际是字符串 | `NodeError: outcomes must be a tuple of names, e.g. ('done',)` | `emerge/graph/node.py:253-254` |
| `outcomes` 写成 list | 转成 tuple 后通过 | `emerge/graph/node.py:255` |
| 结局名重复 | `NodeError: duplicate outcome name 'a'`。重名检查排在合法性检查前面，所以 `("a.b", "a.b")` 报的是重名 | `emerge/graph/node.py:256`、`265-270` |
| 结局名含点或空白、为空串、不是字符串（如 `1`） | `NodeError: outcome ... is not a valid name` | `emerge/graph/node.py:257-259` |
| 结局名是 `"*"` | `NodeError`：`"*"` 在图文件里表示任意结局，不能声明 | `emerge/graph/node.py:260-261` |
| 结局里有不可哈希的值（如 list） | 重名检查用的 `set` 直接抛 `TypeError`，没有包成 `NodeError` | `emerge/graph/node.py:266-270` |
| 中文结局名（如 `"成功"`） | 通过：`NAME_PATTERN` 只排除点和空白 | `emerge/graph/ports.py:13` |
| `reads` 名字重复 | `NodeError: duplicate read name 'f'` | `emerge/graph/node.py:233` |
| `reads` 的元素不是二元组（如 `("abc",)`） | 解包失败，抛 `ValueError: too many values to unpack` | `emerge/graph/node.py:233` |
| `inputs` 或 `outputs` 内部重名；输入和输出同名 | 允许，所有声明都保留 | `emerge/graph/node.py:228-230` |
| `inputs` 里放的不是 `Port`（如字符串） | 不检查，能定义也能注册（见疑点 8） | `emerge/graph/node.py:229-230` |
| `name` 和 `outcomes` 都为空 | 通过，留给不放进图的公共基类用 | `emerge/graph/node.py:225-227` |
| 子类没写 `outcomes` | 继承父类的元组，并对子类重新检查一遍 | `emerge/graph/node.py:232` |
| `name` 含点（如 `"a.b"`） | 通过：类型名不会出现在端点里，`NodeSpec.type` 也只要求非空 | `emerge/graph/graph.py:49` |

### 生命周期约定

| 方法 | 基类实现 | 约定 |
|---|---|---|
| `__init__(params)` | `self.params = params`，值为 `Params` 实例或 `None` | 只做赋值，I/O 放到 `setup()` 里 |
| `async setup(ctx)` | 空 | 准备资源 |
| `async run(ctx) -> Outcome \| None` | `raise NotImplementedError` | 返回就表示结束，在里面循环就是持续运行。没有模式字段，也没有 tick 回调 |
| `async teardown(ctx)` | 空 | 收尾 |

与执行侧的交接：`run()` 以什么方式结束、最后记成什么状态，由 `emerge/runtime/process.py:170-214` 和 supervisor 决定（详见 Runtime 部分）。

| `run()` 的结束方式 | 记为 |
|---|---|
| 返回 `Outcome`，且名字在 `cls.outcomes` 里 | `finished`，带结局和理由 |
| 返回未声明的结局、`None` 或其他值 | `failed`，没有结局，错误信息写明原因 |
| 抛出 `StopRequested` | `stopped` |
| 抛出其他异常 | `failed` |
| 已经被要求停止过 | 一律记 `stopped`，返回的结局只写日志（`emerge/runtime/supervisor.py:853-866`，决策［去掉 unknown 状态］） |
| `teardown()` 抛异常 | 只写日志，不影响结果（`emerge/runtime/process.py:193-198`） |

### Outcome（`emerge/graph/node.py:30-48`）

`frozen=True`、`extra="forbid"`。字段为 `name`（`NAME_PATTERN`）和 `reason`（默认 `""`）。

| 情况 | 结果 |
|---|---|
| `Outcome("success", "cup placed")` | 可以用位置参数（positional-only） |
| `Outcome(name="x")`、`Outcome("x", reason="r")`、`Outcome.model_validate({...})` | 正常 |
| `Outcome("x", None)` | `reason` 取默认值 `""`：值为 `None` 的位置参数会被丢掉（`emerge/graph/node.py:47-48`） |
| `Outcome("x", name="y")` | `TypeError: got multiple values for keyword argument 'name'` |
| `Outcome()` | `ValidationError`：`name` 必填 |
| `name` 含点或空白 | `ValidationError` |
| `Outcome("*")` | 模型层能通过。但节点类不可能声明 `"*"`，执行时会按"返回了未声明的结局"处理 |
| 传多余字段、构造后改字段 | `ValidationError` |

### NodeContext（`emerge/graph/node.py:51-198`）

它是 `runtime_checkable` 的 `Protocol`。`isinstance` 只检查成员是否都存在，缺一个就返回 `False`，不检查同步还是异步，也不检查签名（实验 `e14_protocol.py`）。

| 属性 | 含义 |
|---|---|
| `node_id` | 逻辑 id |
| `instance_id` | 当前这一份实例（`<id>@v<版本>`）。执行侧会把它盖在节点发出的每条消息、每个引用和事件上 |
| `run_id`、`workspace`、`config` | 运行 id、工作区路径、`WorkspaceConfig`（只在 `TYPE_CHECKING` 下导入，图层不依赖 `config` / `models`） |
| `artifacts_dir` | `runs/<run_id>/nodes/<node_id>/<instance_id>/` |
| `instance_description` | 主 Agent 放置实例时写的那句话 |
| `outcomes` | 本类型声明的结局，只读 |

| 方法 | 同步/异步 | 作用 | 协议 docstring 写明的异常 |
|---|---|---|---|
| `get_latest_input_message(port)` | async | 取已订阅输入上的最新一条；还没收到过则返回 `None` | — |
| `wait_for_input_message(port, newer_than=None, timeout=None, where=None)` | async | 等一条版本大于 `newer_than` 的消息（`newer_than=None` 时等第一条）。给了 `where` 时，先在端口还保留的消息里找，再等新消息。输入中途被改接时，改在新来源上继续等，旧来源改接前发的消息不会交出 | `WaitTimeout`：超时；`InputClosed`：输入已关闭，且没有符合条件的消息；`StopRequested`：阻塞时被叫停 |
| `wait_for_input_messages(ports, *, newer_than=None, match=None, timeout=None)` | async | 每个端口在各自游标之后都有消息时，返回 `{port: Message}`。给了 `match` 时，只有各路取值相同的一组才算数；有多组时取最早的一组 | `WaitTimeout`、`StopRequested`；`InputClosed`：某一路已关闭，这一组再也配不齐 |
| `iter_input_messages(port)` | 普通函数，返回 `AsyncIterator`（实现是 async generator） | 按版本顺序逐条交出已消费那条之后的消息。输入关闭、剩余消息交完后正常结束；被改接不会结束迭代。被更新消息挤掉的会留下空档 | `StopRequested` |
| `publish_output_message(port, *, text=None, data=None, refs=())` | async | 发布到输出端口，返回执行侧盖章后的 `Message`（带版本号和时间线编号）。`await` 返回只表示消息已交给本进程的发送端 | — |
| `report_to_main_agent(text)` | async | 向主 Agent 发进度说明 | — |
| `emit_event(event)` | async | 推一个自定义事件 | — |
| `ask_main_agent(question)` | async | 向主 Agent 提问并等待回答 | — |
| `take_main_agent_messages()` | async | 取走主 Agent 发来、还没被取的全部消息，旧的在前；没有时返回空元组；不等新消息 | — |
| `wait_for_main_agent_message(timeout=None)` | async | 等下一条主 Agent 消息。它和 `take_main_agent_messages` 共用一个队列，每条消息只交出一次 | `WaitTimeout`、`StopRequested` |
| `set_status_snapshot(data)` | async | 覆盖写一份"我正在做什么"的小结构，只保留最新一份，不主动推送 | — |
| `ref_dir(ref)` | 同步 | 返回引用对应的目录 `runs/<run_id>/nodes/<producer>/<producer_instance>/<port>/<key>/` | — |
| `wait_for_run_permission()` | async | 协作式检查点：暂停时阻塞；被要求停止后返回 `False`。在暂停状态下启动的实例，第一次调用就会阻塞 | — |
| `get_model_client()` | 同步 | 第一次调用时才创建模型客户端 | — |

`runtime/context.py` 的实现还会抛出协议没写的异常：端口不是本类型声明的、`data` / 快照 / 事件不能 JSON 序列化，都抛 `NodeError`；`publish_output_message`、`ask_main_agent` 这类请求如果执行侧回复"已停止"，抛 `StopRequested`。细节见 Runtime 部分。

### 节点可能遇到的三个异常（`emerge/exceptions.py:45-63`）

| 异常 | 基类 | 何时抛 | 说明 |
|---|---|---|---|
| `StopRequested` | `BaseException` | 节点阻塞在等待处时被叫停 | 工具层和节点代码里常见的 `except Exception` 接不住它（实验 `e1_node.py` 确认），所以停止信号能一直传出 `run()`，最后记为 `stopped`。如果是 `Exception` 的子类，就可能被吞掉，节点继续跑，直到 `stop_timeout` 后被强杀。需要清理时按名字捕获，清理完再抛出（决策［阶段 4 补充：停止异常］） |
| `WaitTimeout` | `EmergeError` | 等待超时 | 节点可以正常捕获处理 |
| `InputClosed` | `EmergeError` | 输入已关闭（来源节点结束、被删除，或运行结束），且没有可交出的消息 | 原名 `ProducerGone`，阶段 6 改名（决策［阶段 6］输入关闭与 `InputClosed`）。来源节点被替换不算关闭 |

## ports.py

本文件定义跨节点边界传递的数据：端口声明、消息、引用、主 Agent 发给节点的消息，以及让这些数据只读的冻结 JSON。`Message` 和 `MainAgentMessage` 由 `runtime/bus.py` 盖章后构造；`Ref` 由节点自己构造，`storage/artifacts.py` 把它换算成目录。本文件里的模型都是 `frozen=True`、`extra="forbid"`。

### 名字规则

`NAME_PATTERN = ^[^.\s]+$`，即非空、不含点和空白（`emerge/graph/ports.py:13`）。这样规定是因为端点写作 `"node.port"`，按最后一个点切分（决策［阶段 4］端口与节点 id 不允许包含点号）。

| 用 `NAME_PATTERN` 的字段 | 不用的字段 |
|---|---|
| `Port.name`、`Ref.producer`、`Ref.port`、`NodeSpec.id`、`Outcome.name`、类上的 `outcomes`；图文件里的节点 id、输入端口名、`"node.port"` 的两段、启动来源和结局 | `Message.source_node` / `source_port`（普通字符串）、`Subscription` 的四个字段、`StartSource.node`、`NodeSpec.type`（只要求非空）、`Ref.producer_instance`（只要求非空）、`Ref.key`（另有规则） |

### Port、Ref

| 模型 | 字段 | 约束/默认 |
|---|---|---|
| `Port` | `name` | `NAME_PATTERN` |
| | `type_tag` | 非空字符串。只做软校验：订阅两端标签不同时，只报 warning `TYPE_TAG_MISMATCH` |
| | `description` | `""` |
| `Ref` | `producer` | 产出它的节点 id，`NAME_PATTERN` |
| | `producer_instance` | 产出它的实例 id，非空。实例 id 里有 `@`，所以不套用名字规则 |
| | `port` | 产出它的输出端口，`NAME_PATTERN` |
| | `key` | 写者自定的键，规则见下表 |

`Ref` 只有四个字段全部相同才相等：同一个键出现在两个输出端口上，或者被替换前后的两个实例各写一次，得到的都是不同的引用（实验 `e13_ports.py`）。引用本身不带时间，时间在发布它的那条消息上（`content_time`）。

| `key` 的取值 | 结果 | 位置 |
|---|---|---|
| `""` | `ValidationError`（`min_length=1`） | `emerge/graph/ports.py:88` |
| `"."`、`".."` | `ValidationError: ref key '..' must not be '.' or '..' or contain '/', '\'` | `emerge/graph/ports.py:90-96` |
| 含 `/` 或 `\` | 同上 | `emerge/graph/ports.py:94` |
| `"a..b"`、`".hidden"`、`"a b"`、`"帧1"` | 通过：这些都是普通目录名，跳不出引用目录 | — |

### Message、MainAgentMessage

| 字段 | 谁填 | 约束 |
|---|---|---|
| `Message.source_node` / `source_port` | 执行侧 | 字符串 |
| `Message.source_instance` | 执行侧 | 非空。节点被替换后 id 不变，靠这个字段区分旧实例迟到的消息 |
| `Message.version` | 执行侧 | `>= 0`，端口内递增 |
| `Message.timestamp` | 执行侧 | 钟表时间（float） |
| `Message.content_time` | 执行侧，由 bus 盖章 | 必填 int，`>= 0`。整个运行共用一条递增时间线，跨节点也能比较先后。节点不能设置它，`publish_output_message` 也没有这个参数。它与图片帧号无关 |
| `Message.text` | 节点 | `""` |
| `Message.data` | 节点 | 必须是 Mapping，给 list 会报 `ValidationError`。会被冻结。只放小结构，内容本体走 `refs` |
| `Message.refs` | 节点 | `tuple[Ref, ...]`；给 dict 会自动转成 `Ref` |
| `MainAgentMessage.seq` | 执行侧 | `>= 1` |
| `MainAgentMessage.timestamp`、`content_time` | 执行侧 | `content_time` 必填，`>= 0`，与 `Message` 共用同一个计数器 |
| `MainAgentMessage.text` / `data` / `refs` | 主 Agent | 规则同 `Message` |

每个输出端口保留最近 `RuntimeConfig.port_buffer` 条消息（默认 64），保证消费方漏掉通知后还能接着取。`Event.content_time` 可以为空（`emerge/events.py:45`）。

### 冻结 JSON（`FrozenJson`、`freeze`、`thaw`，`emerge/graph/ports.py:25-61`）

为什么要冻结：图的新旧版本共用没变的 `NodeSpec`。如果 `params` 可变，通过一个版本做的修改会出现在所有版本里（决策［阶段 4］图、节点实例、订阅……都是 frozen）。

| 操作 | 结果 |
|---|---|
| `freeze(x)` | 深拷贝：Mapping 变 `FrozenDict`，list 和 tuple 变 `FrozenList`，其他值原样保留 |
| 写操作：`d[k] = v`、`del`、`\|=`、`update`、`pop`、`popitem`、`setdefault`、`clear`；列表的 `append`、`extend`、`insert`、`pop`、`remove`、`clear`、`sort`、`reverse`、`+=`、`*=`、下标赋值 | `TypeError: FrozenDict is read-only` |
| 与普通 dict / list 比较 | 相等。tuple 冻结后成了 `FrozenList`，与 list 相等 |
| `json.dumps` | 正常 |
| `pickle` 往返 | 仍是 `FrozenDict` / `FrozenList`（靠 `__reduce__`），可以送进子进程 |
| `.copy()`、`d \| {}` | 返回普通 dict，是可以修改的副本 |
| `model_dump()`、`thaw(x)` | 返回普通 dict / list，tuple 会变成 list |

## edge.py

本文件定义两种边的数据模型，以及端点解析。订阅是数据依赖；启动边是控制依赖，挂在目标节点实例上（`NodeSpec.start_edges`），不携带数据。判定启动边能否触发的纯函数 `start_status` 写在 `graph.py:319`，也放在本节讲。

### 结构

| 模型 | 字段 | 语义 |
|---|---|---|
| `Subscription` | `source_node`、`source_port`、`target_node`、`target_port` | 一个输入读一个输出。按（节点 id，端口名）定位，不按位置。允许成环。一个输入最多只能有一个来源，由校验检查 |
| `StartSource` | `node`；`outcome=None` | `outcome=None` 表示"做完"：来源给出的任何声明过的结局都算满足。没给结局就结束的来源（被停止、出错）什么要求都满足不了。`str()` 形如 `v[success]` 或 `v` |
| `StartEdge` | `sources=()` | all-of：这条边上的来源全部满足，边才触发。模型层允许空边，由校验报 `START_EDGE_EMPTY`。`str()` 形如 `a and v[success]`，空边为 `(no sources)` |
| 一个节点上的多条边 | — | any-of：任一条触发即可启动；每个节点最多启动一次（由执行侧保证） |
| `ANY_OUTCOME = "*"` | — | 只在图文件里使用，表示任意结局；节点类不能声明这个结局 |

模型里的来源只是引用：来源节点是否存在、要求的结局是否声明过，都交给校验报出带错误码的 issue，构造模型时不会抛错（`emerge/graph/edge.py:43-44`）。

### parse_endpoint / start_edges_from_after

| 输入 | 结果 | 位置 |
|---|---|---|
| `"a.b"` | `("a", "b")` | `emerge/graph/edge.py:80` |
| `"a.b.c"` | `("a.b", "c")`，按最后一个点切分。节点 `a.b` 不可能存在，之后校验会报 `ENDPOINT_MISSING` | `emerge/graph/edge.py:80` |
| `"ab"`、`".b"`、`"a."` | `GraphValidationError: endpoint 'ab' is not of the form 'node.port'` | `emerge/graph/edge.py:81-82` |
| `"a b.c"` | `("a b", "c")`。这里不拒绝，之后校验报 `ENDPOINT_MISSING` | — |
| `after=["a", "b", "a"]` | 一条边 `a and b`：去重并保持顺序，每个来源只要求"做完" | `emerge/graph/edge.py:74-75` |
| `after=[]` | `()`，即根节点，不会生成空边 | `emerge/graph/edge.py:75` |
| `after="ab"`（传成字符串） | 被当成 `["a", "b"]`。builder 和 `GraphPatch` 都没有防这种写法，只有序列化升级时防了（`emerge/graph/serialization.py:141`） | — |

### start_status（`emerge/graph/graph.py:319-377`）

输入是图 `graph`，以及 `ended = {已结束节点 id: 结局，没有结局时为 None}`。函数只对不在 `ended` 里的节点给出 `READY` / `WAITING` / `NEVER`。正在运行的节点也会得到 `READY`，调用方只对 `pending` 的节点采取动作。`skipped` 节点不用传，传成 `None` 结果也一样。调用方有：`supervisor._advance`（启动节点或记为 skipped）、`supervisor.add_node`、`supervisor.update_start_edges`，以及 `mutations.preview`（dry run 时预报）。

| 概念 | 定义 | 位置 |
|---|---|---|
| 来源满足要求 | 结局不是 `None`，并且要求是"做完"，或者结局与要求相等 | `emerge/graph/graph.py:375-377` |
| 边已触发 | 边上有来源，且每个来源都在 `ended` 里并满足要求 | `emerge/graph/graph.py:360-364` |
| 边还可能触发 | 边上有来源，且每个来源要么已结束并满足要求，要么还没结束、自己也还可能启动 | `emerge/graph/graph.py:367-372` |
| 还可能启动（`possible`） | 从根节点出发反复扩展，直到不再变化：节点没有边，或者某条边还可能触发 | `emerge/graph/graph.py:339-348` |
| `READY` | 没有启动边，或者某条边已触发 | `emerge/graph/graph.py:351-352` |
| `WAITING` | 不是 `READY`，但还可能启动 | `emerge/graph/graph.py:353-354` |
| `NEVER` | 其余情况。会沿启动边往下传，环上的节点也落在这里 | `emerge/graph/graph.py:355-356` |

判定表（实验 `e4_start_status.py` 的实际输出）：

| 图 | `ended` | 结果 |
|---|---|---|
| a 是根，b 等 a 做完 | `{}` | a=READY，b=WAITING |
| 同上 | `a: "success"` | b=READY |
| 同上 | `a: None` | b=NEVER |
| b 等 `a[success]` | `a: "failure"` 或 `a: None` | b=NEVER |
| b 等 `a[success] and c` | 只有 `a: "success"` | b=WAITING（c=READY） |
| 同上 | `a: "success", c: None` | b=NEVER |
| 同上 | `a: "success", c: "x"` | b=READY |
| `v[success]`→p，`v[failure]`→r，r→q，x 有两条边（等 p，或等 r） | `v: "success"` | p=READY，r=NEVER，q=NEVER，x=WAITING |
| 同上 | `v: "failure"` | p=NEVER，r=READY，q=WAITING，x=WAITING |
| 同上 | `v: None` | p、r、q、x 全部 NEVER |
| 同上 | `v: "success", p: "ok"` | x=READY |
| 来源不在图里 | `{}` | NEVER |
| 来源不在图里，但在 `ended` 里且结局满足 | `{ghost: "ok"}` | READY：函数不检查来源是否在图里。`supervisor._ended()` 只传图内节点，实际运行中碰不到这种情况 |
| 只有一条空边 | `{}` | NEVER |
| 一条空边，另有一条等 a | `{}` | WAITING |
| 启动边成环 a↔b，c 在 a 后面 | `{}` | a、b、c 全部 NEVER（这种图校验本来就会拒绝） |
| a→b→c 一条链 | `a: None` | b、c 都是 NEVER |
| `ended` 里有图里没有的 id | — | 忽略 |
| 图里有重复 id | — | 按第一个出现的节点的边判定（`emerge/graph/graph.py:335`） |
| 一条边上同一来源出现两次（`a` 和 `a[success]`） | `a: "x"` | NEVER：两个要求都要满足。校验会报 `START_DUPLICATE_SOURCE` |

## graph.py

本文件定义不可变的图值对象和结构查询；要改图，只能通过 `patch.py` 生成新版本。图不保存运行状态，也不 import 注册表，外部读取信息由调用方通过 `reads_by_type` 传入。

### 数据结构

| 结构 | 字段 | 约束/默认 |
|---|---|---|
| `NodeSpec` | `id` | `NAME_PATTERN`，逻辑 id。订阅和启动边都指向它 |
| | `type` | 注册名，非空 |
| | `params` | `FrozenJson`，默认 `{}` |
| | `start_edges` | `tuple[StartEdge, ...]`，为空表示根节点 |
| | `description` | 主 Agent 写的实例说明，默认 `""` |
| | `instance_id` | 非空。缺少或为 `""` 时自动补成 `id@v0`（`emerge/graph/graph.py:65-71`）；`id` 不是字符串时不补 |
| | `code` | 生成代码类型固定的实现版本；配置模块里的类型为 `None` |
| `Replacement` | `node`、`version`（`>= 1`）、`old_instance_id`、`new_instance_id`、`old_type`、`new_type`、`old_code`、`new_code` | 一次替换在第 `version` 版提交 |
| `Graph` | `version`（`>= 0`，默认 0）、`nodes`、`subscriptions`、`replacements` | `replacements` 按时间顺序记录，节点被删除后记录仍保留 |
| `Affected`（frozen dataclass） | `subscriptions`、`start_dependencies`、`start_dependents`、`reads` | 只作为一次查询的返回值，不参与序列化 |
| `StartStatus` | `READY="ready"`、`WAITING="waiting"`、`NEVER="never"` | `StrEnum` |

`make_instance_id(node_id, version)` 返回 `"<id>@v<version>"`（`emerge/graph/graph.py:32-38`）。版本号在一次运行中只增不减，所以替换节点、删除后再加同名节点，拿到的都是从未出现过的实例 id；校验还会用 `DUPLICATE_INSTANCE` 兜底。用版本号派生 id 而不用随机 id，是为了让同一个 patch 作用在同一张图上时，结果逐字节相同（决策［阶段 4 补充：节点替换］）。

### 查询方法

| 方法 | 返回 | 边界情况 | 位置 |
|---|---|---|---|
| `node(id)` | 第一个 id 匹配的 `NodeSpec` | 不存在返回 `None`；有重复 id 时取第一个；线性扫描 | `emerge/graph/graph.py:107-111` |
| `node_ids()`、`len(g)` | 节点 id 元组、节点数 | — | `emerge/graph/graph.py:113-117` |
| `is_current(id, instance_id)` | 这个实例是不是该 id 的当前实例 | 节点不在图里返回 `False`。bus 用它丢弃旧实例的消息和退出（`emerge/runtime/bus.py:217-218`） | `emerge/graph/graph.py:121-128` |
| `replacements_of(id)` | 该 id 的替换历史，旧的在前 | 节点被删除后仍能查到 | `emerge/graph/graph.py:130-131` |
| `source_of(node, port)` | 喂给这个输入的订阅 | 没有返回 `None`；非法图里有多条时取第一条 | `emerge/graph/graph.py:135-141` |
| `consumers_of(node, port)` | 读这个输出的全部订阅 | — | `emerge/graph/graph.py:143-146` |
| `upstream(id)` / `downstream(id)` | 通过订阅相连的上游 / 下游节点，去重，按订阅顺序排列 | 只看订阅 | `emerge/graph/graph.py:148-154` |
| `start_edges_of(id)` | 该节点的启动边 | 节点不存在返回 `()` | `emerge/graph/graph.py:158-160` |
| `start_dependencies(id)` | 该节点所有启动边上的来源，去重，按声明顺序 | 包括图里不存在的 id | `emerge/graph/graph.py:162-164` |
| `start_dependents(id)` | 启动边来源里包含它的节点 | — | `emerge/graph/graph.py:166-172` |
| `node_ids_without_start_dependencies()` | 没有启动边的节点，即根节点 | 只有一条空边的节点不算根；重复 id 会被列两次 | `emerge/graph/graph.py:174-176` |
| `upstream_closure(ids)` / `downstream_closure(ids)`，可选参数 `include_start_edges=True` | 直接或间接依赖的节点 / 被依赖的节点，排好序的元组，包含起点本身 | 起点里不在图中的 id 会被忽略；用队列加 `seen` 集合迭代，遇到订阅环能正常结束（实验：b、c 之间有订阅环，`downstream_closure(["a", "nope"])` 返回 `("a", "b", "c")`）；`include_start_edges=False` 时只看订阅 | `emerge/graph/graph.py:180-216` |
| `affected_by(id, port=None, *, reads_by_type=None)` | `Affected`：碰到这个节点（或它某个端口）的订阅、它的启动依赖、依赖它启动的节点、它的外部读取 | `port` 同时按输入端口名和输出端口名过滤订阅，启动边部分不受 `port` 影响；`reads_by_type` 为空或 `None` 时 `reads=()` | `emerge/graph/graph.py:220-244` |
| `external_reads(reads_by_type, node_ids=())` | `(节点 id, 活源名)` 列表 | `node_ids` 为空时取全部节点 | `emerge/graph/graph.py:246-258` |
| `start_edge_cycles()` | 启动边上的环，每个环一条闭合路径 | 见下文 | `emerge/graph/graph.py:262-284` |
| `str(g)` | 第一行 `graph vN: …`，之后节点、订阅、`a[success] and c ..> b (start)`、`t@v0 => t@v1 (replaced at v1)` 各占一行 | — | `emerge/graph/graph.py:305-316` |

### 只读保证

| 保证 | 做法 | 位置 |
|---|---|---|
| 模型不能改 | `NodeSpec`、`Graph`、`Replacement`、`Subscription`、`StartEdge`、`StartSource` 都是 `frozen=True`、`extra="forbid"`，给字段赋值会抛 `ValidationError` | `emerge/graph/graph.py:41-42`、`emerge/graph/edge.py:26` |
| 参数不能改 | `params` 是 `FrozenJson`。实验：`params["a"].append(2)` 抛 `TypeError: FrozenList is read-only` | `emerge/graph/graph.py:50` |
| 改图只产生新对象 | `apply_graph_patch` 从不修改传入的图，新旧版本共用没变的 `NodeSpec` | `emerge/graph/patch.py:142-174` |
| 不缓存索引 | `model_copy(update=...)` 会把缓存一起复制，导致缓存过期；图只有几十个节点，所以直接线性扫描 | `emerge/graph/graph.py:108-110` |

### 启动边成环检测（`start_edge_cycles`）

1. 先剥离不在环上的节点：统计每个节点在图内有多少个不同的启动依赖（不在图里的来源不计数），从计数为 0 的节点开始逐个"完成"，每完成一个就把依赖它的节点计数减一；最后计数仍大于 0 的节点就是"残余"，它们在环上或在环后面（`emerge/graph/graph.py:286-301`）。
2. 把残余按 id 排序，从每个点出发，每一步走到"第一个也在残余里的依赖"，直到回到走过的点，截出一条闭合路径（`emerge/graph/graph.py:271-279`）。
3. 按环的节点集合去重（`emerge/graph/graph.py:280-283`）。

| 图 | 结果（实验 `e3_graph.py`、`e3b_cycles.py`） |
|---|---|
| x 等 x | `[("x", "x")]` |
| q 的第二条边等自己 | `[("q", "q")]` |
| a↔b 和 c↔d，两个环不相交 | 两个都报 |
| a↔b 和 a↔c，共用节点 a | 只报 a 先走到的那一个（见疑点 1） |
| 环后面的节点 d | 不单独报 |
| 来源不在图里 | 不算环 |

## registry.py

本文件记录有哪些节点类型、各自从哪里来、怎样在另一个进程里重新加载。它不是纯函数，会 import 模块或 exec 文件。调用方有：`agent/tools/graph.py` 的 `register_node_code`（先在临时注册表里试注册一次，再注册到正式注册表）、`runtime/process.py:224-247`（子进程用新的注册表按 locator 重新加载）、`runtime/checkpoint.py:180-192`（恢复运行时补登实例固定的旧版本）、`validation.py`（`find_entry`）、builder 和 patch（`pin`）。

### 数据

| 结构 | 字段 | 说明 |
|---|---|---|
| `NodeLocator` | `origin`（`"module"` 表示配置模块，`"file"` 表示生成代码）、`module`、`path`、`version`、`digest` | `path`、`version`、`digest` 只对 `"file"` 有值。`digest` 是注册时文件字节的 sha256；子进程加载前会先比对（`emerge/runtime/process.py:230-241`），文件内容被改过就拒绝加载 |
| `NodeEntry` | `name`、`cls`、`locator`、`params_schema` | `description`、`inputs`、`outputs`、`reads`、`irreversible`、`outcomes`、`params_model`、`version` 都从类或 locator 读取。`params_schema` 是 `Params.model_json_schema()`，没有 `Params` 时为 `{"type": "object", "properties": {}}`。`to_dict()` 是目录视图（schema 会被深拷贝） |
| `_entries` | 名字 → 当前实现 | 这就是目录 |
| `_versions` | `(名字, 版本)` → 条目 | 只记录 file 来源的条目。实例固定在其中某个版本上，注册新版本不影响已放置的旧实例 |
| `loading` | 正在执行的文件的 locator | 装饰器据此把来源记为 file |

### 查询

| 方法 | 行为 | 找不到时 |
|---|---|---|
| `find_entry(name, code=None)` | `code` 为 `None` 时返回目录里的当前实现；否则查 `_versions[(name, code)]` | 返回 `None` |
| `require_entry(name, code=None)` | 同上 | `RegistryError: node type 'zzz' is not registered`，带版本时为 `'a version 3'` |
| `versions(name)` | 该名字注册过的生成代码版本，旧的在前 | 返回 `()`；模块类型也返回 `()` |
| `pin(name, code=None)` | 给了 `code` 就原样返回，不检查是否存在（不存在由校验报 `UNKNOWN_CODE_VERSION`）。没给时：当前实现是 file 来源就返回它的版本，模块来源或未注册返回 `None` | — |
| `names()`、`catalog()`、`in`、`len()` | 只看目录 | — |
| `external_reads_by_node_type()` | `{类型: reads 名字元组}` | — |

### register(cls, locator)（`emerge/graph/registry.py:172-204`）

| 情况 | 处理 | 位置 |
|---|---|---|
| `name` 为空或全是空白 | `RegistryError: node class X has no name` | `emerge/graph/registry.py:173-174` |
| `outcomes` 为空 | `RegistryError: node class 'x' declares no outcomes; list the ones its run() can return` | `emerge/graph/registry.py:177-180` |
| 同一个类对象再注册一次 | 返回已有条目，locator 保持原样（即使这次传了不同的 locator） | `emerge/graph/registry.py:183-184` |
| 同名但不是同一个类，且任一方是 module 来源 | `RegistryError: node type 'a' is registered twice: m1 and m2`。配置模块的名字归配置所有，生成代码也不能占用 | `emerge/graph/registry.py:188-191` |
| 同名但不是同一个类，双方都是 file 来源 | 新的成为当前实现 | `emerge/graph/registry.py:203` |
| file 来源，且同一 `(名字, 版本)` 已登记但 digest 不同 | `RegistryError: ... version '1' is already registered with different code; register the change under a new version` | `emerge/graph/registry.py:194-201` |
| file 来源正常注册 | 同时记入 `_versions` 和目录 | `emerge/graph/registry.py:202-203` |

### load_modules(modules)（`emerge/graph/registry.py:206-218`）

| 情况 | 处理 |
|---|---|
| 正常 | 临时把自己设为 active registry，逐个 import 模块；模块里加了 `@register_node` 的类会被注册，没加装饰器的不会 |
| 某个模块 import 失败 | `RegistryError: cannot import node module 'x': ModuleNotFoundError: ...`。失败之前已 import 的模块所注册的类会保留，不回滚（实验 `e16_misc.py`）；active registry 会恢复 |
| 调用方 | 仓库里目前没有调用方。按计划由应用入口用 `config.nodes` 调用（阶段 9） |

### register_from_file(path, version)（`emerge/graph/registry.py:220-268`）

1. `version` 去掉空白后为空，抛 `RegistryError: a generated node version must not be empty`。
2. 把路径解析成绝对路径；不是文件则抛 `RegistryError: node file not found: ...`。
3. 只读一次文件字节，算 sha256。模块名为 `emerge_generated_nodes.<stem>_v<版本标签>_<sha1(路径, 版本, digest) 前 10 位>`。路径、版本、内容任一变化，模块名就不同（`emerge/graph/registry.py:278-284`）。版本标签中非 `[A-Za-z0-9_]` 的字符换成 `_`，并去掉开头的 `v`，例如 `v1.2-beta` 得到 `1_2_beta`。
4. 如果同名模块已在本注册表登记过（说明文件、版本、内容都相同），直接把这些条目设为当前实现，把同一路径下的其他旧条目移出目录，然后返回，不重复执行文件。
5. 给目录做快照，临时把自己设为 active registry，并把 `loading` 设为这个文件的 locator。
6. `compile(source, dont_inherit=True)` 后 exec。执行的就是算过摘要的那份字节。文件里的装饰器按 file 来源注册到本注册表（`emerge/graph/registry.py:287-300`）。
7. 扫描模块里所有"定义在本模块内的 `Node` 子类"并逐个注册，没加装饰器的类和公共基类也会被扫到（`emerge/graph/registry.py:254-256`）。
8. 第 6、7 步抛出任何异常：恢复快照，从 `sys.modules` 删掉这个模块，再把异常抛出。exec 里抛的异常会被包成 `RegistryError: cannot load node file ...: <原类型>: <原信息>`。
9. 无论成功与否，都恢复原来的 `loading` 和 active registry。
10. 一个条目都没注册到，抛 `RegistryError: node file ... registered no node classes`。
11. 把同一路径下旧版本的条目移出目录（按版本仍可查到），返回本次注册的条目。

| 情况（实验 `e2_registry.py`） | 结果 |
|---|---|
| 同一文件、同一版本、同样内容注册两次 | 复用已有模块，不新建模块对象 |
| 同一版本，但文件内容改了 | `RegistryError`（包了一层：`cannot load node file ...: RegistryError: node type 'g' version '1' is already registered with different code ...`），目录保持不变 |
| 新版本 | 新版本成为当前实现；旧版本按 `find_entry(name, "1")` 仍能查到；两个模块都留在 `sys.modules` 里，正在运行的旧进程不受影响 |
| 新版本的文件去掉了某个类 h | h 退出目录，`find_entry("h", 旧版本)` 仍能查到 |
| 语法错误 | `RegistryError: cannot load node file ...: SyntaxError: ...` |
| 文件先注册了一个类，后面又抛异常 | 整批回滚，这个类不在目录里，也不在 `_versions` 里 |
| 文件里没有 `Node` 子类 | `RegistryError: ... registered no node classes`。这个合成模块仍留在 `sys.modules` 里，因为这条路径没有清理；它无害，下次加载会覆盖 |
| 文件里有一个没写 `name` 的公共基类 | `RegistryError: node class Shared has no name`，整批回滚（见疑点 4） |
| 文件里的类名字与配置模块已注册的名字相同 | `RegistryError: node type 'a' is registered twice: m1 and emerge_generated_nodes...` |
| 一个文件里两个类用了同一个 `name` | 后一个静默覆盖前一个 |

### 装饰器与 active registry

| 接口 | 行为 | 位置 |
|---|---|---|
| `@register_node` | 注册到 `_RegistryHolder.current`。locator 优先用该注册表的 `loading`（文件加载期间），否则为 `NodeLocator(origin="module", module=cls.__module__)` | `emerge/graph/registry.py:321-326` |
| `get_active_registry()` / `set_active_registry(r)` | 读取 / 替换当前注册表；`set_active_registry` 返回之前的那个。builder、`apply_graph_patch`、`RuntimeService`、supervisor 在没传 registry 时都用它 | `emerge/graph/registry.py:329-337` |

## builder.py

这是一个可变、短生命周期的组装器，`build()` 时才校验并冻结成 `Graph`。仓库里它只被 `spec.graph_from_graph_spec` 使用，Runtime 不用 builder。

| 方法 | 行为 | 边界情况 |
|---|---|---|
| `GraphBuilder(registry=None)` | 默认使用 `get_active_registry()` | — |
| `add_node(id, type, *, params=None, start_edges=(), after=(), description="", code=None)` | 追加一个 `NodeSpec`（存在列表里，不去重）：`params` 为 `None` 时取 `{}`；启动边是 `start_edges` 加上由 `after` 换算出的一条边；`code = registry.pin(type, code)`；实例 id 为 `@v0` | 重复 id 不会覆盖前一个，`build()` 时报 `DUPLICATE_ID`，同时报 `DUPLICATE_INSTANCE`（两者都是 `@v0`） |
| `connect_ports("n.p", "n.p")` | 解析端点；完全相同的订阅只记一次 | 端点格式错误时立即抛 `GraphValidationError`；同一输入接了两个不同来源，`build()` 时报 `INPUT_MULTIPLE_SOURCES` |
| `connect_port_pairs(pairs)` | 对每一对调用 `connect_ports` | — |
| `declare_external_read(id, source)` | 只记在 builder 里，不进 `Graph` | 对不存在的 id 声明的会被忽略 |
| `external_reads_by_node_type()` | 注册表里的 `reads` 并上实例级声明，按类型合并：一个实例的声明会算到同类型的所有实例上，这是一种安全方向的过近似 | — |
| `validate_graph()` | 对 `build_unvalidated_graph()` 的结果调用 `validate` | — |
| `build()` | 有 error 时抛 `GraphValidationError: graph has N error(s):\n  CODE: msg`；warning 写进 `emerge.graph.builder` 日志；返回 `version=0` 的图 | — |
| `build_unvalidated_graph()` | 不校验，直接组装成图 | — |
| `build_graph(nodes, connections=(), *, registry=None)` | 简写：接受 `(id, type)` 对和端点对 | — |

## validation.py

`validate(graph, catalog)` 返回 `ValidationResult`，自身不抛异常。调用方有 `builder.build`、`apply_graph_patch`，以及间接经由 builder 的 `graph_from_graph_spec`。`catalog` 只需要满足一个 Protocol，即实现 `find_entry(name, code)`（`emerge/graph/validation.py:91-94`）。每个实例按它固定的版本查找类型（`emerge/graph/validation.py:100`）。

### 错误码总表

| 错误码 | 触发条件 | 级别 | Issue 上的定位字段 | 位置 |
|---|---|---|---|---|
| `DUPLICATE_ID` | 同一个 id 再次出现，每多出现一次报一条 | error | `node` | `emerge/graph/validation.py:129-136` |
| `DUPLICATE_INSTANCE` | `instance_id` 在图内重复，或者等于替换历史里某个 `old_instance_id`（旧实例不能复活，否则它的旧消息会重新算数） | error | `node` | `emerge/graph/validation.py:139-154` |
| `UNKNOWN_TYPE` | 类型没注册；写了 `code` 但类型本身也没注册时，同样报这个 | error | `node` | `emerge/graph/validation.py:115-116` |
| `UNKNOWN_CODE_VERSION` | 写了 `code`，类型已注册但没有这个版本（包括给模块类型写了 `code`） | error | `node` | `emerge/graph/validation.py:107-114` |
| `PARAM_INVALID` | 类型没有 `Params` 但 `params` 非空，报 `node type takes no parameters`；`Params` 校验失败，报 `loc: msg; loc: msg`。类型未知时不检查参数 | error | `node` | `emerge/graph/validation.py:157-170` |
| `ENDPOINT_MISSING` | 订阅的来源或目标节点不在图里；节点类型已知，但没有这个输出 / 输入端口。端点所在节点类型未知时不报（端口无从得知，避免同一原因报两次） | error | `subscription` | `emerge/graph/validation.py:180-187` |
| `INPUT_MULTIPLE_SOURCES` | 同一个（节点，输入端口）出现第二条订阅，完全相同的重复订阅也算；端点缺失时也照样检查 | error | `node`、`port`、`subscription` | `emerge/graph/validation.py:188-199` |
| `TYPE_TAG_MISMATCH` | 两端端口都能找到，但 `type_tag` 不同 | **warning** | `subscription` | `emerge/graph/validation.py:200-210` |
| `START_EDGE_EMPTY` | 某条启动边没有来源（消息里带边的序号） | error | `node` | `emerge/graph/validation.py:229-236` |
| `START_DUPLICATE_SOURCE` | 一条边里同一个来源出现不止一次，不管要求的结局是否相同 | error | `node` | `emerge/graph/validation.py:237-245` |
| `START_UNKNOWN_NODE` | 来源不在图里，每出现一次报一条 | error | `node` | `emerge/graph/validation.py:246-255` |
| `START_UNKNOWN_OUTCOME` | 要求的结局不在来源类型（按来源实例固定的版本）声明的结局里。来源类型未知时不报 | error | `node` | `emerge/graph/validation.py:256-268` |
| `START_CYCLE` | `start_edge_cycles()` 返回的每个环报一条，消息为 `start edges form a cycle: p -> q -> p` | error | `node`=路径起点，`path`=完整闭合路径 | `emerge/graph/validation.py:269-277` |
| `REPLACEMENT_PORT_MISMATCH` | 只由 `apply_graph_patch` 产生，见 patch 一节 | error | `subscription` | `emerge/graph/patch.py:341-369` |

### 其他行为

| 情况 | 处理 | 位置 |
|---|---|---|
| issue 的顺序 | 依次为：`DUPLICATE_ID` → `DUPLICATE_INSTANCE` → 逐个节点的类型 / 版本 / 参数 → 逐条订阅（来源缺失、目标缺失、多来源、标签不同）→ 逐条启动边（空边、重复来源、未知节点、未知结局）→ 环 | `emerge/graph/validation.py:97-122` |
| 级别怎么定 | `Issue.severity` 由错误码推出，只有 `TYPE_TAG_MISMATCH` 是 warning | `emerge/graph/validation.py:48`、`62-64` |
| 结果接口 | `errors`、`warnings`、`ok`（没有 error 即为真）；`raise_if_invalid()` 抛 `GraphValidationError: graph has N error(s):\n  CODE: msg` | `emerge/graph/validation.py:67-88` |
| 有重复 id 时的类型查询 | `types` 以 id 为键，同 id 的节点都按最后一个的类型检查。实验中把已注册的类型报成了 `unknown node type 'src'`（见疑点 6） | `emerge/graph/validation.py:100` |
| 两个实例固定在不同版本上 | 各自按自己的版本检查结局和端口。实验：k 固定在 v1，v2 才有的结局 `bad` 报 `START_UNKNOWN_OUTCOME` | `emerge/graph/validation.py:100`、`256-258` |

## patch.py

把改图表示为数据，流程是"计算 → 校验 → 提交"：先算出候选结构，整体校验通过后才生成新版本；任何一步失败都抛 `GraphValidationError`，原图不变。调用方有：`supervisor.apply_patch`（运行中改图，`emerge/runtime/supervisor.py:453`）、`supervisor` 恢复运行时把中断的节点换成新实例（`emerge/runtime/supervisor.py:282-292`）、`mutations.preview`（dry run）、`mutations.graph_file_status`。`REPLACEABLE_STATES` 和 `TERMINAL_STATES` 只在这里定义，`runtime/state.py:35-36` 从这里导入。

### Edit 与构造方法

| 构造方法 | `op` | 读取的字段 |
|---|---|---|
| `GraphPatch.add_node(id, type, *, params=None, start_edges=(), after=(), description="", code=None)` | `add_node` | `node`、`type`、`params`（`None` 取 `{}`）、`start_edges`（含 `after` 换算出的边）、`description`、`code` |
| `GraphPatch.remove_node(id)` | `remove_node` | `node` |
| `GraphPatch.replace_node(id, type=None, *, params=None, description=None, code=None)` | `replace_node` | `type`、`params`、`description` 为 `None` 时沿用原值；`code` 为 `None` 时固定到应用 patch 时的当前版本 |
| `GraphPatch.replace_node_params(id, params)` | `update_params` | `params`（整组替换，不合并；`None` 当作 `{}`） |
| `GraphPatch.set_node_description(id, description)` | `set_description` | `description`（`None` 当作 `""`） |
| `GraphPatch.connect(source, target)` / `disconnect(source, target)` | `connect` / `disconnect` | `source`、`target`，都是 `"node.port"` 格式 |
| `GraphPatch.replace_start_edges(id, start_edges=(), *, after=())` | `set_start_edges` | `start_edges`（整组替换；为空则节点成为根） |
| `p.combine_patches(*others)` | — | 把各 patch 的 edit 按顺序拼接 |
| `bool(p)` | — | 有没有 edit |

`Edit` 是 `frozen=True`、`extra="forbid"`，`op` 只接受上面 8 种，旧的 `set_after` 会报 `ValidationError`。

### apply_graph_patch(graph, patch, registry=None, *, states=None)（`emerge/graph/patch.py:142-174`）

1. patch 里没有 edit：原样返回同一个图对象，版本号不变（`emerge/graph/patch.py:156-157`）。
2. 建草稿：`version = graph.version + 1`；`registry` 默认取 active registry；`states` 里没有的节点一律按 `pending` 处理（离线调用本来就没有运行状态）。
3. 按顺序应用每个 edit。只要有一个前提不满足，就立即抛 `GraphValidationError`，消息里不带错误码，后面的 edit 也不再处理。
4. 组装候选图：被替换的节点留在原位置，新增的节点排到末尾，被删的去掉；`replacements` 是原有记录加上本次新增的记录（实验 `e16_misc.py`）。
5. 对候选图调用 `validate`，再加上替换的端口兼容检查；有 error 时抛 `GraphValidationError: patch rejected at version N: CODE: msg; CODE: msg`。
6. warning 写进 `emerge.graph.patch` 日志。
7. 返回候选图，版本号加 1。

### 各种 op（报错都是 `GraphValidationError`）

| op | 允许的节点状态 | 效果 | 报错条件 | 位置 |
|---|---|---|---|---|
| `add_node` | 新节点，不看状态 | 新建 `NodeSpec`，`instance_id = id@v<新版本>`，`code = registry.pin(type, code)` | id 已存在：`node 'a' already exists`；本 patch 里刚删过这个 id：`node 'c' is removed by this patch; use replace_node to put a new instance behind it`；缺 `node` / `type`：`patch edit is missing 'node'` | `emerge/graph/patch.py:194-213` |
| `remove_node` | 只允许 `pending` | 删除节点及其全部订阅（两个方向都删，包括接进已结束节点的订阅：从没启动过的节点没发过消息，已结束节点的记录不会丢东西）。别的 pending 节点启动边里对它的引用保持不动，最后由校验报 `START_UNKNOWN_NODE`，必须在同一个 patch 里改掉这些引用（静默清掉会让原本在等的节点立刻启动） | 已开始还没结束：`node 't' is running: only a node that has not started can be removed; stop it instead; it then stays in the graph`；已终止：`... ; it stays in the graph as it ended`；本 patch 已替换它：`... is replaced by this patch and cannot also be removed`；它出现在某个非 pending 节点（含 `skipped`）的启动边里：`node 't' is named in the start edges of 'c', which are no longer pending and cannot change, so it cannot be removed`；不在图里：`node 'x' is not in the graph` | `emerge/graph/patch.py:214-246` |
| `replace_node` | `pending`、`starting`、`running`、`paused` | 见下文"替换" | `stopping` 或终止状态：`node 't' is stopping: only a node that has not started or is still running can be replaced or given new parameters`（`stopping` 的进程已经在退出）；本 patch 里已经给它分配过新实例：`node 't' already gets a new instance in this patch`；不在图里 | `emerge/graph/patch.py:286-319` |
| `update_params` | `pending`：原地修改。`starting` / `running` / `paused`：转成 `replace_node`，得到新实例，`code` 保持原来固定的版本 | 整组替换参数 | 终止状态：`node 't' is finished: an ended node stays as it ended, so its parameters cannot change`；`stopping`：走替换那条路，报 `... is stopping: only a node that has not started or is still running can be replaced or given new parameters`；已开始的节点在同一 patch 里第二次拿新实例：`already gets a new instance` | `emerge/graph/patch.py:249-259` |
| `set_description` | 除终止状态外都允许，包括 `stopping` | 原地修改描述 | 终止状态：`... so its description cannot change` | `emerge/graph/patch.py:260-263` |
| `connect` | 目标节点不能是终止状态；来源节点任意状态（已结束节点的输出照样可以接） | 追加一条订阅；如果完全相同的订阅已存在，什么也不做，也不检查状态 | 目标已终止：`node 't' is finished: an ended node stays as it ended, so its inputs are part of what it ended with`；端点格式错误；目标输入已有别的来源，最后校验报 `INPUT_MULTIPLE_SOURCES`（需要在同一 patch 里先 `disconnect` 旧的） | `emerge/graph/patch.py:264-268` |
| `disconnect` | 目标节点不能是终止状态；来源节点任意状态 | 删除这条订阅 | 订阅不存在：`no subscription a.aux -> t.inp`；目标已终止：同 `connect` | `emerge/graph/patch.py:269-276` |
| `set_start_edges` | 只允许 `pending` | 整组替换启动边 | 非 pending：`node 't' is running: start edges can only change before the node starts` | `emerge/graph/patch.py:277-284` |

op × 状态（实验 `e6_patch_matrix.py`，表中的节点 t 是被操作的节点）：

| op | `pending` | `starting` / `running` / `paused` | `stopping` | `finished` / `failed` / `stopped` / `skipped` |
|---|---|---|---|---|
| `remove_node` | 允许 | 拒绝 | 拒绝 | 拒绝 |
| `replace_node` | 允许，新实例 | 允许，新实例 | 拒绝 | 拒绝 |
| `update_params` | 允许，同一实例（diff 里是 `modified ['params']`） | 允许，新实例（diff 里是 `replaced`） | 拒绝 | 拒绝 |
| `set_description` | 允许 | 允许 | 允许 | 拒绝 |
| `connect` / `disconnect`（t 是目标） | 允许 | 允许 | 允许 | 拒绝 |
| `connect` / `disconnect`（t 是来源） | 允许 | 允许 | 允许 | 允许 |
| 重复 `connect` 一条已有的订阅 | 允许：结构不变，版本号仍加 1 | 同左 | 同左 | 同左 |
| `set_start_edges` | 允许 | 拒绝 | 拒绝 | 拒绝 |

### 同一 patch 内的组合

| 组合（实验 `e7_patch_cases.py`） | 结果 | 原因 |
|---|---|---|
| 先 `remove_node` c，再 `add_node` c | 拒绝：`is removed by this patch; use replace_node ...` | 防止绕过状态检查和替换历史 |
| 先 `add_node` x，再 `replace_node` x | 拒绝：`already gets a new instance in this patch` | 实例 id 由版本号派生，第二个会和第一个重复（`emerge/graph/patch.py:188-190`、`335-338`） |
| 对同一节点 `replace_node` 两次 | 拒绝，原因同上 | 同上 |
| 先 `replace_node` c，再 `remove_node` c | 拒绝：`is replaced by this patch and cannot also be removed` | — |
| 先 `remove_node`，再 `replace_node` / `remove_node` 同一节点 | 拒绝：`is not in the graph` | — |
| 对 running 节点 `update_params` 两次，或 `update_params` 加 `replace_node` | 拒绝：`already gets a new instance` | 已开始的节点改参数就要换实例 |
| 对 pending 节点 `update_params` 两次 | 允许，以后一次为准 | 原地修改，不占用新实例 |
| 先 `connect` 新来源，再 `disconnect` 旧来源 | 允许，diff 里是一条 `rewired` | 只校验最终结果 |
| 只 `connect` 第二个来源 | 拒绝：`INPUT_MULTIPLE_SOURCES` | — |

### 版本号

| 情况 | 版本号 |
|---|---|
| 空 patch | 不变，返回原对象 |
| 任意 edit 的前提不满足，或最终校验有 error | 抛异常，原图和版本号都不变 |
| 成功 | 加 1。本次新建的实例 id 都是 `@v<新版本>`，替换记录的 `version` 也是新版本号 |
| 非空、但实际什么也没改的 patch（重复 `connect`、写入相同的描述） | 仍然加 1，diff 为空（见疑点 5） |

### 替换（`replace_node`，以及已开始节点的 `update_params`）

| 项目 | 替换后 | 位置 |
|---|---|---|
| 逻辑 `id` | 不变，所以订阅和启动边原样保留（它们指向的是逻辑 id） | `emerge/graph/patch.py:297-302` |
| `instance_id` | 新值 `id@v<新版本>` | `emerge/graph/patch.py:304` |
| `start_edges` | 原样保留，即使类型变了也一样 | `emerge/graph/patch.py:302` |
| `type` | 给了就用新的，否则沿用 | `emerge/graph/patch.py:296` |
| `params` | 给了（包括 `{}`）就用新的，否则沿用。按新类型的 `Params` 校验，所以换类型时沿用旧参数，可能报 `PARAM_INVALID` | `emerge/graph/patch.py:301` |
| `description` | 给了就用新的，否则沿用（阶段 8 的工具要求必填，这里只是兜底） | `emerge/graph/patch.py:303` |
| `code` | `registry.pin(type, code)`：给了就用给的；没给就取当前版本，可能不是原来固定的那个版本 | `emerge/graph/patch.py:305` |
| 替换历史 | 追加一条 `Replacement`，记录新旧实例、新旧类型、新旧代码版本 | `emerge/graph/patch.py:308-319` |

端口兼容检查（`_replacement_ports`，`emerge/graph/patch.py:341-384`）只检查"替换前后都存在、且一端是被替换节点"的订阅：

| 情况 | 结果 |
|---|---|
| 保留的订阅在替换后找不到端口 | `ENDPOINT_MISSING`（普通校验报出），整个 patch 拒绝 |
| 替换前两端标签相同，替换后不同 | `REPLACEMENT_PORT_MISMATCH: t.out -> c.inp carried 'img', but after the replacement it connects 'mask' to 'img'; rewire it in the same patch`，整个 patch 拒绝 |
| 替换前标签就不同 | 仍只报 warning `TYPE_TAG_MISMATCH`，不把原有的问题算到这次替换头上 |
| 在同一 patch 里改接到别处 | 原订阅不再保留，不检查；新订阅按普通规则校验，标签不同只报 warning |
| 某端类型未知 | 跳过，由 `UNKNOWN_TYPE` 报 |

`code` 省略时取哪个版本：

| 入口 | 取值 |
|---|---|
| `GraphBuilder.add_node`、`GraphPatch.add_node` | 当前版本 |
| `GraphPatch.replace_node` | 新类型（或原类型）的当前版本 |
| 对已开始的节点 `update_params` | 原来固定的版本（`emerge/graph/patch.py:259`） |
| 图文件：类型不变且不写 `code` | 原来固定的版本；类型变了则取新类型的当前版本（`emerge/graph/spec.py:366`） |
| 恢复运行时替换中断的节点（Runtime） | 显式传入原来固定的版本（`emerge/runtime/supervisor.py:283-287`） |
| 写了一个没注册的版本，或给模块类型写了 `code` | 校验报 `UNKNOWN_CODE_VERSION` |

### diff_graphs(old, new)（`emerge/graph/patch.py:453-515`）

Runtime 根据 diff 决定启停哪些进程（`supervisor.apply_patch`），并据此给主 Agent 回报提交结果。

| 字段 | 含义 | 顺序 |
|---|---|---|
| `nodes.added` / `nodes.removed` | 按 id 比较新增和删除的节点 | 按 id 排序 |
| `nodes.modified` | `NodeModification(node, fields)`：实例没变，但有字段变了。`fields` 按 `NodeSpec` 的声明顺序排列，实际出现的是 `params`、`start_edges`、`description` 的组合。Runtime 对它们分别处理：参数要换进程，启动边要对照已有结局，描述只需要显示 | 按 id 排序 |
| `nodes.replaced` | 实例变了的节点。能在 `new.replacements` 里找到对应 `(旧实例, 新实例)` 的记录就直接用；找不到时（跨了两次替换，或跨 patch 先删后加同一个 id）按两端合成一条，`version=max(new.version, 1)` | 按 id 排序 |
| `subscriptions.rewired` | 同一个输入端口换了来源，记成 `(旧订阅, 新订阅)` 一条，不算成一删一增 | 按目标排序 |
| `subscriptions.added` / `removed` | 其余新增 / 删除的订阅 | 按图里订阅列表的顺序，不排序（docstring 写的是 sorted） |
| `bool(diff)` | 上面任一项非空即为真 | — |

### 新边遇到已结束的节点

patch 不接收结局，所以"新加的启动边是否已经满足"由调用方拿已发生的结局调用 `start_status` 判断（`supervisor.add_node` / `update_start_edges`，`mutations.preview`）。

| 情况 | 结果（实验 `e7_patch_cases.py`、`e10_spec_edits.py`） |
|---|---|
| 新订阅从已结束节点的输出接出 | 允许：已结束节点的输出仍然可读 |
| 新订阅接进已结束节点的输入，或断开它的输入 | 拒绝 |
| 新节点（或 pending 节点的新启动边）等一个已结束节点，要求的结局与实际相符或要求为"做完" | patch 通过；`start_status` 返回 READY，Runtime 会立即启动它 |
| 同上，但结局不符，或来源没有结局 | patch 通过；`start_status` 返回 NEVER，Runtime 记为 `skipped` |
| 删除一个 pending 节点，而它的输出接进了某个已结束节点 | 允许，那条订阅随之删除（`emerge/graph/patch.py:240-246`） |
| 删除一个节点，而某个非 pending 节点的启动边里引用了它 | 拒绝 |

## serialization.py

存储用的完整、无损格式，覆盖 `Graph` 和 `GraphPatch`。`storage/sqlite.py:732-744` 用 `dumps_graph` / `loads_graph` 存取图，`runtime/mutations.py:180` 用 `patch_to_dict` 把 patch 写进 `graph.patched` 事件。它和主 Agent 编辑的图文件（`spec.py`）是两种格式，互不替代。

### 输出

| 键 | 内容 |
|---|---|
| `schema_version` | 3 |
| `version` | 图版本号 |
| `nodes[]` | `id`、`type`、`params`（thaw 后的普通值）、`start_edges`（`[{"sources": [{"node", "outcome"}]}]`）、`description`、`instance_id`、`code` |
| `subscriptions[]` | `source_node`、`source_port`、`target_node`、`target_port` |
| `replacements[]` | `Replacement.model_dump()` |
| patch 的 `edits[]` | 9 个字段全写：`op`、`node`、`type`、`params`、`start_edges`、`description`、`code`、`source`、`target`。`None` 写成 `null`；`replace_node` 的 `params` / `description` 为 `null` 时表示"沿用"，往返后仍是 `None`，不会变成 `{}` |

`dumps_graph` / `dumps_patch` 使用 `sort_keys=True`、`ensure_ascii=False`，列表保持图里的顺序。同一张图两次输出逐字节相同；实验中 v3 往返后对象相等、字节相同。

### 读取

| 情况 | 处理 | 位置 |
|---|---|---|
| 没有 `schema_version`，或者是 `4`、`"3"`（字符串） | `GraphValidationError: unsupported schema_version None; expected one of [1, 2, 3]` | `emerge/graph/serialization.py:124-128` |
| `schema_version` 写成 `1.0` 或 `true` | 按 1 读：集合成员判断按相等比较 | `emerge/graph/serialization.py:126` |
| JSON 语法错误 | `GraphValidationError: invalid graph JSON: ...`（patch 为 `invalid patch JSON`） | `emerge/graph/serialization.py:77-80`、`117-120` |
| 顶层不是对象（`[]`、`null`） | `AttributeError`，没有转成 `GraphValidationError`（见疑点 7） | `emerge/graph/serialization.py:125` |
| 顶层有未知键 | 忽略：只取 `version`、`nodes`、`subscriptions`、`replacements` | `emerge/graph/serialization.py:61-66` |
| 节点 / 订阅里有未知字段 | `GraphValidationError: invalid graph data: ...`（来自 `extra="forbid"`） | `emerge/graph/serialization.py:68-69` |
| 缺 `version` | 取 0 | `emerge/graph/serialization.py:62` |
| 读入时不做图结构校验 | 只做模型校验，不查注册表 | — |

### 版本升级

| `schema_version` | 节点 | edit |
|---|---|---|
| 1 | 没有 `instance_id`，由 `NodeSpec` 的校验器补成 `id@v0`；没有 `code`，取 `None`；没有替换历史 | — |
| 2 | 带 `instance_id`、`code`、`replacements` | — |
| 1、2 共同 | `after`（list 或 tuple）转成一条来源全为"做完"的边（去重）；`after` 不是 list / tuple（例如字符串）时报 `GraphValidationError: invalid 'after' in schema 2 data: 'ab'`；没有 `after` 键的节点原样保留（`emerge/graph/serialization.py:135-145`） | `after` 同样转换；`op: set_after` 改成 `set_start_edges`，没写 `after` 时得到空的 `start_edges`，即成为根节点（`emerge/graph/serialization.py:148-155`） |
| 3 | `start_edges`、`description`。出现 `after` 按未知字段报错，不做隐式升级 | `op: set_after` 不合法，报 `invalid patch data` |

## spec.py

图文件 `emerge.graph-spec/v1` 是主 Agent 读写的图：它只写主 Agent 该决定的内容，交回一份改过的文件就等于提交一个差异 patch。哪些修改允许，只在 `patch.py` 里规定一次，文件做不到 patch 做不到的事。Runtime 负责把文件写到 `runs/<run_id>/graph.json`，并通过 `mutations.commit_graph_file` 提交（详见 Runtime 部分）。本文件函数的调用方有：`mutations.render_graph_file`（`dumps_graph_spec(graph_spec_from_graph(g))`）、`commit_graph_file` 和 `graph_file_status`（`loads_graph_spec`、`patch_from_graph_spec`）、`checkpoint` 读取草稿的 `graph_version`、`service._values`，以及 `agent/tools/graph.py` 用来检查新节点有没有写描述。`apply_graph_spec` 和 `graph_from_graph_spec` 在仓库里没有 Runtime 调用方。

### 格式

| 位置 | 字段 | 约束/默认 |
|---|---|---|
| 顶层 | `format` | 必填，必须是 `"emerge.graph-spec/v1"` |
| | `graph_version` | 可选，int `>= 0`，表示文件是从哪个图版本写出来的。`graph_spec_from_graph` 总会写；pydantic 宽松模式下，`"3"` 这样的字符串也会被接受 |
| | `nodes` | `{节点 id: 节点}`，id 要符合 `NAME_PATTERN`；默认 `{}` |
| 节点 | `type` | 必填，非空 |
| | `code` | 可选，非空字符串或 `null`；只有生成代码类型才写 |
| | `description` | 默认 `""` |
| | `params` | 默认 `{}`，必须是对象 |
| | `inputs` | `{输入端口: "节点.端口"}`，表示哪个输出喂这个输入；端口名和端点都要符合名字规则 |
| | `start_when` | 不写或写 `null` 表示根节点。`{"any_of": [{"all_of": {来源: 结局}}]}`，`any_of` 和 `all_of` 都不能为空，结局写 `"*"` 表示任意结局 |
| 不出现在文件里 | 实例 id、替换历史、节点状态和结局 | 由框架管理 |

对应关系：每条 `inputs` 对应一条 `Subscription`；`any_of` 的每一项对应一条 `StartEdge`；`all_of` 的每个键值对对应一个 `StartSource`，其中 `"*"` 对应 `outcome=None`（`emerge/graph/spec.py:100-111`）。示例文件中的一个节点：

```json
"left_stage_apple": {
  "type": "arm_place",
  "params": {"arm": "left", "slot": "left"},
  "inputs": {"held_object": "left_check_grasp.held_object", "destination": "observe_scene.staging_table"},
  "start_when": {"any_of": [{"all_of": {"left_check_grasp": "success", "right_check_grasp": "success"}}]}
}
```

用示例节点类（`fx.py`）加载示例文件，可以建图并通过校验：11 个节点，根节点只有 `observe_scene`，`report_failure` 有 9 条 `any_of`。

### 读文件：三步检查（`loads_graph_spec`，`emerge/graph/spec.py:242-259`）

前一步有错时，后一步不执行。

| 步骤 | 检查内容 | 报错形式 |
|---|---|---|
| 1. JSON | 语法错误；同一个对象里出现两次的键（`json` 默认会保留后一个，悄悄丢掉一个节点或一条输入） | `graph file is not valid JSON: line L column C: msg`；`graph file: key 'a' appears twice in one object`。两种都只报遇到的第一处 |
| 2. 字段 | 未知字段（拼错的键）、必填字段、类型、id / 端口 / 结局的写法、`"node.port"` 写法、`any_of` / `all_of` 不能为空、`code` 不能为空串 | `graph file has errors:` 后面每条错误一行（`loc: msg`）。不同字段的错误会一起报出 |
| 3. 引用 | 每条 `inputs` 的来源节点、每个 `start_when` 的来源节点，都必须在文件里（`emerge/graph/spec.py:121-137`） | 一条消息列出全部缺失引用：`<root>: Value error, these name a node that is not in the file: nodes.a.inputs.in reads 'ghost.out'; nodes.a.start_when.any_of.0 waits for 'ghost2'` |

第 3 步放在格式层、不交给图校验的原因：删除节点时会连带删掉从它出发的订阅，如果还有输入指向被删的节点，这条输入会无声无息地消失（决策［图文件：格式检查］）。

| 情况（实验 `e11_spec_format.py`） | 结果 |
|---|---|
| 两个对象里各有一个重复键 | 只报第一个（见疑点 2） |
| 一个节点拼错字段名，另一个节点缺 `type` | 两条一起报 |
| 两个非法节点 id | 只报第一个：id 校验器遇到第一个就抛出 |
| 同一节点的两条非法 `inputs` | 只报第一条 |
| `"in": "b.x.y"` | 报错：节点部分 `b.x` 含点 |
| `all_of` 为空，或里面的结局不合法 | 报这条错误，还会多报一条 `any_of: Tuple should have at least 1 item after validation, not 0`（见疑点 3） |
| `"start_when": null`、`"code": null` | 等同于没写 |
| 顶层是数组 | `<root>: Input should be a valid dictionary or instance of GraphSpec` |
| 第 2 步有错，同时也有缺失的引用 | 只报第 2 步的错误 |
| 节点读自己的输出、启动条件等自己 | 格式层通过；等自己的情况在提交时由校验报 `START_CYCLE` |
| 端口不存在、类型未注册、结局未声明 | 格式层不查，提交时由校验报 |

### graph → 文件（`graph_spec_from_graph`，`emerge/graph/spec.py:153-194`）

输出的文件带 `graph_version = graph.version`。原样交回不会产生任何修改。每张合法的图都能写成文件：JSON 的键不能重复，所以在一条边里写两次同一个来源的情况，由校验错误 `START_DUPLICATE_SOURCE` 事先排除；结局名也不能是 `"*"`。

| 情况 | 结果 |
|---|---|
| 有重复 id | `GraphValidationError: node id 'a' is used more than once; a graph file cannot hold both` |
| 一个输入有两个来源 | `input b.i has two sources; a graph file holds one` |
| 启动边里同一来源出现两次，或启动边为空 | `a start edge of 'b' cannot be written as a graph file condition: a and a[ok]` |
| 订阅或启动边引用了不在图里的节点；`code` 为 `""` | `the graph cannot be written as a graph file: <pydantic 错误>` |
| 空图 | `"nodes": {}` |

### 写文本（`dumps_graph_spec`，`emerge/graph/spec.py:197-236`）

| 规则 | 说明 |
|---|---|
| 顶层键的顺序 | `format` → `graph_version`（不为 `None` 时才写）→ `nodes` |
| 节点键的顺序 | `type` → `code`（不为 `None` 时才写）→ `description`（总是写）→ `params`（总是写）→ `inputs`（非空才写）→ `start_when`（有才写） |
| 缩进 | 两个空格 |
| 第 0–2 层（顶层、`nodes`、每个节点的字段） | 非空的对象或数组，每项单独一行 |
| 第 3 层及以下 | 如果 `所在列 + 紧凑 JSON 的长度 < 100`，就写成一行（分隔符为 `, ` 和 `: `）；否则展开，下一层再按同样规则判断。用严格小于，是为了给后面可能跟的逗号留位置 |
| 空对象、空数组、标量 | 总是一行（`{}`、`[]`） |
| 宽度怎么算 | 按字符数算，一个中文字符算 1。实验中 `report_failure` 的 `params` 行有 84 个字符，终端显示宽度却是 115 列 |
| 非 ASCII 字符 | 原样输出（`ensure_ascii=False`） |
| 结尾 | 一个换行 |
| 确定性 | 同一个 spec 写两次逐字节相同，读回后数据不变 |

示例文件读入后再写出，并不逐字节相同（实验 `e9_spec_example.py`）：多了 `"graph_version": 0`；示例里手工折行的 `params`、`start_when`，只要不超过 100 字符就会被合成一行。比如示例中的

```json
      "start_when": {
        "any_of": [{"all_of": {"observe_scene": "success"}}]
      }
```

写出后变成 `"start_when": {"any_of": [{"all_of": {"observe_scene": "success"}}]}`。数据完全一致，最宽一行 97 个字符。

### 文件 → 新图（`graph_from_graph_spec`，`emerge/graph/spec.py:277-291`）

经由 builder 组装：版本号为 0，会做校验；忽略文件里的 `graph_version`；没写 `code` 的生成代码类型固定到当前版本。

### 改过的文件 → patch（`patch_from_graph_spec(graph, spec, *, replace=())`，`emerge/graph/spec.py:297-330`）

1. 文件写了 `graph_version`，但与 `graph.version` 不同：抛 `GraphValidationError: the graph file was written from graph version 0, but the graph is at version 1 now; take a fresh file of the current graph and make the edit again`。不尝试合并：期间节点可能已经结束，主 Agent 也可能刚提交过别的修改（决策［图文件：防止改错版本］）。
2. `replace` 里有不同时存在于图和文件中的 id：抛 `cannot replace 'ghost': only a node in both the graph and the file has an instance to replace`。
3. 图里有、文件里没有的节点，各生成一个 `remove_node`，排在最前面。
4. 按文件里的节点顺序处理每个节点：
   - 是新节点：生成 `add_node`。
   - 在 `replace` 里，或 `type` 变了，或写了 `code` 且与原版本不同：生成 `replace_node`，`type`、`params`、`description` 都取文件里的值，`code` 规则见上一节的表；如果启动条件按集合比较有差别，再加一个 `set_start_edges`（`emerge/graph/spec.py:363-374`）。
   - 其他情况：`params` 不同生成 `update_params`，`description` 不同生成 `set_description`，启动条件按集合比较不同生成 `set_start_edges`（`emerge/graph/spec.py:377-385`）。
5. 订阅逐条比较：图里有、文件里没有的生成 `disconnect`（跟随被删节点一起消失的订阅除外）；文件里有、图里没有的生成 `connect`（`emerge/graph/spec.py:398-405`）。
6. 把这些 edit 拼成一个 patch；什么都没变时 patch 为空。这一步不看节点状态，状态由 `apply_graph_patch` 检查。

`apply_graph_spec(graph, spec_or_text, registry=None, *, states=None, replace=())` 把读文件、算 patch、应用三步合在一起；文件什么都没改时，返回原来的图对象。

### 在文件里做某种编辑会得到什么（用示例文件实测）

实验 `e10_spec_edits.py`、`e10b_spec_ended.py`、`e11_spec_format.py`：图由示例文件建成（版本 0），节点状态为 `observe_scene=finished`（结局 `success`）、`left_pick_apple` 和 `right_pick_banana` 为 `running`，其余为 `pending`。文件由 `graph_spec_from_graph` 写出（`graph_version: 0`），修改后用 `patch_from_graph_spec` 算 patch，再用 `apply_graph_patch(..., states=...)` 应用。

| 在文件里的编辑 | 生成的 edit | 结果 |
|---|---|---|
| 不改 | 空 | `apply_graph_spec` 返回原图对象，版本号不变 |
| 改 pending 节点 `left_check_grasp` 的 `description` | `set_description` | 得到 v1，diff 为 `modified [description]` |
| 改 running 节点的 `description` | `set_description` | 允许 |
| 改 finished 节点 `observe_scene` 的 `description` | `set_description` | 拒绝：`node 'observe_scene' is finished: an ended node stays as it ended, so its description cannot change` |
| 删掉 `description` 键 | `set_description("")` | 允许 |
| 改 pending 节点的 `params` | `update_params` | 同一实例，`modified [params]` |
| 改 running 节点 `left_pick_apple` 的 `params` | `update_params` | 换成新实例 `left_pick_apple@v0 → @v1`，出现在 diff 的 `replaced` 里 |
| 改 finished 节点的 `params` | `update_params` | 拒绝：`... so its parameters cannot change` |
| `params` 内容不变，只换了键的顺序 | 空 | 无变化（比较的是 dict 是否相等） |
| `params` 里把 `1` 改成 `1.0` 或 `true` | 空 | 无变化：Python 里 `1 == 1.0 == True`，这类修改会被静默忽略（实验 `e17_params_eq.py`） |
| 删掉 `params` 键 | 视为 `{}` 再比较 | 原本就是 `{}` 则无变化，否则生成 `update_params({})` |
| 改 pending 节点的 `type` | `replace_node`（`params` 和 `description` 取文件里的值） | 换新实例 |
| 同时改 `type`、`params`、`description` | 一个 `replace_node` | 换新实例 |
| 改 running 节点的 `type`，新类型的端口对不上 | `replace_node` | 拒绝：`ENDPOINT_MISSING: node 'left_pick_apple' has no input port 'target'; REPLACEMENT_PORT_MISMATCH: ...` |
| pending 节点同时改 `type` 和 `start_when` | `replace_node` 和 `set_start_edges` | 允许。如果节点已经启动，`set_start_edges` 会被拒绝，整个 patch 都被拒绝 |
| 改 pending 节点的 `start_when`（`success` 改成 `*`） | `set_start_edges` | `modified [start_edges]` |
| 改 running 节点的 `start_when` | `set_start_edges` | 拒绝：`... is running: start edges can only change before the node starts` |
| 调换 `any_of` 各项的顺序、`all_of` 各键的顺序，或节点在文件里的顺序 | 空 | 无变化：启动条件按集合比较，节点按 id 对应 |
| 删掉 `report_failure` 的 `start_when` | `set_start_edges(())` | 该节点成为根节点，`start_status` 返回 READY |
| 改一条 `inputs` 的来源 | `disconnect` 和 `connect` | diff 里有一条 `rewired` |
| 删掉一条 `inputs` | `disconnect` | 允许 |
| 加一条 `inputs` | `connect` | 允许 |
| 改 running 节点的 `inputs` | `disconnect` 和 `connect` | 允许：running 不算已结束 |
| 改或删 finished 节点的 `inputs` | `disconnect`（以及 `connect`） | 拒绝：`... so its inputs are part of what it ended with` |
| 新加一个节点，读 finished 节点 `observe_scene` 的输出 | `add_node` 和 `connect` | 允许 |
| 新加一个节点，`start_when` 写 `{"observe_scene": "failure"}`（该节点实际以 `success` 结束） | `add_node` | 得到 v1；`start_status` 返回 NEVER，Runtime 会记为 `skipped` |
| 同上，但写成 `"*"` | `add_node` | 得到 v1；`start_status` 返回 READY，Runtime 会立即启动它 |
| 删除没被别处引用的 pending 节点 `report_success` | `remove_node` | 允许，它的输入订阅一起删除 |
| 删除 `check_fridge_open`，但别处的引用没改 | — | 格式错误（第 3 步），一次列出 3 处引用 |
| 删除 `check_fridge_open`，同时改掉引用它的地方 | `remove_node`，再给两个引用方各生成一个 `set_start_edges` | 允许（引用方都是 pending） |
| 删除 running 节点 `left_pick_apple`（引用已改掉） | `remove_node` 等 | 拒绝：`... is running: only a node that has not started can be removed; stop it instead; it then stays in the graph` |
| 给节点改 id（`report_success` 改成 `report_ok`） | `remove_node`、`add_node`、`connect` | 新 id 是一个全新的节点，没有替换记录 |
| `replace=["left_pick_apple"]`（running） | `replace_node`，各字段取文件里的值 | 换新实例 |
| `replace=["observe_scene"]`（finished） | `replace_node` | 拒绝（状态不允许） |
| `replace=["ghost"]`，或 `replace` 指定一个已从文件删掉的节点 | — | `patch_from_graph_spec` 直接抛错：`cannot replace ...` |
| `replace=[id]`，同时改了这个节点的 `params` | 一个带新 `params` 的 `replace_node` | 换新实例 |
| `replace=[id]`，同时改了这个节点的 `start_when` | `replace_node` 和 `set_start_edges` | pending 时允许 |
| 文件是在 v0 写的，而图已经到了 v1 | — | 拒绝（过期），提示重新取一份文件 |
| 删掉 `graph_version` 那一行，再交给 v1 的图 | 按当前图比较 | 被接受。但文件里保留的旧内容会把 v0 到 v1 之间别人做的修改改回去（实验中 v1 改过的描述又变回了原文） |
| 输入接到一个不存在的端口 | `connect` | 拒绝：`ENDPOINT_MISSING` |
| `start_when` 写了来源类型没声明的结局 | `set_start_edges` | 拒绝：`START_UNKNOWN_OUTCOME` |
| `start_when` 等自己 | `set_start_edges` | 拒绝：`START_CYCLE: start edges form a cycle: report_success -> report_success` |
| `params` 缺少必填字段 | `update_params` | 拒绝：`PARAM_INVALID: message: Field required` |
| 输入两端的类型标签不同 | `connect` | 允许，只把 warning 写进日志 |
| 给模块类型的节点写上 `code` | `replace_node(code=...)` | 拒绝：`UNKNOWN_CODE_VERSION` |
| 生成代码类型的节点：`code` 从 `"1"` 改成 `"2"` | `replace_node(code="2")` | 换新实例，使用 v2 的代码 |
| 删掉 `code` 那一行 | 空 | 继续沿用原来的版本 |
| 删掉 `code` 那一行，并且 `replace=[k]` | `replace_node(code="1")` | 换新实例，代码版本不变 |
| `code` 写一个没注册的版本 | `replace_node` | 拒绝：`UNKNOWN_CODE_VERSION` |
| 模块类型改成生成代码类型，不写 `code` | `replace_node(code=None)` | 固定到当前版本 |
| 生成代码类型改成模块类型，但 `code` 行没删 | `replace_node(type=src, code="1")` | 拒绝：`UNKNOWN_CODE_VERSION`（`code` 写了就算数） |
| 同上，并删掉 `code` 行 | `replace_node(code=None)` | 允许 |
| 新加一个生成代码类型的节点，不写 `code` | `add_node` | 固定到当前版本 |

## visualization.py

本文件提供两个纯函数：`to_text` 和 `to_mermaid`。它们都可以接收 `states={id: 状态}` 和 `outcomes={id: 结局}` 作为叠加信息，只读不写，也不修改图。`to_text` 由 `agent/tools/graph.py:103` 的 `show_graph` 调用；`to_mermaid` 目前在仓库里没有调用方。

| 叠加 | 显示 | 位置 |
|---|---|---|
| 只有状态 | `<状态>`，如 `running` | `emerge/graph/visualization.py:110-114` |
| 只有结局 | `finished: <结局>`（状态默认按 `finished` 显示） | 同上 |
| 状态和结局都有 | `<状态>: <结局>`，如 `finished: success` | 同上 |
| 都没有 | 不显示 | 同上 |

| `to_text` 的行 | 格式 |
|---|---|
| 第一行 | `graph v2  nodes=3  subscriptions=2` |
| 节点 | `  p (mid) instance p@v2  [running]`。固定了版本的类型写成 `类型@版本` |
| 实例说明 | 有 `description` 时多一行 `      about: ...` |
| 替换历史 | 每条一行：`replaced v1: p@v0 (mid) -> p@v1 (mid)` |
| 订阅 | `in  inp <- v.out` / `out out -> end.inp` |
| 启动边 | 第一条写 `starts after p and v[success]`，后面的写 `or after v[failure]`；空边写 `(no sources)` |
| 最后一行 | 有根节点时写 `roots: v` |

| `to_mermaid` 的元素 | 画法 | 位置 |
|---|---|---|
| 节点 | `n0["v<br/>src<br/>v@v0<br/>finished: success"]`。id 用图里的下标（`n0`、`n1`……），所以 `a-b` 和 `a_b` 不会被画成同一个节点。`skipped` 节点加 `:::skipped` 样式。实例说明不画 | `emerge/graph/visualization.py:27`、`38-42` |
| 订阅 | `n0 -- "out->inp" --> n1` | `emerge/graph/visualization.py:43-45` |
| 单来源的启动边 | `n0 -.->\|"success"\| n1`；要求"做完"的标 `done` | `emerge/graph/visualization.py:49-51` |
| 多来源的启动边 | 先汇到一个小圆点 `j0(("and"))`，再 `j0 -.-> 目标`。一个节点的几条边各自独立指向它，这样"全部满足"和"任一条"在图上分得开 | `emerge/graph/visualization.py:52-58` |
| 空边 | 不画 | `emerge/graph/visualization.py:52` |
| 替换历史 | 每条记录画成 `r0(["p@v0<br/>mid<br/>replaced v1"]):::replaced`，按从旧到新的顺序用 `== replaced by ==>` 串起来，最后接到当前节点 | `emerge/graph/visualization.py:61-67`、`129-135` |
| 样式定义 | 只有用到时才输出 `classDef replaced` / `classDef skipped` | `emerge/graph/visualization.py:68-71` |
| 引用了不在图里的节点（未校验的图） | 用 `x1`、`x2` 作 id 照常画出 | `emerge/graph/visualization.py:31-34` |
| 节点名是 Mermaid 保留字（`end`、`graph` 等） | 在 `nN` 后面加 `_`。由于 id 本来就不会和保留字相同，这一步实际上不起作用，但也没有害处 | `emerge/graph/visualization.py:29`、`35` |
| 标签里的双引号 | 转义成 `#quot;` | `emerge/graph/visualization.py:138-140` |

实验 `e12_vis.py` 的一段输出（v 以 success 结束，p 被替换了两次，end 被 skipped）：

```text
flowchart LR
    n2_["end<br/>sink<br/>end@v0<br/>skipped"]:::skipped
    j0(("and"))
    n1 -.->|"done"| j0
    n0 -.->|"success"| j0
    j0 -.-> n2_
    n0 -.->|"failure"| n2_
    r0 == replaced by ==> r1
    r1 == replaced by ==> n1
```

## 疑点（待核实）

1. **成环检测在几个环共用一个节点时只报一个环**。`emerge/graph/graph.py:265-266` 的 docstring（"Distinct loops sharing a node are reported once each"）和决策记录 HANDOFF.md:168（"共用一个节点的不同环各报一条"）都说每个环都会报出，但 `emerge/graph/graph.py:279` 每一步只走"第一个在残余里的依赖"。实验 `e3b_cycles.py`：a 等 b 或 c，b 等 a，c 等 a，结果只有 `[('a', 'b', 'a')]`；三角形加一条弦的图也只报一个环。图照样会被拒绝，只是报错信息不全，主 Agent 修好一个环之后才会看到下一个。
2. **图文件的格式检查并不会"一次报出这一步的所有错误"**。`emerge/graph/spec.py:245-247` 的 docstring 和 HANDOFF.md:235 都说每一步会报出它发现的全部错误。实际上，第 1 步遇到第一个重复键就停止（`emerge/graph/spec.py:270-272`）；第 2 步里节点 id、`inputs`、`all_of` 三个校验器都是遇到第一个坏项就抛出（`emerge/graph/spec.py:45-69`）。实验：两处重复键只报 1 处，两个非法 id 只报 1 个，同一节点的两条非法输入只报 1 条。
3. **`all_of` 出错时会多报一条误导性的错误**。`all_of` 为空或结局名不合法时，除了这条错误本身，还会多出 `nodes.X.start_when.any_of: Tuple should have at least 1 item after validation, not 0`，而文件里的 `any_of` 其实并不为空（`emerge/graph/spec.py:79`、`87`；实验 `e11_spec_format.py`）。主 Agent 看到的就是这段文字。
4. **生成代码文件里不能有没写 `name` 的公共基类**。`emerge/graph/registry.py:254-256` 会注册文件中定义的每一个 `Node` 子类，所以一个没有 `name` 的公共基类会报 `RegistryError: node class Shared has no name`，整个文件都注册失败（实验 `e2_registry.py`）。`emerge/graph/node.py:225-227` 的注释说公共基类可以不写 `name`；这对配置模块成立，因为那里只注册加了装饰器的类，但对生成代码不成立。
5. **非空但实际什么都没改的 patch 也会生成新版本**。`emerge/graph/patch.py:156-157` 只对"没有 edit"的 patch 原样返回。重复 `connect` 一条已有订阅、写入相同的描述，都会让版本号加 1，diff 却是空的（实验：`set_description` 写相同文字，得到 v1，`bool(diff) == False`）。`emerge/runtime/supervisor.py:454` 和 `emerge/runtime/mutations.py:196` 只判断 `new is old`，因此这次提交会被当成一次真实的改图，报成 `committed=True`。这与 HANDOFF.md:172"避免给版本历史塞无意义的空版本"的本意不一致。图文件这条路径不会生成这种 patch，只有直接提交 patch 的调用方会遇到。
6. **重复 id 时，校验用最后一个节点的类型检查所有同 id 节点**。`emerge/graph/validation.py:100` 的 `types` 字典以 id 为键，后出现的覆盖先出现的，而 `Graph.node()` 取的是第一个。实验：先放 `a: src`、再放 `a: nope`，会报出 `unknown node type 'src'`，可 `src` 是已注册的类型。这种图本来就会因为 `DUPLICATE_ID` 被拒绝，所以只是多了一条误导信息。
7. **`loads_graph` / `loads_patch` 读到非对象的 JSON 时抛 `AttributeError`**。`emerge/graph/serialization.py:125` 直接调用 `data.get`，`loads_graph("[]")`、`loads_graph("null")` 抛的是 `AttributeError`，而不是 `GraphValidationError`。Storage 只读自己写进去的数据，所以实际风险不大。
8. **定义期和注册时都不检查端口元素的类型和 `reads` 的形状**。`emerge/graph/node.py:229-231` 只是把它们转成 tuple，`emerge/graph/registry.py:172-204` 也不检查。实验 `e15_badports.py`：`inputs = ("img",)` 可以定义，也可以注册，要到 `NodeEntry.to_dict()`（`emerge/graph/registry.py:104`）或校验 `_port`（`emerge/graph/validation.py:222`）时才抛 `AttributeError`。`reads` 形状不对时，定义期抛的是 `ValueError`，不是 `NodeError`。主 Agent 生成的节点代码如果写错，得到的会是一个难懂的错误。

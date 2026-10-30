# 我核对过的疑点（汇总用）

## 存储（parts/04）
- S1 已核实代码：sqlite.py:69 `_TOKEN_RE = [CJK]+|[^\s]+`，非 CJK 后紧跟的汉字被并进一个词 → "UR5机械臂"、"偏好，颜色"查不到；纯符号词（"。"）让 AND 整体落空。功能性 bug，影响记忆检索。
- S2 已核实代码：sqlite.py:240-252 open() 在 _init_schema 失败时不关连接；WAL 在版本检查前设置。小。
- S7 已知：盖章取号与写消息是两个事务（用户 2026-10-28 同意保持）；sqlite.py:1-5 docstring、base.py:433 措辞仍是"同一事务"。只是注释过时。
- 其他（写后校验、delete_session 先查后删、aclose 不取锁、注释错类型）：小，调用方目前不触发。

## 主 Agent（parts/07）
- A1 已核实代码：control.py:113-121 else 分支把任何非 interrupt/pause/resume 的 action 当 stop；ControlAction 只是 Literal 注解。阶段 9 的 TUI 接入时要在入口校验。
- A3 已核实代码：mutations.py:335、347 给模型的文字里有不存在的工具名 `reset_graph_file`（工具叫 reset_graph）；tools/graph.py:76 又追加 "Call reset_graph"，同一条回复两种说法。checkpoint.py:104 也有。
- A4 已核实代码：graph.json 不存在时，commit_graph 回复 "The file was left exactly as you wrote it"（tools/graph.py:77 对 missing_file 也走这句）；graph_file_status 对不存在返回 has_changes=False → run_status/show_graph 说与运行中的图一致。
- A5 已核实代码：models/base.py:180-189 图片在构造请求时按路径读取，读不到抛 ModelError；agent/memory.py:166-170 刷新时删除不再被引用的记忆图片 → 历史里引用过的图片一旦被删，之后每次请求失败，直到压缩去掉图片。节点覆盖/删除自己产物同理。
- A2、A6–A12：子任务实验复现或推理，我未逐条复核；属于边角情况。

## Agent 通用模块（parts/02）
- C1 已核实代码：loop.py:93 `turn_index = len(history.turns)` 在本轮开始时记下；本轮内压缩（before_model）把前 count 轮并成 1 轮后轮数变少；取消时 truncate_to_turn_count(turn_index)（loop.py:136,142）在 turn_index ≥ 当前轮数时什么都不删（messages.py:77-82）→ 被取消的半轮留在历史和库里，可能留下没有结果的 tool_call。条件：同一轮里压缩折叠 ≥2 轮后再取消。
- C3（子任务实测）：压缩后同一轮里运行信息重复追加一条；无害。
- C4（子任务实测）：两个工具调用之间非取消异常 → 历史留下没结果的 tool_call；主 Agent 里 StaleCallGuard 检查路径时 is_graph_file 遇到怪路径会抛异常可触发。

## Runtime 核心（parts/05）
- R4 已核实代码：context.py:358-362 wait_for_run_permission 未暂停时不让出事件循环直接返回 True；停止/暂停指令由读线程经 call_soon_threadsafe（bus.py:136）投递，要事件循环运行 receive（context.py:175-183）才设标志 → 只做同步工作 + await wait_for_run_permission() 的循环永远看不到停止，stop_timeout 后被强杀，teardown 不跑。修法：先 await asyncio.sleep(0)。影响设计核心的协作边界。
- R3 已核实代码：bus.py:321-326 void_asks 不持锁、遍历快照、每条间 await；answer_ask 在 _timeline 锁内 pop（bus.py:303-306）→ 同一实例 ≥2 条待答提问时可能 KeyError，_finish 里跳过 _on_current_ended（supervisor.py:845-847）→ 下游输入不关闭、启动边不推进。罕见。
- R1 已核实代码：shutdown(closing=False)（end_run）时 _finish→_on_current_ended→_advance 仍标 skipped（supervisor.py:752-758 不看 _shutting_down）；决策记录 HANDOFF.md:447 说 pending 保持 pending。closing=True 时 _finish 早返回（supervisor.py:838-839），会话关闭不受影响。小。
- R2（子任务实测）：starting 时 pause 再 resume，setup 未完成就记 running。显示问题。
- R5（子任务实测）：节点 fork 的子进程持管道副本 → exited 等它退出，强杀后等待无上限。
- R6（子任务实测）：ctx.aclose() 抛错 → 结局丢失记 failed。

## Runtime 服务层（parts/06）
- V8 已核实代码：checkpoint.py:180-192 + registry.py:203 全新进程里打开会话，生成类型还没注册，load_pinned_code 载入的固定版本成了"当前实现"（多个固定版本时取决于顺序）。阶段 9 计划要先加载 workspace/code/ 已有生成代码，装配顺序做对可避免。
- V4（子任务实测）：dry run 把等旧实例的节点报 started，真提交报 after_old_instance（preview 不看 _holding）。
- V5（子任务实测）：不改变任何东西的 patch 也版本+1（patch.py:156-162）；只影响 apply_patch，文件提交不会。
- V1/V2（子任务实测）：Subscription 丢弃不退订（强引用）；aclose 唤不醒等待中的迭代。阶段 9 TUI 用 stream 时要注意。
- V9、V12（子任务实测）：重开全部结束的运行不推 graph idle；end_run 时替换中的新实例留 pending。

## 模型层（parts/01）
- M1 已核实代码：retry.py:60-64 只按 HTTP 状态码判断可重试；Anthropic 流中途 `event: error`（如 overloaded_error）由 SDK 以原响应（200）构造 APIStatusError → 不重试，一次失败即 ModelError。实际负载高时常见。
- M2（子任务实测）：非 ModelError 异常（IndexError 等）绕过统一处理，不发 model.finished、不重试。
- M3：空 api_key 计划说本地模型可不传，代码抛 ConfigError（与决策记录一致，计划旧）。
- M5（子任务实测）：request.reasoning_effort="" 因 `or` 回落到配置值，无法按请求关掉推理。
- M6/M7（子任务实测）：流无完成标志时三家处理不同；Responses 非流式不查 status。
- M8（推断）：换 provider 后只含思考的 assistant 消息被重建成空消息。

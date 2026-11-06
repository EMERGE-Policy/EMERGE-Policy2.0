# 机器人底层接口（设计草案）

更新于 2026-11-06：RoboDojo 驱动可打开 RoboDojo 自带的回合录像，存进对话的 `robot/recorder/`（6.2 录像）；驱动多了 `folder` 属性（4.5）。早些时候：机器人文件名统一小写（`manifest.json`、`command.json`、`tick.json`、`body.json`、`action.json`、`state.json`、`observation.json`），`ROBOT.md` 改名 `body.md`，`actions.jsonl` 改名 `history.jsonl`；下面以前的记录里用的还是旧名。同日早些时候：加入本体隔离（见 2.9）：每个本体一个文件夹，`ACTION.json`、`STATE.json`、`OBSERVATION.json`、`ROBOT.md`、`obs/` 都在本体文件夹里，不再放在 `robot/` 顶层；`RobotClient` 构造时绑定本体；位姿用本体系；新增 `config.json`。2.1～2.8、第 4、5 节里按"整机一套文件"写的地方，以 2.9 为准。2026-11-04：TUI 搬到顶层，由它为每个对话启动底层（4.8～4.10）。接通 RoboDojo（第 6 节）。机器人文件改成每个对话一套，放在 `sessions/<对话>/robot/`；底层每走一拍就覆盖保存一次场景，启动时从这个场景文件恢复；节点不碰场景，场景操作走留给 TUI 的命令口 `COMMAND.json`。2026-11-03：本体状态和观测分成 `STATE.json`、`OBSERVATION.json` 两个文件。RoboDojo 部分对照 `third_party/RoboDojo`（`f57e163`）的源码核实过。

## 0. 一句话

每个对话有自己的一套机器人文件，放在对话文件夹 `sessions/<对话>/robot/` 下。一个底层进程服务一个对话。节点写 `action.json` 占臂、发动作，读 `state.json` 拿本体状态，读 `observation.json` 拿观测。底层只有一个时钟，按"发观测 → 节点决策 → 一起执行 → 全部做完再发观测"的循环推进仿真；"发观测"指在同一拍写出本体状态和观测这两个文件，它们都带拍号，每个动作都标注它是在哪一拍下做出的。底层每走一拍就把整个场景覆盖保存到 `robot/scene`，进程被杀掉后再启动，会从这个文件回到原来的场景。节点不做场景操作；底层留了一个命令口 `command.json`，以后给 TUI 用，现在只有一个命令：重置场景。节点用的是所在环境的客户端，它继承 `RobotClient`。

## 1. 分工

```
节点 A（左臂）、节点 B（右臂）……   sessions/<对话>/robot/            底层（跑在仿真器进程里）
  占臂、发动作、等结果 ──写──▶  action.json       ──读──▶  按节拍推进仿真，回填结果
  读本体状态          ◀──读──  state.json        ◀──写──  仿真停下来时，同一拍写出本体状态
  读观测              ◀──读──  observation.json  ◀──写──  和观测
  了解本体            ◀──读──  body.md           ◀──写──  启动时写一段介绍
TUI（以后）
  发命令（如重置场景）──写──▶  command.json      ──读──▶  执行命令，回填结果
                               scene             ◀─读写─  启动时读；每走一拍覆盖写
```

| 谁 | 负责 |
|---|---|
| 节点（用户写） | 什么时候占住、放开一条臂；什么时候发动作；动作还在 pending 时怎么等；怎么避免互相等 |
| TUI（以后） | 通过命令口给底层发命令，比如重置场景 |
| 底层 `run_simulation`（所有仿真器共用） | 启动仿真并从 `robot/scene` 恢复场景；节拍：决定什么时候走一拍；停下来时上传本体状态和观测；标动作状态；每走一拍保存场景；执行命令口的命令；退出时关闭仿真 |
| 驱动（每种仿真器一个） | 启动、关闭仿真环境；介绍本体；拍观测；把两条臂的动作合成一拍推进下去（RoboDojo 就是一次 `take_action`）；把整个场景存成文件、从文件原样恢复 |

- 实际操作机器人的是图里的节点。节点类型还没做，这里不预设。
- 动作集不定：动作的类型、参数、结果内容都由驱动决定。
- 不提供任何真值。

## 2. 文件

### 2.1 放在哪

机器人文件和对话的 `runs/`、`memory/` 平级，每个对话一套，互不相干：

```
workspace/
  config.json  AGENTS.md  USER.md  skills/
  sessions/
    <对话>/                      对话文件夹，节点的 ctx.session_dir
      .emerge/  trace.jsonl  memory/  code/  runs/
      robot/
        body.md                  本体介绍
        action.json              每条臂：谁在用、当前动作
        command.json             命令口：TUI 发来的命令（现在只有重置场景）
        state.json               最新的本体状态
        observation.json         最新的观测
        launch.json              这个对话的底层怎么启动：驱动名和驱动配置
        first_scene              这个对话开始时的场景；重置命令回到它
        scene                    整个场景，底层每走一拍覆盖保存
        simulation.log           底层作为独立进程运行时的输出
        recorder/                驱动的录像（RoboDojo 打开 record 时，见 6.2）
        obs/<拍号>/               每一拍的 state.json、observation.json 和各相机的 .jpg
```

- 文件里出现的路径（比如图像路径）一律相对对话文件夹，和 2.0 的约定一样，主 agent 拿到就能用 `read_file` 打开。
- 一个底层进程只服务一个对话，启动时指定（见 4.8）。换对话就是换个参数重启底层；TUI 会替你做（见 4.9）。

下面以对话里正在跑的 run `R3` 为例：节点 A 管左臂，B 管右臂，当前是第 15 拍。

### 2.2 action.json

每个本体一份，每条臂两样东西：谁在用这条臂（`holder`），以及它的当前动作（`action`：正在做的，或者最近做完的那个）。

```json
{
  "left_arm": {
    "holder": {"run": "R3", "node": "A"},
    "action": {"id": "a3f9c2e17b04", "run": "R3", "action_type": "...", "parameters": {}, "step": 15,
               "status": "running", "started_at": "2026-11-04T10:00:01.000Z"}
  }
}
```

动作的字段沿用 1.0 ACTION.md 的写法，多了 `run` 和 `step`，去掉了取消相关的字段：

| 字段 | 谁写 | 说明 |
|---|---|---|
| `id` | 节点 | 动作编号 |
| `run` | 节点 | 发这个动作的节点属于哪个 run（`ctx.run_id`） |
| `action_type`、`parameters` | 节点 | 动作类型和参数，由驱动定义 |
| `step` | 节点 | 必填。这个动作是在哪一拍的观测下做出的；底层只记录，不检查 |
| `status` | 底层 | `pending` → `running` → `completed` / `failed` |
| `result` | 底层 | 一句人能读懂的话，由驱动给出 |
| `started_at`、`finished_at`、`duration_ms` | 底层 | 开始时间、结束时间、用时 |

- **holder**：谁在用这条臂，由节点写，记 run 和节点名；为 null 表示没人用。节点做完任务、不再发动作时，把它清成 null（即"放开"），底层就不再等这条臂。
- **没占住也能发**：没人占的臂也能接动作，只是仿真不会为它停下来等。被同一个 run 里别的节点占着的臂，发动作会被拒（`ArmHeld`）。
- **一次一个**：一条臂同时只能有一个没结束的动作；新动作直接取代当前动作，所以文件大小固定，每拍读它的开销不随对话变长。
- **历史（给人 debug 用，节点不读）**：每个动作结束时（完成、失败、换 run、重置、重启、停止），底层往本体文件夹的 `history.jsonl` 追加一行完整记录（多一个 `arm` 字段）。只有底层写、只追加，不加锁，也不在每拍的读写路径上。`tail -f robot/left/history.jsonl` 就能看。
- **被取代**：节点可以用新动作顶掉别的 run 留下、还没结束的动作。底层记得上一轮看到的未结束动作，发现它不见了，就在 `history.jsonl` 里记一行 "Failed: replaced by another action."，历史不会漏。- **换 run**：同一个对话里，主 agent 结束一个 run、再开一个新的，场景和拍号都接着用。上一个 run 的节点已经停了，它留下的 holder 和没做完的动作不能留给新 run：别的 run 占着的臂，对新 run 的节点来说算没人占；底层一看到新 run 的请求，就把旧 run 的 holder 清掉，把它没做完的动作记为 failed，结果写 "Failed: run ended."。
- **加锁**：两边写这个文件都要加锁，写完后整体替换。

### 2.3 state.json（本体状态）

机器人自身的状态，按臂组织。

```json
{
  "tick": 15,
  "arms": {
    "left_arm":  {"joint_positions": [j1, j2, j3, j4, j5, j6], "ee_pose": [x, y, z, qw, qx, qy, qz], "gripper": 0.9},
    "right_arm": {"joint_positions": [j1, j2, j3, j4, j5, j6], "ee_pose": [x, y, z, qw, qx, qy, qz], "gripper": 0.0}
  }
}
```

| 字段 | 内容 |
|---|---|
| `tick` | 第几拍 |
| `arms.<臂>.joint_positions` | 关节角，单位 rad |
| `arms.<臂>.ee_pose` | 末端位姿 `[x, y, z, qw, qx, qy, qz]`，世界系，单位米 |
| `arms.<臂>.gripper` | 夹爪实测开度，0~1，1 = 全开 |

### 2.4 observation.json（观测）

相机看到的外部世界，按相机组织。

```json
{
  "tick": 15,
  "cameras": {
    "cam_head":        {"image": "robot/obs/15/cam_head.jpg", "intrinsics": [[fx, 0, cx], [0, fy, cy], [0, 0, 1]], "extrinsics": [[...], [...], [...], [...]]},
    "cam_left_wrist":  {"image": "robot/obs/15/cam_left_wrist.jpg", "intrinsics": "...", "extrinsics": "..."},
    "cam_right_wrist": {"image": "robot/obs/15/cam_right_wrist.jpg", "intrinsics": "...", "extrinsics": "..."}
  },
  "extra": {"instruction": "..."}
}
```

| 字段 | 内容 |
|---|---|
| `tick` | 第几拍 |
| `cameras.<相机>.image` | 图像文件路径，相对于对话文件夹 |
| `cameras.<相机>.intrinsics` | 3×3 内参矩阵 |
| `cameras.<相机>.extrinsics` | 4×4 外参矩阵，相机到世界 |
| `extra` | 驱动额外给的字段 |

### 2.5 本体状态和观测的共同规则

- **同一拍**：本体状态和观测在同一拍一起拍，两个文件带同一个拍号；图像、臂的状态、相机内外参都来自这一拍。
- **什么时候上传**：一次执行里所有动作都做完、仿真停下时上传一次。停着期间不重复上传，执行过程中也不上传。底层刚启动时也是停着的，所以启动后马上会上传一次。
- **拍号**：每走一拍加 1，只增不减，按对话计数。对话第一次启动底层时从 0 开始；之后底层重启，接着上次最后的拍号往下数（上次最后一拍加 1）；重置场景时也不归零，接着加 1。
- **按拍号存**：每拍的内容存在 `obs/<拍号>/` 下：`state.json`、`observation.json` 和各相机的 `.jpg`，存了就不再覆盖，底层重启也不清。`state.json`、`observation.json` 永远是最新一份的副本。拿动作上的 `step`，就能找回当时的本体状态和观测。
- **写的顺序**：先把 `obs/<拍号>/` 下的文件写全，再换 `observation.json`，最后换 `state.json`。所以 `state.json` 一显示新拍号，这一拍的所有文件都已经就位：要和它配对的观测，就按它的拍号去读 `obs/<拍号>/observation.json`（`read_observation(state["tick"])`）。
- **约定**：位姿格式是 `[x, y, z, qw, qx, qy, qz]`；末端位姿和相机外参（相机到世界）在同一个世界坐标系里。夹爪取实测开度，范围 0~1，1 表示全开。
- **扩展**：驱动通过 `get_extra()` 往观测里加别的字段，放在 `observation.json` 的 `extra` 下，比如 RoboDojo 的任务指令。

### 2.6 scene（整个场景）

这个对话此刻的整个世界，用来在底层进程被杀掉、或者换对话后重新启动时，原样回到原来的场景。节点不碰它，也不需要知道它。

- **存什么**：回到一模一样的场景所需的全部东西，比如每个物体的位姿和速度、关节角和关节速度、布料和流体的状态、机器人上一次下发的指令、任务进行到哪一步。具体存什么、用什么格式，由驱动的 `save_scene` 决定，底层不看内容。
- **怎么来的**：对话第一次启动底层时，`robot/scene` 还不存在，底层把启动参数 `--scene` 指定的场景文件拷过来。所以 `--scene` 给的文件要和驱动 `save_scene` 存出来的格式一样。
- **什么时候存**：底层每走一拍（每调一次 `execute_action`）就调一次驱动的 `save_scene`，覆盖保存。重置场景之后也存一次。仿真停着时世界不变，不用存。只留最新的一份。实测每拍约 2.5 ms，同步做。
- **快照写盘不拖慢节拍**：快照的 JPEG 编码和 json 由后台写盘线程按交给它的顺序写（`robot/base/simulation/writer.py`），这是 base 内部的事，驱动接口不变。仿真线程只在世界停住时调驱动的 `get_*` 取数据、拷一份交过去。一批动作结束时，动作的结束状态跟在快照后面由写盘线程写进 `action.json`；写完之前仿真不会再读 `action.json`，所以节点看到动作结束时，那一拍的快照一定已经能读到。
- **整体替换**：底层让驱动先存到同目录的临时文件，存完再整体替换 `robot/scene`。存到一半进程被杀掉，留下的仍是上一份完整的。
- **不拖慢节点**：最后一个动作做完的那一拍，先发观测、写动作结果，再存场景。节点这时已经拿到了它要的全部东西，存场景和节点思考同时进行。
- **什么时候读**：只在底层启动时读一次（`load_scene(robot/scene)`）。

### 2.7 command.json（命令口）

留给 TUI 的口子：TUI 往这里写一个命令，底层执行并回填结果。节点不用它，`RobotClient` 里也没有对应的方法；robot 包另外提供写命令、查结果的函数（`robot.base.frontend.command_port`），给 TUI 调。现在只有一个命令 `reset`：把场景重置成这个对话启动底层时指定的初始场景（`--scene` 那个文件）。以后要加命令（比如暂停、换场景），在这里加 `op`。同一时间只放一个命令。

```json
{"id": "5c2e91f0aa13", "op": "reset", "status": "completed", "result": "Scene reset.",
 "started_at": "2026-11-04T10:00:05.001Z", "finished_at": "2026-11-04T10:00:05.240Z", "duration_ms": 239}
```

| 字段 | 谁写 | 说明 |
|---|---|---|
| `id` | TUI | 命令编号 |
| `op` | TUI | 命令名，现在只有 `reset` |
| `status` | 底层 | `pending` → `running` → `completed` / `failed` |
| `result` | 底层 | 一句人能读懂的话 |
| `started_at`、`finished_at`、`duration_ms` | 底层 | 开始时间、结束时间、用时 |

- **一次一个**：上一个命令还没做完时，不能发新的（`CommandBusy`）。
- **重置**：底层调 `load_scene(--scene 指定的文件)`。场景换好后拍号加 1，先上传新场景的本体状态和观测，再把所有没做完的动作记为 failed，结果写 "Failed: scene changed."，最后保存场景。这样节点看到动作失败时，读到的已经是新场景。`holder` 保持不动，由节点自己决定要不要放开。重置失败时，场景、动作和拍号都不变。
- **不认识的命令**：记为 failed，结果写 "Error: RobotError: unknown command '<op>'"。
- **时机**：底层在每一轮开头处理命令，也就是两拍之间，所以不会打断正在走的那一拍。

### 2.8 body.md

一段文字，介绍这个本体，底层每次启动时写一次。

### 2.9 本体隔离（manifest）

**本体**是一个节点操作的对象：它的臂、它的相机、它自己的位姿坐标系。本体之间互相不知道对方，谁拥有什么只在一个地方写明：每个环境手写的 `manifest.json`（RoboDojo 的在 `robot/robodojo/manifest.json`）。

```json
{
  "bodies": {
    "left":  {"arms": ["left_arm"],  "cameras": ["cam_head", "cam_left_wrist"], "extra": ["instruction"], "base_pose": [-0.3, -0.45, 0.765, 0.7071, 0, 0, 0.7071], "allowed_actions": ["move_joints", "move_ee", "set_gripper", "stay"]},
    "right": {"arms": ["right_arm"], "cameras": ["cam_head", "cam_right_wrist"], "extra": ["instruction"], "base_pose": [0.3, -0.45, 0.765, 0.7071, 0, 0, 0.7071], "allowed_actions": ["move_joints", "move_ee", "set_gripper", "stay"]}
  }
}
```

| 字段 | 含义 |
|---|---|
| `arms` | 本体的所有臂；一条臂恰好属于一个本体 |
| `cameras` | 这个本体看得到的相机；两个本体都看得到的相机（如头部相机拍得到对侧的臂，这是物理事实）各写一遍 |
| `extra` | 观测的 `extra` 里这个本体看得到的字段，其余丢掉 |
| `base_pose` | `[x, y, z, qw, qx, qy, qz]`，本体系在世界里的位置。本体读到和发出的位姿都在这个系里；不写就用世界系 |
| `allowed_actions` | 本体能发的动作类型；不写就是驱动支持的都能发 |

**启动时检查**：驱动的 `manifest()` 必须恰好拥有 `arm_names()` 和 `camera_names()`，多一个或少一个，`run_simulation` 直接拒绝运行（`ManifestError`）。

**文件**：

```
robot/
  manifest.json            每个本体的臂和相机（底层启动时写）
  tick.json                最新一拍的拍号，所有本体的快照都发完才写
  command.json  scene  launch.json  first_scene  simulation.log     底层和前端自己的，留在顶层
  <本体>/
    body.json              这个本体拥有的臂、看得到的相机、允许的动作；不含其他本体
    body.md  action.json  state.json  observation.json  obs/<拍号>/
```

- 一个本体的文件里只有它自己的臂和 manifest 里列给它的相机，位姿和相机外参换算到本体系；对侧的数据不会出现在里面。
- 所有本体的快照取自同一拍，各自发布；全部发完才写 `tick.json`。`SimulationProcess` 的状态和拍号读它。

**节点这一侧**：`RobotClient(session_dir, run, node, body)` 构造时绑定一个本体，只读写这个本体的文件夹。对不属于本体的臂、不在 `allowed_actions` 里的动作，在碰任何文件之前抛 `BodyViolation`；报错里只出现本体自己的内容。谁绑哪个本体由上层决定，不由节点自己选。

**执行这一侧**：底层每一轮都对每个本体的 `action.json` 再校验一次，防的是绕过 client 直接写文件：

- 出现不属于该本体的臂：丢弃。
- 动作类型不在 `allowed_actions` 里：记为 failed，结果写 "Failed: body '<本体>' may not send '<动作>'."
- 动作参数里的位姿（驱动用 `pose_parameters()` 声明，如 `move_ee` 的 `pose`）从本体系换算成世界系再交给驱动，读和写用同一对函数（`protocol/frames.py`），所以驱动始终在世界系里工作。
- 动作结果只写回发起的本体，结果里别的本体的臂名换成 "another arm"。
- 驱动的 `describe(body)` 只描述这个本体，不提对侧，位姿用本体系。

**边界**：隔离的是内容，不是时序。时钟仍然只有一个，所有被占着的臂都有动作才一起开始，所以一个节点能从延迟里推测对面在忙。防的是误用和顺手一读，不防存心去读同一文件系统里别的文件。

### 2.10 config.json

每个环境自带一份 `config.json`，放在环境文件夹里（RoboDojo 的在 `robot/robodojo/config.json`，由驱动的 `config_path()` 指明；`python -m robot --robot-config <文件>` 可换），每个键都可省略，写错键名会直接报错：

| 键 | 默认 | 含义 |
|---|---|---|
| `driver` | `{}` | 驱动构造参数的默认值（RoboDojo 的 `env_cfg`、`device_id`、`layout_seed`、`step_lim`、`finish_on_episode_end`、`headless`、`record`）；对话的 `launch.json` 里的同名项优先 |

其余键：

| 键 | 默认 | 含义 |
|---|---|---|
| `poll_interval` | 0.01 | 没有东西在动时，每隔多少秒看一次文件 |
| `jpeg_quality` | 95 | 各本体 `obs/` 下图像的 JPEG 质量，1～100 |
| `log_level` | `INFO` | `DEBUG`、`INFO`、`WARNING`、`ERROR` |
| `save_scene` | `true` | 每走一拍存一次场景；关掉后再启动，是从上一份存下的场景接着跑，不是从停下的地方 |

## 3. 节拍

一拍就是底层调一次驱动的 `execute_action`；在 RoboDojo 里就是一次 `take_action`，对应仿真时间 0.04 秒。

底层按这个循环走：

```
发观测 → 节点看观测、做决策、发动作 → 一起执行 → 所有动作都做完 → 再发观测 → ……
```

**什么时候开始执行**

1. 仿真停着时，等所有"有人用"的臂都发来动作，再让这些动作在同一拍一起开始。有人用的臂还没有动作，说明它的节点还在想，就继续等。
2. 没人用的臂不用等，原地不动。"没人用"包括从来没被占过，以及节点做完任务后已经放开。
3. 一份观测对应一次执行：这次执行里的动作都是根据同一份观测做的决策，动作上的 `step` 就是这份观测的拍号。

**什么时候发观测**

1. 执行开始后一直往下走，直到这次执行里所有动作都做完。先做完的臂原地不动，等其他臂做完。
2. 所有动作都做完了，仿真停下，发一次观测，标上当前拍号。停着期间不再重复发，执行过程中也不发。
3. 执行过程中新发来的动作不会插进当前这次执行，要等下一次。

**每一轮循环**

1. 有新 run 的请求，先清掉旧 run 留下的 holder 和动作（见 2.2）。命令口有命令就执行（见 2.7）。
2. 有正在执行的动作：让驱动走一拍，拍号加 1。这一拍里结束的动作，按结果记为 completed 或 failed。然后保存场景。
3. 没有正在执行的动作：如果所有有人用的臂都有了新动作，就把它们改成 running，开始下一次执行；否则停着。刚停下时发一份观测，然后等一会儿。
4. 回到第 1 步。

**一个顺序要求**：最后一个动作做完时，要先发这一拍的观测（本体状态和观测两个文件），再写这个动作的结束状态，最后保存场景。先做完的动作在结束时还没有新观测，节点要等 `state.json` 的拍号变大，才说明这次执行全部结束、新的本体状态和观测都已发出。

**例子**

A 管左臂，B 管右臂。L1 要执行 15 拍，L2 要执行 25 拍。

| 第几拍 | 左臂（节点 A） | 右臂（节点 B） | 底层 |
|---|---|---|---|
| 0 | 读观测 0，开始想 | 读观测 0，开始想 | 刚启动，停着：发观测 0 |
| 0 | 下发 L1（`step`=0） | 还在想 | B 还没有动作，继续等 |
| 0 | | 下发 L2（`step`=0） | 两条臂都有动作了，一起开始 |
| 1→15 | 执行 L1 | 执行 L2 | 在走，不发观测；每拍存一次场景 |
| 15 | L1 执行完，原地不动 | 继续执行 L2 | 还有动作没做完，不发观测 |
| 16→25 | 等着 | 执行 L2 | 在走；每拍存一次场景 |
| 25 | | L2 执行完 | 全部做完：先发观测 25，再把 L2 记为 completed，最后存场景 |
| 25 | 读观测 25，下发 L3（`step`=25） | 读观测 25，下发 L4（`step`=25） | 两个都发来了，一起开始下一次执行 |

## 4. 驱动接口

接入一个新的仿真器，就是写一个 `RobotDriver` 的子类。方法分五组：本体信息、观测、执行、场景、生命周期。带 `@abstractmethod` 的必须实现，其余可选。

### 4.1 本体信息（启动时调一次）

```python
class RobotDriver(ABC):

    @abstractmethod
    def describe(self) -> str:
        """一段文字介绍本体，原样写进 body.md。"""

    @abstractmethod
    def arm_names(self) -> list[str]:
        """对上暴露的臂，例如 ["left_arm", "right_arm"]。"""

    @abstractmethod
    def camera_names(self) -> list[str]:
        """所有相机，例如 ["cam_head", "cam_left_wrist", "cam_right_wrist"]。"""
```

### 4.2 观测（仿真停下来时，在同一拍里依次调用）

```python
    @abstractmethod
    def get_joint_positions(self, arm: str) -> list[float]:
        """这条臂的关节角，单位 rad，实测值。"""

    @abstractmethod
    def get_ee_pose(self, arm: str) -> list[float]:
        """这条臂的末端位姿 [x, y, z, qw, qx, qy, qz]，世界系，单位米，实测值。"""

    @abstractmethod
    def get_gripper(self, arm: str) -> float:
        """夹爪实测开度，0~1，1 = 全开。"""

    @abstractmethod
    def get_images(self) -> dict[str, np.ndarray]:
        """所有相机当前的图像：{相机名: H×W×3 的 uint8 RGB}。一次拍全。"""

    @abstractmethod
    def get_intrinsics(self, camera: str) -> np.ndarray:
        """这个相机的 3×3 内参矩阵。"""

    @abstractmethod
    def get_extrinsics(self, camera: str) -> np.ndarray:
        """这个相机的 4×4 外参矩阵，相机到世界，和末端位姿用同一个世界系。"""

    def get_extra(self) -> dict:
        """可选：想额外放进观测的字段，比如任务指令。默认为空。"""
        return {}
```

图像是 H×W×3 的 uint8 numpy 数组，底层用 Pillow 存成 JPEG（质量 95）；内外参是 numpy 矩阵。

### 4.3 执行（每走一拍调用一次）

```python
    @abstractmethod
    def execute_action(self, actions: dict[str, dict]) -> dict[str, str]:
        """让仿真走一拍，每条臂做它当前的动作。

        actions 是 {臂: {"id", "action_type", "parameters"}}，只包含有动作的臂；不在里面的臂原地不动。
        第一次见到的 id 是这一拍开始的新动作；不再传进来的 id，驱动可以忘掉。两条臂怎么合成一拍，在这里决定。

        返回这一拍结束的动作：{臂: 结果}。结果是一句人能读懂的话，和 1.0 一样，以 "Error:" 或 "Failed:" 开头算失败，
        其余算完成。不支持的动作类型或参数，立即以 "Failed: ..." 结束。抛异常则传进来的动作都算失败。"""
```

### 4.4 场景（只给底层用，节点看不到）

```python
    @abstractmethod
    def save_scene(self, path: Path) -> None:
        """把此刻的整个场景存到这个文件，要能原样恢复：物体的位姿和速度、关节、布料和流体、上一次的指令、任务进度等。
        格式由驱动自己定。底层每走一拍调一次，所以要快；慢的话可以先把状态拷到内存，再在后台线程写盘。"""

    @abstractmethod
    def load_scene(self, path: Path) -> None:
        """从这个文件恢复场景，返回时世界和存的时候一模一样，可以拍观测。
        文件格式和 save_scene 存的一样；底层启动时用它从 robot/scene 恢复，执行重置命令时用它载入 --scene 指定的文件。
        文件不存在或格式不对，就直接抛异常。"""
```

重置不需要单独的接口：重置就是 `load_scene(--scene 指定的文件)`。

### 4.5 生命周期

```python
    @abstractmethod
    def start(self) -> None:
        """启动仿真环境。只负责把仿真起起来，场景由底层随后调 load_scene 加载。"""

    @abstractmethod
    def close(self) -> None:
        """关闭仿真环境，释放资源。启动失败后调用、或者被调用多次，都要能安全执行。"""

    def is_finished(self) -> bool:
        """可选：要不要结束运行，比如回合已经结束。返回 True 后 run_simulation 退出。默认 False。"""
        return False
```

驱动的构造函数只接收配置参数，不做耗时的事；真正启动仿真放在 `start()` 里，和 1.0 的 `load_environment()` 一样。

`start()` 之前，底层把对话的 `robot/` 文件夹赋给驱动的 `folder` 属性（基类默认 `None`）。驱动要存自己的文件（比如录像）就放在这里，节点不读；位置由 `paths.py` 给，比如 `recorder_dir(folder)`。

### 4.6 底层什么时候调哪个方法

```text
启动：
    start()：把仿真环境跑起来
    robot/scene 不存在：把 --scene 指定的文件拷过来
    load_scene(robot/scene)
    describe() 的内容写进 body.md
    上次留下的、没做完的动作和命令记为 failed，所有 holder 清空
    拍号：有 state.json 就取它的拍号加 1，没有就是 0
    上传观测

每一轮：
    来了新 run 的请求：清掉旧 run 的 holder，它没做完的动作记为 failed，结果写 "Failed: run ended."
    命令口有命令（现在只有 reset）：
        load_scene(--scene 指定的文件)；拍号加 1，上传新场景的观测；
        等写盘线程写完，再把没做完的动作都记为 failed，结果写 "Failed: scene changed."；存场景
        命令记为 completed；抛异常就记为 failed，结果写 "Error: <异常类型>: <异常信息>"
    有正在执行的动作，或者所有有人用的臂都发来了新动作（新动作改成 running，一起开始）：
        results = execute_action({正在执行动作的臂: 它的动作})
        拍号加 1
        results 里每个结束的动作：按结果判断 completed 或 failed（以 "Error:"、"Failed:" 开头算 failed）
            如果这是最后一个动作，取观测交给写盘线程，写盘线程写完快照再写状态、finished_at、duration_ms；否则直接写
        save_scene：先存到临时文件，再整体替换 robot/scene
    写盘线程还有快照或跟在它后面的结束状态没写完：先等它写完，再读 action.json
    否则停着：如果是刚停下，上传观测；然后等一会儿
    is_finished() 为真：调用 close()，退出

上传观测（在同一拍里调完）：
    每条臂：get_joint_positions(arm)、get_ee_pose(arm)、get_gripper(arm)    → 本体状态
    get_images()，按拍号存成 obs/<拍号>/<相机名>.jpg                       → 观测
    每个相机：get_intrinsics(camera)、get_extrinsics(camera)                → 观测
    get_extra()                                                           → 观测的 extra
    写 obs/<拍号>/state.json、obs/<拍号>/observation.json，再换 observation.json，最后换 state.json
```

### 4.7 出错时

- `start()` 或启动时的 `load_scene` 抛异常：仿真没起来。`run_simulation` 调一次 `close()` 后退出。`robot/scene` 不动。
- `execute_action` 抛异常：和 1.0 一样，这一拍传进去的动作都记为 failed，结果写 "Error: <异常类型>: <异常信息>"。
- `save_scene` 抛异常：记日志，`robot/scene` 保持上一份，下一拍再存。
- 写盘线程写快照出错：记日志，跟在后面的结束状态照样写，等着动作的节点不会卡住。
- 执行重置命令时 `load_scene` 抛异常：这个命令记为 failed，结果写 "Error: <异常类型>: <异常信息>"。
- 观测相关的方法抛异常：说明仿真本身出了问题。底层把所有没做完的动作记为 failed，然后退出。
- 启动时，上次留下的、没做完的动作和命令一律记为 failed，结果写 "Failed: simulation restarted."，从不重放；所有 holder 清空。`obs/` 不清，拍号接着数。
- 退出时，这次没做完的动作和命令记为 failed，结果写 "Failed: simulation stopped."。场景已经在每一拍存好了，退出时不用另存。
- **Ctrl+C 要收回给 Python**：Isaac Sim 会装自己的 SIGINT 处理，收到信号时直接关掉自己的 app 并从信号落点结束进程，不抛 `KeyboardInterrupt`——上面那条"退出时记为 failed"就执行不到，动作会留在文件里显示成还在跑。`run_simulation` 因此在下层 `start()` 之后把 SIGINT 恢复成 Python 默认的处理（`_take_back_sigint()`），Ctrl+C 才是一次正常的 `KeyboardInterrupt`。

### 4.8 底层入口：run_simulation

`run_simulation` 负责把仿真环境运行起来，所有仿真器共用一个。它依次做四件事：

1. 调 `driver.start()` 启动仿真环境，再从 `robot/scene` 恢复场景（第一次就先从 `scene` 参数指的文件拷一份）。
2. 写 body.md，上传一次观测。
3. 按 4.6 的循环一直运行：读 action.json 和 command.json，按节拍调用驱动的方法，再把观测、结果和场景写回文件。
4. `driver.is_finished()` 为真、出错，或者被 Ctrl+C 停下时，调 `driver.close()` 关掉仿真。

```python
def run_simulation(driver: RobotDriver, session_dir: Path, scene: Path, *, poll_interval: float = 0.01) -> None:
    """启动仿真环境，从对话的 robot/scene 恢复场景，按节拍运行，直到 is_finished() 为真或者出错，最后关闭仿真。"""
```

| 参数 | 含义 |
|---|---|
| `driver` | 某个仿真器的驱动对象，此时还没启动 |
| `session_dir` | 要服务的对话文件夹 `sessions/<对话>/`；机器人文件在它下面的 `robot/` |
| `scene` | 初始场景文件，格式和驱动 `save_scene` 存的一样。对话第一次启动时拷成 `robot/scene`；执行重置命令时也载入它 |
| `poll_interval` | 仿真停着等的时候，每隔多少秒看一次有没有新动作或新的命令，默认 0.01 秒 |

命令行有两种用法：

```bash
# 给对话记录一个新的底层并启动：从这个场景重新开始
python -m robot --session <对话文件夹> --driver robodojo --scene first_scene.json [--config robodojo.json]
# 按对话记录的底层接着跑：从 robot/scene 恢复
python -m robot --session <对话文件夹>
```

- **记录**：第一种用法先确认驱动名有效，再把驱动名和配置写进 `robot/launch.json`，把场景文件拷成 `robot/first_scene`，并删掉旧的 `robot/scene`，所以是从新场景重新开始。
- **启动**：两种用法最后都一样：按 `launch.json` 找到驱动类（驱动注册表）、构造驱动，以 `robot/first_scene` 为初始场景调 `run_simulation`。
- 对话从没记录过底层时，第二种用法直接报错退出。

**一个例子**：对话 S1 第一次用第一种用法启动，拍号从 0 开始。跑到第 120 拍时把进程杀掉，`robot/scene` 是第 120 拍存下的。再用第二种用法启动，载入它，世界回到第 120 拍的样子，拍号从 121 接着数。

### 4.9 谁来启动底层：TUI

平时不用手动启动。TUI（顶层包 `tui/`）替每个对话管底层，用的是 `robot.SimulationProcess`：它以子进程运行 `python -m robot --session <对话>`，输出写进 `robot/simulation.log`，环境变量（包括 `CUDA_VISIBLE_DEVICES`）跟 TUI 一样；`stop()` 像 Ctrl+C 一样停它并等它退出。

| 时机 | TUI 做什么 |
|---|---|
| 启动 `emerge`、`/new` | 依次选环境、选任务（`group`）、选布局（`name`），选项来自驱动的 `scenes()`（见 4.10）；先停掉当前对话的底层，开新对话，用 `record_launch` 记下选择，再启动底层 |
| `/sessions` 打开旧对话、`emerge -s <id>` | 停掉当前对话的底层，打开那个对话；它记录过底层的话就启动，从保存的场景接着跑 |
| `/quit`、退出 | 对话关闭后停掉底层 |

侧边栏的 ROBOT 一栏显示驱动、配置、状态（启动中 / 运行中 / 已退出）和当前拍号；退出了就给出日志路径。

### 4.10 能从哪些场景开始：scenes()

```python
@dataclass(frozen=True)
class SceneChoice:
    group: str               # 给人挑的第一级，比如任务 "stack_bowls"
    name: str                # 第二级，比如 "layout 0"
    config: dict             # 驱动的构造参数
    scene: Any               # 第一个场景，驱动自己的格式（要能转成 JSON）

class RobotDriver(ABC):
    @classmethod
    def scenes(cls) -> list[SceneChoice]:
        """能从哪些场景开始；不启动任何东西，要快。默认空。"""
```

RoboDojo 的实现是扫描 `Assets/Eval_Layout/RoboDojo/arx_x5/0/` 下的布局文件：54 个任务、2690 个布局，`config` 是 `{"task", "env_cfg", "layout_seed"}`，`scene` 是 `{"task", "layout_id"}`。

## 5. 节点这边：RobotClient

`RobotClient` 是节点用的接口：占臂、放臂、发动作、读本体状态和观测，对节点来说就是一组可以直接调用的工具。它是一个具体的类，只做一件事：读写对话文件夹下 `robot/` 里的文件，和 `run_simulation` 交流。

- 每个环境的客户端继承它，加上按自己动作集写好的方法（比如 RoboDojo 的 `move_joints(...)`），节点就不用手拼 `("move", {...})`。
- 以后接真机，改的是仿真这一侧：在 `simulation/` 下写一个新的驱动，照样读写这个文件夹，节点这一侧不用改。

所有方法都立即返回，不会在里面等。需要等的话，由节点自己轮询（节点的规则由用户写）。下面是它的方法：

```python
class RobotClient:

    def __init__(self, session_dir: Path, run: str, node: str) -> None:
        """run 和 node 是使用者是谁：哪个 run 里的哪个节点。占臂、发动作时都写上。"""

    # 本体
    def read_description(self) -> str:
        """body.md 的内容。"""

    # 臂
    def hold(self, arm: str) -> None:
        """占住一条臂。这条臂已被同一个 run 里别的节点占着时，抛 ArmHeld；别的 run 占着的算没人占。"""

    def release(self, arm: str) -> None:
        """放开这条臂。做完任务、不再发动作时调用，底层就不再等这条臂。"""

    # 动作
    def send_action(self, actions: dict[str, tuple[str, dict]], step: int) -> dict[str, str]:
        """下发动作：{臂: (action_type, parameters)}，可以一次给两条臂。
        step 是做决定时看的那份观测的拍号。返回 {臂: 动作 id}。
        只要有一条臂还有本 run 没结束的动作（抛 ArmBusy），或者被本 run 别的节点占着（抛 ArmHeld），
        这一次就一个动作都不写。"""

    def read_action(self, arm: str) -> dict | None:
        """这条臂当前的动作，包括状态、结果等。没有动作时返回 None。
        以前的动作在本体的 history.jsonl 里，是给人 debug 的，节点不读。"""

    # 本体状态和观测
    def read_state(self, tick: int | None = None) -> dict | None:
        """读本体状态：不给 tick 就读最新一份（state.json），给了就读 obs/<tick>/state.json。
        没有这一份时返回 None。"""

    def read_observation(self, tick: int | None = None) -> dict | None:
        """读观测：不给 tick 就读最新一份（observation.json），给了就读 obs/<tick>/observation.json。
        没有这一份时返回 None。里面的图像路径相对于对话文件夹。"""


class RobotError(Exception): ...     # 机器人这一层所有错误的基类，比如臂名不存在、仿真还没启动
class ArmHeld(RobotError): ...       # 这条臂已被本 run 别的节点占着
class ArmBusy(RobotError): ...       # 这条臂上还有没结束的动作
```

节点能做的就是上面这些：占臂、放臂、发动作、读本体状态和观测。场景的保存和恢复由底层自己做，场景操作走命令口（2.7），节点都碰不到。

命令口给 TUI 用的函数（不在 `RobotClient` 里）：

```python
# robot/base/frontend/command_port.py
def send_command(session_dir: Path, op: str) -> str:
    """往 command.json 写一个命令，立即返回命令 id。上一个命令还没做完时抛 CommandBusy。"""

def read_command(session_dir: Path, command_id: str) -> dict | None:
    """查看这个命令的状态和结果；不是当前这个命令时返回 None。"""


class CommandBusy(RobotError): ...   # 上一个命令还没做完
```

节点里大概这样用：

```python
from robot import RobotClient    # 或换成环境自己的客户端，比如 RoboDojoClient

robot = RobotClient(ctx.session_dir, run=ctx.run_id, node=ctx.node_id)    # 文件在 ctx.session_dir / "robot"
robot.hold("left_arm")
state = robot.read_state()
obs = robot.read_observation(state["tick"])    # 和 state 同一拍的观测
ids = robot.send_action({"left_arm": ("<动作类型>", {...})}, step=state["tick"])
# 动作还是 pending 或 running 时怎么等、做完后怎么等新观测，由节点自己决定
robot.release("left_arm")
```

写节点规则时要注意两点：

- 自己的动作做完后，要等 `state.json` 的拍号变大，再读本体状态和观测、做下一步决策。这次执行里可能还有别的臂没做完，新的一拍要等所有动作都做完才会发出。
- 节点占着臂又去等别的节点，会卡死。比如 A 占着左臂、等 B 拉开门：下一次执行要等所有占着臂的节点都发来动作，A 不发，B 拉门的动作就开始不了。等别人之前，要先放开臂，或者发一个原地保持的动作。

## 6. 接 RoboDojo（已接通）

代码在 `robot/robodojo/`：`RoboDojoDriver`（驱动）、`RoboDojoClient`（客户端），注册名 `robodojo`。RoboDojo 本身一行没改。

### 6.1 怎么跑起来

平时由 TUI 启动（4.9）。手动启动：

```bash
python -m robot --session <对话文件夹> --driver robodojo --scene first_scene.json --config robodojo.json
```

- `robodojo.json`（驱动配置）：`{"task": "stack_bowls", "device_id": 5}`。可选项：`env_cfg`（默认 `arx_x5`）、`layout_seed`（评测布局集，默认 0）、`step_lim`（默认 100000；任务自带的上限是按一个评测回合定的，比如 `stack_blocks` 550 步，agent 反复尝试很快就会用完）、`finish_on_episode_end`（RoboDojo 判回合结束时底层退出，默认 true）、`headless`（默认 true）、`record`（RoboDojo 自带的回合录像，默认 false，见 6.2）。换任务要换进程。
- `first_scene.json`（第一个场景）：`{"task": "stack_bowls", "layout_id": 0}`，指 `Assets/Eval_Layout/RoboDojo/arx_x5/<layout_seed>/stack_bowls_0.json` 这个评测布局。
- 启动约 1 分钟（Isaac Sim 约 15 秒，建场景、cuRobo 编译内核占其余时间）；之后每拍约 140 ms（含每拍存一次场景）。

### 6.2 驱动怎么做

- **建环境**：照 RoboDojo `src/eval_client/main.py` 的做法拼配置、调 `create_eval_env`，只服务一个环境（`num_envs=1`）。策略服务器的客户端换成一个什么都不发的替身（RoboDojo 重置时会调它）。
- **重置后先登记成功条件**：照 `run_eval()` 开头调 `run_reward()`、`get_score()`。不登记的话 RoboDojo 把"没有检查项"算成功，第一拍就结束回合。
- **一拍**：两条臂的目标拼成 `left/right_arm_joint_state`、`left/right_ee_joint_state` 四个键，调一次 `env.take_action`。没有动作的臂沿用上一次的指令（还没下过指令时用当前实测值）。
- **动作集**（都在关节空间，`ticks` 拍内匀速插值）：

| 动作 | 参数 | 说明 |
|---|---|---|
| `move_joints` | `joints`（6 个，rad）、`gripper`（0~1，可选）、`ticks`（默认 25） | 关节空间直线运动 |
| `move_ee` | `pose`（`[x, y, z, qw, qx, qy, qz]`，世界系）、`gripper`、`ticks` | 开始时解一次逆运动学，之后同 `move_joints`；无解则立即 failed |
| `set_gripper` | `gripper`（0~1，1 全开）、`ticks`（默认 10） | |
| `stay` | `ticks`（默认 1） | 停在上一次指令的位置 |

  动作结束时，实测值可能还没完全到目标（比如夹爪 10 拍后实测 0.19），之后几拍会继续收敛。

- **观测**：关节 `rm.get_joint`，末端 `rm.get_real_endpose(is_relative=False)`（世界系），夹爪按 `gripper_scale` 归一化，图像取一次 `env.get_obs()` 的 `vision.<相机>.color`，内外参取 `camera_manager`。`get_extra` 给任务指令和步数：`{"instruction", "steps", "step_lim"}`。
- **录像**：用 RoboDojo 自带的。它每调一次 `get_obs()` 就往回合视频里追加一帧（每个相机一个 mp4，帧率 `collect_freq`，`arx_x5` 是 25，正好是真实速度），但只在 `run_eval()` 里收尾，驱动不调它，所以驱动自己管：
  - `record` 为 false（默认）：把这个追加关掉，不留半截的临时视频。
  - `record` 为 true：每拍 `take_action` 之后调一次 `get_obs()`，一拍一帧；同一拍的快照直接用这次的观测，不多记一帧。录制中的临时文件和收尾后的视频都放在对话的 `robot/recorder/`（驱动把 RoboDojo 的 `_stream_dir` 指到这里，不再写它的 `eval_result/`）。重置场景（`load_scene`）前和关闭时调 `save_video` 收尾，存成 `recorder/<进程启动时间>_<序号>_<相机>_<success|fail|unfinished>.mp4`；回合没结束就是 `unfinished`，带启动时间是为了重启后不覆盖之前的视频。录制中的是 `env0_<相机>.tmp.mp4`，进程被杀时留下的会在下次录像时被覆盖。世界停着等节点时不走拍，视频里没有这段。
- **回合结束**：RoboDojo 在任务成功或步数用完时结束回合，之后不再执行动作；`finish_on_episode_end` 为 true（默认）时底层随之退出。恢复场景时按当前的 `step_lim` 重新判断：因为步数用完而结束的，步数还有余量就继续；因为任务成功而结束的，仍是结束。
- **场景文件**（JSON）：`{"task", "layout_id", "state"}`。`state` 里有：每条臂全部关节的位置和速度；每个物体（刚体、关节体、几何体）的位姿、速度、关节；流体的粒子位置；RoboDojo 的步数和回合标志；每条臂上一次的指令。`load_scene` 先按 `layout_id` 重建布局，再把 `state` 写回去。
- **已实测**：把左臂摆到一个姿态后 `kill -9`，再用同一条命令启动，关节差 < 0.001 rad，碗的位姿、夹爪、步数一致，拍号接着数。
- **还恢复不了的**（会写在 `state.not_restored` 里）：任务的奖励记录（RoboDojo 存在任务对象内部）；布料（garment）类物体还没写。

### 6.3 RoboDojo 要注意的事实

| 事实 | 位置 |
|---|---|
| 一次 `take_action` 推进 0.04 秒：内部 10 个物理子步，前 8 步插值，后 2 步保持。每调用一次都计入 `step_lim` | `src/eval_client/eval_env.py:475` |
| 关节动作必须同时给齐四个键：`left/right_arm_joint_state`（各 6 维）和 `left/right_ee_joint_state`（各 1 维） | `eval_env.py:358` |
| 一次动作里两条臂不能混用关节空间和末端空间 | `eval_env.py:351` |
| 末端模式每步只做一次 IK；IK 失败时那条臂不报错，原地不动 | `eval_env.py:415` |
| obs 里的夹爪是上一次的命令值，不是实测值；1 表示全开 | `env/observation_manager/obs_manager.py:190`、`env/reward_manager/func_parser.py:1523` |
| obs 里的末端位姿相对环境原点，相机外参却是世界系，驱动要统一成一个坐标系 | `env/robot_manager/robot_manager.py:131`、`env/camera_manager/camera_manager.py:469` |
| `get_obs()` 只渲染、不推进物理，但会往回合视频里追加一帧；视频只在 `run_eval` 里由 `save_video` 收尾，`reset` 和 `close` 会把没收尾的丢掉 | `eval_env.py:269`、`eval_env.py:921`、`eval_env.py:199`、`eval_env.py:224` |
| 任务成功或步数用完时回合结束；每次 `take_action` 之后都会自动检查 | `eval_env.py:465`、`eval_env.py:841` |
| 两条臂共用一个 CuRobo 规划器，碰撞世界里只有桌子 | `robot_manager.py:51`、`env/planner_manager/curobo_planner.py:425` |
| 左臂基座在 (-0.3, -0.45, 0.765)，右臂在 (0.3, -0.45, 0.765)；关节速度上限 5 rad/s | `env_cfg/robot/dual_x5.yml`、`env/robot_manager/robot_config/x5.py:41` |
| 同一任务内换布局用 `env.reset(seed=[i])`；换任务要重启 Isaac 进程 | `src/eval_client/main.py` |
| 布局文件 `Assets/Eval_Layout/<基准>/<配置>/<种子>/<任务>_<编号>.json` 记录物体的初始摆放。`env.reset(seed=[编号])` 按这个文件重建场景，臂回到初始姿态 | `env/seed_manager/seed_manager.py`、`src/eval_client/eval_env.py:208` |
| 场景里的物体分刚体、关节体、布料、流体等几类，各自读状态的方式不同 | `env/scene_manager/objects/`、`env/scene_manager/layout_manager.py:420` |
| 回合进行中调 `env.reset`，会把 RoboDojo 的步数清零，main.py 也会把这一回合的成绩记在新布局名下 | `eval_env.py:208`、`eval_env.py:771` |

## 7. 2.0 要改的

- **共用一个环境**：agent 和底层共用 EmergePolicy2.0 这个 conda 环境。`robot/` 用到的 numpy、Pillow 已加进 `pyproject.toml` 的依赖，并装进了这个环境。
- **Python 3.11（已做）**：Isaac Sim 5.1 只支持 3.11，EmergePolicy2.0 已换成 3.11。2.0 里两处 PEP 695 泛型改成了 `TypeVar`（`emerge/runtime/context.py`、`emerge/storage/artifacts.py`），`requires-python` 改成 `>=3.11`。
- **环境里装了什么**：按 RoboDojo `scripts/install.sh` 的版本装 PyTorch 2.7.0+cu128、Isaac Sim 5.1、Isaac Lab（`third_party/RoboDojo/third_party/IsaacLab`）、cuRobo（不编译，内核运行时编译），以及 RoboDojo 锁定的那些包；再装 emerge 和 robot（editable）。和 RoboDojo 的锁定只有一处不同：emerge 依赖的 anthropic 要 `typing_extensions>=4.14`，而 `isaacsim-kernel` 写死了 4.12.2，这里放开成 4.16（`typing_extensions` 只增不删，Isaac Sim 实测能跑）；rich 按 emerge 的要求是 13.9.4。安装脚本在 `/data/yuqingchi/tmp/emerge-robot/install/`。
- **安装时要注意的网络情况**：这台机器走代理约 1 MB/s；国内镜像直连快，但只对 HTTP/2 快，HTTP/1.1 每个连接被限到约 0.2 MB/s。pip 只会 HTTP/1.1，所以用 uv（HTTP/2）装包；Isaac Sim 的 wheel 在 `pypi.nvidia.cn`（PyPI 上的 isaacsim-* 只是占位包）；资产从 `hf-mirror.com` 多连接并行下载。
- **RoboDojo 自己的准备**：子模块 IsaacLab、curobo、XPolicyLab 按锁定版本拉取；资产在 `third_party/RoboDojo/Assets`（41 GB）；`utils/update_embodiment_config_path.py` 要在 RoboDojo 根目录跑一次，生成带本机绝对路径的 `Assets/Robots/*/curobo.yml`。
- **TUI 搬到顶层 `tui/`**：从 `emerge/tui/` 搬出来，和 `emerge/`、`robot/` 并列，依赖这两个包（反过来没有依赖）。`emerge` 命令指向 `tui.cli:main`：终端里打开 TUI，否则走 emerge 的 headless；`python -m emerge` 只剩 headless。emerge 为此加了 `config.api_key_missing()` 和 `Application.create(new_session=True)`。
- **新增 `robot/`**：在仓库根目录，和 `emerge/` 并列，是一个独立的包，不依赖 `emerge`。`pyproject.toml` 的打包列表里加上了它。graph、runtime、agent 都不动；节点只用到 `ctx.session_dir`、`ctx.run_id`、`ctx.node_id`：

```
robot/
  __init__.py                 对外导出 base 里的类
  __main__.py                 命令行入口：python -m robot --session ... [--driver ... --scene ... --config ...]
  base/                       所有环境共用的部分；三方互不调用，只通过对话的 robot/ 文件夹交流
    errors.py                 RobotError、ArmHeld、ArmBusy、CommandBusy
    protocol/                 robot/ 文件夹本身：三方共同遵守的约定，不认识任何一方
      paths.py                每个文件放在哪
      fileio.py               整文件原子落盘；两边都写的文件加锁读-改-写（edit_json）
      lifecycle.py            动作和命令共有的生命周期：Status、status_of、Request（start/end）
      actions.py              action.json：Holder、Action、Arm、ActionFile（read/edit）
      commands.py             command.json：Command（read/edit），命令名 RESET
      snapshots.py            state.json、observation.json：State、Observation、Snapshot；publish 的写入顺序
    client/                   节点这一侧
      robot_client.py         RobotClient：节点用的接口，占臂、发动作、读状态和观测；环境的客户端继承它
    simulation/               仿真这一侧
      driver.py               RobotDriver：抽象类，每个环境继承它；SceneChoice
      capture.py              从驱动拍一份快照：numpy 转成 list，图像编成 JPEG
      loop.py                 run_simulation()：启动、按节拍推进、执行命令、存场景、退出时关闭
    frontend/                 前端（TUI）这一侧
      registry.py             环境注册表：环境名 → 驱动类和客户端类
      launch.py               对话的底层怎么启动（launch.json、first_scene），作为子进程运行（SimulationProcess）
      command_port.py         命令口：send_command、read_command
  robodojo/                   RoboDojo 环境：RoboDojoDriver(RobotDriver)、RoboDojoClient(RobotClient)
```

- `third_party/RoboDojo` 是 submodule；它的子模块和资产都已拉好，见上。

## 8. 先不做的

- 动作集（暂不确定）。
- 排队、动作历史。

## 9. 待定

- **一次声明，两边共用**：在 base 里给每个观测接口声明一次它落在哪个文件、哪个位置、按臂还是按相机；底层按声明写，client 按同一份声明读，驱动加一个接口，节点就能直接调同名方法。方向已认可，细节没定。
- **`get_extra` 的去留**：任务指令这类环境特有的接口先不考虑，大概率去掉。

# EMERGE-Policy 2.0

用户只和一个主 agent 对话。主 agent 用工具在运行时搭建、启动和修改一张图（Graph），后台的 Runtime 执行这张图，并把进度和结果报告给 agent。

## 安装

项目分两层，装在同一个 conda 环境里：

- `emerge`：agent 这一层。
- `robot`：硬件这一层，负责操作机器人。现在接的是 RoboDojo 的仿真机器人（基于 Isaac Sim 5.1），对接代码在 `robot/robodojo/`；以后接真机，就是在 `robot/` 下再加一个驱动。

两层之上是界面 `tui`：全屏终端界面，它打开对话，也替每个对话启动和停止机器人。

第 2～6 步装的都是 RoboDojo 仿真要用的东西。Isaac Sim 5.1 只支持 Python 3.11，所以环境是 3.11。只用 agent 的话，做第 1 步和第 7 步就够了。

### 1. 建环境、拉代码

```bash
conda create -n EmergePolicy2.0 python=3.11 -y
conda activate EmergePolicy2.0
git submodule update --init third_party/RoboDojo
cd third_party/RoboDojo
git submodule update --init third_party/IsaacLab third_party/curobo XPolicyLab   # RoboDojo 锁定的提交
cd ../..
```

### 2. PyTorch 和 Isaac Sim

版本和 RoboDojo 的 `scripts/install.sh` 一致。

```bash
pip install torch==2.7.0 torchvision==0.22.0 torchaudio==2.7.0 --index-url https://download.pytorch.org/whl/cu128
pip install "isaacsim[all,extscache]==5.1.0" --extra-index-url https://pypi.nvidia.com
```

### 3. RoboDojo 锁定的包

RoboDojo 用的是下面这些版本，它在后面每一步装完后都会把这些版本装回来，我们也照做。

```bash
cat > /tmp/robodojo-pins.txt <<'EOF'
numpy==1.26.0
packaging==23.0
typing_extensions==4.12.2
filelock==3.13.1
websockets==12.0
click==8.1.7
psutil==5.9.8
wheel==0.45.1
starlette==0.45.3
scipy==1.15.3
warp-lang==1.11.0
onnx>=1.18,<1.22
ipython<9
virtualenv==20.30.0
EOF
pip install -r /tmp/robodojo-pins.txt
pip install huggingface_hub transforms3d open3d msgpack-numpy opencv-python-headless==4.11.0.86 pillow matplotlib scikit-learn
```

### 4. Isaac Lab

和 `isaaclab.sh --install none` 做的事一样：把 `source/` 下的每个包以 editable 方式装上。Isaac Lab 会要求把部分锁定的包升级（比如 starlette 0.49.1），装完后再把锁定的版本装回来，和 RoboDojo 的安装脚本一样。

```bash
LAB=third_party/RoboDojo/third_party/IsaacLab/source
pip install "setuptools<82.0.0" setuptools_scm
pip install -e $LAB/isaaclab -e $LAB/isaaclab_assets -e $LAB/isaaclab_contrib -e $LAB/isaaclab_tasks \
            -e "$LAB/isaaclab_rl[none]" -e "$LAB/isaaclab_mimic[none]"
pip install -r /tmp/robodojo-pins.txt
```

### 5. cuRobo

不需要编译：它的 CUDA 内核在运行时才编译，所以不需要版本匹配的 `nvcc`。

```bash
pip install --no-build-isolation -e "third_party/RoboDojo/third_party/curobo[cu12]" -c /tmp/robodojo-pins.txt
pip install -r /tmp/robodojo-pins.txt
```

### 6. RoboDojo 的资产和机器人配置

资产一共 41 GB，其中 `Assets/Material` 就占 26 GB。`update_embodiment_config_path.py` 会生成 `Assets/Robots/*/curobo.yml`，里面写的是本机的绝对路径；仓库换了位置，要重新跑一次。

```bash
cd third_party/RoboDojo
bash scripts/init_assets.sh                      # 从 Hugging Face（RoboDojo-Benchmark/RoboDojo）下载 Assets/
python utils/update_embodiment_config_path.py
cd ../..
```

### 7. emerge 和 robot

```bash
grep -v typing_extensions /tmp/robodojo-pins.txt > /tmp/robodojo-pins-emerge.txt
pip install -e . -c /tmp/robodojo-pins-emerge.txt
```

这里是整个环境里唯一和 RoboDojo 锁定版本不一样的地方：emerge 依赖 `anthropic>=1.15`，它要求 `typing_extensions>=4.14`，而 `isaacsim-kernel` 写死了 4.12.2。`typing_extensions` 只会往后加东西、不会删，Isaac Sim 用新版本实测能跑，所以这一步让 pip 把它升级。pip 会提示和 `isaacsim-kernel` 冲突，这是预期内的。另外 `rich` 会按 emerge 的要求装成 13.x。

### 8. 验证

```bash
python -c "import emerge, robot, torch; print('CUDA:', torch.cuda.is_available())"
mkdir -p /tmp/emerge-check/session
echo '{"task": "stack_bowls"}' > /tmp/emerge-check/robodojo.json
echo '{"task": "stack_bowls", "layout_id": 0}' > /tmp/emerge-check/first_scene.json
CUDA_VISIBLE_DEVICES=0 python -m robot --driver robodojo --config /tmp/emerge-check/robodojo.json \
    --scene /tmp/emerge-check/first_scene.json --session /tmp/emerge-check/session
```

大约一分钟后，`/tmp/emerge-check/session/robot/` 下应该有 `STATE.json`、`OBSERVATION.json` 和 `obs/0/cam_head.jpg`（画面是桌上三个碗）。按 Ctrl+C 停止。机器人文件怎么组织、节点怎么用，见 `docs/robot-interface.md`。

### 国内下载慢怎么办

在我们的机器上，上面的安装有两处很慢，下面是实际管用的办法：

- **镜像对 pip 限速。** 国内镜像走 HTTP/2 很快，但 HTTP/1.1 每个连接被限到约 0.2 MB/s，而 pip 只会用 HTTP/1.1。[uv](https://github.com/astral-sh/uv) 走 HTTP/2，还能并行下载；`uv pip install --python $(which python) ...` 的参数和上面的 pip 命令一样。PyPI 用 `--default-index https://pypi.tuna.tsinghua.edu.cn/simple`；Isaac Sim 用 `--index https://pypi.nvidia.cn`（国内访问 `pypi.nvidia.com` 会跳转到这里；PyPI 上的 `isaacsim-*` 只是占位包）；再加上 `--index-strategy unsafe-best-match`。PyTorch cu128 的 wheel 在 `https://mirrors.aliyun.com/pytorch-wheels/cu128/`，用 `curl`（走 HTTP/2）下到一个目录，再用 `--find-links` 指给 uv。
- **资产在 Hugging Face 上。** `hf-mirror.com` 上也有，但同样每个连接约 0.2 MB/s，所以要多个文件同时下（我们开了 48 个并行的 `curl`）。文件列表可以在设置 `HF_ENDPOINT=https://hf-mirror.com` 后，用 `huggingface_hub.HfApi().list_repo_tree("RoboDojo-Benchmark/RoboDojo", repo_type="dataset", path_in_repo="Assets", recursive=True)` 拿到。文件放到 `third_party/RoboDojo/.cache/robodojo_assets_repo/` 下，再把 `third_party/RoboDojo/Assets` 链接到其中的 `Assets/`，和 `init_assets.sh` 的做法一样。

## 使用

```bash
CUDA_VISIBLE_DEVICES=5 emerge            # 或 python -m tui；-w 指定工作区（默认当前目录下的 workspace/）
```

打开后的流程：

1. **检查工作区**：工作区不存在就从模板 `emerge/workspace_template/` 建一个；模型服务还没有 key 的话，依次问模型服务、API 地址、API key 和模型名，写进工作区的 `config.json`。agent 和 runtime 的其余设置也在这个文件里。
2. **选机器人**：先选环境（现在只有 `robodojo`，也可以选"不接机器人"），再选任务，再选布局。
3. **开对话**：新建一个对话，同时在后台为它启动机器人（一个单独的 `python -m robot` 进程，用的 GPU 由 `CUDA_VISIBLE_DEVICES` 决定）。侧边栏的 ROBOT 一栏显示它的状态和拍号，输出在对话文件夹的 `robot/simulation.log`。

对话里的命令：

- `/new`：新建对话，同样先选机器人和场景；原来对话的机器人先停掉。
- `/sessions`：打开以前的对话接着做；它的机器人从上次停下的场景接着跑（机器人每走一拍都会把整个场景存一次）。
- `/quit`：退出，机器人跟着停掉。
- `emerge -s <对话 id>`：直接打开某个对话，机器人同样接着跑。

每个对话把自己的所有东西放在工作区里自己的文件夹 `sessions/<session_id>/` 下：数据库、记忆、在对话里生成的节点代码、各次运行（run），以及机器人的文件 `robot/`。工作区本身只放所有对话共用的设置（`config.json`、`AGENTS.md`、`USER.md`、`skills/`）。

不用界面时，`python -m emerge` 是 headless 模式：stdin 每行一条消息，stdout 输出 JSON Lines（不管机器人）。单独启动某个对话的机器人：`python -m robot --session <对话文件夹>`（按它记录的设置接着跑），或者加上 `--driver ... --scene ... [--config ...]` 让它从新场景重新开始。

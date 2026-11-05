# EMERGE-Policy 2.0

## 安装

### 1. 建环境、拉代码

```bash
conda create -n EmergePolicy2.0 python=3.11 -y
conda activate EmergePolicy2.0
git submodule update --init third_party/RoboDojo
cd third_party/RoboDojo
git submodule update --init third_party/IsaacLab third_party/curobo XPolicyLab
cd ../..
```

### 2. PyTorch 和 Isaac Sim

```bash
pip install torch==2.7.0 torchvision==0.22.0 torchaudio==2.7.0 --index-url https://download.pytorch.org/whl/cu128
pip install "isaacsim[all,extscache]==5.1.0" --extra-index-url https://pypi.nvidia.com
```

### 3. emerge、robot、Isaac Lab、cuRobo 和 RoboDojo 的锁定版本

```bash
pip install "setuptools<82.0.0" setuptools_scm
pip install --no-build-isolation -e ".[robodojo]"
pip install starlette==0.45.3
```

### 4. 资产

```bash
cd third_party/RoboDojo
bash scripts/init_assets.sh
python utils/update_embodiment_config_path.py
cd ../..
```

## 使用

```bash
emerge
```


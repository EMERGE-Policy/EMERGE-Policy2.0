# EMERGE-Policy 2.0

The user talks to a single main agent, which uses tools to assemble, start, and modify a Graph at runtime. A background Runtime executes the Graph and reports progress and results back to the agent.

## Installation

```bash
conda create -n EmergePolicy2.0 python=3.12 -y
conda activate EmergePolicy2.0
pip install -e .
```

## Usage

```bash
emerge
# or
python -m emerge
```

The workspace template ships in `emerge/workspace_template/`. On first run, `init_workspace()` copies it to the runtime workspace (`~/.emerge/workspace` by default, or the directory passed with `--workspace`). Providers, API keys, and model, agent, and runtime settings are configured in that workspace's `config.json`.



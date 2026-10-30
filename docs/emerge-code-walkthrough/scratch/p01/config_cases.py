"""Probe emerge.config behaviors (no network, no real workspace)."""
import copy
import json
import tempfile
from pathlib import Path

from emerge.config import WorkspaceConfig, load_config, init_workspace, ProvidersConfig, _template_dir
from emerge.exceptions import ConfigError

TEMPLATE = json.loads((_template_dir() / "config.json").read_text())


def run(name, data=None, raw=None, prep=None):
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d) / "ws"
        ws.mkdir()
        if raw is not None:
            (ws / "config.json").write_bytes(raw)
        elif data is not None:
            (ws / "config.json").write_text(json.dumps(data))
        if prep:
            prep(ws)
        try:
            c = load_config(ws)
            print(f"[OK] {name}: workspace={c.workspace == ws.resolve()}")
            return c
        except ConfigError as e:
            print(f"[ConfigError] {name}: {e}".replace(str(ws), "<ws>"))
        except Exception as e:
            print(f"[{type(e).__name__}] {name}: {e}".replace(str(ws), "<ws>"))


def mod(f):
    d = copy.deepcopy(TEMPLATE)
    f(d)
    return d


run("template", TEMPLATE)
run("missing file", None)
run("bad json", raw=b'{"a": 1,}')
run("BOM", raw=b'\xef\xbb\xbf' + json.dumps(TEMPLATE).encode())
run("non-utf8", raw=b'{"x": "\xff"}')
run("top-level array", raw=b'[]')
run("api_mode bad", mod(lambda d: d["model"].__setitem__("api_mode", "foo")))
run("unknown top field", mod(lambda d: d.__setitem__("foo", 1)))
run("unknown provider", mod(lambda d: d["providers"].__setitem__("gemini", {"base_url": "x"})))
run("thinking in model.options", mod(lambda d: d["model"]["options"].__setitem__("thinking", {"type": "adaptive"})))
run("thinking in openai options", mod(lambda d: d["providers"]["openai"]["options"].__setitem__("thinking", {"type": "adaptive"})))
run("include in openai options", mod(lambda d: d["providers"]["openai"]["options"].__setitem__("include", [])))
run("agent.max_iterations", mod(lambda d: d["agent"].__setitem__("max_iterations", 5)))
run("agent.request_timeout", mod(lambda d: d["agent"].__setitem__("request_timeout", 5)))
run("context above limit", mod(lambda d: d["agent"].__setitem__("max_context_tokens", 2000000)))
run("output above limit", mod(lambda d: d["model"]["options"].__setitem__("max_output_tokens", 200000)))
run("both above", mod(lambda d: (d["model"]["options"].__setitem__("max_output_tokens", 200000), d["agent"].__setitem__("max_context_tokens", 2000000))))
run("equal limits", mod(lambda d: (d["model"]["options"].__setitem__("max_output_tokens", 128000), d["agent"].__setitem__("max_context_tokens", 1050000))))
run("omit agent with small limit", mod(lambda d: (d.pop("agent"), d["model"].__setitem__("limit", {"context": 32000, "output": 8192}), d["model"]["options"].__setitem__("max_output_tokens", 8192))))
run("omit options with small limit", mod(lambda d: (d["model"].pop("options"), d["model"].__setitem__("limit", {"context": 200000, "output": 8192}), d["agent"].__setitem__("max_context_tokens", 100000))))
run("missing limit", mod(lambda d: d["model"].pop("limit")))
run("missing model", mod(lambda d: d.pop("model")))
run("empty name", mod(lambda d: d["model"].__setitem__("name", "")))
run("empty base_url", mod(lambda d: d["providers"]["openai"].__setitem__("base_url", "")))
run("missing base_url", mod(lambda d: d["providers"]["openai"].pop("base_url")))
run("timeout 0", mod(lambda d: d["providers"]["openai"].__setitem__("timeout", 0)))
run("timeout str", mod(lambda d: d["providers"]["openai"].__setitem__("timeout", "1000")))
run("temperature 2.5", mod(lambda d: d["model"]["options"].__setitem__("temperature", 2.5)))
run("max_output 1.0", mod(lambda d: d["model"]["options"].__setitem__("max_output_tokens", 1.0)))
run("max_output 1.5", mod(lambda d: d["model"]["options"].__setitem__("max_output_tokens", 1.5)))
run("max_output true", mod(lambda d: d["model"]["options"].__setitem__("max_output_tokens", True)))
run("speed bad", mod(lambda d: d["model"]["options"].__setitem__("speed", "turbo")))
run("reasoning_effort any", mod(lambda d: d["model"]["options"].__setitem__("reasoning_effort", "whatever")))
run("limits null", mod(lambda d: d["agent"].__setitem__("limits", None)))
run("limits zero", mod(lambda d: d["agent"].__setitem__("limits", {"max_iterations": 0})))
run("providers.openai null", mod(lambda d: d["providers"].__setitem__("openai", None)))
run("providers empty", mod(lambda d: d.__setitem__("providers", {})))
run("compact 1.5", mod(lambda d: d["agent"].__setitem__("compact_threshold", 1.5)))
run("max_retries -1", mod(lambda d: d["agent"].__setitem__("max_retries", -1)))
run("stop_timeout 0", mod(lambda d: d["runtime"].__setitem__("stop_timeout", 0)))
run("port_buffer 0", mod(lambda d: d["runtime"].__setitem__("port_buffer", 0)))
run("runtime.max_restarts", mod(lambda d: d["runtime"].__setitem__("max_restarts", 0)))
run("thinking type bad", mod(lambda d: d["providers"]["anthropic"]["options"]["thinking"].__setitem__("type", "enabled")))
run("display bad", mod(lambda d: d["providers"]["anthropic"]["options"]["thinking"].__setitem__("display", "full")))
run("nodes not list", mod(lambda d: d.__setitem__("nodes", "a")))
run("provider anthropic + responses (config only)", mod(lambda d: (d["model"].__setitem__("provider", "anthropic"))))
run("config.json is a directory", None, prep=lambda ws: (ws / "config.json").mkdir())

# frozen
c = run("template again", TEMPLATE)
try:
    c.model.name = "x"
except Exception as e:
    print("frozen assignment:", type(e).__name__)
# ProvidersConfig.get
print("get openai:", type(c.providers.get("openai")).__name__, "get foo:", c.providers.get("foo"), "get model_config:", c.providers.get("model_config"))
# direct model_validate: workspace private attr
d = WorkspaceConfig.model_validate(TEMPLATE)
try:
    print(d.workspace)
except Exception as e:
    print("direct model_validate workspace:", type(e).__name__, e)

# load_config path that is a file
with tempfile.TemporaryDirectory() as dd:
    f = Path(dd) / "afile"
    f.write_text("x")
    try:
        load_config(f)
    except Exception as e:
        print("workspace is a file:", type(e).__name__, str(e).replace(dd, "<tmp>"))
    # init_workspace on existing file
    print("init on existing file:", init_workspace(f))
    ws = Path(dd) / "new" / "deep"
    print("init new:", init_workspace(ws), sorted(str(p.relative_to(ws)) for p in ws.rglob("*")))
    print("init again:", init_workspace(ws))
    # relative path
    import os
    os.chdir(dd)
    print("init relative:", init_workspace(Path("rel")), (Path(dd) / "rel" / "config.json").exists())
    # str "" -> default workspace? don't actually call (would create ~/.emerge); just show truthiness
    print("bool(''):", bool(""), "bool(Path('')):", bool(Path("")))

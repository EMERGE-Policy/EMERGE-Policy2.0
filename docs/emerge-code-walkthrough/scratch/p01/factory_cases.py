import json, copy, tempfile
from pathlib import Path
from emerge.config import ModelConfig, OpenAIProviderConfig, AnthropicProviderConfig, WorkspaceConfig, load_config, _template_dir
from emerge.models.factory import create_provider, create_client

def mc(provider, api_mode, speed="standard"):
    return ModelConfig.model_validate({"provider": provider, "api_mode": api_mode, "name": "m", "limit": {"context": 1000, "output": 100}, "options": {"max_output_tokens": 10, "speed": speed}})

oa = OpenAIProviderConfig(base_url="https://api.openai.com/v1", api_key="k")
an = AnthropicProviderConfig(base_url="https://api.anthropic.com/v1/", api_key="k")
cases = [
    ("anthropic+responses ultrafast", mc("anthropic", "responses", "ultrafast"), an),
    ("openai+anthropic_messages", mc("openai", "anthropic_messages"), oa),
    ("anthropic+chat", mc("anthropic", "chat_completions"), an),
    ("anthropic ultrafast", mc("anthropic", "anthropic_messages", "ultrafast"), an),
    ("chat ultrafast", mc("openai", "chat_completions", "ultrafast"), oa),
    ("chat fast, empty key", mc("openai", "chat_completions", "fast"), OpenAIProviderConfig(base_url="x", api_key="")),
    ("responses ultrafast", mc("openai", "responses", "ultrafast"), oa),
    ("anthropic ok", mc("anthropic", "anthropic_messages", "fast"), an),
    ("anthropic base_url V1 uppercase", mc("anthropic", "anthropic_messages"), AnthropicProviderConfig(base_url="https://relay.example/V1", api_key="k")),
    ("anthropic base_url /v1beta", mc("anthropic", "anthropic_messages"), AnthropicProviderConfig(base_url="https://relay.example/api/v1beta", api_key="k")),
]
for name, m, p in cases:
    try:
        prov = create_provider(m, p)
        print(f"{name}: {type(prov).__name__} base_url={prov.client.base_url} timeout={prov.client.timeout} retries={prov.client.max_retries}")
    except Exception as e:
        print(f"{name}: {type(e).__name__}: {e}")

tpl = json.loads((_template_dir() / "config.json").read_text())
d = copy.deepcopy(tpl); d["providers"].pop("openai")
with tempfile.TemporaryDirectory() as t:
    (Path(t) / "config.json").write_text(json.dumps(d))
    cfg = load_config(Path(t))
    try:
        create_client(cfg)
    except Exception as e:
        print("create_client no entry:", type(e).__name__, e)
cfg2 = WorkspaceConfig.model_validate(tpl)
try:
    create_client(cfg2)
except Exception as e:
    print("create_client without load_config:", type(e).__name__, e)

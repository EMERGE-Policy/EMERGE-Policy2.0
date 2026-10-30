"""Scratch: a request whose history holds an image path that no longer exists (no network call)."""
import sys
from pathlib import Path

sys.path.insert(0, "/data/yuqingchi/tmp/emerge-phase-6")
from harness import make_config  # noqa: E402
from emerge.models.factory import create_client  # noqa: E402
from emerge.models.types import Message, ModelRequest, ToolCall  # noqa: E402
from emerge.exceptions import ModelError  # noqa: E402

ws = Path("/data/yuqingchi/tmp/emerge-code-walkthrough/scratch/agent/ws_files").resolve()
config = make_config(ws)
client = create_client(config)
provider = client.provider
print("provider:", provider.provider, provider.api_mode, "workspace:", getattr(provider, "workspace", None))
call = ToolCall(id="c1", name="read_file", arguments={"path": "memory/asset/gone.png"})
request = ModelRequest(messages=[
    Message.system("s"), Message.user("look"),
    Message(role="assistant", content=[], tool_calls=[call]),
    Message.tool("c1", "Image: memory/asset/gone.png", ["memory/asset/gone.png"]),
])
try:
    provider._input(request)
    print("built fine")
except ModelError as e:
    print("ModelError while building the request:", e)

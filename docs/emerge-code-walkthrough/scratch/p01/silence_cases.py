"""Local socket server: heartbeats vs silence vs slow plain response. Uses create_provider timeouts."""
import asyncio
import json
import os
import time

os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

from emerge.config import ModelConfig, OpenAIProviderConfig
from emerge.models.factory import create_provider
from emerge.models import Message, ModelRequest, ModelClient, RetryPolicy

CHUNK = lambda d: "data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": [{"index": 0, "delta": d, "finish_reason": None}]}) + "\n\n"
MODE = {"m": None}


async def handle(reader, writer):
    data = await reader.readuntil(b"\r\n\r\n")
    headers = data.decode()
    length = int([l for l in headers.split("\r\n") if l.lower().startswith("content-length")][0].split(":")[1])
    body = json.loads(await reader.readexactly(length))
    mode = MODE["m"]
    if body.get("stream"):
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n")
        await writer.drain()
        if mode == "heartbeats":
            for _ in range(6):  # 6 x 0.3s = 1.8s of heartbeats, timeout is 0.8s
                writer.write(b": keep-alive\n\n"); await writer.drain(); await asyncio.sleep(0.3)
            writer.write(CHUNK({"content": "done"}).encode() + b"data: [DONE]\n\n")
        elif mode == "silence":
            await asyncio.sleep(2.0)
        elif mode == "refuse":
            pass
        await writer.drain()
    else:
        await asyncio.sleep(1.6)  # longer than the 0.8s silence limit
        payload = json.dumps({"id": "c", "object": "chat.completion", "created": 0, "model": "m", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "slow plain"}}]}).encode()
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: " + str(len(payload)).encode() + b"\r\nconnection: close\r\n\r\n" + payload)
        await writer.drain()
    writer.close()


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    model = ModelConfig.model_validate({"provider": "openai", "api_mode": "chat_completions", "name": "m", "limit": {"context": 1000, "output": 100}, "options": {"max_output_tokens": 10}})
    prov_cfg = OpenAIProviderConfig(base_url=f"http://127.0.0.1:{port}/v1", api_key="k", timeout=800)
    for mode in ("heartbeats", "silence", "refuse"):
        MODE["m"] = mode
        p = create_provider(model, prov_cfg)
        print("client timeout:", p.client.timeout) if mode == "heartbeats" else None
        c = ModelClient(p, retry=RetryPolicy(max_retries=0))
        t0 = time.monotonic()
        try:
            r = await c.generate(ModelRequest(messages=[Message.user("x")]))
            print(f"{mode}: ok text={r.message.text!r} streaming={p.streaming} in {time.monotonic()-t0:.1f}s")
        except Exception as e:
            print(f"{mode}: {type(e).__name__}: {str(e)[:110]} in {time.monotonic()-t0:.1f}s")
        await c.aclose()
    server.close()

asyncio.run(main())

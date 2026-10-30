"""Offline harness: providers built on mock HTTP transports (no network)."""
import json

import anthropic
import httpx
import httpx2
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from emerge.config import AnthropicOptions, ModelConfig, OpenAIOptions, ThinkingConfig
from emerge.models.providers.anthropic.messages import AnthropicMessagesProvider
from emerge.models.providers.openai.chat_completions import OpenAIChatCompletionsProvider
from emerge.models.providers.openai.responses import OpenAIResponsesProvider


def sse(events, named=True):
    out = []
    for e in events:
        if isinstance(e, str):
            out.append(e)  # raw line(s)
            continue
        if named and "type" in e:
            out.append(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n")
        else:
            out.append(f"data: {json.dumps(e)}\n\n")
    return "".join(out).encode()


class Script:
    """Each request pops the next responder; records request bodies."""

    def __init__(self, *responders):
        self.responders = list(responders)
        self.bodies = []

    def __call__(self, request):
        body = json.loads(request.content or b"{}")
        self.bodies.append(body)
        r = self.responders.pop(0)
        return r(request, body) if callable(r) else r


def model_cfg(provider, api_mode, **opts):
    return ModelConfig.model_validate({
        "provider": provider, "api_mode": api_mode, "name": "m",
        "limit": {"context": 200000, "output": 32000},
        "options": {"max_output_tokens": 1000, **opts},
    })


TIMEOUTS = dict(connect=30.0, read=600.0, write=600.0, pool=600.0)


def responses_provider(script, summary="auto", **opts):
    client = AsyncOpenAI(base_url="http://test/v1", api_key="k", max_retries=0, timeout=httpx.Timeout(**TIMEOUTS),
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(script)))
    return OpenAIResponsesProvider(client, model_cfg("openai", "responses", **opts), OpenAIOptions(reasoning_summary=summary))


def chat_provider(script, **opts):
    client = AsyncOpenAI(base_url="http://test/v1", api_key="k", max_retries=0, timeout=httpx.Timeout(**TIMEOUTS),
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(script)))
    return OpenAIChatCompletionsProvider(client, model_cfg("openai", "chat_completions", **opts))


def anthropic_provider(script, thinking="adaptive", display="summarized", **opts):
    client = AsyncAnthropic(base_url="http://test", api_key="k", max_retries=0, timeout=anthropic.Timeout(**TIMEOUTS),
                            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(script)))
    return AnthropicMessagesProvider(client, model_cfg("anthropic", "anthropic_messages", **opts),
                                     AnthropicOptions(thinking=ThinkingConfig(type=thinking, display=display)))


def oa_sse(events, named=True):
    return lambda req, body: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events, named))


def oa_json(obj, status=200, headers=None):
    return lambda req, body: httpx.Response(status, json=obj, headers=headers or {})


def an_sse(events):
    return lambda req, body: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events))


def an_json(obj, status=200, headers=None):
    return lambda req, body: httpx2.Response(status, json=obj, headers=headers or {})


RESP_FINAL = {
    "id": "resp_1", "object": "response", "created_at": 0, "model": "m", "status": "completed",
    "output": [
        {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "think A"}, {"type": "summary_text", "text": "think B"}], "encrypted_content": "ENC" * 10},
        {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Hello", "annotations": []}]},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "get", "arguments": "{\"a\":1}", "status": "completed"},
    ],
    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 2}},
    "service_tier": "default",
}

RESP_EVENTS = [
    {"type": "response.created", "sequence_number": 0, "response": {**RESP_FINAL, "status": "in_progress", "output": []}},
    {"type": "response.output_item.added", "output_index": 0, "sequence_number": 1, "item": {"type": "reasoning", "id": "rs_1", "summary": []}},
    {"type": "response.reasoning_summary_part.added", "output_index": 0, "summary_index": 0, "item_id": "rs_1", "sequence_number": 2, "part": {"type": "summary_text", "text": ""}},
    {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0, "item_id": "rs_1", "sequence_number": 3, "delta": "think A"},
    {"type": "response.reasoning_summary_part.added", "output_index": 0, "summary_index": 1, "item_id": "rs_1", "sequence_number": 4, "part": {"type": "summary_text", "text": ""}},
    {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 1, "item_id": "rs_1", "sequence_number": 5, "delta": "think B"},
    {"type": "response.output_item.added", "output_index": 1, "sequence_number": 6, "item": {"type": "message", "id": "msg_1", "role": "assistant", "status": "in_progress", "content": []}},
    {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "item_id": "msg_1", "sequence_number": 7, "delta": "Hel", "logprobs": []},
    {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "item_id": "msg_1", "sequence_number": 8, "delta": "lo", "logprobs": []},
    {"type": "response.output_item.added", "output_index": 2, "sequence_number": 9, "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "get", "arguments": "", "status": "in_progress"}},
    {"type": "response.function_call_arguments.delta", "output_index": 2, "item_id": "fc_1", "sequence_number": 10, "delta": "{\"a\":"},
    {"type": "response.function_call_arguments.delta", "output_index": 2, "item_id": "fc_1", "sequence_number": 11, "delta": "1}"},
    {"type": "response.completed", "sequence_number": 12, "response": RESP_FINAL},
]

CHAT_FINAL = {
    "id": "cc_1", "object": "chat.completion", "created": 0, "model": "m", "service_tier": "default",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": "Hi", "reasoning_content": "thought", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get", "arguments": "{\"a\":1}"}}]}}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 1}},
}


def chunk(delta=None, finish=None, usage=None, choices=True, tier="default"):
    c = {"id": "cc_1", "object": "chat.completion.chunk", "created": 0, "model": "m", "service_tier": tier,
         "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}] if choices else []}
    if usage:
        c["usage"] = usage
    return c


CHAT_EVENTS = [
    chunk({"role": "assistant", "reasoning_content": "tho"}),
    chunk({"reasoning_content": "ught"}),
    chunk({"content": "Hi"}),
    chunk({"reasoning_content": "again"}),
    chunk({"content": "!"}),
    chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "get", "arguments": ""}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"a\":"}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}, finish="tool_calls"),
    chunk(choices=False, usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}),
    "data: [DONE]\n\n",
]

AN_MESSAGE = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m", "content": [], "stop_reason": None, "stop_sequence": None,
              "usage": {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}

AN_EVENTS = [
    {"type": "message_start", "message": AN_MESSAGE},
    {"type": "ping"},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "let me "}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "think"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hello"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "tu_1", "name": "get", "input": {}}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"a\":"}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "1}"}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 9}},
    {"type": "message_stop"},
]

AN_FINAL = {**AN_MESSAGE, "stop_reason": "tool_use", "content": [
    {"type": "thinking", "thinking": "let me think", "signature": "SIG"},
    {"type": "text", "text": "Hello"},
    {"type": "tool_use", "id": "tu_1", "name": "get", "input": {"a": 1}},
], "usage": {"input_tokens": 5, "output_tokens": 9, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20, "speed": "fast"}}

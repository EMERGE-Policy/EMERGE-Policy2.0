"""Middleware hooks around model calls and tool calls.

`wrap_model_call` / `wrap_tool_call` form an onion in registration order; `before_model` and
`after_model` run inside that onion, before/after the actual call. Turn hooks fire exactly once.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from emerge.agent.context import Compactor, ContextBuilder, RequestBuilder
from emerge.agent.messages import History
from emerge.agent.state import RunContext
from emerge.agent.tool import ToolResult
from emerge.models.client import ModelClient
from emerge.models.types import ModelRequest, ModelResponse, ToolCall

if TYPE_CHECKING:
    from emerge.agent.loop import TurnResult

ModelHandler = Callable[[RunContext, ModelRequest], Awaitable[ModelResponse]]
ToolHandler = Callable[[RunContext, ToolCall], Awaitable[ToolResult]]


class AgentMiddleware:
    """Base class: every hook is optional. Subclasses override only what they need."""

    async def on_turn_start(self, ctx: RunContext) -> None:
        pass

    async def on_turn_end(self, ctx: RunContext, result: "TurnResult") -> None:
        pass

    async def before_model(self, ctx: RunContext, request: ModelRequest) -> ModelRequest | None:
        """Return a replacement request, or None to leave it unchanged."""
        return None

    async def after_model(self, ctx: RunContext, response: ModelResponse) -> ModelResponse | None:
        return None

    async def wrap_model_call(self, ctx: RunContext, request: ModelRequest, handler: ModelHandler) -> ModelResponse:
        return await handler(ctx, request)

    async def wrap_tool_call(self, ctx: RunContext, call: ToolCall, handler: ToolHandler) -> ToolResult:
        return await handler(ctx, call)


class MiddlewareChain:
    def __init__(self, middlewares: Sequence[AgentMiddleware] = ()) -> None:
        self.middlewares = list(middlewares)

    async def notify_turn_start(self, ctx: RunContext) -> None:
        for middleware in self.middlewares:
            await middleware.on_turn_start(ctx)

    async def notify_turn_end(self, ctx: RunContext, result: "TurnResult") -> None:
        for middleware in self.middlewares:
            await middleware.on_turn_end(ctx, result)

    async def model_call(self, ctx: RunContext, request: ModelRequest, core: ModelHandler) -> ModelResponse:
        async def inner(ctx: RunContext, request: ModelRequest) -> ModelResponse:
            for middleware in self.middlewares:
                replacement = await middleware.before_model(ctx, request)
                if replacement is not None:
                    request = replacement
            response = await core(ctx, request)
            for middleware in reversed(self.middlewares):
                replacement = await middleware.after_model(ctx, response)
                if replacement is not None:
                    response = replacement
            return response

        handler = inner
        for middleware in reversed(self.middlewares):
            handler = _wrap_model(middleware, handler)
        return await handler(ctx, request)

    async def tool_call(self, ctx: RunContext, call: ToolCall, core: ToolHandler) -> ToolResult:
        handler = core
        for middleware in reversed(self.middlewares):
            handler = _wrap_tool(middleware, handler)
        return await handler(ctx, call)


def _wrap_model(middleware: AgentMiddleware, next_handler: ModelHandler) -> ModelHandler:
    async def handler(ctx: RunContext, request: ModelRequest) -> ModelResponse:
        return await middleware.wrap_model_call(ctx, request, next_handler)

    return handler


def _wrap_tool(middleware: AgentMiddleware, next_handler: ToolHandler) -> ToolHandler:
    async def handler(ctx: RunContext, call: ToolCall) -> ToolResult:
        return await middleware.wrap_tool_call(ctx, call, next_handler)

    return handler


class CompactionMiddleware(AgentMiddleware):
    """Compacts the history in `before_model` and records exact usage in `on_turn_end`.

    This is the only middleware allowed to change the request prefix: it replaces older turns with
    a summary, drops their images and strips thinking blocks from the turns that stay.
    """

    def __init__(
        self,
        client: ModelClient,
        context: ContextBuilder,
        history: History,
        builder: RequestBuilder,
        *,
        max_context_tokens: int,
        compact_threshold: float = 0.8,
        keep_ratio: float = 0.5,
    ) -> None:
        self.history = history
        self.builder = builder
        self.compactor = Compactor(
            client,
            context,
            max_context_tokens=max_context_tokens,
            compact_threshold=compact_threshold,
            keep_ratio=keep_ratio,
        )
        self._sent_messages = 0
        self._input_tokens = 0

    async def before_model(self, ctx: RunContext, request: ModelRequest) -> ModelRequest | None:
        if self.compactor.over_budget(request) and await self.compactor.compact(self.history, ctx):
            request = self.builder.rebuild(request)
        self._sent_messages = len(request.messages)
        # Return the request: compaction changed the history, and this call must send the new prefix.
        return request

    async def after_model(self, ctx: RunContext, response: ModelResponse) -> ModelResponse | None:
        self._input_tokens = response.usage.input_tokens
        return None

    async def on_turn_end(self, ctx: RunContext, result: "TurnResult") -> None:
        # The last response's input count is the whole prefix as it was sent; the sum across calls is not.
        self.compactor.record_usage(self._sent_messages, self._input_tokens or result.usage.input_tokens)

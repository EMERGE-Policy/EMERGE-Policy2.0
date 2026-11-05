"""The agent loop: model call, tool execution, repeat until done or a limit is reached."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from emerge.agent.context import RequestBuilder, call_model
from emerge.agent.messages import History
from emerge.agent.middleware import MiddlewareChain
from emerge.agent.state import RunContext, RunLimits
from emerge.agent.tool import ToolResult, Toolset, error_result
from emerge.callbacks import EventEmitter
from emerge.events import Event, EventType
from emerge.exceptions import StopRequested
from emerge.models.client import ModelClient
from emerge.models.types import Message, ModelRequest, ModelResponse, ToolCall, Usage

FinishReason = Literal[
    "stop", "max_iterations", "max_input_tokens", "max_output_tokens", "max_tool_calls", "cancelled", "closed"
]

# Called at each boundary of a turn, i.e. before every model call and before every tool call;
# returns False (or awaits a hook that returns False) to end the turn there.
BoundaryHook = Callable[[RunContext], Awaitable[bool | None]]

# What a turn starts or carries on with: one input, or several that enter together.
TurnInput = str | Message | Sequence[Message]


class TurnResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    finish_reason: FinishReason = "stop"


class _Cancelled(Exception):
    pass


class _Closed(Exception):
    pass


# The result given to tool calls a turn ended as `closed` never ran.
NOT_EXECUTED_CLOSED = "not executed: session closed"


class AgentLoop:
    """Runs one turn at a time over a shared history.

    The loop knows nothing about inboxes or control channels: the caller passes a
    `boundary_hook` that may append messages at the end of the history or end the turn early.

    A turn ends in one of three ways besides finishing: cancelled (the hook returned False, the
    task was cancelled, or a stop was raised), which drops the whole turn; or closed
    (`close_turn()`), which keeps everything the turn completed.
    """

    def __init__(
        self,
        client: ModelClient,
        tools: Toolset,
        builder: RequestBuilder,
        history: History,
        middleware: MiddlewareChain | None = None,
        limits: RunLimits | None = None,
        *,
        callbacks: EventEmitter | None = None,
        stream: bool = True,
    ) -> None:
        self.client = client
        self.tools = tools
        self.builder = builder
        self.history = history
        self.chain = middleware or MiddlewareChain()
        self.limits = limits or RunLimits()
        self.callbacks = callbacks
        self.stream = stream
        # Set by close_turn(); cleared when the turn it ended is over.
        self._closing = False
        self._inflight: asyncio.Future[ModelResponse] | None = None

    async def run_turn(
        self,
        user_input: TurnInput,
        run_context: RunContext,
        *,
        boundary_hook: BoundaryHook | None = None,
    ) -> TurnResult:
        """Start a new turn with `user_input`; several messages all enter it before the first model call."""
        first, *rest = _messages(user_input)
        turn_index = len(self.history.turns)
        self.history.start_turn(first)
        if rest:
            self.history.append_messages_to_current_turn(*rest)
        for message in (first, *rest):
            self._emit(EventType.AGENT_MESSAGE, run_context, role="user", text=message.text)
        return await self._drive(turn_index, run_context, boundary_hook)

    async def continue_turn(
        self,
        user_input: TurnInput,
        run_context: RunContext,
        *,
        boundary_hook: BoundaryHook | None = None,
    ) -> TurnResult:
        """Append `user_input` to the open (last) turn and carry on with it instead of starting a new one.

        For a turn left unfinished, e.g. by a closed session: what it already did stays, and the model
        continues from there. Cancelling the continuation drops the whole turn, like any cancellation.
        """
        if not self.history.turns:
            return await self.run_turn(user_input, run_context, boundary_hook=boundary_hook)
        messages = _messages(user_input)
        turn_index = len(self.history.turns) - 1
        self.history.append_messages_to_current_turn(*messages)
        for message in messages:
            self._emit(EventType.AGENT_MESSAGE, run_context, role="user", text=message.text)
        return await self._drive(turn_index, run_context, boundary_hook)

    def close_turn(self) -> None:
        """End the running turn as `closed`, keeping what it completed.

        A reply the model is still producing is cancelled and not written; a tool already running
        finishes and its result is kept; the calls of the same reply not run yet each get the result
        "not executed: session closed". No further model call is made.
        """
        self._closing = True
        if self._inflight is not None:
            self._inflight.cancel()

    async def _drive(self, turn_index: int, run_context: RunContext, boundary_hook: BoundaryHook | None) -> TurnResult:
        try:
            await self.chain.notify_turn_start(run_context)
            try:
                result = await self._run(run_context, boundary_hook)
            except _Cancelled:
                # The boundary hook ended the turn; the unfinished turn is not written.
                self.history.truncate_to_turn_count(turn_index)
                result = TurnResult(usage=run_context.usage, finish_reason="cancelled")
                await self.chain.notify_turn_end(run_context, result)
            except (asyncio.CancelledError, StopRequested):
                # A stop raised from inside a tool leaves the turn half done; drop it like a cancellation
                # so the history never holds a tool call without its result.
                self.history.truncate_to_turn_count(turn_index)
                result = TurnResult(usage=run_context.usage, finish_reason="cancelled")
                with contextlib.suppress(asyncio.CancelledError):
                    await self.chain.notify_turn_end(run_context, result)
                raise
            else:
                await self.chain.notify_turn_end(run_context, result)
            return result
        finally:
            self._closing = False

    async def _run(self, ctx: RunContext, boundary_hook: BoundaryHook | None) -> TurnResult:
        text = ""
        calls: list[ToolCall] = []
        iterations = 0
        executed = 0
        try:
            while True:
                await self._at_boundary(boundary_hook, ctx)
                if self._closing:
                    raise _Closed
                reason = self._check_limits(ctx, iterations)
                if reason is not None:
                    return TurnResult(text=text, tool_calls=calls, usage=ctx.usage, finish_reason=reason)

                self.builder.context.append_runtime_info(self.history)
                request = self.builder.build()
                response = await self._call_model(ctx, request)
                iterations += 1
                ctx.record_usage(response.usage)

                assistant = response.message
                self.history.append_messages_to_current_turn(assistant)
                text = assistant.text or text
                if assistant.text:
                    self._emit(EventType.AGENT_MESSAGE, ctx, role="assistant", text=assistant.text)
                if not assistant.tool_calls:
                    return TurnResult(text=text, tool_calls=calls, usage=ctx.usage, finish_reason="stop")

                exhausted = False
                for index, call in enumerate(assistant.tool_calls):
                    if self.limits.max_tool_calls is not None and executed >= self.limits.max_tool_calls:
                        # Every tool call needs a result, so the unexecuted ones are answered and the turn ends.
                        self.history.append_messages_to_current_turn(Message.tool(call.id, "not executed: tool call budget exhausted"))
                        exhausted = True
                        continue
                    await self._at_boundary(boundary_hook, ctx)
                    if self._closing:
                        rest = assistant.tool_calls[index:]
                        self.history.append_messages_to_current_turn(*(Message.tool(c.id, NOT_EXECUTED_CLOSED) for c in rest))
                        raise _Closed
                    executed += 1
                    calls.append(call)
                    result = await self.chain.tool_call(ctx, call, self._tool_call)
                    self.history.append_messages_to_current_turn(result.to_message(call.id))
                if exhausted:
                    return TurnResult(text=text, tool_calls=calls, usage=ctx.usage, finish_reason="max_tool_calls")
        except _Closed:
            return TurnResult(text=text, tool_calls=calls, usage=ctx.usage, finish_reason="closed")

    async def _call_model(self, ctx: RunContext, request: ModelRequest) -> ModelResponse:
        # Run as its own task so close_turn() can cancel the reply without cancelling the turn.
        call = asyncio.ensure_future(self.chain.model_call(ctx, request, self._model_call))
        self._inflight = call
        try:
            return await call
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if self._closing and (task is None or not task.cancelling()):
                raise _Closed from None
            raise
        finally:
            self._inflight = None

    def _check_limits(self, ctx: RunContext, iterations: int) -> FinishReason | None:
        if self.limits.max_iterations is not None and iterations >= self.limits.max_iterations:
            return "max_iterations"
        if self.limits.max_input_tokens is not None and ctx.usage.input_tokens >= self.limits.max_input_tokens:
            return "max_input_tokens"
        if (
            self.limits.max_output_tokens_total is not None
            and ctx.usage.output_tokens >= self.limits.max_output_tokens_total
        ):
            return "max_output_tokens"
        return None

    async def _at_boundary(self, hook: BoundaryHook | None, ctx: RunContext) -> None:
        if hook is not None and await hook(ctx) is False:
            raise _Cancelled

    async def _model_call(self, ctx: RunContext, request: ModelRequest) -> ModelResponse:
        return await call_model(self.client, request, stream=self.stream)

    async def _tool_call(self, ctx: RunContext, call: ToolCall) -> ToolResult:
        self._emit(EventType.TOOL_STARTED, ctx, tool=call.name, call_id=call.id, arguments=call.arguments)
        tool = self.tools.get(call.name)
        if tool is None:
            result = error_result(call.name, f"unknown tool; available: {', '.join(t.name for t in self.tools) or 'none'}")
        else:
            result = await tool.invoke(call.arguments)
        self._emit(
            EventType.TOOL_FINISHED,
            ctx,
            tool=call.name,
            call_id=call.id,
            is_error=result.is_error,
            content=result.content,
        )
        return result

    def _emit(self, type: EventType, ctx: RunContext, **payload: object) -> None:
        if self.callbacks is not None:
            self.callbacks.emit_nowait(Event(type=type, run_id=ctx.run_id, payload=dict(payload)))


def _messages(user_input: TurnInput) -> list[Message]:
    if isinstance(user_input, str):
        return [Message.user(user_input)]
    if isinstance(user_input, Message):
        return [user_input]
    if not user_input:
        raise ValueError("a turn needs at least one message")
    return list(user_input)

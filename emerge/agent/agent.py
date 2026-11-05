"""The main agent: the orchestrator the user talks to. Nodes do the work; it plans and arranges them.

It is assembled from the agent modules (`AgentLoop`, `ContextBuilder`, middleware, a `Toolset`) and
stays resident while its session is open: it sleeps until something reaches its inbox, handles it
as one turn, and sleeps again. It never blocks in a tool waiting for nodes; what happens in the run
reaches it as notifications (see inbox.py).

- System text, built when the session starts and rebuilt only after compaction: prompts/system.md,
  graph.md, tools.md, the workspace's AGENTS.md and USER.md, then prompts/memory.md followed by the
  conversation's MEMORY.md. Edits to those files take effect at the next compaction or in a new
  session.
- Everything the agent reads and writes for the conversation (files, memory, runs) is inside the
  conversation's folder, `storage.session_dir`; only the settings come from the workspace.
- Runtime info: the current run's summary and whether its graph file holds uncommitted edits,
  appended to the conversation whenever it changed (never put into the system text).
- Middleware, outermost first: `TurnControl` (so a turn can be interrupted), `StaleCallGuard`,
  `PromptCacheKey`, and compaction.
- A node's question (`ask_main_agent`) is answered on the side, like Claude Code's /btw: a request
  with the same prefix as the main conversation (system text, tool definitions, the messages done so
  far) plus the question, with tools turned off. It neither waits for nor interrupts the running
  turn, and is never passed on to the user; the exchange is not written into the conversation, a
  record of it goes through the inbox instead.
- Its own events (agent.*, tool.*, model.*) carry the current run's id, so they land in that run's
  trace; with no run they go to the session trace. The model events of side requests (a node's
  question, compaction) carry `side` in their payload.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from emerge.agent.context import ContextBuilder, Instruction, RequestBuilder, call_model
from emerge.agent.control import ControlAction, Controller, ControlResult, TurnControl
from emerge.agent.inbox import Inbox, InboxItem, to_messages
from emerge.agent.loop import AgentLoop, TurnResult
from emerge.agent.memory import MemoryStore
from emerge.agent.messages import History
from emerge.agent.middleware import AgentMiddleware, CompactionMiddleware, MiddlewareChain, ModelHandler, ToolHandler
from emerge.agent.skills import SkillStore
from emerge.agent.state import RunContext, RunLimits
from emerge.agent.tool import Tool, ToolResult, Toolset, error_result
from emerge.agent.tools import ToolContext, catalog, execution, files, graph, memory
from emerge.callbacks import CallbackView, EventEmitter
from emerge.config import WorkspaceConfig
from emerge.events import Event
from emerge.exceptions import ModelError, RuntimeControlError
from emerge.graph.registry import NodeRegistry
from emerge.models.base import ModelProvider
from emerge.models.client import ModelClient
from emerge.models.factory import create_client
from emerge.models.types import Message, ModelRequest, ModelResponse, ToolCall, Usage
from emerge.runtime.checkpoint import ResumeReport
from emerge.runtime.feedback import Feedback
from emerge.runtime.run import ENDED_STATUSES
from emerge.runtime.service import RuntimeService
from emerge.storage.base import PendingInput, Storage

if TYPE_CHECKING:
    from emerge.agent.session import Session

logger = logging.getLogger(__name__)

PROMPTS = Path(__file__).resolve().parent / "prompts"

# Events of the main agent that are not in events.EventType (a phase 0 interface); handlers get them
# through `on_event`, like runtime.run.RUN_STATUS.
# A turn started or finished: payload {phase, continued} / {phase, finish_reason, error, text}.
AGENT_TURN = "agent.turn"
# Items from the inbox entered the conversation: payload {kind, text}.
AGENT_INBOX = "agent.inbox"
# A node's question answered on the side: payload {phase, ask_id, node_id, question, answer}.
AGENT_FORK = "agent.fork"
# A control command was carried out: payload {action, node_id, done, text}.
AGENT_CONTROL = "agent.control"

STALE_CALL = (
    "not executed: the user said something new, or used the control channel, after this call was made. "
    "Read the latest messages and decide again whether it is still wanted."
)

REOPENED = (
    "[Session reopened] This session was closed and has now been opened again. Runs that were open "
    "are carried on as reported below: nodes that had ended stay as they were, nodes that were running "
    "start again as new instances, and questions the old instances left unanswered are void (a new "
    "instance asks again if it needs to; do not answer the old ones)."
)
REOPENED_TURN = (
    " This turn was in progress when the session closed. Everything it did before is above: do not "
    "repeat it. A tool result 'not executed: session closed' means that call never ran; call it again "
    "only if it is still needed."
)

AgentStatus = Literal["detached", "sleeping", "working", "closing"]


class StaleCallGuard(AgentMiddleware):
    """Refuses graph and execution tool calls made before the user's latest message or command.

    Each tool call is stamped with the intent count of the request that produced it (what the model
    had seen); if the count has moved by the time the call is about to run, a guarded call is
    answered with a refusal instead of running.
    """

    def __init__(self, intent: Callable[[], int], guarded: Callable[[ToolCall], bool]) -> None:
        self.intent = intent
        self.guarded = guarded
        self._stamps: dict[str, int] = {}

    async def wrap_model_call(self, ctx: RunContext, request: ModelRequest, handler: ModelHandler) -> ModelResponse:
        seen = ctx.intent_counter
        response = await handler(ctx, request)
        for call in response.message.tool_calls:
            self._stamps[call.id] = seen
        return response

    async def wrap_tool_call(self, ctx: RunContext, call: ToolCall, handler: ToolHandler) -> ToolResult:
        stamp = self._stamps.pop(call.id, None)
        if stamp is not None and stamp != self.intent() and self.guarded(call):
            return error_result(call.name, STALE_CALL)
        return await handler(ctx, call)


class PromptCacheKey(AgentMiddleware):
    """Marks requests with the session's cache key, so requests sharing a prefix share its cache.

    Without a key, a request may be served where its prefix was never cached; with one, the main
    conversation and the side requests answering node questions reuse the same cached prefix.
    Compaction requests have a prefix of their own and are not marked. Each provider decides what
    the key becomes: OpenAI sends it as `prompt_cache_key`, Anthropic needs no key.
    """

    def __init__(self, key: str | None) -> None:
        self.key = key

    async def before_model(self, ctx: RunContext, request: ModelRequest) -> ModelRequest | None:
        return with_cache_key(request, self.key)


def with_cache_key(request: ModelRequest, key: str | None) -> ModelRequest:
    return request.model_copy(update={"cache_key": key})


class _Tagged:
    """Emits through `target`, adding fields to every payload; marks the model events of side requests."""

    def __init__(self, target: EventEmitter, **fields: Any) -> None:
        self._target = target
        self._fields = fields

    async def emit(self, event: Event) -> None:
        self.emit_nowait(event)

    def emit_nowait(self, event: Event) -> None:
        self._target.emit_nowait(dataclasses.replace(event, payload={**event.payload, **self._fields}))

    def with_event_defaults(
        self, *, run_id: str | None = None, node_id: str | None = None, acts_on_environment: bool | None = None
    ) -> CallbackView:
        return self._target.with_event_defaults(run_id=run_id, node_id=node_id, acts_on_environment=acts_on_environment)


class _RunEvents:
    """The main agent's emitter: every event goes out under the session's current run."""

    def __init__(self, target: EventEmitter, run_id: Callable[[], str | None]) -> None:
        self._target = target
        self._run_id = run_id

    async def emit(self, event: Event) -> None:
        self.emit_nowait(event)

    def emit_nowait(self, event: Event) -> None:
        self._target.emit_nowait(dataclasses.replace(event, run_id=self._run_id()))

    def with_event_defaults(
        self, *, run_id: str | None = None, node_id: str | None = None, acts_on_environment: bool | None = None
    ) -> CallbackView:
        return self._target.with_event_defaults(run_id=run_id, node_id=node_id, acts_on_environment=acts_on_environment)


class MainAgent:
    """The main agent of one conversation, built on that conversation's storage, runtime and
    registry; `Session.create` / `Session.open` attach it.

    `submit()` and `control()` are what the user does; everything else reaches it from the runtime,
    through `on_feedback` (pass `on_feedback=lambda item: agent.on_feedback(item)` when opening the
    `RuntimeService`). Its events go out through callbacks: `agent.message`, `tool.*`, `model.*`
    and the `AGENT_*` events of this module.
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        storage: Storage,
        runtime: RuntimeService,
        registry: NodeRegistry | None = None,
        *,
        client: ModelClient | None = None,
        provider: ModelProvider | None = None,
        callbacks: EventEmitter | None = None,
        stream: bool = True,
        extra_tools: Iterable[Tool] = (),
    ) -> None:
        """Without `client`, a client of the configured model is made, on `provider` when one is given."""
        self.config = config
        # The conversation's folder, where its files, memory and runs live.
        self.session_dir = storage.session_dir
        self.storage = storage
        self.runtime = runtime
        self.registry = registry if registry is not None else runtime.registry
        # The runtime's manager by default: it already has the one JSONL tracer.
        self.events = _RunEvents(callbacks if callbacks is not None else runtime.callbacks, lambda: self.current_run_id)
        if client is None:
            client = create_client(config, callbacks=self.events, image_root=self.session_dir, provider=provider)
        self.client = client
        self.stream = stream
        self.extra_tools = tuple(extra_tools)
        self.session: Session | None = None
        self.inbox = Inbox()
        self.turns = TurnControl()
        self.controller = Controller(self)
        self.usage = Usage()
        self._status: AgentStatus = "detached"
        self._serve_task: asyncio.Task[None] | None = None
        # Set once the latest turn's ending is recorded (status, usage, events).
        self._turn_over: asyncio.Event | None = None
        self._forks: set[asyncio.Task[None]] = set()
        # After a reopen while idle: the note and the run info it was first shown with.
        self._reopen_note: str | None = None
        self._reopen_info: str | None = None
        self._cache_key: str | None = None

    # -- what the user does -------------------------------------------------------

    @property
    def status(self) -> AgentStatus:
        return self._status

    @property
    def current_run_id(self) -> str | None:
        return self.session.current_run_id if self.session is not None else None

    async def submit(self, text: str, images: Sequence[str] = ()) -> None:
        """A user message: kept in storage at once, then handled now (if asleep) or at the next boundary."""
        session = self._require_session()
        message = Message.user(text, list(images))
        pending = await session.add_pending(message)
        self.inbox.put_user(message, pending_seq=pending.seq)
        session.set_intent(self.inbox.intent)

    async def control(self, action: ControlAction, node_id: str | None = None) -> ControlResult:
        """Carry out an explicit command at once, without the model (see control.py)."""
        self._require_session()
        result = await self.controller.execute(action, node_id)
        self._emit(AGENT_CONTROL, action=action, node_id=node_id, done=result.done, text=result.text)
        return result

    def note_control(self, text: str, *, run_id: str | None) -> None:
        """Record a control command the user carried out, for the model to see; it counts as intent."""
        self.inbox.put_control(text, run_id=run_id)
        if self.session is not None:
            self.session.set_intent(self.inbox.intent)

    def on_feedback(self, item: Feedback) -> None:
        """What the runtime tells the main agent: a question is answered on the side, the rest queues."""
        if self.session is None or self._status in ("detached", "closing"):
            return
        if item.kind == "ask":
            task = asyncio.create_task(self._answer_node(item), name=f"main-agent-answer:{item.data.get('ask_id')}")
            self._forks.add(task)
            task.add_done_callback(self._forks.discard)
        else:
            self.inbox.put_notice(item.text, run_id=item.run_id)

    async def wait_until_idle(self, timeout: float | None = None) -> None:
        """Until the agent sleeps with nothing waiting and no question being answered."""

        async def settled() -> None:
            while not (self._status == "sleeping" and not len(self.inbox) and not self._forks):
                await asyncio.sleep(0.05)

        await asyncio.wait_for(settled(), timeout)

    # -- serving a session (called by Session) ----------------------------------------

    async def attach(self, session: Session) -> None:
        """Assemble everything that belongs to `session`; it is served once `start()` is called."""
        if self.session is not None:
            raise RuntimeControlError(f"the main agent already serves session {self.session.id}")
        self.session = session
        session.agent = self
        self.inbox = Inbox(session.record.intent_count)
        self.usage = session.record.usage
        self.memory = MemoryStore(self.storage, self.session_dir)
        await self.memory.refresh()
        self.skills = SkillStore(self.config.skills_dir)
        self.context = ContextBuilder(
            self._instructions(), runtime_info=self._runtime_info, system=session.record.system
        )
        tools = ToolContext(
            session_dir=self.session_dir,
            config=self.config,
            storage=self.storage,
            runtime=self.runtime,
            registry=self.registry,
            session=session,
            memory=self.memory,
            skills=self.skills,
        )
        self.tools = Toolset(
            [
                *catalog.make_tools(tools),
                *files.make_tools(tools),
                *graph.make_tools(tools),
                *execution.make_tools(tools),
                *memory.make_tools(tools),
                *self.extra_tools,
            ]
        )
        self.history: History = session.history
        self.builder = RequestBuilder(self.context, self.history, self.tools.specs())
        self.turns = TurnControl()
        guard = StaleCallGuard(lambda: self.inbox.intent, self._guarded)
        compaction = CompactionMiddleware(
            self._side_client("compaction"),
            self.context,
            self.history,
            self.builder,
            max_context_tokens=self.config.agent.max_context_tokens,
            compact_threshold=self.config.agent.compact_threshold,
        )
        # Keyed per session: one conversation, one cached prefix.
        self._cache_key = session.id
        self.loop = AgentLoop(
            self.client,
            self.tools,
            self.builder,
            self.history,
            MiddlewareChain([self.turns, guard, PromptCacheKey(self._cache_key), compaction]),
            RunLimits.from_config(self.config.agent.limits),
            callbacks=self.events,
            stream=self.stream,
        )
        session.system_source = self.context.get_system_prompt
        self.history.set_listener(session)
        if session.record.system is None:
            session.save_system(self.context.get_system_prompt())
        self._status = "sleeping"

    async def start(
        self,
        *,
        reports: Sequence[ResumeReport] = (),
        unfinished_turn: bool = False,
        pending: Sequence[PendingInput] = (),
    ) -> None:
        """Begin serving: carry on an unfinished turn if there is one, then sleep until called upon."""
        session = self._require_session()
        for item in pending:
            # Counted already when it arrived, before the session was closed.
            self.inbox.put_user(item.message, pending_seq=item.seq, counted=False)
        first: Message | None = None
        if unfinished_turn:
            first = Message.user(_reopened(REOPENED + REOPENED_TURN, reports))
            # Busy from now on, not from when the task first runs: nobody may take it for asleep.
            self._status = "working"
        elif reports:
            self._reopen_note = _reopened(REOPENED, reports)
        self._serve_task = asyncio.create_task(self._serve(first), name=f"main-agent:{session.id}")

    async def detach(self) -> None:
        """Stop serving: a running turn is closed (see AgentLoop.close_turn), questions being
        answered on the side are cancelled. Called by `Session.close()`."""
        if self._status == "detached":
            return
        self._status = "closing"
        if self.turns.task is not None and not self.turns.task.done():
            self.loop.close_turn()
        if self._turn_over is not None:
            # The serve loop records how the turn ended, then stops by itself.
            await self._turn_over.wait()
        if self._serve_task is not None:
            # Asleep on the inbox, or already done.
            self._serve_task.cancel()
            await asyncio.gather(self._serve_task, return_exceptions=True)
            self._serve_task = None
        forks = list(self._forks)
        for task in forks:
            task.cancel()
        await asyncio.gather(*forks, return_exceptions=True)
        self.history.set_listener(None)
        if self.session is not None:
            self.session.agent = None
        self.session = None
        self._status = "detached"

    # -- turns ----------------------------------------------------------------------------

    async def _serve(self, first: Message | None) -> None:
        if first is not None:
            await self._run_turn(first, continued=True)
        while self._status != "closing":
            await self.inbox.wait()
            if self._status == "closing":
                break
            messages = self._admit(self.inbox.take())
            if not messages:
                continue
            await self._run_turn(messages, continued=False)

    async def _run_turn(self, messages: Message | list[Message], *, continued: bool) -> None:
        session = self._require_session()
        ctx = RunContext.create(self.session_dir, intent_counter=self.inbox.intent, run_id=session.id)
        start = self.loop.continue_turn if continued else self.loop.run_turn
        task: asyncio.Task[TurnResult] = asyncio.create_task(
            start(messages, ctx, boundary_hook=self._at_boundary), name="main-agent-turn"
        )
        self.turns.begin(task)
        self._turn_over = over = asyncio.Event()
        session.set_status("turn_open")
        self._status = "working"
        self._emit(AGENT_TURN, phase="started", continued=continued)
        await asyncio.wait({task})
        self.turns.end()
        text, error = "", None
        if task.cancelled():
            finish = "cancelled"
        elif task.exception() is not None:
            exc = task.exception()
            finish, error = "error", f"{type(exc).__name__}: {exc}"
            logger.error("main agent turn failed: %s", error, exc_info=exc)
        else:
            result = task.result()
            finish, text = result.finish_reason, result.text
        self._add_usage(ctx.usage)
        if finish != "closed":
            # A closed turn stays open in storage; reopening the session carries it on.
            session.set_status("idle")
        if self._status != "closing":
            self._status = "sleeping"
        self._emit(AGENT_TURN, phase="finished", finish_reason=finish, error=error, text=text)
        over.set()

    async def _at_boundary(self, ctx: RunContext) -> bool | None:
        """At each boundary: end the turn if interrupted; before a model call, take in the inbox."""
        if self.turns.interrupt_requested:
            return False
        if self._status == "closing" or _awaiting_tool_results(self.history):
            # Between a reply's tool calls and their results nothing else may go into the conversation.
            return None
        messages = self._admit(self.inbox.take())
        if messages:
            self.history.append_messages_to_current_turn(*messages)
        ctx.intent_counter = self.inbox.intent
        return None

    def _admit(self, items: list[InboxItem]) -> list[Message]:
        """Messages for taken items; a user message's pending row goes once it is written."""
        session = self._require_session()
        for item in items:
            if item.kind == "user" and item.message is not None and item.pending_seq is not None:
                session.hold_pending(item.message, item.pending_seq)
            self._emit(AGENT_INBOX, kind=item.kind, text=item.text)
        return to_messages(items)

    # -- a node's question, answered on the side --------------------------------------

    async def _answer_node(self, item: Feedback) -> None:
        ask_id = str(item.data.get("ask_id"))
        question = str(item.data.get("question", ""))
        node_id = item.node_id or "?"
        who = self._describe_node(item.run_id, node_id)
        self._emit(AGENT_FORK, phase="started", ask_id=ask_id, node_id=node_id, question=question)
        request = self.fork_request(
            f"[Question from a node] {who} is asking while it runs (question {ask_id}):\n\n{question}\n\n"
            "Answer the node directly, in a few sentences, from what this conversation already tells you. "
            "You cannot call tools here. If you cannot tell, say so and give the safest guidance you can; "
            "the node carries on with whatever you answer."
        )
        try:
            response = await call_model(self._side_client("fork"), request, stream=self.stream)
            self._add_usage(response.usage)
            answer = response.message.text.strip() or "(no answer)"
        except ModelError as e:
            answer = f"The main agent could not answer just now ({e}). Carry on with your own best judgement."
        try:
            await self.runtime.answer(ask_id, answer)
            delivered = ""
        except RuntimeControlError:
            delivered = " The node did not get it: its question was no longer waiting (it ended or was replaced)."
        self.inbox.put_qa(
            f"{who} asked (question {ask_id}): {question}\n"
            f"You answered on the side, from this conversation and without tools: {answer}{delivered}\n"
            "Keep what you do next consistent with this answer.",
            run_id=item.run_id,
        )
        self._emit(AGENT_FORK, phase="answered", ask_id=ask_id, node_id=node_id, question=question, answer=answer)

    def fork_request(self, text: str) -> ModelRequest:
        """The main conversation's request as of now, ending with `text`, with tools turned off.

        Same system text, same tool definitions, the messages done so far: a reply whose tool calls
        are still being answered is left out, so the prefix is the request that produced it.
        """
        request = self.builder.build()
        messages = _answered(request.messages)
        request = request.model_copy(update={"messages": [*messages, Message.user(text)], "tool_choice": "none"})
        return with_cache_key(request, self._cache_key)

    def _describe_node(self, run_id: str, node_id: str) -> str:
        try:
            spec = self.runtime.graph(run_id).node(node_id)
        except RuntimeControlError:
            spec = None
        if spec is None:
            return f"Node {node_id}"
        about = f', described as "{spec.description}"' if spec.description else ""
        return f"Node {node_id} (instance {spec.instance_id}, type {spec.type}{about})"

    # -- context ---------------------------------------------------------------------------

    def _instructions(self) -> list[Instruction]:
        config = self.config
        return [
            lambda: _prompt("system.md"),
            lambda: _prompt("graph.md"),
            lambda: _prompt("tools.md"),
            lambda: _contents(config.agents_md, "AGENTS.md", "workspace instructions from the user"),
            lambda: _contents(config.user_md, "USER.md", "about the user"),
            self._memory_text,
        ]

    def _memory_text(self) -> str:
        path = self.memory.path.relative_to(self.session_dir).as_posix()
        text = self.memory.read().strip()
        return f"{_prompt('memory.md')}\n\nContents of {path}:\n\n{text or '(empty)'}"

    def _runtime_info(self) -> str:
        info = self._run_info()
        if self._reopen_note is None:
            return info
        if self._reopen_info is None:
            self._reopen_info = info
        if info == self._reopen_info:
            return f"{self._reopen_note}\n\n{info}".strip()
        # The run has moved on since: the note was shown with the earlier info and is not repeated.
        self._reopen_note = self._reopen_info = None
        return info

    def _run_info(self) -> str:
        run_id = self.current_run_id
        run = next((run for run in self.runtime.runs() if run.run_id == run_id), None)
        if run is None:
            return ""
        lines = ["[Run status, shown again whenever it changes]", self.runtime.summary(run.run_id)]
        if run.status not in ENDED_STATUSES:
            status = self.runtime.graph_file_status(run.run_id)
            path = Path(status.path).relative_to(self.session_dir).as_posix()
            if status.has_changes:
                stale = " It was written from an older graph version: reset_graph, then redo the edits." if status.stale else ""
                lines.append(f"graph file {path} holds edits not committed yet: commit_graph applies them, reset_graph drops them.{stale}")
            else:
                lines.append(f"graph file {path} matches graph version {status.current_version}.")
        return "\n".join(lines)

    # -- plumbing ---------------------------------------------------------------------------

    def _side_client(self, side: str) -> ModelClient:
        """The main client's connection, its model events marked `side` ("fork", "compaction").

        A side request streams its own tokens; the mark keeps them apart from the main agent's
        reply wherever the events are shown.
        """
        return ModelClient(
            self.client.provider,
            model=self.client.model,
            retry=self.client.retry,
            limiter=self.client.limiter,
            callbacks=_Tagged(self.events, side=side),
        )

    def _guarded(self, call: ToolCall) -> bool:
        if call.name in graph.GUARDED or call.name in execution.GUARDED:
            return True
        return files.writes_graph_file(self.session_dir, call)

    def _add_usage(self, usage: Usage) -> None:
        self.usage = self.usage + usage
        if self.session is not None:
            self.session.set_usage(self.usage)

    def _require_session(self) -> Session:
        if self.session is None:
            raise RuntimeControlError("the main agent serves no session; open or create one first")
        return self.session

    def _emit(self, event_type: str, **payload: Any) -> None:
        self.events.emit_nowait(Event(type=event_type, payload=payload))


def _awaiting_tool_results(history: History) -> bool:
    """Whether the last reply's tool calls are still being answered."""
    if not history.turns:
        return False
    messages = history.turns[-1].messages
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "assistant":
            answered = {m.tool_call_id for m in messages[index + 1 :] if m.role == "tool"}
            return any(call.id not in answered for call in messages[index].tool_calls)
    return False


def _answered(messages: list[Message]) -> list[Message]:
    """The messages up to a point where every tool call has its result."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            answered = {m.tool_call_id for m in messages[index + 1 :] if m.role == "tool"}
            return messages if all(call.id in answered for call in message.tool_calls) else messages[:index]
    return messages


def _reopened(head: str, reports: Sequence[ResumeReport]) -> str:
    return "\n\n".join([head, *(report.describe() for report in reports)])


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _contents(path: Path, name: str, label: str) -> str:
    text = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    return f"Contents of {name} ({label}):\n\n{text}" if text else ""

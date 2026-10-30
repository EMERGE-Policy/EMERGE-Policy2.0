"""The control channel: the user's explicit commands, carried out at once without the model.

Three things are controlled apart, never mixed up:

- the main agent's current turn: `interrupt` cancels it. A reply still streaming is cancelled at
  once; a tool already running finishes first, and the turn ends at the next boundary. The turn is
  dropped as a whole, from the history and from storage, as any cancelled turn is.
- nodes starting: pausing the whole run (`pause` without a node) also makes the nodes that start
  meanwhile start paused, until `resume`.
- node processes already running: `pause`, `resume` and `stop` with a node act on that node;
  `stop` without a node stops every node and ends the run as `stopped`.

Run and node commands go straight to the `RuntimeService`, then leave a record in the inbox so the
model knows the user did it, and does not undo it. Closing the session is not an interrupt: the
turn is closed, keeping what it did (see session.py).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from emerge.agent.middleware import AgentMiddleware, ModelHandler, ToolHandler
from emerge.agent.state import RunContext
from emerge.agent.tool import ToolResult
from emerge.exceptions import RuntimeControlError
from emerge.models.types import ModelRequest, ModelResponse, ToolCall
from emerge.runtime.run import ENDED_STATUSES

if TYPE_CHECKING:
    from emerge.agent.agent import MainAgent

ControlAction = Literal["interrupt", "pause", "resume", "stop"]


@dataclass(frozen=True, slots=True)
class ControlResult:
    action: ControlAction
    done: bool
    # What happened, in one line, for the interface.
    text: str
    run_id: str | None = None
    node_id: str | None = None


class TurnControl(AgentMiddleware):
    """Knows whether the running turn is waiting for the model or running a tool, so it can be stopped."""

    def __init__(self) -> None:
        self.task: asyncio.Task[object] | None = None
        self._phase: Literal["model", "tool"] | None = None
        self._interrupt = False

    @property
    def interrupt_requested(self) -> bool:
        return self._interrupt

    def begin(self, task: asyncio.Task[object]) -> None:
        self.task = task
        self._phase = None
        self._interrupt = False

    def end(self) -> None:
        self.task = None
        self._phase = None
        self._interrupt = False

    async def interrupt(self) -> bool:
        """Cancel the running turn; returns False when no turn is running."""
        task = self.task
        if task is None or task.done():
            return False
        self._interrupt = True
        if self._phase != "tool":
            # A reply being produced is abandoned now; a running tool is left to finish, and the
            # turn's checkpoint hook ends the turn right after it.
            task.cancel()
        await asyncio.wait({task})
        return True

    async def wrap_model_call(self, ctx: RunContext, request: ModelRequest, handler: ModelHandler) -> ModelResponse:
        self._phase = "model"
        try:
            return await handler(ctx, request)
        finally:
            self._phase = None

    async def wrap_tool_call(self, ctx: RunContext, call: ToolCall, handler: ToolHandler) -> ToolResult:
        self._phase = "tool"
        try:
            return await handler(ctx, call)
        finally:
            self._phase = None


class Controller:
    """Carries out control commands for one main agent."""

    def __init__(self, agent: MainAgent) -> None:
        self.agent = agent

    async def execute(self, action: ControlAction, node_id: str | None = None) -> ControlResult:
        agent = self.agent
        if action == "interrupt":
            done = await agent.turns.interrupt()
            return ControlResult(action, done, "the current turn was cancelled" if done else "no turn is running")
        run_id = agent.current_run_id
        if run_id is None or run_id not in {run.run_id for run in agent.runtime.runs() if run.status not in ENDED_STATUSES}:
            return ControlResult(action, False, "there is no open run", run_id=run_id, node_id=node_id)
        target = f"node {node_id} of run {run_id}" if node_id else f"the whole run {run_id}"
        try:
            if action == "pause":
                await agent.runtime.pause(run_id, node_id)
                done = f"paused {target}"
            elif action == "resume":
                await agent.runtime.resume(run_id, node_id)
                done = f"resumed {target}"
            else:
                await agent.runtime.stop(run_id, node_id)
                done = f"stopped {target}" + ("" if node_id else ", which ended the run as stopped")
        except RuntimeControlError as e:
            return ControlResult(action, False, str(e), run_id=run_id, node_id=node_id)
        agent.note_control(done, run_id=run_id)
        return ControlResult(action, True, done, run_id=run_id, node_id=node_id)

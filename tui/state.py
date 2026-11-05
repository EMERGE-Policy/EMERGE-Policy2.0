"""What the screen shows, reduced from events: the transcript entries and the sidebar's run picture.

`ViewState.accept(event)` takes every event the TUI's callback handler sees, and `accept_values`
the snapshots of a `values` stream subscription. Nothing here draws; app.py renders the state.

Transcript entries are dicts with a `role`:

- `user`: what the user typed (shown at once, `queued` until it reaches the main agent);
- `assistant`: the main agent's reply, `streaming` while tokens arrive;
- `thinking`: its reasoning while it streams, collapsed to one line once the reply is complete;
- `tool`: one tool call with its arguments, status and result;
- `notice`: a run notification, a node question's record, or a control record that reached it;
- `fork`: a node's question being answered on the side, then the answer;
- `system`: notes from the interface itself.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from emerge.events import Event, EventType
from emerge.models.types import Message

# Event names of the main agent and the run (module constants there, not in EventType).
AGENT_TURN = "agent.turn"
AGENT_INBOX = "agent.inbox"
AGENT_FORK = "agent.fork"
AGENT_CONTROL = "agent.control"
RUN_STATUS = "run.status"
RUN_IDLE = "run.idle"

ENDED = frozenset({"finished", "failed", "stopped", "skipped"})
# Lines of the conversation that are framework input, not the user speaking.
_NOTICE_PREFIXES = ("[Run notifications", "[Node questions answered]", "[Control channel]")
_HIDDEN_PREFIXES = ("[Run status",)


@dataclass
class ViewState:
    workspace: str = ""
    model: str = ""
    session_id: str = ""
    session_title: str = ""
    agent_status: str = "starting"
    entries: list[dict[str, Any]] = field(default_factory=list)
    # Token totals of every model call made in this process, main agent and side requests alike.
    usage: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0, "cached": 0})
    turn_started: float | None = None
    turn_seconds: float = 0.0
    # The current run, as the last values snapshot and run events show it.
    run_id: str | None = None
    run_status: str = ""
    graph_version: int = 0
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    node_types: dict[str, str] = field(default_factory=dict)
    asks: dict[str, tuple[str, str]] = field(default_factory=dict)
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=8))
    dirty: bool = True
    _streams: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    _queued: deque[dict[str, Any]] = field(default_factory=deque)
    _forks: dict[str, dict[str, Any]] = field(default_factory=dict)
    _tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The reply just completed by streaming, so the agent.message with the same text is not added twice.
    _last_final: dict[str, Any] | None = None

    # -- what the interface itself adds ------------------------------------------------------

    def note(self, text: str, kind: str = "") -> None:
        """A line the interface adds to the transcript; `kind` (session, robot, warn, error) picks its highlight."""
        self.entries.append({"role": "system", "text": text, "kind": kind})
        self.dirty = True

    def user_typed(self, text: str) -> None:
        entry = {"role": "user", "text": text, "queued": True}
        self.entries.append(entry)
        self._queued.append(entry)
        self.dirty = True

    def reset(self, session_id: str, title: str = "") -> None:
        """A different session: start the transcript and the run picture over."""
        self.session_id, self.session_title = session_id, title
        self.entries.clear()
        self._streams.clear()
        self._queued.clear()
        self._forks.clear()
        self._tools.clear()
        self.run_id, self.run_status, self.graph_version = None, "", 0
        self.nodes.clear()
        self.node_types.clear()
        self.asks.clear()
        self.recent.clear()
        self.turn_started, self.turn_seconds = None, 0.0
        self.dirty = True

    def load_history(self, messages: list[Message]) -> None:
        """The stored conversation of a session being opened, as transcript entries."""
        calls: dict[str, dict[str, Any]] = {}
        for message in messages:
            text = message.text
            if message.role == "user":
                if not text or text.startswith(_HIDDEN_PREFIXES):
                    continue
                if text.startswith("[Session reopened]"):
                    self.entries.append({"role": "system", "text": text})
                elif text.startswith(_NOTICE_PREFIXES):
                    self.entries.append({"role": "notice", "text": text})
                elif text.startswith("[Earlier conversation, compacted]"):
                    self.entries.append({"role": "system", "text": "(earlier conversation compacted)"})
                else:
                    self.entries.append({"role": "user", "text": text})
            elif message.role == "assistant":
                if text.strip():
                    self.entries.append({"role": "assistant", "text": text})
                for call in message.tool_calls:
                    entry = {"role": "tool", "tool": call.name, "arguments": call.arguments, "status": "running", "content": ""}
                    calls[call.id] = entry
                    self.entries.append(entry)
            elif message.role == "tool" and message.tool_call_id in calls:
                entry = calls[message.tool_call_id]
                entry["content"] = text
                entry["status"] = "error" if text.startswith('{"tool"') else "done"
        self.dirty = True

    # -- events ---------------------------------------------------------------------------------

    def accept(self, event: Event) -> None:
        kind, payload = event.type, event.payload
        side = payload.get("side")
        if kind == EventType.MODEL_USAGE:
            usage = payload.get("usage", {})
            self.usage["input"] += usage.get("input_tokens", 0)
            self.usage["output"] += usage.get("output_tokens", 0)
            self.usage["total"] += usage.get("total_tokens", 0)
            self.usage["cached"] += usage.get("cached_tokens") or 0
        elif kind == EventType.MODEL_STARTED:
            if side == "compaction":
                self.note("compacting the conversation…")
        elif kind == EventType.MODEL_TOKEN and side is None:
            self._token(str(payload.get("call_id")), payload.get("kind", "text"), payload.get("text", ""))
        elif kind == EventType.MODEL_FINISHED and side is None:
            self._finish_stream(str(payload.get("call_id")), failed="error" in payload)
        elif kind == EventType.AGENT_MESSAGE:
            self._agent_message(payload)
        elif kind == EventType.TOOL_STARTED:
            entry = {"role": "tool", "tool": payload.get("tool"), "arguments": payload.get("arguments") or {}, "status": "running", "content": ""}
            self._tools[str(payload.get("call_id"))] = entry
            self.entries.append(entry)
        elif kind == EventType.TOOL_FINISHED:
            entry = self._tools.pop(str(payload.get("call_id")), None)
            if entry is not None:
                entry["status"] = "error" if payload.get("is_error") else "done"
                entry["content"] = payload.get("content", "")
        elif kind == AGENT_INBOX:
            self._inbox(payload)
        elif kind == AGENT_TURN:
            self._turn(payload)
        elif kind == AGENT_FORK:
            self._fork(payload)
        elif kind == AGENT_CONTROL:
            self.note(f"control: {payload.get('text')}")
        elif kind == EventType.NODE_ASK and event.node_id:
            self.asks[str(payload.get("ask_id"))] = (event.node_id, str(payload.get("question", "")))
        elif kind == EventType.NODE_ANSWER:
            self.asks.pop(str(payload.get("ask_id")), None)
        elif kind == EventType.NODE_STATE and event.node_id and event.run_id == self.run_id:
            self._node_state(event)
        elif kind == RUN_STATUS and event.run_id == self.run_id:
            self.run_status = str(payload.get("status"))
        elif kind == RUN_IDLE and event.run_id == self.run_id:
            self.recent.append("graph idle: every node has ended")
        elif kind == EventType.RUN_FINISHED and event.run_id == self.run_id:
            self.run_status = str(payload.get("status"))
            self.recent.append(f"run ended: {payload.get('status')}")
        elif kind == EventType.GRAPH_PATCHED and event.run_id == self.run_id:
            self.graph_version = int(payload.get("version", self.graph_version))
            self.recent.append(f"graph v{payload.get('previous_version')} → v{payload.get('version')}")
        else:
            return
        self.dirty = True

    def accept_values(self, run_id: str, payload: dict[str, Any]) -> None:
        """A `values` snapshot of the current run: topology, node states, latest port values."""
        if run_id != self.run_id:
            return
        self.graph_version = int(payload.get("graph_version", self.graph_version))
        graph = payload.get("graph", {}).get("nodes", {})
        self.node_types = {node_id: spec.get("type", "") for node_id, spec in graph.items()}
        self.nodes = {node_id: dict(view) for node_id, view in payload.get("nodes", {}).items()}
        self.dirty = True

    # -- details --------------------------------------------------------------------------------

    def _token(self, call_id: str, kind: str, text: str) -> None:
        streams = self._streams.setdefault(call_id, {})
        role = "thinking" if kind == "reasoning" else "assistant"
        entry = streams.get(role)
        if entry is None:
            entry = streams[role] = {"role": role, "text": "", "streaming": True}
            self.entries.append(entry)
        entry["text"] += text

    def _finish_stream(self, call_id: str, *, failed: bool) -> None:
        streams = self._streams.pop(call_id, {})
        for entry in streams.values():
            entry["streaming"] = False
            if failed and entry in self.entries:
                # A reply cut off (interrupted, session closing) never reached the conversation.
                self.entries.remove(entry)
        if not failed and "assistant" in streams:
            # The text shown is the reply; agent.message comes next with the same text.
            streams["assistant"]["final"] = True
            self._last_final = streams["assistant"]

    def _agent_message(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text", ""))
        if payload.get("role") == "assistant":
            last = self._last_final
            if last is not None and last.get("text", "").strip() == text.strip():
                self._last_final = None
                return
            self.entries.append({"role": "assistant", "text": text})
        elif text.startswith("[Session reopened]"):
            self.entries.append({"role": "system", "text": text})

    def _inbox(self, payload: dict[str, Any]) -> None:
        kind, text = payload.get("kind"), str(payload.get("text", ""))
        if kind == "user":
            entry = self._queued.popleft() if self._queued else None
            if entry is None:
                # Typed elsewhere (another interface, a queued message after reopening).
                self.entries.append({"role": "user", "text": text})
            else:
                entry["queued"] = False
        else:
            self.entries.append({"role": "notice", "kind": kind, "text": text})

    def _turn(self, payload: dict[str, Any]) -> None:
        if payload.get("phase") == "started":
            self.agent_status = "working"
            self.turn_started = time.monotonic()
            return
        self.agent_status = "sleeping"
        reason = payload.get("finish_reason")
        # A reply cut off by an interrupt or a close sends no model.finished, and was never written.
        for streams in self._streams.values():
            for entry in streams.values():
                entry["streaming"] = False
                if reason in ("cancelled", "closed", "error") and entry in self.entries:
                    self.entries.remove(entry)
        self._streams.clear()
        if self.turn_started is not None:
            self.turn_seconds = time.monotonic() - self.turn_started
            self.turn_started = None
        if payload.get("error"):
            self.note(f"the turn failed: {payload['error']}")
        elif reason == "cancelled":
            self.note("the turn was interrupted and dropped")
        elif reason not in ("stop", "closed"):
            self.note(f"the turn ended: {reason}")

    def _fork(self, payload: dict[str, Any]) -> None:
        ask_id = str(payload.get("ask_id"))
        if payload.get("phase") == "started":
            entry = {"role": "fork", "node": payload.get("node_id"), "question": payload.get("question", ""), "answer": None}
            self._forks[ask_id] = entry
            self.entries.append(entry)
        else:
            entry = self._forks.pop(ask_id, None)
            if entry is not None:
                entry["answer"] = payload.get("answer", "")

    def _node_state(self, event: Event) -> None:
        payload = event.payload
        node = self.nodes.setdefault(str(event.node_id), {})
        state = str(payload.get("state"))
        node.update(state=state, outcome=payload.get("outcome"), reason=payload.get("reason") or "", error=payload.get("error"))
        if state in ENDED:
            detail = payload.get("outcome") or payload.get("error") or ""
            self.recent.append(f"{event.node_id} {state}{': ' + str(detail)[:60] if detail else ''}")

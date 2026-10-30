import importlib
from typing import TYPE_CHECKING, Any

from emerge.agent.context import ContextBuilder, Compactor, RequestBuilder, call_model
from emerge.agent.loop import AgentLoop, FinishReason, TurnResult
from emerge.agent.messages import History, HistoryListener, Turn
from emerge.agent.middleware import AgentMiddleware, CompactionMiddleware, MiddlewareChain
from emerge.agent.skills import Skill, SkillStore
from emerge.agent.state import RunContext, RunLimits
from emerge.agent.tool import Tool, ToolResult, Toolset, error_result, tool

if TYPE_CHECKING:
    from emerge.agent.agent import (
        AGENT_CONTROL,
        AGENT_FORK,
        AGENT_INBOX,
        AGENT_TURN,
        MainAgent,
        PromptCacheKey,
        StaleCallGuard,
    )
    from emerge.agent.control import ControlAction, Controller, ControlResult, TurnControl
    from emerge.agent.inbox import Inbox, InboxItem, InboxKind
    from emerge.agent.memory import MemoryImage, MemoryResult, MemoryStore, split_entries
    from emerge.agent.session import Session

# The main agent pulls in the runtime, storage and graph packages. The modules above are shared with
# agent nodes and must not, so the main agent's names load only when first used.
_MAIN_AGENT = {
    "emerge.agent.agent": (
        "AGENT_CONTROL",
        "AGENT_FORK",
        "AGENT_INBOX",
        "AGENT_TURN",
        "MainAgent",
        "PromptCacheKey",
        "StaleCallGuard",
    ),
    "emerge.agent.control": ("ControlAction", "ControlResult", "Controller", "TurnControl"),
    "emerge.agent.inbox": ("Inbox", "InboxItem", "InboxKind"),
    "emerge.agent.memory": ("MemoryImage", "MemoryResult", "MemoryStore", "split_entries"),
    "emerge.agent.session": ("Session",),
}
_LAZY = {name: module for module, names in _MAIN_AGENT.items() for name in names}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'emerge.agent' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


__all__ = [
    "AGENT_CONTROL",
    "AGENT_FORK",
    "AGENT_INBOX",
    "AGENT_TURN",
    "AgentLoop",
    "AgentMiddleware",
    "CompactionMiddleware",
    "Compactor",
    "ContextBuilder",
    "ControlAction",
    "ControlResult",
    "Controller",
    "FinishReason",
    "History",
    "HistoryListener",
    "Inbox",
    "InboxItem",
    "InboxKind",
    "MainAgent",
    "MemoryImage",
    "MemoryResult",
    "MemoryStore",
    "MiddlewareChain",
    "PromptCacheKey",
    "RequestBuilder",
    "RunContext",
    "RunLimits",
    "Session",
    "Skill",
    "SkillStore",
    "StaleCallGuard",
    "Tool",
    "ToolResult",
    "Toolset",
    "Turn",
    "TurnControl",
    "TurnResult",
    "call_model",
    "error_result",
    "split_entries",
    "tool",
]

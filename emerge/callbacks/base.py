from typing import ClassVar

from emerge.events import Event, EventType

# Only known event types are routed to hooks, so an arbitrary type string can never resolve to on_event itself.
_HOOKS: dict[str, str] = {str(t): "on_" + str(t).replace(".", "_") for t in EventType}


class CallbackHandler:
    # None means every event type; a set restricts what the manager delivers to this handler.
    event_types: ClassVar[frozenset[str] | None] = None

    def accepts(self, event: Event) -> bool:
        return self.event_types is None or event.type in self.event_types

    async def on_event(self, event: Event) -> None:
        hook = _HOOKS.get(event.type)
        if hook is not None:
            await getattr(self, hook)(event)

    async def close(self) -> None:
        pass

    async def on_run_started(self, event: Event) -> None:
        pass

    async def on_run_finished(self, event: Event) -> None:
        pass

    async def on_node_state(self, event: Event) -> None:
        pass

    async def on_node_report(self, event: Event) -> None:
        pass

    async def on_node_custom(self, event: Event) -> None:
        pass

    async def on_node_ask(self, event: Event) -> None:
        pass

    async def on_node_answer(self, event: Event) -> None:
        pass

    async def on_node_main_agent_message(self, event: Event) -> None:
        pass

    async def on_port_published(self, event: Event) -> None:
        pass

    async def on_model_started(self, event: Event) -> None:
        pass

    async def on_model_token(self, event: Event) -> None:
        pass

    async def on_model_finished(self, event: Event) -> None:
        pass

    async def on_model_usage(self, event: Event) -> None:
        pass

    async def on_tool_started(self, event: Event) -> None:
        pass

    async def on_tool_finished(self, event: Event) -> None:
        pass

    async def on_graph_patched(self, event: Event) -> None:
        pass

    async def on_agent_message(self, event: Event) -> None:
        pass

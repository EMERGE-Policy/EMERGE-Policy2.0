"""The application: everything one workspace needs, put together in order and taken apart in reverse.

`Application.create(workspace)`:

1. a workspace that does not exist yet is created from the template, and the user is told to fill
   in `config.json` before anything else happens;
2. `load_config`;
3. one `CallbackManager` for everything, with a handler that logs what deserves a log line;
4. the node registry: the modules `config.nodes` lists, then the generated nodes already in
   `code/` (every version, oldest first, so the newest is current and older ones stay loadable);
5. the store (`<workspace>/.emerge/state.db`);
6. the `RuntimeService`, whose feedback goes to the main agent, and the `MainAgent`; the agent's
   model client then checks that the configured API mode answers;
7. a session: the one asked for, else the most recent, else a new one. Opening an old one carries
   it on: the conversation comes back from storage and runs left open carry on, without asking.

`aclose()` closes the session (the main agent finishes as session.py describes and open runs are
closed, to be carried on next time), then the rest in reverse order. Quitting, `/new` and switching
to another session all close the session the same way.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import emerge.config as emerge_config

from emerge.agent import MainAgent, Session
from emerge.callbacks import CallbackHandler, CallbackManager
from emerge.config import CONFIG_FILE, WorkspaceConfig, init_workspace, load_config
from emerge.events import Event, EventType
from emerge.exceptions import ConfigError, EmergeError, ModelAPIModeError, ModelError, RegistryError
from emerge.graph.node import Node
from emerge.graph.registry import NodeLocator, NodeRegistry
from emerge.models.base import ModelProvider
from emerge.models.client import ModelClient
from emerge.models.retry import RetryPolicy
from emerge.runtime import RUN_IDLE, RuntimeService
from emerge.storage import Storage, code_path, list_generated_versions, open_storage

logger = logging.getLogger(__name__)
events_logger = logging.getLogger("emerge.events")


class StartupError(EmergeError):
    """The application cannot start; the message says what to do about it."""


class EventLog(CallbackHandler):
    """Logs what an operator would look for afterwards: failures, nodes ending, runs ending."""

    event_types = frozenset(
        {EventType.NODE_STATE, EventType.MODEL_FINISHED, EventType.RUN_FINISHED, EventType.GRAPH_PATCHED, RUN_IDLE}
    )

    async def on_event(self, event: Event) -> None:
        payload = event.payload
        if event.type == EventType.MODEL_FINISHED and payload.get("error"):
            events_logger.warning("model call failed: %s", payload["error"])
        elif event.type == EventType.NODE_STATE and payload.get("state") in ("finished", "failed", "stopped", "skipped"):
            level = logging.WARNING if payload.get("state") == "failed" else logging.INFO
            detail = payload.get("outcome") or payload.get("error") or ""
            events_logger.log(level, "run %s: node %s %s %s", event.run_id, event.node_id, payload.get("state"), detail)
        elif event.type == EventType.GRAPH_PATCHED:
            events_logger.info("run %s: graph version %s", event.run_id, payload.get("version"))
        elif event.type == RUN_IDLE:
            events_logger.info("run %s: graph idle", event.run_id)
        elif event.type == EventType.RUN_FINISHED:
            events_logger.info("run %s ended: %s", event.run_id, payload.get("status"))


class Application:
    """One workspace, assembled. Create it with `Application.create()`; `aclose()` when done."""

    def __init__(
        self,
        config: WorkspaceConfig,
        callbacks: CallbackManager,
        registry: NodeRegistry,
        storage: Storage,
        runtime: RuntimeService,
        agent: MainAgent,
    ) -> None:
        self.config = config
        self.workspace = config.workspace
        self.callbacks = callbacks
        self.registry = registry
        self.storage = storage
        self.runtime = runtime
        self.agent = agent
        self.session: Session | None = None
        self.closed = False

    @classmethod
    async def create(
        cls,
        workspace: Path | str | None = None,
        *,
        session_id: str | None = None,
        handlers: tuple[CallbackHandler, ...] = (),
        check_model: bool = True,
        config: WorkspaceConfig | None = None,
        provider: ModelProvider | None = None,
    ) -> Application:
        """Assemble the application and open a session; `handlers` see every event from the start.

        `config` replaces reading the workspace's config.json (its workspace is used); `provider`
        replaces the configured model provider. Both are for embedding and tests.
        """
        if config is None:
            target = Path(workspace).expanduser().resolve() if workspace is not None else None
            if target is not None and init_workspace(target):
                raise StartupError(
                    f"Created a new workspace at {target} from the template. Fill in {target / 'config.json'} "
                    "(the model provider's api_key at least), then start again."
                )
            try:
                config = load_config(target)
            except ConfigError as e:
                raise StartupError(str(e)) from None
        if provider is None:
            _require_api_key(config)
        callbacks = CallbackManager()
        for handler in (EventLog(), *handlers):
            callbacks.add_handler(handler)
        registry = load_registry(config)
        storage = await open_storage(config.workspace, port_buffer=config.runtime.port_buffer)
        runtime: RuntimeService | None = None
        agent: MainAgent | None = None
        try:
            runtime = await RuntimeService.open(
                storage, config, registry, callbacks=callbacks, on_feedback=lambda item: agent and agent.on_feedback(item)
            )
            try:
                agent = MainAgent(config, storage, runtime, registry, callbacks=callbacks)
            except ConfigError as e:
                raise StartupError(f"{e}. Set it in {config.config_path}.") from None
            if provider is not None:
                await agent.client.aclose()
                retry = RetryPolicy(max_retries=config.agent.max_retries)
                agent.client = ModelClient(provider, model=config.model.name, retry=retry, callbacks=agent.events)
            if check_model:
                await check_model_api(agent, config)
            app = cls(config, callbacks, registry, storage, runtime, agent)
            await app.open_session(session_id)
            return app
        except BaseException:
            if runtime is not None:
                await runtime.aclose()
            await storage.aclose()
            await callbacks.aclose()
            raise

    async def open_session(self, session_id: str | None = None) -> Session:
        """Open `session_id`, else the most recent session, else a new one."""
        if session_id is None:
            recent = await Session.list(self.storage, limit=1)
            session_id = recent[0].id if recent else None
        if session_id is None:
            self.session = await Session.create(self.agent)
        else:
            if await self.storage.get_session(session_id) is None:
                raise StartupError(f"no session {session_id!r} in {self.workspace}")
            self.session = await Session.open(self.agent, session_id)
        return self.session

    async def switch_session(self, session_id: str | None = None) -> Session:
        """Close the current session as on quitting, then open `session_id`, or a new one when None."""
        if self.session is not None:
            await self.session.close()
            self.session = None
        if session_id is None:
            self.session = await Session.create(self.agent)
            return self.session
        return await self.open_session(session_id)

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.session is not None:
                await self.session.close()
        finally:
            await self.runtime.aclose()
            await self.agent.client.aclose()
            await self.storage.aclose()
            await self.callbacks.aclose()


def load_registry(config: WorkspaceConfig) -> NodeRegistry:
    """The configured node modules, then the generated nodes saved under `code/`."""
    registry = NodeRegistry()
    try:
        registry.load_modules(config.nodes)
        for name in config.nodes:
            _register_imported(registry, name)
    except RegistryError as e:
        raise StartupError(f"{e}. Check the module paths in {config.config_path} (\"nodes\").") from None
    code_dir = config.code_dir
    for directory in sorted(path for path in code_dir.iterdir() if path.is_dir()) if code_dir.is_dir() else ():
        for version in list_generated_versions(config.workspace, directory.name):
            try:
                registry.register_from_file(code_path(config.workspace, directory.name, version), version)
            except (RegistryError, OSError) as e:
                # One broken generated file must not keep the workspace from starting.
                logger.warning("generated node %s v%s not loaded: %s", directory.name, version, e)
    return registry


def _require_api_key(config: WorkspaceConfig) -> None:
    """Stop before any request when the model provider's key is empty or still the template's placeholder."""
    name = config.model.provider
    entry = config.providers.get(name)
    if entry is None:
        raise StartupError(f"model.provider is {name!r}, but providers.{name} is missing in {config.config_path}")
    try:
        template = json.loads((Path(emerge_config.__file__).parent / "workspace_template" / CONFIG_FILE).read_text(encoding="utf-8"))
        placeholder = template.get("providers", {}).get(name, {}).get("api_key")
    except (OSError, ValueError):
        placeholder = None
    if not entry.api_key.strip() or entry.api_key == placeholder:
        raise StartupError(f"no API key for {name}: put it in providers.{name}.api_key in {config.config_path}")


def _register_imported(registry: NodeRegistry, module_name: str) -> None:
    """Register the node classes of a module imported before, whose decorators will not run again.

    Importing registers a module's nodes only the first time in a process; an application
    assembled again in the same process still has to see them.
    """
    module = sys.modules.get(module_name)
    for obj in list(vars(module).values()) if module is not None else ():
        if (
            isinstance(obj, type)
            and issubclass(obj, Node)
            and obj.__module__ == module_name
            and obj.name
            and obj.outcomes
            and obj.name not in registry
        ):
            registry.register(obj, NodeLocator(origin="module", module=module_name))


async def check_model_api(agent: MainAgent, config: WorkspaceConfig) -> None:
    model = config.model
    try:
        await agent.client.probe_api_compatibility()
    except ModelAPIModeError as e:
        raise StartupError(f"{model.provider} does not support api_mode {model.api_mode!r} for {model.name}: {e}") from None
    except ModelError as e:
        raise StartupError(
            f"the model {model.name} ({model.provider} / {model.api_mode}) did not answer a test request: {e}. "
            f"Check providers.{model.provider} (base_url, api_key) in {config.config_path}."
        ) from None

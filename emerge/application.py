"""The application: everything one workspace needs, put together in order and taken apart in reverse.

A workspace is its settings (`config.json`, AGENTS.md, USER.md, skills/) and one folder per
conversation, `sessions/<session_id>/`, holding everything that conversation has. The application
holds what the whole workspace shares and, at any time, one open conversation built on its folder.

`Application.create(workspace)`:

1. a workspace that does not exist yet is created from the template, and the user is told to fill
   in `config.json` before anything else happens;
2. `load_config`;
3. one `CallbackManager` for everything, with a handler that logs what deserves a log line;
4. the configured API mode is checked to answer;
5. a conversation: the one asked for, else the most recent, else a new one.

Opening a conversation builds, on its folder:

- its store (`.emerge/state.db`);
- its node registry: the modules `config.nodes` lists, then the nodes generated in the conversation
  (`code/`, every version, oldest first, so the newest is current and older ones stay loadable);
- its `RuntimeService`, whose feedback goes to the main agent, and its `MainAgent`;
- its `Session`. Opening an old one carries it on: the conversation comes back from storage and runs
  left open carry on, without asking.

Quitting, `/new` and switching to another conversation all close the open one the same way: the
session first (the main agent finishes as session.py describes and open runs are closed, to be
carried on next time), then the rest in reverse order.
"""

from __future__ import annotations

import logging
import shutil
import sys
import uuid
from pathlib import Path

from emerge.agent import MainAgent, Session, list_sessions
from emerge.callbacks import CallbackHandler, CallbackManager
from emerge.config import WorkspaceConfig, api_key_missing, init_workspace, load_config
from emerge.events import Event, EventType
from emerge.exceptions import ConfigError, EmergeError, ModelAPIModeError, ModelError, RegistryError
from emerge.graph.node import Node
from emerge.graph.registry import NodeLocator, NodeRegistry
from emerge.models.base import ModelProvider
from emerge.models.factory import create_client
from emerge.runtime import RUN_IDLE, RuntimeService
from emerge.storage import (
    Storage,
    code_path,
    list_generated_names,
    list_generated_versions,
    open_storage,
    read_session,
    session_dir,
)

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
    """One workspace, assembled. Create it with `Application.create()`; `aclose()` when done.

    `session_dir`, `storage`, `registry`, `runtime` and `agent` belong to the open conversation.
    Switching replaces them once the next conversation is open; until then they are the closed
    one's, so a view reading them meanwhile sees a closed conversation, never nothing. `session` is
    None while no conversation is open.
    """

    def __init__(
        self, config: WorkspaceConfig, callbacks: CallbackManager, *, provider: ModelProvider | None = None
    ) -> None:
        self.config = config
        self.workspace = config.workspace
        self.callbacks = callbacks
        # Replaces the configured model provider in every conversation; for embedding and tests.
        self.provider = provider
        self.session_dir: Path | None = None
        self.storage: Storage | None = None
        self.registry: NodeRegistry | None = None
        self.runtime: RuntimeService | None = None
        self.agent: MainAgent | None = None
        self.session: Session | None = None
        self.closed = False

    @classmethod
    async def create(
        cls,
        workspace: Path | str | None = None,
        *,
        session_id: str | None = None,
        new_session: bool = False,
        handlers: tuple[CallbackHandler, ...] = (),
        check_model: bool = True,
        config: WorkspaceConfig | None = None,
        provider: ModelProvider | None = None,
    ) -> Application:
        """Assemble the application and open a conversation; `handlers` see every event from the start.

        The conversation is `session_id`, else a new one with `new_session`, else the most recent one.

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
        app = cls(config, callbacks, provider=provider)
        try:
            if check_model:
                await check_model_api(config, provider)
            await (app.switch_session(None) if new_session and session_id is None else app.open_session(session_id))
        except BaseException:
            await app.aclose()
            raise
        return app

    async def open_session(self, session_id: str | None = None) -> Session:
        """Open `session_id`, else the most recent conversation, else a new one."""
        if session_id is None:
            recent = await list_sessions(self.workspace, limit=1)
            if not recent:
                return await self._open(uuid.uuid4().hex, new=True)
            session_id = recent[0].session.id
        await self._require_stored(session_id)
        return await self._open(session_id, new=False)

    async def switch_session(self, session_id: str | None = None) -> Session:
        """Close the open conversation as on quitting, then open `session_id`, or a new one when None."""
        if session_id is not None:
            await self._require_stored(session_id)
        await self._close_session()
        return await self._open(session_id or uuid.uuid4().hex, new=session_id is None)

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self._close_session()
        finally:
            if self.provider is not None:
                await self.provider.aclose()
            await self.callbacks.aclose()

    # -- one conversation ------------------------------------------------------------

    async def _open(self, session_id: str, *, new: bool) -> Session:
        """Build everything the conversation needs on its folder, then create or carry on its session."""
        folder = session_dir(self.workspace, session_id)
        config = self.config
        storage = await open_storage(folder, port_buffer=config.runtime.port_buffer)
        runtime: RuntimeService | None = None
        agent: MainAgent | None = None
        try:
            registry = load_registry(config, folder)
            runtime = await RuntimeService.open(
                storage,
                config,
                registry,
                callbacks=self.callbacks,
                on_feedback=lambda item: agent and agent.on_feedback(item),
            )
            try:
                agent = MainAgent(config, storage, runtime, registry, callbacks=self.callbacks, provider=self.provider)
            except ConfigError as e:
                raise StartupError(f"{e}. Set it in {config.config_path}.") from None
            session = await (Session.create(agent, session_id) if new else Session.open(agent))
        except BaseException:
            if runtime is not None:
                await runtime.aclose()
            if agent is not None and self.provider is None:
                await agent.client.aclose()
            await storage.aclose()
            if new:
                # Nothing of a conversation that never started is worth keeping.
                shutil.rmtree(folder, ignore_errors=True)
            raise
        self.session_dir, self.storage, self.registry = folder, storage, registry
        self.runtime, self.agent, self.session = runtime, agent, session
        return session

    async def _close_session(self) -> None:
        """Close the open conversation: its session, then its runtime, model client and storage."""
        session, self.session = self.session, None
        if session is None:
            return
        try:
            await session.close()
        finally:
            await self.runtime.aclose()
            if self.provider is None:
                # A provider given to `create` serves every conversation; `aclose` closes it once.
                await self.agent.client.aclose()
            await self.storage.aclose()

    async def _require_stored(self, session_id: str) -> None:
        try:
            folder = session_dir(self.workspace, session_id)
        except ValueError as e:
            raise StartupError(str(e)) from None
        if await read_session(folder) is None:
            raise StartupError(f"no session {session_id!r} in {self.workspace}")


def load_registry(config: WorkspaceConfig, session_dir: Path) -> NodeRegistry:
    """The configured node modules, then the nodes generated in the conversation in `session_dir`."""
    registry = NodeRegistry()
    try:
        registry.load_modules(config.nodes)
        for name in config.nodes:
            _register_imported(registry, name)
    except RegistryError as e:
        raise StartupError(f"{e}. Check the module paths in {config.config_path} (\"nodes\").") from None
    for name in list_generated_names(session_dir):
        for version in list_generated_versions(session_dir, name):
            try:
                registry.register_from_file(code_path(session_dir, name, version), version)
            except (RegistryError, OSError) as e:
                # One broken generated file must not keep the conversation from opening.
                logger.warning("generated node %s v%s not loaded: %s", name, version, e)
    return registry


def _require_api_key(config: WorkspaceConfig) -> None:
    """Stop before any request when the model provider's key is empty or still the template's placeholder."""
    name = config.model.provider
    if config.providers.get(name) is None:
        raise StartupError(f"model.provider is {name!r}, but providers.{name} is missing in {config.config_path}")
    if api_key_missing(config):
        raise StartupError(f"no API key for {name}: put it in providers.{name}.api_key in {config.config_path}")


def _register_imported(registry: NodeRegistry, module_name: str) -> None:
    """Register the node classes of a module imported before, whose decorators will not run again.

    Importing registers a module's nodes only the first time in a process; every registry built
    after that in the same process (each conversation has its own) still has to see them.
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


async def check_model_api(config: WorkspaceConfig, provider: ModelProvider | None = None) -> None:
    """Send the configured model a test request, through `provider` when one is given."""
    model = config.model
    try:
        client = create_client(config, provider=provider)
    except ConfigError as e:
        raise StartupError(f"{e}. Set it in {config.config_path}.") from None
    try:
        await client.probe_api_compatibility()
    except ModelAPIModeError as e:
        raise StartupError(f"{model.provider} does not support api_mode {model.api_mode!r} for {model.name}: {e}") from None
    except ModelError as e:
        raise StartupError(
            f"the model {model.name} ({model.provider} / {model.api_mode}) did not answer a test request: {e}. "
            f"Check providers.{model.provider} (base_url, api_key) in {config.config_path}."
        ) from None
    finally:
        if provider is None:
            await client.aclose()

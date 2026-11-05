"""The node registry: what node types exist, where they come from, and how to reload them.

The graph stores type names only. A node process is a separate interpreter, so it cannot receive a
class: it asks the registry for the entry's locator and imports the code again from there. That is
why every entry carries where it came from, not just what it is.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from emerge.exceptions import RegistryError
from emerge.graph.node import Node
from emerge.graph.ports import Port

Origin = Literal["module", "file"]

# Generated modules load under this synthetic package so their names never collide with real ones.
GENERATED_PACKAGE = "emerge_generated_nodes"
_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


@dataclass(frozen=True, slots=True)
class NodeLocator:
    """Where a node class can be imported from again, in another process."""

    origin: Origin
    module: str
    path: str | None = None
    version: str | None = None
    # sha256 of the file as registered. The path can be rewritten later, so a process reloading
    # generated code compares this before trusting that the file still holds this version.
    digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "module": self.module,
            "path": self.path,
            "version": self.version,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class NodeEntry:
    name: str
    cls: type[Node]
    locator: NodeLocator
    params_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def description(self) -> str:
        return self.cls.description

    @property
    def inputs(self) -> tuple[Port, ...]:
        return self.cls.inputs

    @property
    def outputs(self) -> tuple[Port, ...]:
        return self.cls.outputs

    @property
    def reads(self) -> tuple[tuple[str, str], ...]:
        return self.cls.reads

    @property
    def reads_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.reads)

    @property
    def acts_on_environment(self) -> bool:
        return self.cls.acts_on_environment

    @property
    def outcomes(self) -> tuple[str, ...]:
        return self.cls.outcomes

    @property
    def params_model(self) -> type[BaseModel] | None:
        return self.cls.Params

    @property
    def version(self) -> str | None:
        return self.locator.version

    def to_dict(self) -> dict[str, Any]:
        """Catalog view: JSON-ready for agent tools and for catalog queries."""
        return {
            "name": self.name,
            "description": self.description,
            "inputs": [port.model_dump() for port in self.inputs],
            "outputs": [port.model_dump() for port in self.outputs],
            "reads": [{"name": name, "description": note} for name, note in self.reads],
            "acts_on_environment": self.acts_on_environment,
            "outcomes": list(self.outcomes),
            "params_schema": copy.deepcopy(self.params_schema),
            "locator": self.locator.to_dict(),
        }


class NodeRegistry:
    """Registered node types, in registration order."""

    def __init__(self) -> None:
        # The catalog: the current implementation of each name.
        self._entries: dict[str, NodeEntry] = {}
        # Every generated implementation ever registered, by (name, version). Instances pin one of
        # these, so registering a newer version leaves the instances placed on an older one alone.
        self._versions: dict[tuple[str, str], NodeEntry] = {}
        # Locator for the file currently being executed; decorators read it to mark their origin.
        self.loading: NodeLocator | None = None

    def find_entry(self, name: str, code: str | None = None) -> NodeEntry | None:
        """The current implementation of `name`, or the generated version `code` of it."""
        if code is None:
            return self._entries.get(name)
        return self._versions.get((name, code))

    def require_entry(self, name: str, code: str | None = None) -> NodeEntry:
        entry = self.find_entry(name, code)
        if entry is None:
            label = name if code is None else f"{name} version {code}"
            raise RegistryError(f"node type {label!r} is not registered")
        return entry

    def versions(self, name: str) -> tuple[str, ...]:
        """Generated versions registered under `name`, oldest first; empty for configured types."""
        return tuple(version for entry_name, version in self._versions if entry_name == name)

    def pin(self, name: str, code: str | None = None) -> str | None:
        """The code version an instance of `name` placed now runs.

        An explicit `code` is kept as given and checked by validation. Without one, a generated type
        pins its current version; a configured type has no versions and pins nothing.
        """
        if code is not None:
            return code
        entry = self._entries.get(name)
        return entry.locator.version if entry is not None and entry.locator.origin == "file" else None

    def names(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def catalog(self) -> tuple[NodeEntry, ...]:
        return tuple(self._entries.values())

    def external_reads_by_node_type(self) -> dict[str, tuple[str, ...]]:
        """Live-source names per registered type, for graph impact queries."""
        return {name: entry.reads_names for name, entry in self._entries.items()}

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    # -- registration -------------------------------------------------------

    def register(self, cls: type[Node], locator: NodeLocator) -> NodeEntry:
        if not cls.name.strip():
            raise RegistryError(f"node class {cls.__qualname__} has no name")
        # Every type placed in a graph says how it can finish, because start edges elsewhere name
        # outcomes by string. A shared base class stays out of this: it is never registered.
        if not cls.outcomes:
            raise RegistryError(
                f"node class {cls.name!r} declares no outcomes; list the ones its run() can return"
            )
        existing = self._entries.get(cls.name)
        if existing is not None:
            if existing.cls is cls:
                return existing
            # Configured modules own their names: a second module, or generated code, reusing one is
            # a configuration mistake and is reported at once. Generated code replaces its own
            # earlier versions under the same name.
            if locator.origin == "module" or existing.locator.origin == "module":
                raise RegistryError(
                    f"node type {cls.name!r} is registered twice: {existing.locator.module} and {locator.module}"
                )
        schema = cls.Params.model_json_schema() if cls.Params is not None else {"type": "object", "properties": {}}
        entry = NodeEntry(name=cls.name, cls=cls, locator=locator, params_schema=schema)
        if locator.origin == "file" and locator.version is not None:
            pinned = self._versions.get((cls.name, locator.version))
            # A version names one implementation: instances pinned to it must reload the same code.
            if pinned is not None and pinned.locator.digest != locator.digest:
                raise RegistryError(
                    f"node type {cls.name!r} version {locator.version!r} is already registered with "
                    "different code; register the change under a new version"
                )
            self._versions[(cls.name, locator.version)] = entry
        self._entries[cls.name] = entry
        return entry

    def load_modules(self, modules: Iterable[str]) -> None:
        """Import each module, which triggers the `@register_node` calls inside it."""
        previous = set_active_registry(self)
        try:
            for module_name in modules:
                try:
                    importlib.import_module(module_name)
                except Exception as e:  # a broken module is a startup error, not a node failure
                    raise RegistryError(
                        f"cannot import node module {module_name!r}: {type(e).__name__}: {e}"
                    ) from e
        finally:
            set_active_registry(previous)

    def register_from_file(self, path: Path | str, version: str) -> tuple[NodeEntry, ...]:
        """Load nodes from generated code; registering a name again makes it the current entry.

        Each load gets a fresh module name, because a node process still running the previous
        version keeps its own module object and the two must not share one. Older versions stay
        findable by version for the instances pinned to them.
        """
        if not version.strip():
            raise RegistryError("a generated node version must not be empty")
        file_path = Path(path).resolve()
        if not file_path.is_file():
            raise RegistryError(f"node file not found: {file_path}")
        source = file_path.read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        module_name = _module_name(file_path, version, digest)
        loaded = tuple(entry for entry in self._versions.values() if entry.locator.module == module_name)
        if loaded:
            # The same file, version and content again: reuse the module instead of running it twice.
            for entry in loaded:
                self._entries[entry.name] = entry
            self._forget_superseded(file_path, keep=module_name)
            return loaded
        locator = NodeLocator(
            origin="file", module=module_name, path=str(file_path), version=version, digest=digest
        )
        # The decorators inside the file register into whichever registry is active, so this one is
        # made active while the file runs. A load that fails part way leaves the catalog as it was.
        snapshot = (dict(self._entries), dict(self._versions))
        previous_registry = set_active_registry(self)
        previous_loading = self.loading
        self.loading = locator
        try:
            module = _exec_module(module_name, file_path, source)
            # Picks up node classes declared without the decorator; decorated ones are already in.
            for obj in list(vars(module).values()):
                if isinstance(obj, type) and issubclass(obj, Node) and obj.__module__ == module_name:
                    self.register(obj, locator)
        except BaseException:
            self._entries, self._versions = snapshot
            sys.modules.pop(module_name, None)
            raise
        finally:
            self.loading = previous_loading
            set_active_registry(previous_registry)
        entries = tuple(entry for entry in self._entries.values() if entry.locator == locator)
        if not entries:
            raise RegistryError(f"node file {file_path} registered no node classes")
        self._forget_superseded(file_path, keep=module_name)
        return entries

    def _forget_superseded(self, path: Path, *, keep: str) -> None:
        """Take older versions of this file out of the catalog; they stay findable by version."""
        for name, entry in list(self._entries.items()):
            locator = entry.locator
            if locator.origin == "file" and locator.path == str(path) and locator.module != keep:
                del self._entries[name]


def _module_name(path: Path, version: str, digest: str) -> str:
    # Unique per path, version and content: re-registering edited code under the same version must
    # not reuse the module object a running node may still hold.
    key = hashlib.sha1(f"{path}\0{version}\0{digest}".encode()).hexdigest()[:10]
    stem = _UNSAFE.sub("_", path.stem)
    label = _UNSAFE.sub("_", version).lstrip("v")
    return f"{GENERATED_PACKAGE}.{stem}_v{label}_{key}"


def _exec_module(module_name: str, path: Path, source: bytes) -> ModuleType:
    _ensure_package(module_name.rpartition(".")[0])
    module = ModuleType(module_name)
    module.__file__ = str(path)
    # In sys.modules before it runs, so tracebacks and pickling can resolve the module by name.
    sys.modules[module_name] = module
    try:
        # Runs the bytes that were hashed, not a second read of the file; dont_inherit keeps this
        # module's own __future__ flags out of the generated code.
        exec(compile(source, str(path), "exec", dont_inherit=True), module.__dict__)
    except Exception as e:
        sys.modules.pop(module_name, None)
        raise RegistryError(f"cannot load node file {path}: {type(e).__name__}: {e}") from e
    return module


def _ensure_package(name: str) -> None:
    if name in sys.modules:
        return
    package = ModuleType(name)
    package.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = package


class _RegistryHolder:
    """The registry the `@register_node` decorator uses.

    A decorator runs at import time and has no reference to hand, so it reaches the active
    registry through this holder. Tests and scratch scripts swap in their own instance.
    """

    current: ClassVar[NodeRegistry] = NodeRegistry()


def register_node(cls: type[Node]) -> type[Node]:
    """Class decorator: register the class under `Node.name` in the active registry."""
    registry = _RegistryHolder.current
    locator = registry.loading or NodeLocator(origin="module", module=cls.__module__)
    registry.register(cls, locator)
    return cls


def get_active_registry() -> NodeRegistry:
    return _RegistryHolder.current


def set_active_registry(registry: NodeRegistry) -> NodeRegistry:
    """Make `registry` the one decorators and `get_active_registry()` use; returns the previous one."""
    previous = _RegistryHolder.current
    _RegistryHolder.current = registry
    return previous

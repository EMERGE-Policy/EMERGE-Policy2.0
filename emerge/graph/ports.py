"""Ports, messages and references: the data that crosses a node boundary.

A message carries text, a small JSON structure and references. Content bodies (images, point
clouds) live in the workspace and are reached through a `Ref`; they never travel in a message.
"""

from collections.abc import Mapping
from typing import Annotated, Any, NoReturn

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainSerializer, field_validator

# Endpoints are written "node.port", so ids and port names cannot contain dots or whitespace.
NAME_PATTERN = r"^[^.\s]+$"
# A ref key names a directory under runs/<run_id>/nodes/..., so these would leave it.
_KEY_SEPARATORS = "/\\"
_KEY_RESERVED = frozenset({".", ".."})


def _read_only(self: object, *args: object, **kwargs: object) -> NoReturn:
    raise TypeError(f"{type(self).__name__} is read-only")


# dict / list subclasses rather than proxies: they still compare equal to plain JSON data, encode
# with json.dumps, and pickle into a spawned node process.
class FrozenDict(dict[str, Any]):
    __setitem__ = __delitem__ = __ior__ = _read_only
    clear = pop = popitem = setdefault = update = _read_only

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (dict(self),))


class FrozenList(list[Any]):
    __setitem__ = __delitem__ = __iadd__ = __imul__ = _read_only
    append = extend = insert = pop = remove = clear = sort = reverse = _read_only

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (list(self),))


def freeze(value: Any) -> Any:
    """Deep read-only copy of JSON-like data."""
    if isinstance(value, Mapping):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return FrozenList(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Plain mutable copy of frozen data, for callers that edit it or hand it on as JSON."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


# JSON-like field of an immutable model. Graph versions share node specs, so a mutable dict here
# would let an edit made through one version show up in every other.
FrozenJson = Annotated[Mapping[str, Any], AfterValidator(freeze), PlainSerializer(thaw)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Port(_Frozen):
    name: str = Field(pattern=NAME_PATTERN)
    # Soft: two ends of a subscription tagged differently get a warning, not a rejection.
    type_tag: str = Field(min_length=1)
    description: str = ""


class Ref(_Frozen):
    """A reference to one piece of content.

    The scope is (producer, producer_instance, port, key), composed by the framework: a node never
    builds a path itself, it asks `NodeContext.ref_dir()` for the directory. The same key on two
    output ports, or written by an instance and again by the instance that replaced it, makes two
    different references. The reference carries no time of its own; the message that published it
    carries `content_time`.
    """

    producer: str = Field(pattern=NAME_PATTERN)
    producer_instance: str = Field(min_length=1)
    port: str = Field(pattern=NAME_PATTERN)
    key: str = Field(min_length=1)

    @field_validator("key")
    @classmethod
    def _key_is_a_directory_name(cls, value: str) -> str:
        # The key becomes a directory name, so it must not climb out of the reference directory.
        if value in _KEY_RESERVED or any(char in value for char in _KEY_SEPARATORS):
            raise ValueError(f"ref key {value!r} must not be '.' or '..' or contain '/', '\\'")
        return value


class Message(_Frozen):
    """One published value.

    Execution fills `source_node`, `source_instance`, `source_port`, `version`, `timestamp` and
    `content_time`; a node only supplies `text`, `data` and `refs`. A replaced node keeps its id,
    so `source_instance` is what tells a late message from the old instance apart.

    Content time is the message's place on the run's one graph-wide timeline: a single increasing
    integer that the bus stamps as it publishes, so a larger number is always later, across nodes
    too. Nodes cannot set it, compute it or inherit it, and it has nothing to do with frame
    numbers. The same counter stamps `MainAgentMessage` and the events nodes and the main agent
    send each other (`Event.content_time`, None on process events such as model tokens).
    `timestamp` is the wall-clock publish moment and answers a different question.

    `data` holds small JSON structures (status, counts, flags). Content bodies belong in the
    workspace and are reached through `refs`.

    An output port keeps its most recent messages (RuntimeConfig.port_buffer, 64 by default), not
    just the newest one, so a consumer can continue from the version it last saw after missing a
    notification. `get_latest_input_message()` still returns the newest; the buffer exists so nothing is skipped.
    """

    source_node: str
    source_instance: str = Field(min_length=1)
    source_port: str
    version: int = Field(ge=0)
    timestamp: float
    content_time: int = Field(ge=0)
    text: str = ""
    data: FrozenJson | None = None
    refs: tuple[Ref, ...] = ()


class MainAgentMessage(_Frozen):
    """Something the main agent sent to one node.

    The framework only delivers it, in the order sent; whether the node reads it and what it means
    are up to the node. Execution fills `seq`, `timestamp` and `content_time` from the same graph-wide
    counter as `Message`. Content follows the Message rules: small structures in `data`, content
    bodies through `refs`.
    """

    seq: int = Field(ge=1)
    timestamp: float
    content_time: int = Field(ge=0)
    text: str = ""
    data: FrozenJson | None = None
    refs: tuple[Ref, ...] = ()

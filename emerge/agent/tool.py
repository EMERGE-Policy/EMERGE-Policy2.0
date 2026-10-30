"""Tool definitions built from function signatures.

`@tool` turns a function into a `Tool`: the parameter model and JSON schema come from its type
annotations and docstring. Arguments are validated with Pydantic; bad arguments and exceptions
inside the tool become an error result for the model instead of aborting the loop.
"""

import inspect
import json
import re
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from emerge.exceptions import ToolError
from emerge.models.types import ImagePart, Message, ToolSpec

_SECTION = re.compile(r"^(args|arguments|parameters|returns?|raises|yields|examples?|notes?)\s*:\s*$", re.IGNORECASE)
_ARG_ENTRY = re.compile(r"^(\*{0,2}\w+)\s*(?:\(([^)]*)\))?\s*:\s*(.*)$")


class ToolResult(BaseModel):
    """What a tool hands back to the loop: text, image paths, or both."""

    model_config = ConfigDict(extra="forbid")

    content: str = ""
    images: list[str] = []
    is_error: bool = False

    def to_message(self, tool_call_id: str) -> Message:
        return Message.tool(tool_call_id, self.content or "(no output)", self.images)


class Tool:
    def __init__(self, fn: Callable[..., Any], *, name: str | None = None, description: str | None = None) -> None:
        self.fn = fn
        self.name = name or fn.__name__
        args_model = _args_model(fn, self.name)
        doc = inspect.getdoc(fn) or ""
        self.description = description if description is not None else _summary(doc)
        self.args_model = args_model
        self.spec = ToolSpec(name=self.name, description=self.description, parameters=args_model.model_json_schema())

    async def invoke(self, arguments: dict[str, Any]) -> ToolResult:
        if set(arguments) == {"_raw"}:
            return _error(self.name, f"arguments are not valid JSON: {arguments['_raw']}")
        try:
            args = self.args_model.model_validate(arguments)
        except ValidationError as e:
            details = [{"loc": ".".join(str(x) for x in item["loc"]), "msg": item["msg"]} for item in e.errors()]
            return _error(self.name, "invalid arguments", details)
        kwargs = {field: getattr(args, field) for field in type(args).model_fields}
        try:
            value = self.fn(**kwargs)
            if inspect.isawaitable(value):
                value = await value
            return _normalize_tool_result(value)
        except Exception as e:  # a failing tool is reported to the model, not raised into the loop
            return _error(self.name, f"{type(e).__name__}: {e}")

    def __repr__(self) -> str:
        return f"Tool({self.name!r})"


class Toolset:
    """Registered tools in registration order; that order is the order sent to the model."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        self.add(*tools)

    def add(self, *tools: Tool) -> None:
        for tool in tools:
            if tool.name in self._tools:
                raise ToolError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    def remove(self, name: str) -> None:
        del self._tools[name]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)


def tool(
    fn: Callable[..., Any] | None = None, *, name: str | None = None, description: str | None = None
) -> Any:
    """Decorate a sync or async function as a tool. Usable as `@tool` or `@tool(name=..., description=...)`."""

    def wrap(func: Callable[..., Any]) -> Tool:
        return Tool(func, name=name, description=description)

    return wrap if fn is None else wrap(fn)


def _args_model(fn: Callable[..., Any], tool_name: str) -> type[BaseModel]:
    signature = inspect.signature(fn)
    try:
        hints = inspect.get_annotations(fn, eval_str=True)
    except Exception as e:
        raise ToolError(f"tool {tool_name!r} has unresolvable type annotations: {e}") from None
    descriptions = _arg_descriptions(inspect.getdoc(fn) or "")
    fields: dict[str, tuple[Any, Any]] = {}
    for param_name, param in signature.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise ToolError(f"tool {tool_name!r} cannot use *args or **kwargs")
        annotation = hints.get(param_name)
        if annotation is None:
            raise ToolError(f"tool {tool_name!r} parameter {param_name!r} needs a type annotation")
        default = ... if param.default is inspect.Parameter.empty else param.default
        if param_name in descriptions:
            fields[param_name] = (annotation, Field(default=default, description=descriptions[param_name]))
        else:
            fields[param_name] = (annotation, default)
    return create_model(
        f"{tool_name}_args",
        __config__=ConfigDict(extra="forbid"),
        **fields,  # type: ignore[call-overload]
    )


def _summary(doc: str) -> str:
    lines: list[str] = []
    for line in inspect.cleandoc(doc).splitlines():
        stripped = line.strip()
        if not stripped or _SECTION.match(stripped):
            break
        lines.append(stripped)
    return " ".join(lines)


def _arg_descriptions(doc: str) -> dict[str, str]:
    result: dict[str, str] = {}
    in_args = False
    current: str | None = None
    for line in inspect.cleandoc(doc).splitlines():
        stripped = line.strip()
        if _SECTION.match(stripped):
            in_args = stripped.split(":", 1)[0].lower() in ("args", "arguments", "parameters")
            current = None
            continue
        if not in_args or not stripped:
            continue
        match = _ARG_ENTRY.match(stripped)
        if match:
            current = match.group(1).lstrip("*")
            result[current] = match.group(3).strip()
        elif current is not None:
            result[current] = (result[current] + " " + stripped).strip()
    return result


def _normalize_tool_result(value: Any) -> ToolResult:
    match value:
        case ToolResult():
            return value
        case str():
            return ToolResult(content=value)
        case ImagePart():
            return ToolResult(content=f"Image: {value.path}", images=[value.path])
        case BaseModel():
            return ToolResult(content=value.model_dump_json())
        # Plain JSON values are common for small results (counts, flags, dicts); serialize them as-is.
        case None:
            return ToolResult()
        case bool() | int() | float() | dict() | list() | tuple():
            return ToolResult(content=json.dumps(value, ensure_ascii=False, default=str))
        case _:
            raise TypeError(
                f"tool returned {type(value).__name__}; expected str, ImagePart, ToolResult, a Pydantic model "
                "or a JSON-serializable value"
            )


def error_result(tool_name: str, message: str, details: list[dict[str, str]] | None = None) -> ToolResult:
    """An error the model can read and react to, rather than an exception that breaks the loop."""
    payload: dict[str, Any] = {"tool": tool_name, "error": message}
    if details:
        payload["details"] = details
    return ToolResult(content=json.dumps(payload, ensure_ascii=False), is_error=True)


def _error(tool_name: str, message: str, details: list[dict[str, str]] | None = None) -> ToolResult:
    return error_result(tool_name, message, details)

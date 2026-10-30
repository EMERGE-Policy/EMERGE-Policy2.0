"""Experiments on emerge.agent.tool: schema generation, definition errors, invoke results."""
import asyncio
import enum
import functools
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel

from emerge.agent.tool import Tool, ToolResult, Toolset, tool
from emerge.models.types import ImagePart


def show(label, fn):
    try:
        t = fn()
        print(f"[{label}] OK ->", json.dumps(t.spec.parameters, ensure_ascii=False)[:400])
        return t
    except BaseException as e:
        print(f"[{label}] RAISE {type(e).__module__}.{type(e).__name__}: {str(e)[:160]}")


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Point(BaseModel):
    x: int
    y: int = 0


class Foo:
    pass


def types_fn(
    a: int,
    b: float,
    c: str,
    d: bool,
    e: list[int],
    f: dict[str, Any],
    g: Literal["x", "y"],
    h: Color,
    i: Optional[int] = None,
    j: int | str = 1,
    k: Point = Point(x=1),
    l: datetime | None = None,
    m: Path = Path("."),
    n: tuple[int, int] = (0, 0),
    o: Any = None,
    p: bytes = b"",
    q: set[str] = set(),
):
    """Types demo."""


print("=== schema of supported types")
t = show("types", lambda: Tool(types_fn))
print(json.dumps(t.spec.parameters, ensure_ascii=False, indent=None)[:3000])

print("=== unsupported / edge definitions")
def arb(x: Foo): ...
show("arbitrary class", lambda: Tool(arb))
def cb(x: Callable[[int], int]): ...
show("Callable", lambda: Tool(cb))
def noann(x): ...
show("no annotation", lambda: Tool(noann))
def noneann(x: None): ...
show("annotation None", lambda: Tool(noneann))
def varargs(*xs: int): ...
show("*args", lambda: Tool(varargs))
def kwargs(**kw: int): ...
show("**kwargs", lambda: Tool(kwargs))
def under(_x: int): ...
show("leading underscore param", lambda: Tool(under))
def fwd(x: "Undefined"): ...  # noqa
show("unresolvable forward ref", lambda: Tool(fwd))
def base(x: int, y: int) -> int:
    return x + y
show("functools.partial", lambda: Tool(functools.partial(base, y=1)))
show("lambda", lambda: Tool(lambda x: x))
show("@tool('name') positional", lambda: tool("myname"))
def posonly(x: int, /) -> int:
    return x
pt = show("positional-only", lambda: Tool(posonly))
class C:
    def meth(self, x: int) -> int:
        """Method tool."""
        return x
mt = show("bound method", lambda: Tool(C().meth))
def kwonly(*, x: int) -> int:
    return x
kt = show("keyword-only", lambda: Tool(kwonly))

print("=== docstring parsing")
def doc1(path: str, pattern: str = "*.py", depth: int = 1, **_unused: int) -> str: ...
def doc_fn(path: str, pattern: str = "*.py", depth: int = 1) -> str:
    """Count files under a directory.
    Second line of the summary.

    More prose that is not in the description.

    Args:
        path (str): Directory to scan.
            Continues here.
        pattern: Glob pattern.
            Note: not a section header, looks like an entry.
        depth: How deep.

        Trailing prose after a blank line: still in Args?
        unknown: An entry for a param that does not exist.

    Returns:
        A count.
    """
    return ""
t = Tool(doc_fn)
print("description:", repr(t.description))
print("props:", json.dumps(t.spec.parameters["properties"], ensure_ascii=False))
def numpy_fn(x: int) -> int:
    """Numpy style.

    Parameters
    ----------
    x : int
        The x.
    """
    return x
t = Tool(numpy_fn)
print("numpy description:", repr(t.description), "props:", t.spec.parameters["properties"])
def args_first(x: int) -> int:
    """Args:
        x: The x.
    """
    return x
t = Tool(args_first)
print("docstring starting with Args: description=", repr(t.description), t.spec.parameters["properties"])
def nodoc(x: int) -> int:
    return x
t = Tool(nodoc)
print("no docstring: description=", repr(t.description), "name=", t.name)
t = Tool(nodoc, name="", description="")
print("name='' ->", repr(t.name), "description='' ->", repr(t.description))
print("full schema of doc_fn:", json.dumps(Tool(doc_fn).spec.parameters, ensure_ascii=False))


async def invoke_cases():
    print("=== invoke")
    @tool
    def add(a: int, b: int = 2) -> int:
        """Add."""
        return a + b
    for args in [{"a": 1}, {"a": "3"}, {"a": "x"}, {}, {"a": 1, "zzz": 2}, {"_raw": "{bad json"}, {"a": 1.0}, {"a": 1.5}, {"a": True}]:
        r = await add.invoke(args)
        print("add", args, "->", r.model_dump())
    @tool
    def nested(p: Point) -> str:
        return f"{type(p).__name__}:{p.x},{p.y}"
    print("nested", (await nested.invoke({"p": {"x": "4"}})).model_dump())
    print("nested bad", (await nested.invoke({"p": {"y": 1}})).model_dump())
    ut = Tool(under)
    print("underscore invoke {'_x':1}", (await ut.invoke({"_x": 1})).model_dump())
    print("underscore invoke {}", (await ut.invoke({})).model_dump())
    try:
        pp = Tool(functools.partial(base, y=1), name="pp")
        print("partial with name:", pp.spec.parameters)
    except BaseException as e:
        print("partial with name RAISE", type(e).__name__, e)
    print("posonly invoke", (await pt.invoke({"x": 1})).model_dump() if pt else None)
    print("method invoke", (await mt.invoke({"x": 5})).model_dump() if mt else None)
    print("kwonly invoke", (await kt.invoke({"x": 5})).model_dump() if kt else None)

    returns = {
        "None": None, "empty str": "", "str": "hi", "int": 3, "bool": True, "float": 1.5,
        "dict": {"a": 1, "p": Path("/x"), "t": datetime(2026, 1, 1)}, "list": [1, "二"],
        "tuple": (1, 2), "set": {1}, "dict int keys": {1: "a"}, "dict tuple keys": {(1, 2): "a"},
        "ImagePart": ImagePart(path="img/a.png"), "list[ImagePart]": [ImagePart(path="a.png")],
        "BaseModel": Point(x=1), "ToolResult err": ToolResult(content="bad", is_error=True),
        "ToolResult imgs": ToolResult(content="", images=["a.png", "b.png"]), "bytes": b"x", "nan": float("nan"),
    }
    for label, value in returns.items():
        def make(v):
            def ret() -> Any:
                return v
            return ret
        t = Tool(make(value), name="ret")
        r = await t.invoke({})
        print(f"return {label}:", r.model_dump(), "-> msg text", repr(r.to_message("c1").text), "imgs", [p.path for p in r.to_message("c1").images])

    @tool
    async def aboom(x: int) -> str:
        raise ValueError("bad x")
    print("async raise", (await aboom.invoke({"x": 1})).model_dump())

    @tool
    def sync_returns_coro(x: int) -> Any:
        async def inner():
            return "from coroutine"
        return inner()
    print("sync returning coroutine", (await sync_returns_coro.invoke({"x": 1})).model_dump())

    @tool
    async def agen(x: int):
        yield 1
    print("async generator", (await agen.invoke({"x": 1})).model_dump())

    @tool
    async def cancels(x: int) -> str:
        raise asyncio.CancelledError()
    try:
        await cancels.invoke({"x": 1})
    except asyncio.CancelledError:
        print("CancelledError propagates out of invoke")
    from emerge.exceptions import StopRequested
    @tool
    def stops(x: int) -> str:
        raise StopRequested()
    try:
        await stops.invoke({"x": 1})
    except StopRequested:
        print("StopRequested propagates out of invoke")

    print("=== Toolset")
    ts = Toolset()
    a = Tool(nodoc, name="a"); b = Tool(nodoc, name="b"); a2 = Tool(nodoc, name="a")
    try:
        ts.add(a, b, a2)
    except Exception as e:
        print("dup:", type(e).__name__, e, "-> registered now:", [t.name for t in ts])
    try:
        ts.remove("zzz")
    except Exception as e:
        print("remove missing:", type(e).__name__, e)
    print("get missing:", ts.get("zzz"), "contains a:", "a" in ts, "len:", len(ts))
    print("is decorated a Tool and callable?", isinstance(add, Tool), callable(add))

asyncio.run(invoke_cases())

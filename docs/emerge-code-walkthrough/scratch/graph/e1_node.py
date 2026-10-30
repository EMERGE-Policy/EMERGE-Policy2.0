import inspect
from fx import try_, P
from emerge.graph import Node, Outcome, NodeContext, Port
from emerge.exceptions import StopRequested, WaitTimeout, InputClosed, EmergeError

def mk(**attrs):
    def f():
        cls = type("X", (Node,), attrs)
        return (cls.inputs, cls.outputs, cls.reads, cls.outcomes)
    return f

print("== __init_subclass__")
try_("outcomes as bare string 'done'", mk(name="x", outcomes="done"))
try_("outcomes list ['a','b'] -> tuple", mk(name="x", outcomes=["a", "b"]))
try_("duplicate outcomes", mk(name="x", outcomes=("a", "a")))
try_("outcome with dot", mk(name="x", outcomes=("a.b",)))
try_("outcome with space", mk(name="x", outcomes=("a b",)))
try_("outcome empty string", mk(name="x", outcomes=("",)))
try_("outcome non-string int", mk(name="x", outcomes=(1,)))
try_("outcome unhashable list", mk(name="x", outcomes=(["a"],)))
try_("outcome '*'", mk(name="x", outcomes=("*",)))
try_("outcome unicode '成功'", mk(name="x", outcomes=("成功",)))
try_("dup + invalid: ('a.b','a.b')", mk(name="x", outcomes=("a.b", "a.b")))
try_("duplicate reads", mk(name="x", outcomes=("a",), reads=(("f", "1"), ("f", "2"))))
try_("reads as list of lists", mk(name="x", outcomes=("a",), reads=[["f", "1"]]))
try_("reads bad shape ('abc',)", mk(name="x", outcomes=("a",), reads=("abc",)))
try_("duplicate input port names", mk(name="x", outcomes=("a",), inputs=[P("i"), P("i")]))
try_("same name in and out", mk(name="x", outcomes=("a",), inputs=[P("i")], outputs=[P("i")]))
try_("inputs not Port objects", mk(name="x", outcomes=("a",), inputs=("i",)))
try_("empty name & outcomes (base class)", mk())
try_("name with dot 'a.b'", mk(name="a.b", outcomes=("x",)))

class Base(Node):
    outcomes = ("success", "failure")
class Child(Base):
    name = "child"
print("inherited outcomes:", Child.outcomes)

print("== Outcome")
try_("Outcome('success','cup placed')", lambda: Outcome("success", "cup placed"))
try_("Outcome(name='x')", lambda: Outcome(name="x"))
try_("Outcome('x', reason='r')", lambda: Outcome("x", reason="r"))
try_("Outcome('x', name='y')", lambda: Outcome("x", name="y"))
try_("Outcome()", lambda: Outcome())
try_("Outcome('a.b')", lambda: Outcome("a.b"))
try_("Outcome('*')", lambda: Outcome("*"))
try_("Outcome('x', None)", lambda: Outcome("x", None))
try_("Outcome extra field", lambda: Outcome("x", foo=1))
try_("Outcome.model_validate({'name':'x','reason':'r'})", lambda: Outcome.model_validate({"name": "x", "reason": "r"}))
def mut():
    o = Outcome("x"); o.name = "y"
try_("mutate Outcome", mut)

print("== Node instance")
class N(Node):
    name = "n"; outcomes = ("ok",)
n = N()
print("params default", n.params, "repr", repr(n))
import asyncio
try_("run() default", lambda: asyncio.run(N().run(None)))
print("setup/teardown are coroutines:", inspect.iscoroutinefunction(Node.setup), inspect.iscoroutinefunction(Node.teardown))

print("== NodeContext sync/async")
for name, member in NodeContext.__dict__.items():
    if callable(member) and not name.startswith("_"):
        kind = "async" if inspect.iscoroutinefunction(member) else ("asyncgen" if inspect.isasyncgenfunction(member) else "sync")
        print(f"  {name}: {kind}")
print("protocol attrs:", [k for k in NodeContext.__annotations__])

print("== exceptions")
print("StopRequested is Exception?", issubclass(StopRequested, Exception), "BaseException?", issubclass(StopRequested, BaseException))
print("WaitTimeout EmergeError?", issubclass(WaitTimeout, EmergeError), "InputClosed EmergeError?", issubclass(InputClosed, EmergeError))
def swallow():
    try:
        try:
            raise StopRequested("stop")
        except Exception:
            return "swallowed by except Exception"
    except StopRequested:
        return "passed through except Exception"
print(swallow())

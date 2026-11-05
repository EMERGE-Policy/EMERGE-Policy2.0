import asyncio
import json
import logging
import tempfile
import threading
from pathlib import Path

from emerge.callbacks import CallbackHandler, CallbackManager, JsonlTracer
from emerge.events import Event, EventType

logging.basicConfig(level=logging.WARNING, format="LOG %(levelname)s %(name)s: %(message)s")


class Rec(CallbackHandler):
    def __init__(self, name, delay=0.0):
        self.name, self.delay, self.got, self.closed = name, delay, [], 0
    async def on_event(self, event):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.got.append(event)
    async def close(self):
        self.closed += 1


class Boom(CallbackHandler):
    async def on_event(self, event):
        raise ValueError("boom")
    async def close(self):
        raise RuntimeError("close boom")


class Hooks(CallbackHandler):
    event_types = frozenset({EventType.NODE_STATE, "custom.x"})
    def __init__(self):
        self.hooked = []
    async def on_node_state(self, event):
        self.hooked.append(("on_node_state", event.type))


async def main():
    m = CallbackManager()
    try:
        m.emit_nowait(Event(type="x"))
    except RuntimeError as e:
        print("emit before add:", e)
    slow, boom, hooks = Rec("slow", 0.01), Boom(), Hooks()
    m.add_handler(slow); m.add_handler(slow); m.add_handler(boom); m.add_handler(hooks)
    print("workers after double add:", len(m._workers))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    for i in range(20):
        m.emit_nowait(Event(type=EventType.NODE_STATE, payload={"i": i}))
    m.emit_nowait(Event(type="custom.x"))
    m.emit_nowait(Event(type="model.token"))
    print("20 emits took %.4fs (non-blocking)" % (loop.time() - t0))
    # other thread emit
    def other():
        for i in range(3):
            m.emit_nowait(Event(type="thread", payload={"i": i}))
    th = threading.Thread(target=other); th.start(); th.join()
    print("pending after thread emits:", len(m._pending))
    m.emit_nowait(Event(type="after-thread"))
    print("pending after loop emit:", len(m._pending))
    # same object shared?
    shared = Event(type="shared")
    r2 = Rec("r2"); m.add_handler(r2)
    m.emit_nowait(shared)
    # view
    v = m.with_event_defaults(run_id="r1", node_id="n1")
    v2 = v.with_event_defaults(node_id="n2", acts_on_environment=True)
    v3 = v2.with_event_defaults(node_id=None)
    v.emit_nowait(Event(type="view1"))
    v2.emit_nowait(Event(type="view2", run_id="explicit"))
    v3.emit_nowait(Event(type="view3"))
    e_orig = Event(type="view-orig")
    v.emit_nowait(e_orig)
    await m.aclose()
    print("slow got", len(slow.got), "ordered", [e.payload.get("i") for e in slow.got if e.type == "node.state"] == list(range(20)), "closed", slow.closed)
    print("slow types tail:", [e.type for e in slow.got][-9:])
    print("hooks:", hooks.hooked[:2], len(hooks.hooked))
    print("r2 shared same object:", any(e is shared for e in r2.got), "slow same object:", any(e is shared for e in slow.got))
    for e in r2.got:
        if e.type.startswith("view"):
            print("  ", e.type, e.run_id, e.node_id, e.acts_on_environment)
    print("orig untouched:", e_orig.run_id, e_orig.node_id)
    m.emit_nowait(Event(type="after-close"))
    await m.aclose()
    print("aclose twice ok")
    # add after close
    late = Rec("late")
    m.add_handler(late)
    m.emit_nowait(Event(type="late-event"))
    await asyncio.sleep(0.01)
    print("late handler got", len(late.got), "task done?", m._workers[id(late)].task.done())
    m.remove_handler(late)
    await asyncio.sleep(0.01)
    print("late closed after remove:", late.closed)

    # remove_handler with queued events
    m2 = CallbackManager()
    r = Rec("r", 0.005); m2.add_handler(r)
    for i in range(5):
        m2.emit_nowait(Event(type="q", payload={"i": i}))
    m2.remove_handler(r)
    m2.emit_nowait(Event(type="after-remove"))
    await asyncio.sleep(0.1)
    print("removed handler processed", len(r.got), "closed", r.closed, [e.type for e in r.got])
    m2.remove_handler(r)  # not present
    await m2.aclose()

    # handler raising CancelledError
    class Cancel(CallbackHandler):
        async def on_event(self, event):
            raise asyncio.CancelledError()
    m3 = CallbackManager()
    c = Cancel(); m3.add_handler(c)
    m3.emit_nowait(Event(type="c"))
    await asyncio.sleep(0.01)
    try:
        await asyncio.wait_for(m3.aclose(), 1)
        print("aclose with cancelled worker: ok")
    except BaseException as e:
        print("aclose with cancelled worker raised:", type(e).__name__)

    # tracer
    with tempfile.TemporaryDirectory() as d:
        tr = JsonlTracer(Path(d))
        m4 = CallbackManager(); m4.add_handler(tr)
        m4.emit_nowait(Event(type=EventType.RUN_STARTED, run_id="r1"))
        m4.emit_nowait(Event(type="x", run_id=""))
        m4.emit_nowait(Event(type="x"))
        m4.emit_nowait(Event(type=EventType.RUN_FINISHED, run_id="r1"))
        m4.emit_nowait(Event(type="late", run_id="r1"))
        m4.emit_nowait(Event(type="bad", run_id="r1", payload={"p": Path("/x")}))
        m4.emit_nowait(Event(type="ok", run_id="r1", payload={"text": "中文"}))
        await asyncio.sleep(0.2)
        print("tracer open files:", sorted(tr._files))
        await m4.aclose()
        print("tracer open files after close:", sorted(tr._files))
        for p in sorted(Path(d).rglob("*.jsonl")):
            lines = p.read_text(encoding="utf-8").splitlines()
            print(" ", p.relative_to(d), [json.loads(l)["type"] for l in lines])
            ev = Event.from_dict(json.loads(lines[0]))
            print("   from_dict type class:", type(ev.type).__name__)
        print("   raw line:", lines[-1][:120])

asyncio.run(main())

# add_handler outside loop
try:
    CallbackManager().add_handler(Rec("x"))
except RuntimeError as e:
    print("add outside loop:", e)

# add from second loop
m = CallbackManager()
async def add(h):
    m.add_handler(h)
asyncio.run(add(Rec("a")))
try:
    asyncio.run(add(Rec("b")))
except RuntimeError as e:
    print("add from other loop:", e)
# emit after loop closed, from main thread (no running loop)
try:
    m.emit_nowait(Event(type="z"))
except RuntimeError as e:
    print("emit after loop closed:", e)

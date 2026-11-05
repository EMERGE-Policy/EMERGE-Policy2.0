"""History details, serialization, ContextBuilder/RequestBuilder edge cases, guard case."""
import asyncio
from pathlib import Path

from fake import FakeProvider, client_for, dump_history, reply
from emerge.agent import (AgentLoop, CompactionMiddleware, ContextBuilder, History, MiddlewareChain,
                          RequestBuilder, RunContext, Toolset)
from emerge.agent.messages import Turn, strip_thinking
from emerge.models.types import Message, NativeItem, TextPart, ImagePart


class Listener:
    def __init__(self): self.calls = []
    def turn_started(self, turn, message): self.calls.append(("turn_started", turn))
    def messages_appended(self, turn, messages): self.calls.append(("messages_appended", turn, len(messages)))
    def turns_dropped(self, start, stop): self.calls.append(("turns_dropped", start, stop))
    def compacted(self, summary, kept): self.calls.append(("compacted", len(kept)))


h = History()
lst = Listener(); h.set_listener(lst)
h.append_messages_to_current_turn(Message.user("no turn yet"))
h.append_messages_to_current_turn()
h.truncate_to_turn_count(5)
h.truncate_to_turn_count(1)
print("append on empty history:", lst.calls, "turns:", len(h))
for bad in (0, 2, -1):
    try:
        h.replace_oldest_turns_with_summary(bad, "s")
    except ValueError as e:
        print(f"replace({bad}):", e)
h.replace_oldest_turns_with_summary(1, "  all of it  ")
print("replace(count == len) allowed:", dump_history(h), repr(h.turns[0].messages[0].text))

# serialization
h2 = History()
h2.start_turn(Message.user("hi", images=["a.png"]))
oa = NativeItem(provider="openai", api_mode="responses", data={"type": "reasoning", "encrypted_content": "xx"})
th = NativeItem(provider="anthropic", api_mode="anthropic_messages", data={"type": "redacted_thinking", "data": "zz"})
h2.append_messages_to_current_turn(Message(role="assistant", content=[TextPart(text="a")], native_items=[oa, th]))
h2.set_listener(Listener())
print("native_items():", [n.provider for n in h2.native_items()])
m = h2.turns[0].messages[1]
s = strip_thinking(m)
print("strip_thinking keeps openai item:", [n.provider for n in s.native_items], "| same object when nothing to strip:", strip_thinking(s) is s,
      "| user message untouched:", strip_thinking(h2.turns[0].messages[0]) is h2.turns[0].messages[0])
try:
    Turn.model_validate({"messages": [], "extra": 1})
except Exception as e:
    print("extra field rejected:", type(e).__name__)

# ContextBuilder
cb = ContextBuilder(["  A  ", lambda: "", lambda: " B "])
print("system:", repr(cb.get_system_prompt()))
cb2 = ContextBuilder(["X"], system="")
print("system='' given → stays:", repr(cb2.get_system_prompt()), "| request roles:", [m.role for m in RequestBuilder(cb2, History(), []).build().messages])
cb3 = ContextBuilder(["FRESH"], system="FROM RECORD")
print("system given:", cb3.get_system_prompt(), "→ after rebuild:", cb3.rebuild_system_prompt())
calls = {"n": 0}
def frag():
    calls["n"] += 1
    return f"built {calls['n']}"
cb4 = ContextBuilder([frag])
cb4.get_system_prompt(); cb4.get_system_prompt()
print("callable fragments evaluated once until rebuild:", calls["n"])
def boom():
    raise OSError("AGENTS.md unreadable")
try:
    ContextBuilder([boom]).get_system_prompt()
except OSError as e:
    print("fragment raising → propagates:", e)

infos = iter(["A", "A", "", "A", "B"])
cb5 = ContextBuilder([], runtime_info=lambda: next(infos))
h5 = History(); h5.start_turn(Message.user("u"))
print("append_runtime_info sequence A,A,'',A,B:", [cb5.append_runtime_info(h5) for _ in range(5)], dump_history(h5))
cb6 = ContextBuilder([], runtime_info=lambda: "B")
print("fresh builder re-appends same text:", cb6.append_runtime_info(h5))
print("no runtime_info callback:", ContextBuilder([]).append_runtime_info(h5))

# RequestBuilder.rebuild keeps per-request overrides
rb = RequestBuilder(ContextBuilder(["S"]), h5, [])
req = rb.build().model_copy(update={"tool_choice": "none", "extra": {"k": 1}, "messages": []})
print("rebuild keeps overrides:", rb.rebuild(req).tool_choice, rb.rebuild(req).extra, len(rb.rebuild(req).messages))


async def guard():
    provider = FakeProvider([reply("ok"), reply("ok2")])
    history = History()
    history.start_turn(Message.user("old")); history.compact(1, "old summary")
    ctxb = ContextBuilder(["S"]); tools = Toolset([])
    builder = RequestBuilder(ctxb, history, [])
    client = client_for(provider)
    comp = CompactionMiddleware(client, ctxb, history, builder, max_context_tokens=1000)
    loop = AgentLoop(client, tools, builder, history, MiddlewareChain([comp]))
    await loop.run_turn("newest " + " ".join(["word"] * 3000), RunContext.create(Path(".")))
    print("guard [summary, huge newest]: summary calls:", len(provider.summary_requests), "turns:", [t.summary for t in history.turns])

asyncio.run(guard())

"""Scratch: exact results of the file tools at their edges (no runtime, temp workspace)."""
import asyncio, os, shutil
from pathlib import Path
from types import SimpleNamespace

from emerge.agent.tools import files
from emerge.models.types import ToolCall

ROOT = Path("/data/yuqingchi/tmp/emerge-code-walkthrough/scratch/agent/ws_files")
OUT = ROOT.parent / "outside.txt"


async def main() -> None:
    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True)
    (ROOT / "config.json").write_text("{}")
    (ROOT / ".emerge").mkdir()
    (ROOT / ".emerge" / "state.db").write_text("x")
    (ROOT / "sessions" / "s1" / "memory").mkdir(parents=True)
    (ROOT / "sessions" / "s2" / "memory").mkdir(parents=True)
    (ROOT / "sessions" / "s2" / "memory" / "MEMORY.md").write_text("other")
    (ROOT / "runs" / "r1").mkdir(parents=True)
    (ROOT / "runs" / "r1" / "graph.json").write_text('{"a": 1}\n')
    (ROOT / "a.txt").write_text("foo\nbar\nfoo\n")
    (ROOT / "crlf.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    (ROOT / "bin.dat").write_bytes(b"\xff\xfe\x00\x81")
    (ROOT / "empty.txt").write_text("")
    (ROOT / "long.txt").write_text("x" * 2105 + "\n" + "\n".join(f"l{i}" for i in range(2, 2504)) + "\n")
    (ROOT / "pic.PNG").write_bytes(b"\x89PNG\r\n")
    OUT.write_text("secret")
    os.symlink(OUT, ROOT / "link_out")
    os.symlink(ROOT / "config.json", ROOT / "link_cfg")
    os.symlink(ROOT / "runs" / "r1" / "graph.json", ROOT / "link_graph")
    (ROOT / "notes").mkdir()

    ctx = SimpleNamespace(workspace=ROOT, session=SimpleNamespace(id="s1"))
    tools = {t.name: t for t in files.make_tools(ctx)}

    async def run(name, **args):
        r = await tools[name].invoke(args)
        text = r.content if len(r.content) < 260 else r.content[:200] + " ... " + r.content[-60:]
        print(f"{name}{args if len(str(args)) < 120 else '(...)'}\n   -> is_error={r.is_error} images={r.images}\n   {text!r}")

    print("== read_file")
    await run("read_file", path="a.txt")
    await run("read_file", path="a.txt", offset=0, limit=0)
    await run("read_file", path="a.txt", offset=-3, limit=-1)
    await run("read_file", path="a.txt", offset=2, limit=1)
    await run("read_file", path="a.txt", offset=4)
    await run("read_file", path="empty.txt")
    await run("read_file", path="bin.dat")
    await run("read_file", path="pic.PNG")
    await run("read_file", path="notes")
    await run("read_file", path="nope.txt")
    await run("read_file", path="")
    r = await tools["read_file"].invoke({"path": "long.txt"})
    print("long.txt header:", r.content.splitlines()[0], "| first line len", len(r.content.splitlines()[1]), "| tail", r.content.splitlines()[1][-20:])
    print("== refusals")
    for p in ["../outside.txt", str(OUT), "link_out", "config.json", "./config.json", "link_cfg", ".emerge/state.db", ".emerge",
              "sessions/s2/memory/MEMORY.md", "sessions/s1/memory/x.md", "sessions", "~/x", "runs/../config.json"]:
        await run("read_file", path=p)
    print("== edit_file")
    await run("edit_file", path="a.txt", old_text="foo", new_text="baz")
    await run("edit_file", path="a.txt", old_text="foo", new_text="baz", replace_all=True)
    await run("edit_file", path="a.txt", old_text="bar", new_text="BAR", replace_all=True)
    await run("edit_file", path="a.txt", old_text="zzz", new_text="y")
    await run("edit_file", path="a.txt", old_text="", new_text="y")
    await run("edit_file", path="a.txt", old_text="BAR", new_text="BAR")
    await run("edit_file", path="nope.txt", old_text="a", new_text="b")
    await run("edit_file", path="notes", old_text="a", new_text="b")
    await run("edit_file", path="bin.dat", old_text="a", new_text="b")
    await run("edit_file", path="crlf.txt", old_text="two", new_text="TWO")
    print("crlf bytes after edit:", (ROOT / "crlf.txt").read_bytes())
    await run("edit_file", path="runs/r1/graph.json", old_text="1", new_text="2")
    await run("edit_file", path="link_graph", old_text="2", new_text="3")
    await run("edit_file", path="link_cfg", old_text="{}", new_text="[]")
    print("== write_file")
    await run("write_file", path="notes", content="x")
    await run("write_file", path="deep/new/f.txt", content="a\nb")
    await run("write_file", path="deep/new/f.txt", content="")
    await run("write_file", path="code/foo/v1.py", content="x=1\n")
    await run("write_file", path="other/MEMORY.md", content="- a\n")
    await run("write_file", path="memory/MEMORY.md", content="- a\n")
    await run("write_file", path="runs/r1/graph.json", content="{}")
    await run("write_file", path="runs/r1/nodes/graph.json", content="{}")
    print("== guarded detection (writes_graph_file)")
    for name, args in [("edit_file", {"path": "runs/r1/graph.json"}), ("write_file", {"path": "./runs/r1/graph.json"}),
                       ("edit_file", {"path": "link_graph"}), ("read_file", {"path": "runs/r1/graph.json"}),
                       ("write_file", {"path": "runs/r1/nodes/graph.json"}), ("edit_file", {"path": 5}), ("edit_file", {})]:
        print(name, args, files.writes_graph_file(ROOT, ToolCall(id="c", name=name, arguments=args)))
    print("== pydantic: missing arg / wrong type / extra / raw")
    await run("read_file")
    await run("read_file", path="a.txt", offset="x")
    await run("read_file", path="a.txt", foo=1)
    r = await tools["read_file"].invoke({"_raw": "{bad json"})
    print("raw ->", r.content)


asyncio.run(main())

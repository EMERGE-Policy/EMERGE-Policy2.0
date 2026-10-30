"""Scratch: exact tool results of graph/execution/catalog/memory tools on a real runtime (scripted model)."""
import asyncio, json, sys
from pathlib import Path

P8 = Path("/data/yuqingchi/tmp/emerge-phase-8")
sys.path.insert(0, str(P8))
import harness8  # noqa: E402
harness8.WORK = Path("/data/yuqingchi/tmp/emerge-code-walkthrough/scratch/agent/work")
from harness8 import Env8  # noqa: E402
from fake_model import Script  # noqa: E402


def short(text, n=420):
    return text if len(text) <= n else text[:n] + f" ...[{len(text)} chars]"


async def main():
    async with Env8("e3", script=Script()) as env:
        await env.new_session()
        agent = env.agent

        async def t(tool_name, **args):
            r = await agent.tools.get(tool_name).invoke(args)
            print(f"--- {tool_name}({', '.join(f'{k}={v!r}' for k, v in args.items()) if len(str(args)) < 150 else '...'}) is_error={r.is_error}\n{short(r.content)}")
            return r

        print("TOOLS:", len(agent.tools), [x.name for x in agent.tools])
        print("== no run yet")
        await t("run_status"); await t("commit_graph"); await t("show_graph"); await t("pause"); await t("reset_graph")
        print("== start_run")
        await t("start_run"); await t("start_run")
        gf = env.graph_file()
        print("graph file:", gf.read_text())
        print("runtime info now:\n" + agent._run_info())

        def write(nodes, version=None):
            v = env.runtime.graph(env.run_id).version if version is None else version
            gf.write_text(json.dumps({"format": "emerge.graph-spec/v1", "graph_version": v, "nodes": nodes}, indent=2))

        print("== description check")
        write({"h": {"type": "holder"}})
        await t("commit_graph", dry_run=True)
        await t("commit_graph")
        await t("commit_graph")
        await t("commit_graph", dry_run=True)
        write({"h": {"type": "holder", "description": "holds"}})
        await t("commit_graph", dry_run=True)
        await t("commit_graph")
        await env.wait_state("h", "running", 60)
        print("== no change / replace unknown / replace known without description change")
        await t("commit_graph")
        await t("commit_graph", replace=["zz"])
        await t("commit_graph", dry_run=True, replace=["h"])
        print("== stale")
        write({"h": {"type": "holder", "description": "holds"}, "x": {"type": "step", "description": "x"}}, version=0)
        print("runtime info with stale draft:\n" + agent._run_info())
        await t("commit_graph")
        await t("commit_graph")
        await t("reset_graph")
        print("== remove a running node")
        write({})
        await t("commit_graph", dry_run=True)
        await t("commit_graph")
        print("file kept as written:", json.loads(gf.read_text())["nodes"])
        await t("reset_graph")
        print("== bad JSON")
        gf.write_text('{"format": "emerge.graph-spec/v1", "nodes": {')
        await t("commit_graph")
        print("runtime info with broken file:\n" + agent._run_info())
        await t("reset_graph")
        print("== missing file")
        gf.unlink()
        await t("commit_graph")
        await t("commit_graph")
        await t("run_status")
        print("runtime info with missing file:\n" + agent._run_info())
        await t("reset_graph")
        print("== looking")
        await t("show_graph")
        await t("node_detail", node_id="zz")
        await t("describe_node_type", name="nope")
        await t("describe_node_type", name="holder")
        print("== send / stop / pause / resume")
        write({"h": {"type": "holder", "description": "holds"}, "w": {"type": "step", "description": "waits", "start_when": {"any_of": [{"all_of": {"h": "done"}}]}}})
        await t("commit_graph")
        await t("send_to_node", node_id="h", text="hello", data={"k": 1})
        await t("send_to_node", node_id="w", text="later")
        await t("send_to_node", node_id="zz", text="x")
        await t("stop_node", node_id="w")
        await t("pause"); await t("resume"); await t("pause", node_id="h"); await t("resume", node_id="h"); await t("resume", node_id="h")
        await t("run_status")
        print("== end")
        await t("end_run")
        await t("end_run")
        await t("show_graph")
        await t("commit_graph")
        await t("reset_graph")
        await t("send_to_node", node_id="h", text="x")
        print("runtime info after end:\n" + agent._run_info())
        await t("start_run")
        print("== register_node_code")
        await t("register_node_code", name="1bad", source="x")
        await t("register_node_code", name="good", source="syntax error here(")
        src = (P8 / "p8_nodes.py").read_text()
        await t("register_node_code", name="holder", source=src)
        node_src = '''
from emerge.graph import Node, Outcome
class Gen(Node):
    name = "gen_one"
    description = "generated"
    outcomes = ("done",)
    async def run(self, ctx):
        return Outcome("done", "ok")
'''
        await t("register_node_code", name="gen_two", source=node_src)
        await t("register_node_code", name="gen_one", source=node_src)
        await t("register_node_code", name="gen_one", source=node_src)
        print("code dir:", sorted(p.name for p in (env.workspace / "code").rglob("*.py")))
        print("== memory")
        await t("write_memory", text="   ")
        (env.workspace / "imgs").mkdir()
        (env.workspace / "imgs" / "a.png").write_bytes(b"\x89PNG1")
        (env.workspace / "imgs" / "b.jpg").write_bytes(b"\xff\xd8JPG")
        (env.workspace / "imgs" / "c.txt").write_text("t")
        await t("write_memory", text="x", images=[{"path": "imgs", "description": "d"}])
        await t("write_memory", text="x", images=[{"path": "imgs/c.txt", "description": "d"}])
        await t("write_memory", text="x", images=[{"path": "imgs/a.png", "description": "d", "extra": 1}])
        await t("write_memory", text="x", scope="team")
        await t("write_memory", text="cup is blue\nsecond line\n\nthird", images=[{"path": "imgs/a.png", "description": "the [cup]"}])
        await t("write_memory", text="global fact about trays", scope="global")
        await t("search_memory", query="cup")
        await t("search_memory", query="trays", limit=0)
        await t("search_memory", query='"')
        await t("search_memory", query="nothing-here")
        print("session MEMORY.md:\n" + (env.workspace / "sessions" / env.session.id / "memory" / "MEMORY.md").read_text())


if __name__ == "__main__":
    asyncio.run(main())

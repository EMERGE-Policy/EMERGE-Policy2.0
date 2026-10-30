"""op x state matrix for apply_graph_patch."""
import logging
from fx import REG
from emerge.graph import GraphBuilder, GraphPatch, apply_graph_patch, diff_graphs, StartEdge, StartSource
logging.disable(logging.WARNING)

STATES = ["pending", "starting", "running", "paused", "stopping", "finished", "failed", "stopped", "skipped"]

def base():
    b = GraphBuilder(REG)
    b.add_node("a", "src").add_node("t", "mid", after=["a"]).add_node("c", "sink", after=["t"])
    b.connect_ports("a.out", "t.inp").connect_ports("t.out", "c.inp")
    return b.build()

g0 = base()
OPS = {
    "remove_node(t)": lambda: GraphPatch.remove_node("t").combine_patches(GraphPatch.replace_start_edges("c", after=["a"])),
    "replace_node(t)": lambda: GraphPatch.replace_node("t", description="new"),
    "update_params(t)": lambda: GraphPatch.replace_node_params("t", {"k": 5}),
    "set_description(t)": lambda: GraphPatch.set_node_description("t", "hello"),
    "connect(a.aux->t.inp) rewire": lambda: GraphPatch.disconnect("a.out", "t.inp").combine_patches(GraphPatch.connect("a.aux", "t.inp")),
    "connect(t.out->x.inp) t as source": lambda: GraphPatch.add_node("x", "sink").combine_patches(GraphPatch.connect("t.out", "x.inp")),
    "disconnect(a.out->t.inp)": lambda: GraphPatch.disconnect("a.out", "t.inp"),
    "disconnect(t.out->c.inp) t as source": lambda: GraphPatch.disconnect("t.out", "c.inp"),
    "set_start_edges(t)": lambda: GraphPatch.replace_start_edges("t", (StartEdge(sources=(StartSource(node="a", outcome="success"),)),)),
    "connect same existing (a.out->t.inp)": lambda: GraphPatch.connect("a.out", "t.inp"),
}
for op, mk in OPS.items():
    print(f"## {op}")
    for st in STATES:
        try:
            g1 = apply_graph_patch(g0, mk(), REG, states={"t": st, "a": "finished"} if "remove" not in op else {"t": st})
            d = diff_graphs(g0, g1)
            parts = []
            if d.nodes.added: parts.append(f"added={list(d.nodes.added)}")
            if d.nodes.removed: parts.append(f"removed={list(d.nodes.removed)}")
            if d.nodes.modified: parts.append("modified=" + ",".join(f"{m.node}{list(m.fields)}" for m in d.nodes.modified))
            if d.nodes.replaced: parts.append("replaced=" + ",".join(f"{r.old_instance_id}->{r.new_instance_id}" for r in d.nodes.replaced))
            if d.subscriptions.added: parts.append(f"subs+={len(d.subscriptions.added)}")
            if d.subscriptions.removed: parts.append(f"subs-={len(d.subscriptions.removed)}")
            if d.subscriptions.rewired: parts.append(f"rewired={len(d.subscriptions.rewired)}")
            print(f"  {st:9s} OK  v{g1.version} {' '.join(parts) or '(diff empty)'}")
        except Exception as e:
            print(f"  {st:9s} ERR {type(e).__name__}: {e}")

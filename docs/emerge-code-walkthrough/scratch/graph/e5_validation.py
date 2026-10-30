from fx import REG, try_
from emerge.graph import (Graph, NodeSpec, StartEdge, StartSource, Subscription, validate, GraphBuilder, Replacement)
def S(n, o=None): return StartSource(node=n, outcome=o)
def E(*s): return StartEdge(sources=tuple(s))
def N(i, t, *edges, **kw): return NodeSpec(id=i, type=t, start_edges=tuple(edges), **kw)
def sub(a, b):
    an, ap = a.rsplit(".", 1); bn, bp = b.rsplit(".", 1)
    return Subscription(source_node=an, source_port=ap, target_node=bn, target_port=bp)
def show(label, g):
    r = validate(g, REG)
    print(f"-- {label}: ok={r.ok}")
    for i in r.issues:
        extra = f" path={i.path}" if i.path else ""
        print(f"   {i.severity.value:7s} {i.code.value:24s} node={i.node} port={i.port} sub={'yes' if i.subscription else '-'}{extra} | {i.message}")

show("unknown type", Graph(nodes=(N("a", "nope"),)))
show("unknown type with code", Graph(nodes=(N("a", "nope", code="3"),)))
show("module type with code -> UNKNOWN_CODE_VERSION", Graph(nodes=(N("a", "src", code="3"),)))
show("params on type without Params", Graph(nodes=(N("a", "src", params={"x": 1}),)))
show("params invalid", Graph(nodes=(N("a", "mid", params={"k": "abc", "zz": 1}),)))
show("Params missing required field", Graph(nodes=(N("a", "task_report", params={}),)))
show("endpoints missing", Graph(nodes=(N("a", "src"), N("b", "mid")),
     subscriptions=(sub("ghost.out", "b.inp"), sub("a.nope", "b.inp"), sub("a.out", "b.nope"), sub("a.out", "ghost2.inp"))))
show("endpoint on unknown-type node", Graph(nodes=(N("a", "nope"), N("b", "mid")), subscriptions=(sub("a.out", "b.inp"),)))
show("two sources one input + tag mismatch", Graph(nodes=(N("a", "src"), N("m", "mid2"), N("b", "mid")),
     subscriptions=(sub("a.out", "b.inp"), sub("m.out", "b.inp"))))
show("same subscription twice", Graph(nodes=(N("a", "src"), N("b", "mid")), subscriptions=(sub("a.out", "b.inp"), sub("a.out", "b.inp"))))
show("start edges: empty, dup source, unknown node, unknown outcome", Graph(nodes=(
     N("a", "src"),
     N("b", "mid", E(), E(S("a"), S("a", "success")), E(S("ghost")), E(S("a", "done"))),
     N("u", "nope"), N("c", "mid", E(S("u", "whatever"))))))
show("start cycle self + loop", Graph(nodes=(N("x", "src", E(S("x"))), N("p", "src", E(S("q"))), N("q", "src", E(S("p"))))))
show("duplicate id", Graph(nodes=(N("a", "nope"), N("a", "src", params={"x": 1}))))
show("duplicate id reversed", Graph(nodes=(N("a", "src", params={"x": 1}), N("a", "nope"))))
show("duplicate instance", Graph(nodes=(N("a", "src"), N("b", "src", instance_id="a@v0"))))
show("instance equal to a past replaced one", Graph(version=1, nodes=(N("a", "src", instance_id="a@v0"),),
     replacements=(Replacement(node="a", version=1, old_instance_id="a@v0", new_instance_id="a@v1", old_type="src", new_type="src"),)))
r = validate(Graph(nodes=(N("a", "nope"),)), REG)
try_("raise_if_invalid", r.raise_if_invalid)
print("=== builder")
b = GraphBuilder(REG)
b.add_node("a", "src").add_node("b", "mid", params={"k": 2}, after=["a"]).add_node("s", "sink", after=["b", "a", "b"])
b.connect_ports("a.out", "b.inp").connect_ports("a.out", "b.inp").connect_ports("b.out", "s.inp")
b.declare_external_read("b", "lidar").declare_external_read("ghost", "x")
g = b.build()
print(g)
print("reads by type:", {k: v for k, v in b.external_reads_by_node_type().items() if v})
try_("builder connect bad endpoint", lambda: GraphBuilder(REG).connect_ports("ab", "c.d"))
try_("builder build with error", lambda: GraphBuilder(REG).add_node("a", "src").add_node("a", "src").build())
import logging; logging.basicConfig(level=logging.WARNING, format="LOG %(levelname)s %(name)s: %(message)s")
try_("builder build with warning", lambda: str(GraphBuilder(REG).add_node("a", "src").add_node("m", "mid2").add_node("b", "mid").connect_ports("m.out", "b.inp").build().version))
print("builder after+start_edges:", [str(e) for e in GraphBuilder(REG).add_node("x", "src", start_edges=[E(S("a", "success"))], after=["b"]).build_unvalidated_graph().node("x").start_edges])

import json, logging, textwrap
from pathlib import Path
from fx import REG, EXAMPLE, try_
from emerge.graph import (loads_graph_spec, graph_from_graph_spec, graph_spec_from_graph, dumps_graph_spec, patch_from_graph_spec,
                          apply_graph_patch, NodeRegistry, set_active_registry, GraphBuilder, Graph, NodeSpec, Subscription,
                          StartEdge, StartSource, GraphSpec)
logging.disable(logging.WARNING)

def L(label, text):
    try:
        s = loads_graph_spec(text)
        print(f"[OK ] {label}: nodes={list(s.nodes)}")
    except Exception as e:
        print(f"[ERR] {label}: {type(e).__name__}: {e}")

H = '{"format": "emerge.graph-spec/v1", "nodes": %s}'
L("JSON syntax error", '{"format": "emerge.graph-spec/v1", "nodes": {"a": {"type": "x",}}}')
L("top-level array", "[]")
L("duplicate node key", H % '{"a": {"type": "x"}, "a": {"type": "y"}}')
L("two duplicate keys in different objects", H % '{"a": {"type": "x", "type": "y"}, "b": {"type": "x", "params": {"k": 1, "k": 2}}}')
L("wrong format string", '{"format": "emerge.graph-spec/v2", "nodes": {}}')
L("missing format", '{"nodes": {}}')
L("graph_version -1", '{"format": "emerge.graph-spec/v1", "graph_version": -1, "nodes": {}}')
L("unknown top-level field", '{"format": "emerge.graph-spec/v1", "nodez": {}}')
L("typo field in node + missing type in other node", H % '{"a": {"type": "x", "descripton": "d"}, "b": {"params": {}}}')
L("id with dot", H % '{"a.b": {"type": "x"}}')
L("two bad ids", H % '{"a.b": {"type": "x"}, "c d": {"type": "x"}}')
L("empty type", H % '{"a": {"type": ""}}')
L("code empty string", H % '{"a": {"type": "x", "code": ""}}')
L("code null", H % '{"a": {"type": "x", "code": null}}')
L("bad endpoint (no dot)", H % '{"a": {"type": "x", "inputs": {"in": "b_out"}}, "b": {"type": "x"}}')
L("two bad endpoints in one node", H % '{"a": {"type": "x", "inputs": {"in": "b_out", "in2": "c out"}}, "b": {"type": "x"}}')
L("endpoint 'b.x.y'", H % '{"a": {"type": "x", "inputs": {"in": "b.x.y"}}, "b": {"type": "x"}}')
L("input port name with dot", H % '{"a": {"type": "x", "inputs": {"i.n": "b.out"}}, "b": {"type": "x"}}')
L("empty any_of", H % '{"a": {"type": "x", "start_when": {"any_of": []}}}')
L("empty all_of", H % '{"a": {"type": "x", "start_when": {"any_of": [{"all_of": {}}]}}}')
L("start_when null", H % '{"a": {"type": "x", "start_when": null}}')
L("outcome with space", H % '{"a": {"type": "x"}, "b": {"type": "x", "start_when": {"any_of": [{"all_of": {"a": "not ok"}}]}}}')
L("outcome empty", H % '{"a": {"type": "x"}, "b": {"type": "x", "start_when": {"any_of": [{"all_of": {"a": ""}}]}}}')
L("params not object", H % '{"a": {"type": "x", "params": [1]}}')
L("references missing nodes (pass 3)", H % '{"a": {"type": "x", "inputs": {"in": "ghost.out"}, "start_when": {"any_of": [{"all_of": {"ghost2": "*"}}]}}}')
L("field error + missing reference (pass 3 not run)", H % '{"a": {"type": "", "inputs": {"in": "ghost.out"}}}')
L("self input + self wait (format ok)", H % '{"a": {"type": "x", "inputs": {"in": "a.out"}, "start_when": {"any_of": [{"all_of": {"a": "*"}}]}}}')
L("graph_version as string '3'", '{"format": "emerge.graph-spec/v1", "graph_version": "3", "nodes": {}}')
L("empty nodes", '{"format": "emerge.graph-spec/v1", "nodes": {}}')
L("no nodes key", '{"format": "emerge.graph-spec/v1"}')

print("== graph -> file failures")
def W(label, g):
    try:
        print(f"[OK ] {label}:", dumps_graph_spec(graph_spec_from_graph(g)).replace("\n", " ")[:200])
    except Exception as e:
        print(f"[ERR] {label}: {type(e).__name__}: {e}")
W("duplicate ids", Graph(nodes=(NodeSpec(id="a", type="x"), NodeSpec(id="a", type="y"))))
W("input with two sources", Graph(nodes=(NodeSpec(id="a", type="x"), NodeSpec(id="b", type="x")),
   subscriptions=(Subscription(source_node="a", source_port="o", target_node="b", target_port="i"), Subscription(source_node="a", source_port="p", target_node="b", target_port="i"))))
W("start edge naming a node twice", Graph(nodes=(NodeSpec(id="a", type="x"), NodeSpec(id="b", type="x", start_edges=(StartEdge(sources=(StartSource(node="a"), StartSource(node="a", outcome="ok"))),)))))
W("empty start edge", Graph(nodes=(NodeSpec(id="b", type="x", start_edges=(StartEdge(),)),)))
W("dangling subscription source", Graph(nodes=(NodeSpec(id="b", type="x"),), subscriptions=(Subscription(source_node="ghost", source_port="o", target_node="b", target_port="i"),)))
W("code ''", Graph(nodes=(NodeSpec(id="b", type="x", code=""),)))
W("outcome None and '*' on graph", Graph(nodes=(NodeSpec(id="a", type="x"), NodeSpec(id="b", type="x", start_edges=(StartEdge(sources=(StartSource(node="a"),)), StartEdge(sources=(StartSource(node="a", outcome="ok"),)))))))
W("empty graph", Graph())

print("== rendering widths")
from emerge.graph.spec import _render
print(_render({"k": "x" * 10}, depth=3, column=10))
print(_render({"k": "x" * 90}, depth=3, column=10))
print(repr(_render({}, depth=0, column=0)), repr(_render([], depth=0, column=0)))

print("== code (generated types)")
GEN = Path(__file__).resolve().parent / "gen"
r = NodeRegistry(); prev = set_active_registry(r)
src = REG.find_entry("src"); r.register(src.cls, src.locator)
f1 = GEN / "cam_v1.py"; f2 = GEN / "cam_v2.py"
r.register_from_file(f1, "1")
g = GraphBuilder(r).add_node("k", "cam").add_node("s", "src").build()
r.register_from_file(f2, "2")
FILE = dumps_graph_spec(graph_spec_from_graph(g))
print(FILE)
def P(label, mutate, replace=()):
    d = json.loads(FILE); mutate(d)
    spec = loads_graph_spec(json.dumps(d))
    try:
        p = patch_from_graph_spec(g, spec, replace=replace)
        print(f"[OK ] {label}:", [(e.op, e.node, e.type, e.code) for e in p.edits])
        try:
            ng = apply_graph_patch(g, p, r)
            print("       ->", [(n.id, n.type, n.code, n.instance_id) for n in ng.nodes])
        except Exception as e:
            print(f"       APPLY {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[ERR] {label}: {type(e).__name__}: {e}")
P("code 1 -> 2", lambda d: d["nodes"]["k"].update(code="2"))
P("delete code line", lambda d: d["nodes"]["k"].pop("code"))
P("delete code line + replace=[k]", lambda d: d["nodes"]["k"].pop("code"), replace=["k"])
P("code '9' (unregistered)", lambda d: d["nodes"]["k"].update(code="9"))
P("type src -> cam, no code", lambda d: d["nodes"]["s"].update(type="cam"))
P("type src -> cam, code '1'", lambda d: d["nodes"]["s"].update(type="cam", code="1"))
P("type cam -> src (code line still '1')", lambda d: d["nodes"]["k"].update(type="src"))
P("type cam -> src, code line removed", lambda d: (d["nodes"]["k"].update(type="src"), d["nodes"]["k"].pop("code")))
P("new node cam without code", lambda d: d["nodes"].update(k2={"type": "cam"}))
set_active_registry(prev)

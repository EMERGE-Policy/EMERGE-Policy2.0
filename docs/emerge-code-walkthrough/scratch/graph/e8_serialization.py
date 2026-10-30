import json, logging
from fx import REG, try_
from emerge.graph import (GraphBuilder, GraphPatch, apply_graph_patch, graph_to_dict, graph_from_dict, dumps_graph, loads_graph,
                          patch_to_dict, patch_from_dict, dumps_patch, loads_patch, StartEdge, StartSource)
logging.disable(logging.WARNING)
b = GraphBuilder(REG)
b.add_node("a", "src", description="相机").add_node("t", "mid", params={"k": 2}, start_edges=[StartEdge(sources=(StartSource(node="a", outcome="success"),))])
b.connect_ports("a.out", "t.inp")
g = apply_graph_patch(b.build(), GraphPatch.replace_node("t"), REG)
d = graph_to_dict(g)
print(json.dumps(d, ensure_ascii=False, indent=1)[:900])
s1 = dumps_graph(g); g2 = loads_graph(s1); s2 = dumps_graph(g2)
print("roundtrip equal:", g2 == g, "bytes identical:", s1 == s2)
print("== schema checks")
try_("no schema_version", lambda: graph_from_dict({"nodes": []}))
try_("schema 4", lambda: graph_from_dict({"schema_version": 4}))
try_("schema '3' string", lambda: graph_from_dict({"schema_version": "3"}))
try_("schema 1.0 float", lambda: graph_from_dict({"schema_version": 1.0, "nodes": [{"id": "a", "type": "src", "after": []}]}))
try_("schema true", lambda: graph_from_dict({"schema_version": True}).version)
try_("non-dict JSON '[]'", lambda: loads_graph("[]"))
try_("JSON null", lambda: loads_graph("null"))
try_("bad JSON", lambda: loads_graph("{"))
try_("unknown top-level key ignored", lambda: graph_from_dict({"schema_version": 3, "bogus": 1}).version)
try_("v3 node with 'after'", lambda: graph_from_dict({"schema_version": 3, "nodes": [{"id": "a", "type": "src", "after": []}]}))
try_("v3 node unknown field", lambda: graph_from_dict({"schema_version": 3, "nodes": [{"id": "a", "type": "src", "foo": 1}]}))
print("== upgrades")
v1 = {"schema_version": 1, "version": 3, "nodes": [{"id": "a", "type": "src", "params": {}, "after": []},
       {"id": "b", "type": "mid", "params": {}, "after": ["a", "a"]}], "subscriptions": []}
g1 = graph_from_dict(v1)
print("v1 -> instance ids:", [n.instance_id for n in g1.nodes], "| b edges:", [str(e) for e in g1.node("b").start_edges], "| code:", g1.node("a").code)
v2 = {"schema_version": 2, "version": 2, "nodes": [{"id": "b", "type": "mid", "params": {}, "after": ["a"], "instance_id": "b@v2", "code": None}],
      "subscriptions": [], "replacements": [{"node": "b", "version": 2, "old_instance_id": "b@v0", "new_instance_id": "b@v2", "old_type": "mid", "new_type": "mid"}]}
g2v = graph_from_dict(v2)
print("v2 ->", g2v.node("b").instance_id, [str(e) for e in g2v.node("b").start_edges], len(g2v.replacements))
try_("v2 after as string", lambda: graph_from_dict({"schema_version": 2, "nodes": [{"id": "b", "type": "mid", "after": "ab"}]}))
try_("v2 node without after", lambda: graph_from_dict({"schema_version": 2, "nodes": [{"id": "b", "type": "mid"}]}).node("b").start_edges)
print("== patch")
p = GraphPatch.replace_node("t").combine_patches(GraphPatch.add_node("x", "sink", after=["t"]), GraphPatch.connect("t.out", "x.inp"))
pd = patch_to_dict(p)
print(json.dumps(pd["edits"][0]))
print("patch roundtrip:", loads_patch(dumps_patch(p)) == p, "| replace params None kept:", loads_patch(dumps_patch(p)).edits[0].params)
old = {"schema_version": 2, "edits": [{"op": "set_after", "node": "b", "after": ["a"]}, {"op": "set_after", "node": "c"},
       {"op": "add_node", "node": "z", "type": "src", "after": ["a"]}]}
pp = patch_from_dict(old)
print("v2 set_after ->", [(e.op, [str(x) for x in e.start_edges]) for e in pp.edits])
try_("v3 set_after", lambda: patch_from_dict({"schema_version": 3, "edits": [{"op": "set_after", "node": "b"}]}))
try_("bad patch JSON", lambda: loads_patch("nope"))

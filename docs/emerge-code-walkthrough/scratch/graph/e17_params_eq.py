import json, logging
from fx import REG
from emerge.graph import GraphBuilder, graph_spec_from_graph, dumps_graph_spec, loads_graph_spec, patch_from_graph_spec
logging.disable(logging.WARNING)
g = GraphBuilder(REG).add_node("t", "mid", params={"k": 1}).build()
for new in ({"k": 1.0}, {"k": True}, {"k": 2}):
    d = json.loads(dumps_graph_spec(graph_spec_from_graph(g)))
    d["nodes"]["t"]["params"] = new
    p = patch_from_graph_spec(g, loads_graph_spec(json.dumps(d)))
    print(json.dumps(new), "->", [e.op for e in p.edits] or "(empty patch)")

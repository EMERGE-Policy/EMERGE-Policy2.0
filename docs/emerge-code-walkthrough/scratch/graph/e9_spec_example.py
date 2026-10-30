import difflib, json, logging
from fx import REG, EXAMPLE, try_
from emerge.graph import (loads_graph_spec, graph_from_graph_spec, graph_spec_from_graph, dumps_graph_spec, validate,
                          start_status, to_text)
logging.disable(logging.WARNING)
text = EXAMPLE.read_text(encoding="utf-8")
spec = loads_graph_spec(text)
print("graph_version in example:", spec.graph_version, "| nodes:", len(spec.nodes))
g = graph_from_graph_spec(spec, REG)
print("built version", g.version, "| issues:", [(i.code.value, i.message) for i in validate(g, REG).issues])
print("roots:", g.node_ids_without_start_dependencies())
print("report_failure edges:", len(g.node("report_failure").start_edges))
print("left_stage_apple edge:", [str(e) for e in g.node("left_stage_apple").start_edges])
# start_status scenarios
ok = {"observe_scene": "success", "left_pick_apple": "success", "right_pick_banana": "failure"}
st = start_status(g, ok)
print("right pick failed:", {k: v.value for k, v in st.items()})
# file from graph
out = dumps_graph_spec(graph_spec_from_graph(g))
print("written == example bytes:", out == text)
diff = list(difflib.unified_diff(text.splitlines(), out.splitlines(), "example", "dumps_graph_spec", lineterm="", n=0))
print("\n".join(diff[:60]))
print("max line width:", max(len(l) for l in out.splitlines()))
print("round trip spec equal (ignoring graph_version):", loads_graph_spec(out).nodes == spec.nodes, "| graph_version written:", loads_graph_spec(out).graph_version)
open("example_rewritten.json", "w", encoding="utf-8").write(out)

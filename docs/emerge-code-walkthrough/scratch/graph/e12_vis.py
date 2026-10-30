import logging
from fx import REG
from emerge.graph import (GraphBuilder, GraphPatch, apply_graph_patch, to_text, to_mermaid, StartEdge, StartSource, Graph, NodeSpec, Subscription)
logging.disable(logging.WARNING)
b = GraphBuilder(REG)
b.add_node("v", "src", description='看 "场景"').add_node("p", "mid", start_edges=[StartEdge(sources=(StartSource(node="v", outcome="success"),))])
b.add_node("end", "sink", start_edges=[StartEdge(sources=(StartSource(node="p"), StartSource(node="v", outcome="success"))), StartEdge(sources=(StartSource(node="v", outcome="failure"),))])
b.connect_ports("v.out", "p.inp").connect_ports("p.out", "end.inp")
g = b.build()
g = apply_graph_patch(g, GraphPatch.replace_node("p"), REG)
g = apply_graph_patch(g, GraphPatch.replace_node("p", params={"k": 3}), REG)
states = {"v": "finished", "p": "running", "end": "skipped"}
outcomes = {"v": "success"}
print(to_text(g, states, outcomes))
print("----")
print(to_mermaid(g, states, outcomes))
print("----")
print(to_text(g, {}, {"v": "success"}))
print("---- dangling + outcome-only overlay")
gd = Graph(nodes=(NodeSpec(id="graph", type="t", start_edges=(StartEdge(sources=(StartSource(node="ghost"),)), StartEdge())),),
           subscriptions=(Subscription(source_node="ghost2", source_port="o", target_node="graph", target_port="i"),))
print(to_mermaid(gd))
print(to_text(gd))

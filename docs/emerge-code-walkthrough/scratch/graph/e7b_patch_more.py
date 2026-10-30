import logging
from fx import REG, try_
from emerge.graph import GraphBuilder, GraphPatch, apply_graph_patch, diff_graphs, Graph, NodeSpec, Subscription
logging.disable(logging.WARNING)
b = GraphBuilder(REG)
b.add_node("a", "src").add_node("t", "mid", params={"k": 1}, after=["a"]).add_node("e", "sink")
b.connect_ports("a.out", "t.inp").connect_ports("t.out", "e.inp")
g0 = b.build()
try_("replace t: mid->mid2 params omitted (old params k=1)", lambda: apply_graph_patch(g0, GraphPatch.replace_node("t", "mid2"), REG))
try_("replace t: mid->mid2 params={} + rewire e", lambda: str(apply_graph_patch(g0, GraphPatch.replace_node("t", "mid2", params={}).combine_patches(GraphPatch.disconnect("t.out", "e.inp"), GraphPatch.connect("a.out", "e.inp")), REG).node("t")))
# remove pending source feeding an ended node
try_("remove t (pending) whose output feeds finished e", lambda: str(apply_graph_patch(g0, GraphPatch.remove_node("t"), REG, states={"e": "finished", "a": "finished"})))
# diff multi-field modification
g1 = apply_graph_patch(g0, GraphPatch.replace_node_params("t", {"k": 2}).combine_patches(GraphPatch.set_node_description("t", "d"), GraphPatch.replace_start_edges("t")), REG)
print("modified:", diff_graphs(g0, g1).nodes.modified)
# diff ordering of subscription lists
ga = Graph(nodes=(NodeSpec(id="z", type="src"), NodeSpec(id="a", type="sink"), NodeSpec(id="b", type="sink")))
gb = ga.model_copy(update={"subscriptions": (Subscription(source_node="z", source_port="out", target_node="b", target_port="inp"),
                                              Subscription(source_node="z", source_port="out", target_node="a", target_port="inp"))})
print("subs added order:", [s.target_node for s in diff_graphs(ga, gb).subscriptions.added])
print("GraphDiff bool empty:", bool(diff_graphs(ga, ga)))
# model_copy with hand-changed type same instance -> modified includes type
gc = ga.model_copy(update={"nodes": (NodeSpec(id="z", type="mid"), *ga.nodes[1:])})
print("hand-changed type same instance ->", diff_graphs(ga, gc).nodes)

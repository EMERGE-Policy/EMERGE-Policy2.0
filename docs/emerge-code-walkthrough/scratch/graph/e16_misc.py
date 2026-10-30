import logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fx import REG, try_
from emerge.graph import GraphBuilder, GraphPatch, apply_graph_patch, NodeRegistry
logging.disable(logging.WARNING)
g = GraphBuilder(REG).add_node("a", "src").add_node("b", "mid").add_node("c", "sink").build()
g2 = apply_graph_patch(g, GraphPatch.add_node("z", "src").combine_patches(GraphPatch.replace_node("a"), GraphPatch.remove_node("b")), REG)
print("order after add z / replace a / remove b:", g2.node_ids(), [n.instance_id for n in g2.nodes])
r = NodeRegistry()
try_("load_modules good then missing", lambda: r.load_modules(["modpkg.good", "modpkg.nope"]))
print("after failure, catalog:", r.names(), "(undecorated class in a module is not registered)")

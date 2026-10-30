from fx import try_
from emerge.graph import Node, NodeRegistry, NodeLocator, GraphBuilder, validate
r = NodeRegistry()
class Bad(Node):
    name = "bad"; outcomes = ("ok",); inputs = ("img",)
class Src(Node):
    name = "s"; outcomes = ("ok",)
    from emerge.graph import Port
    outputs = (Port(name="o", type_tag="img"),)
try_("register Bad (string port)", lambda: r.register(Bad, NodeLocator(origin="module", module="m")).name)
r.register(Src, NodeLocator(origin="module", module="m2"))
try_("catalog to_dict", lambda: r.find_entry("bad").to_dict())
g = GraphBuilder(r).add_node("b", "bad").add_node("s", "s").connect_ports("s.o", "b.img").build_unvalidated_graph()
try_("validate graph using it", lambda: validate(g, r))
try_("graph without subscription", lambda: validate(GraphBuilder(r).add_node("b", "bad").build_unvalidated_graph(), r).ok)


from emerge.graph import Node, register_node
@register_node
class Q(Node):
    name = "q"; outcomes = ("ok",)
raise RuntimeError("boom after registering q")

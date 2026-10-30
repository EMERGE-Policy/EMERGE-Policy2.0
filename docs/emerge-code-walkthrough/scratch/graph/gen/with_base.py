
from emerge.graph import Node, register_node
class Shared(Node):          # shared base, no name, no outcomes
    pass
@register_node
class K(Shared):
    name = "k"; outcomes = ("ok",)

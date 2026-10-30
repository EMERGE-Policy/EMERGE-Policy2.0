
from emerge.graph import Node, register_node
@register_node
class G(Node):
    name = "g"; outcomes = ("ok", "fail")

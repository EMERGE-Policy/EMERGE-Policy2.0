from emerge.graph import Node, register_node
@register_node
class Good(Node):
    name = "good_mod"; outcomes = ("ok",)
class Undecorated(Node):
    name = "undecorated_mod"; outcomes = ("ok",)

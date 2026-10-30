
from emerge.graph import Node, Port
class Cam(Node):
    name = "cam"; outcomes = ("ok",); outputs = (Port(name="out", type_tag="img"),)

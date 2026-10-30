
from emerge.graph import Node, Port
class Cam(Node):
    name = "cam"; outcomes = ("ok", "bad"); outputs = (Port(name="out", type_tag="img"),)

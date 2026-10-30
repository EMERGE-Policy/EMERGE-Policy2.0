from emerge.graph import Graph, NodeSpec, StartEdge, StartSource
def N(i, *deps_per_edge):
    return NodeSpec(id=i, type="t", start_edges=tuple(StartEdge(sources=tuple(StartSource(node=d) for d in edge)) for edge in deps_per_edge))
cases = {
  "disjoint a<->b, c<->d": Graph(nodes=(N("a", ["b"]), N("b", ["a"]), N("c", ["d"]), N("d", ["c"]))),
  "a<->b, a<->c (a has deps b,c on one edge)": Graph(nodes=(N("a", ["b", "c"]), N("b", ["a"]), N("c", ["a"]))),
  "a<->b, a<->c (a has two edges)": Graph(nodes=(N("a", ["b"], ["c"]), N("b", ["a"]), N("c", ["a"]))),
  "a<->b, a<->c (a deps c first)": Graph(nodes=(N("a", ["c"], ["b"]), N("b", ["a"]), N("c", ["a"]))),
  "triangle + chord": Graph(nodes=(N("a", ["b"]), N("b", ["c", "a"]), N("c", ["a"]))),
}
for k, g in cases.items():
    print(k, "->", g.start_edge_cycles())

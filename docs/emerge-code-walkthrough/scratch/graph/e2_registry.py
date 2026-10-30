import sys, textwrap
from pathlib import Path
from fx import try_, P
from emerge.graph import Node, NodeRegistry, NodeLocator, register_node, set_active_registry, get_active_registry
from emerge.graph.registry import _RegistryHolder

GEN = Path(__file__).resolve().parent / "gen"
GEN.mkdir(exist_ok=True)

r = NodeRegistry()
mod = NodeLocator(origin="module", module="m1")
mod2 = NodeLocator(origin="module", module="m2")

class A(Node):
    name = "a"; outcomes = ("ok",)
class A2(Node):
    name = "a"; outcomes = ("ok",)
class NoName(Node):
    outcomes = ("ok",)
class Blank(Node):
    name = "   "; outcomes = ("ok",)
class NoOut(Node):
    name = "noout"

print("== register")
try_("no name", lambda: r.register(NoName, mod))
try_("blank name", lambda: r.register(Blank, mod))
try_("no outcomes", lambda: r.register(NoOut, mod))
e1 = try_("A first", lambda: r.register(A, mod).locator)
try_("A again same class, other locator -> returns existing", lambda: r.register(A, mod2).locator)
try_("A2 same name, module vs module", lambda: r.register(A2, mod2))
try_("A2 same name, file vs existing module", lambda: r.register(A2, NodeLocator(origin="file", module="g", path="/x", version="1", digest="d")))
print("params_schema default:", r.find_entry("a").params_schema)
print("to_dict keys:", list(r.find_entry("a").to_dict().keys()))

print("== pin / find / versions")
print("pin module type:", r.pin("a"), "| pin with code:", r.pin("a", "7"), "| pin unknown:", r.pin("zzz"))
try_("require_entry unknown", lambda: r.require_entry("zzz"))
try_("require_entry version unknown", lambda: r.require_entry("a", "3"))
print("find_entry('a','3'):", r.find_entry("a", "3"), "versions('a'):", r.versions("a"))

print("== register_from_file")
f = GEN / "gnode.py"
f.write_text(textwrap.dedent('''
    from emerge.graph import Node, Port, register_node
    @register_node
    class G(Node):
        name = "g"; outcomes = ("ok",)
    class H(Node):            # not decorated: picked up by the module scan
        name = "h"; outcomes = ("done",)
'''))
r2 = NodeRegistry()
before = get_active_registry()
es = try_("v1", lambda: tuple((e.name, e.locator.origin, e.locator.version, e.locator.module) for e in r2.register_from_file(f, "1")))
print("active registry restored:", get_active_registry() is before, "| loading reset:", r2.loading)
mods_before = set(k for k in sys.modules if k.startswith("emerge_generated_nodes"))
es2 = try_("v1 again same content -> reuse", lambda: tuple(e.locator.module for e in r2.register_from_file(f, "1")))
mods_after = set(k for k in sys.modules if k.startswith("emerge_generated_nodes"))
print("new modules after reuse:", mods_after - mods_before)
print("pin g now:", r2.pin("g"), "versions g:", r2.versions("g"))
try_("empty version", lambda: r2.register_from_file(f, "  "))
try_("missing file", lambda: r2.register_from_file(GEN / "nope.py", "1"))
# edit file, same version -> refused
f.write_text(f.read_text() + "\n# edited\n")
try_("same version, edited content", lambda: r2.register_from_file(f, "1"))
print("catalog g after refused:", r2.find_entry("g").locator.version, "| leftover modules:", set(k for k in sys.modules if k.startswith("emerge_generated_nodes")) - mods_after)
es3 = try_("v2 (edited)", lambda: tuple((e.name, e.locator.version) for e in r2.register_from_file(f, "2")))
print("current g:", r2.find_entry("g").locator.version, "| g@1 still findable:", r2.find_entry("g", "1") is not None, "| versions:", r2.versions("g"), "| pin g:", r2.pin("g"))
print("both modules alive:", sorted(k for k in sys.modules if k.startswith("emerge_generated_nodes.")))
# v3: file drops class H
f.write_text(textwrap.dedent('''
    from emerge.graph import Node, register_node
    @register_node
    class G(Node):
        name = "g"; outcomes = ("ok", "fail")
'''))
try_("v3 without H", lambda: tuple(e.name for e in r2.register_from_file(f, "3")))
print("catalog names after v3:", r2.names(), "| h@2 findable:", r2.find_entry("h", "2") is not None)
# re-register v2 content? file content changed so v2 now differs -> refused
try_("re-register '2' with v3 content", lambda: r2.register_from_file(f, "2"))

print("== failure paths")
bad = GEN / "bad_syntax.py"; bad.write_text("def x(:\n")
try_("syntax error", lambda: r2.register_from_file(bad, "1"))
empty = GEN / "empty.py"; empty.write_text("X = 1\n")
try_("no node classes", lambda: r2.register_from_file(empty, "1"))
print("leftover module for empty.py:", [k for k in sys.modules if "empty" in k])
base = GEN / "with_base.py"; base.write_text(textwrap.dedent('''
    from emerge.graph import Node, register_node
    class Shared(Node):          # shared base, no name, no outcomes
        pass
    @register_node
    class K(Shared):
        name = "k"; outcomes = ("ok",)
'''))
try_("file with nameless shared base class", lambda: r2.register_from_file(base, "1"))
print("k in catalog after failure:", "k" in r2)
raising = GEN / "raises.py"; raising.write_text(textwrap.dedent('''
    from emerge.graph import Node, register_node
    @register_node
    class Q(Node):
        name = "q"; outcomes = ("ok",)
    raise RuntimeError("boom after registering q")
'''))
try_("file raising after registering", lambda: r2.register_from_file(raising, "1"))
print("q rolled back:", "q" not in r2, r2.find_entry("q", "1"))
clash = GEN / "clash.py"; clash.write_text(textwrap.dedent('''
    from emerge.graph import Node
    class A(Node):
        name = "a"; outcomes = ("ok",)
'''))
r3 = NodeRegistry(); r3.register(A, mod)
try_("generated code reusing a module-owned name", lambda: r3.register_from_file(clash, "1"))
dup = GEN / "dupname.py"; dup.write_text(textwrap.dedent('''
    from emerge.graph import Node
    class D1(Node):
        name = "d"; outcomes = ("one",)
    class D2(Node):
        name = "d"; outcomes = ("two",)
'''))
r4 = NodeRegistry()
try_("two classes same name in one file", lambda: [(e.name, e.cls.__name__) for e in r4.register_from_file(dup, "1")])
print("module name sample:", r2.find_entry("g").locator.module)
ver = GEN / "verlabel.py"; ver.write_text("from emerge.graph import Node\nclass V(Node):\n    name='vv'; outcomes=('ok',)\n")
try_("version label 'v1.2-beta'", lambda: r4.register_from_file(ver, "v1.2-beta")[0].locator.module)

print("== load_modules")
try_("load_modules nonexistent", lambda: r4.load_modules(["no_such_module_xyz"]))
print("active restored:", get_active_registry() is before)

print("== decorator")
r5 = NodeRegistry(); prev = set_active_registry(r5)
@register_node
class Dec(Node):
    name = "dec"; outcomes = ("ok",)
set_active_registry(prev)
print("dec in r5:", "dec" in r5, r5.find_entry("dec").locator)

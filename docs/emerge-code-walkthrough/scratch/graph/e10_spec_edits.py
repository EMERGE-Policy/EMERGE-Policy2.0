import copy, json, logging
from pydantic import BaseModel
from fx import REG, EXAMPLE, try_, P
from emerge.graph import (Node, register_node, loads_graph_spec, graph_from_graph_spec, graph_spec_from_graph, dumps_graph_spec,
                          patch_from_graph_spec, apply_graph_spec, apply_graph_patch, diff_graphs, start_status, GraphPatch)
logging.disable(logging.WARNING)

@register_node
class GraspCheckStrict(Node):
    name = "grasp_check_strict"
    inputs = (P("pick_result", "pick_result"),)
    outputs = (P("held_object", "held"), P("result", "check"))
    outcomes = ("success", "failure")
    class Params(BaseModel):
        arm: str
        expected_object: str
        threshold: float = 0.5
    Params = Params

g0 = graph_from_graph_spec(loads_graph_spec(EXAMPLE.read_text(encoding="utf-8")), REG)
FILE = dumps_graph_spec(graph_spec_from_graph(g0))
STATES = {"observe_scene": "finished", "left_pick_apple": "running", "right_pick_banana": "running"}
ENDED = {"observe_scene": "success"}

def fmt_edit(e):
    s = e.op + "(" + ", ".join(x for x in [e.node or "", e.type or "", (f"params={dict(e.params)}" if e.params is not None and e.op != "add_node" else ""),
                                          (f"desc={e.description!r}" if e.description is not None and e.op in ("set_description",) else ""),
                                          (f"code={e.code}" if e.code else ""),
                                          (" | ".join(str(x) for x in e.start_edges) if e.op == "set_start_edges" else ""),
                                          (f"{e.source}->{e.target}" if e.source else "")] if x) + ")"
    return s

def run(label, mutate=None, *, text=None, replace=(), states=STATES, graph=g0):
    if text is None:
        d = json.loads(FILE)
        if mutate: mutate(d)
        text = json.dumps(d, ensure_ascii=False)
    print(f"### {label}")
    try:
        spec = loads_graph_spec(text)
    except Exception as e:
        print(f"   FORMAT {type(e).__name__}: {e}")
        return
    try:
        patch = patch_from_graph_spec(graph, spec, replace=replace)
    except Exception as e:
        print(f"   PATCH-FROM-SPEC {type(e).__name__}: {e}")
        return
    print("   patch:", [fmt_edit(e) for e in patch.edits] or "(empty)")
    try:
        new = apply_graph_patch(graph, patch, REG, states=states)
    except Exception as e:
        print(f"   APPLY {type(e).__name__}: {e}")
        return
    if new is graph:
        print("   -> same graph object, version", new.version)
        return
    d = diff_graphs(graph, new)
    print(f"   -> v{new.version}; added={list(d.nodes.added)} removed={list(d.nodes.removed)} modified={[(m.node, m.fields) for m in d.nodes.modified]} "
          f"replaced={[(r.node, r.old_instance_id, r.new_instance_id) for r in d.nodes.replaced]} subs+={[(s.source_node+'.'+s.source_port, s.target_node+'.'+s.target_port) for s in d.subscriptions.added]} "
          f"subs-={[(s.source_node+'.'+s.source_port, s.target_node+'.'+s.target_port) for s in d.subscriptions.removed]} rewired={[(o.source_node+'.'+o.source_port, n.source_node+'.'+n.source_port, n.target_node+'.'+n.target_port) for o, n in d.subscriptions.rewired]}")
    return new

N = lambda d, i: d["nodes"][i]
run("1 no edit")
print("   apply_graph_spec returns same object:", apply_graph_spec(g0, FILE, REG, states=STATES) is g0)
run("2 description of pending left_check_grasp", lambda d: N(d, "left_check_grasp").update(description="new text"))
run("3 description of running left_pick_apple", lambda d: N(d, "left_pick_apple").update(description="new text"))
run("4 description of finished observe_scene", lambda d: N(d, "observe_scene").update(description="new text"))
run("5 params of pending left_check_grasp", lambda d: N(d, "left_check_grasp")["params"].update(arm="right"))
run("6 params of running left_pick_apple", lambda d: N(d, "left_pick_apple")["params"].update(arm="right"))
run("7 params of finished observe_scene", lambda d: N(d, "observe_scene")["params"].update(targets=["apple"]))
run("8 params with same content, keys reordered", lambda d: N(d, "left_check_grasp").update(params={"expected_object": "apple", "arm": "left"}))
run("9 type of pending left_check_grasp", lambda d: N(d, "left_check_grasp").update(type="grasp_check_strict"))
run("10 type of running left_pick_apple -> grasp_check_strict (ports differ)", lambda d: N(d, "left_pick_apple").update(type="grasp_check_strict", params={"arm": "left", "expected_object": "apple"}))
run("11 type+params+description of pending node in one go", lambda d: N(d, "left_check_grasp").update(type="grasp_check_strict", description="strict", params={"arm": "left", "expected_object": "apple", "threshold": 0.9}))
run("12 type change + start_when change on pending", lambda d: (N(d, "left_check_grasp").update(type="grasp_check_strict"), N(d, "left_check_grasp").update(start_when={"any_of": [{"all_of": {"left_pick_apple": "*"}}]})))
run("13 start_when of pending report_success: success -> *", lambda d: N(d, "report_success").update(start_when={"any_of": [{"all_of": {"check_fridge_open": "*"}}]}))
run("14 start_when of running left_pick_apple", lambda d: N(d, "left_pick_apple").update(start_when={"any_of": [{"all_of": {"observe_scene": "*"}}]}))
run("15 reorder any_of of report_failure", lambda d: N(d, "report_failure")["start_when"]["any_of"].reverse())
run("16 reorder all_of keys of left_stage_apple", lambda d: N(d, "left_stage_apple")["start_when"]["any_of"][0].update(all_of={"right_check_grasp": "success", "left_check_grasp": "success"}))
run("17 reorder all_of keys of RUNNING node? (left_pick_apple has 1 key) / reorder nodes in file", lambda d: d.update(nodes=dict(reversed(list(d["nodes"].items())))))
def drop_sw(d): del N(d, "report_failure")["start_when"]
g17 = run("18 drop start_when of report_failure (becomes root)", drop_sw)
if g17: print("   start_status report_failure:", start_status(g17, ENDED)["report_failure"].value)
run("19 rewire one input of open_fridge_together", lambda d: N(d, "open_fridge_together")["inputs"].update(fridge="observe_scene.staging_table"))
run("20 drop input line check_fridge_open.fridge", lambda d: N(d, "check_fridge_open")["inputs"].pop("fridge"))
run("21 add input to report_failure", lambda d: N(d, "report_failure").update(inputs={"result": "check_fridge_open.result"}))
run("22 rewire input of RUNNING left_pick_apple", lambda d: N(d, "left_pick_apple")["inputs"].update(target="observe_scene.banana"))
run("23 add node reading finished observe_scene's output", lambda d: d["nodes"].update(recheck={"type": "fridge_open_check", "description": "again", "params": {}, "inputs": {"fridge": "observe_scene.fridge"}, "start_when": {"any_of": [{"all_of": {"check_fridge_open": "failure"}}]}}))
gx = run("24 add node waiting on observe_scene: failure (already ended success)", lambda d: d["nodes"].update(retry={"type": "task_report", "description": "x", "params": {"status": "s", "message": "m"}, "start_when": {"any_of": [{"all_of": {"observe_scene": "failure"}}]}}))
if gx: print("   start_status retry:", start_status(gx, ENDED)["retry"].value)
gy = run("25 add node waiting on observe_scene: * (already ended)", lambda d: d["nodes"].update(retry={"type": "task_report", "description": "x", "params": {"status": "s", "message": "m"}, "start_when": {"any_of": [{"all_of": {"observe_scene": "*"}}]}}))
if gy: print("   start_status retry:", start_status(gy, ENDED)["retry"].value)
run("26 remove pending report_success", lambda d: d["nodes"].pop("report_success"))
run("27 remove check_fridge_open but leave references", lambda d: d["nodes"].pop("check_fridge_open"))
def rm_check(d):
    d["nodes"].pop("check_fridge_open")
    N(d, "report_success")["inputs"].pop("result"); N(d, "report_success")["start_when"] = {"any_of": [{"all_of": {"open_fridge_together": "success"}}]}
    N(d, "report_failure")["start_when"]["any_of"] = [c for c in N(d, "report_failure")["start_when"]["any_of"] if "check_fridge_open" not in c["all_of"]]
run("28 remove check_fridge_open and fix references", rm_check)
def rm_running(d):
    d["nodes"].pop("left_pick_apple")
    N(d, "left_check_grasp")["inputs"].pop("pick_result"); N(d, "left_check_grasp")["start_when"] = {"any_of": [{"all_of": {"observe_scene": "success"}}]}
    N(d, "report_failure")["start_when"]["any_of"] = [c for c in N(d, "report_failure")["start_when"]["any_of"] if "left_pick_apple" not in c["all_of"]]
run("29 remove running left_pick_apple (references fixed)", rm_running)
def rename(d):
    node = d["nodes"].pop("report_success"); d["nodes"]["report_ok"] = node
run("30 rename report_success -> report_ok", rename)
run("31 replace=[left_pick_apple] (running)", replace=["left_pick_apple"])
run("32 replace=[observe_scene] (finished)", replace=["observe_scene"])
run("33 replace=[ghost]", replace=["ghost"])
run("34 replace=[left_check_grasp] + params change", lambda d: N(d, "left_check_grasp")["params"].update(arm="right"), replace=["left_check_grasp"])
run("35 replace=[report_success] + start_when change", lambda d: N(d, "report_success").update(start_when={"any_of": [{"all_of": {"check_fridge_open": "*"}}]}), replace=["report_success"])
run("36 replace a node removed from file", lambda d: d["nodes"].pop("report_success"), replace=["report_success"])
g1 = apply_graph_patch(g0, GraphPatch.set_node_description("report_success", "v1 text"), REG)
run("37 stale file (graph_version 0, graph at v1)", lambda d: N(d, "report_failure").update(description="x"), graph=g1)
def no_gv(d): d.pop("graph_version"); N(d, "report_failure").update(description="x")
run("38 file without graph_version on v1 graph", no_gv, graph=g1)
run("39 edit -> validation error: input to unknown port", lambda d: N(d, "report_failure").update(inputs={"nope": "check_fridge_open.result"}))
run("40 edit -> START_UNKNOWN_OUTCOME", lambda d: N(d, "report_success").update(start_when={"any_of": [{"all_of": {"check_fridge_open": "maybe"}}]}))
run("41 edit -> self wait START_CYCLE", lambda d: N(d, "report_success").update(start_when={"any_of": [{"all_of": {"report_success": "*"}}]}))
run("42 edit -> PARAM_INVALID", lambda d: N(d, "report_success")["params"].pop("message"))
run("43 edit -> tag mismatch only warning", lambda d: N(d, "report_failure").update(inputs={"result": "observe_scene.apple"}))
run("44 delete 'params' key of a node (defaults to {})", lambda d: N(d, "check_fridge_open").pop("params"))
run("45 delete 'description' key", lambda d: N(d, "check_fridge_open").pop("description"))
run("46 add code to a module type", lambda d: N(d, "check_fridge_open").update(code="2"))

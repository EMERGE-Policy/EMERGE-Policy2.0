import json, logging
import e10_spec_edits as E  # reuses the graph and FILE (prints its own run first)
from e10_spec_edits import run, N
print("\n======== extra: ended targets")
ST = {"observe_scene": "finished", "left_pick_apple": "finished", "right_pick_banana": "running"}
run("X1 change input of FINISHED left_pick_apple", lambda d: N(d, "left_pick_apple")["inputs"].update(target="observe_scene.banana"), states=ST)
run("X2 drop input of FINISHED left_pick_apple", lambda d: N(d, "left_pick_apple").pop("inputs"), states=ST)
run("X3 add input to FINISHED observe_scene", lambda d: None, states=ST)  # (scene has no inputs; nothing to add)
run("X4 remove pending left_check_grasp which reads FINISHED left_pick_apple (refs fixed)", lambda d: (d["nodes"].pop("left_check_grasp"),
    N(d, "left_stage_apple")["inputs"].update(held_object="right_check_grasp.held_object"),
    N(d, "left_stage_apple").update(start_when={"any_of": [{"all_of": {"right_check_grasp": "success"}}]}),
    N(d, "report_failure")["start_when"].update(any_of=[c for c in N(d, "report_failure")["start_when"]["any_of"] if "left_check_grasp" not in c["all_of"]])), states=ST)

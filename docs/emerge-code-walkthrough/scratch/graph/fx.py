"""Scratch fixtures for the graph-layer walkthrough: example node types, registry, helpers."""
from __future__ import annotations

import json
import traceback
from pathlib import Path

from pydantic import BaseModel

from emerge.graph import (
    Node, Outcome, Port, NodeRegistry, register_node, set_active_registry,
)

REG = NodeRegistry()
set_active_registry(REG)

T = "target"


def P(name, tag=T):
    return Port(name=name, type_tag=tag)


@register_node
class SceneObserver(Node):
    name = "scene_observer"
    outputs = (P("apple"), P("banana"), P("staging_table"), P("fridge"))
    outcomes = ("success", "failure")

    class Params(BaseModel):
        targets: list[str]

    Params = Params


@register_node
class ArmPick(Node):
    name = "arm_pick"
    inputs = (P("target"),)
    outputs = (P("result", "pick_result"),)
    outcomes = ("success", "failure")
    irreversible = True

    class Params(BaseModel):
        arm: str

    Params = Params


@register_node
class GraspCheck(Node):
    name = "grasp_check"
    inputs = (P("pick_result", "pick_result"),)
    outputs = (P("held_object", "held"), P("result", "check"))
    outcomes = ("success", "failure", "unclear")

    class Params(BaseModel):
        arm: str
        expected_object: str

    Params = Params


@register_node
class ArmPlace(Node):
    name = "arm_place"
    inputs = (P("held_object", "held"), P("destination"))
    outputs = (P("result", "place_result"),)
    outcomes = ("success", "failure")
    irreversible = True

    class Params(BaseModel):
        arm: str
        slot: str

    Params = Params


@register_node
class DualArmOpenFridge(Node):
    name = "dual_arm_open_fridge"
    inputs = (P("fridge"), P("left_ready", "place_result"), P("right_ready", "place_result"))
    outputs = (P("result", "open_result"),)
    outcomes = ("success", "failure")
    irreversible = True

    class Params(BaseModel):
        left_arm_role: str
        right_arm_role: str

    Params = Params


@register_node
class FridgeOpenCheck(Node):
    name = "fridge_open_check"
    inputs = (P("fridge"), P("open_result", "open_result"))
    outputs = (P("result", "check"),)
    outcomes = ("success", "failure")

    class Params(BaseModel):
        pass

    Params = Params


@register_node
class TaskReport(Node):
    name = "task_report"
    inputs = (P("result", "check"),)
    outcomes = ("done",)

    class Params(BaseModel):
        status: str
        message: str

    Params = Params


# generic small nodes for patch / validation experiments
@register_node
class Src(Node):
    name = "src"
    outputs = (P("out", "img"), P("aux", "img"))
    outcomes = ("success", "failure")


@register_node
class Mid(Node):
    name = "mid"
    inputs = (P("inp", "img"),)
    outputs = (P("out", "img"),)
    outcomes = ("success", "failure")

    class Params(BaseModel):
        k: int = 1

    Params = Params


@register_node
class Mid2(Node):
    """Same ports as mid, but output tag differs: 'mask' instead of 'img'."""
    name = "mid2"
    inputs = (P("inp", "img"),)
    outputs = (P("out", "mask"),)
    outcomes = ("ok",)


@register_node
class Mid3(Node):
    """No 'out' port at all."""
    name = "mid3"
    inputs = (P("inp", "img"),)
    outputs = (P("other", "img"),)
    outcomes = ("ok",)


@register_node
class Sink(Node):
    name = "sink"
    inputs = (P("inp", "img"),)
    outcomes = ("done",)
    reads = (("camera_file", "frames written by an external driver"),)


EXAMPLE = Path("/data/yuqingchi/Code/EMERGE-Policy2.0/docs/graph-examples/dual-arm-fruit-fridge.json")


def try_(label, fn):
    try:
        out = fn()
        print(f"[OK ] {label}: {out!r}" if out is not None else f"[OK ] {label}")
        return out
    except BaseException as e:  # noqa: BLE001
        print(f"[ERR] {label}: {type(e).__name__}: {e}")
        return None

"""`RoboDojoDriver`: RoboDojo's dual ARX X5 tasks (Isaac Sim 5.1) behind the robot files.

The driver builds RoboDojo's own evaluation environment for one task, the way its
`src/eval_client/main.py` does, without the policy server: actions come from the nodes, one
`take_action` per tick. RoboDojo itself is not changed.

Scene files are JSON. A first scene names the task and one of its evaluation layouts:

    {"task": "stack_bowls", "layout_id": 0}

A saved scene adds "state": what is needed to come back to the same moment (objects, robots, the
last command of each arm, the step count). What the driver cannot bring back yet is listed in
"not_restored".

Actions (all in joint space; RoboDojo moves both arms in one `take_action`):

    move_joints  {"joints": [6 floats], "gripper": 0..1 (optional), "ticks": n (default 25)}
                 the arm goes in a straight line in joint space to `joints` over n ticks
    move_ee      {"pose": [x, y, z, qw, qx, qy, qz] world frame, "gripper": 0..1 (optional), "ticks": n}
                 inverse kinematics once at the start, then like move_joints
    set_gripper  {"gripper": 0..1, "ticks": n (default 10)}   1 = fully open
    stay         {"ticks": n}                                  stay where last commanded

An arm without an action keeps its last command.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from robot.base.errors import RobotError
from robot.base.protocol.manifest import Manifest
from robot.base.simulation.driver import RobotDriver, SceneChoice

logger = logging.getLogger(__name__)

ROBODOJO_ROOT = Path(__file__).resolve().parents[2] / "third_party" / "RoboDojo"
ARMS = ("left_arm", "right_arm")
# Who owns which arm and camera: written by hand, next to this file.
MANIFEST_PATH = Path(__file__).with_name("manifest.json")
# The knobs of the robot layer for this environment, and the defaults of the arguments below.
CONFIG_PATH = Path(__file__).with_name("config.json")
# Kit extensions RoboDojo's scripts/eval_policy.sh enables.
KIT_EXTENSIONS = ("isaacsim.replicator.behavior", "isaacsim.sensors.camera")
DEFAULT_TICKS = {"move_joints": 25, "move_ee": 25, "set_gripper": 10, "stay": 1}

DESCRIPTION = """\
One arm of a RoboDojo dual-arm robot (simulated in Isaac Sim): an ARX X5 arm with 6 joints and a parallel gripper; joint speed limit 5 rad/s.
You operate <arm>. Poses are in the frame of its base: x, y, z in metres, orientation as a quaternion [qw, qx, qy, qz].
Cameras: cam_head (fixed, above and behind the table) and <camera> (on the wrist).
One tick is 0.04 s of simulated time (25 Hz).
Actions, all in joint space:
  move_joints {"joints": [6 floats, rad], "gripper": 0..1 optional, "ticks": n}: straight line in joint space over n ticks
  move_ee {"pose": [x, y, z, qw, qx, qy, qz], base frame, metres, "gripper": 0..1 optional, "ticks": n}: inverse kinematics once, then as move_joints; fails when there is no solution
  set_gripper {"gripper": 0..1, "ticks": n}: 1 is fully open
  stay {"ticks": n}: stay where last commanded
"""


class _NoPolicy:
    """Stands in for RoboDojo's policy-server client: the nodes decide, nothing is sent anywhere."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def call(self, func_name: str | None = None, obs: Any = None, **kwargs: Any) -> Any:
        return None

    def close(self) -> None:
        pass


class RoboDojoDriver(RobotDriver):
    """RoboDojo: two ARX X5 arms in Isaac Sim 5.1, one task at a time.

    Configuration (the --config JSON):
        task                   RoboDojo task name, e.g. "stack_bowls" (a task change needs a new process)
        env_cfg                RoboDojo env config under env_cfg/, default "arx_x5"
        device_id              GPU index, default 0
        layout_seed            evaluation layout set under Assets/Eval_Layout/RoboDojo/<env_cfg>/<seed>/, default 0
        step_lim               the episode's step limit, default 100000 (the tasks' own, e.g. 550, are for one
                               evaluation episode; an agent trying things uses them up quickly)
        finish_on_episode_end  end the simulation when RoboDojo ends the episode (success or step limit), default true
        headless               default true
    """

    def __init__(
        self,
        task: str,
        env_cfg: str = "arx_x5",
        device_id: int = 0,
        layout_seed: int = 0,
        step_lim: int = 100_000,
        finish_on_episode_end: bool = True,
        headless: bool = True,
    ) -> None:
        self.task = task
        self.env_cfg_name = env_cfg
        self.device_id = int(device_id)
        self.layout_seed = int(layout_seed)
        self.step_lim = int(step_lim)
        self.finish_on_episode_end = bool(finish_on_episode_end)
        self.headless = bool(headless)
        self.app = None
        self.env = None
        self.layout_id: int | None = None
        # Per arm: the action under way, {"id", "joints": [start, target], "gripper": [start, target], "ticks", "done"}.
        self._plans: dict[str, dict[str, Any]] = {}
        # Per arm: the last command, {"joints": [6], "gripper": 0..1}.
        self._last: dict[str, dict[str, Any]] = {}

    @classmethod
    def config_path(cls) -> Path:
        return CONFIG_PATH

    @classmethod
    def scenes(cls, env_cfg: str = "arx_x5", layout_seed: int = 0) -> list[SceneChoice]:
        """Every evaluation layout of every task: Assets/Eval_Layout/RoboDojo/<env_cfg>/<seed>/<task>_<n>.json."""
        folder = ROBODOJO_ROOT / "Assets" / "Eval_Layout" / "RoboDojo" / env_cfg / str(layout_seed)
        layouts = []
        for path in folder.glob("*.json") if folder.is_dir() else ():
            task, _, number = path.stem.rpartition("_")
            if task and number.isdigit():
                layouts.append((task, int(number)))
        return [
            SceneChoice(group=task, name=f"layout {number}", config={"task": task, "env_cfg": env_cfg, "layout_seed": layout_seed},
                        scene={"task": task, "layout_id": number})
            for task, number in sorted(layouts)
        ]

    # -- the robot ---------------------------------------------------------------

    def describe(self, body: str) -> str:
        text = DESCRIPTION.replace("<arm>", f"{body}_arm").replace("<camera>", f"cam_{body}_wrist")
        return text + f"Task: {self.task}.\n"

    def arm_names(self) -> list[str]:
        return list(ARMS)

    def camera_names(self) -> list[str]:
        return list(self._env().camera_manager.camera_names[0])

    def manifest(self) -> Manifest:
        return Manifest.read(MANIFEST_PATH)

    def pose_parameters(self) -> dict[str, tuple[str, ...]]:
        return {"move_ee": ("pose",)}

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", str(self.device_id))
        os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
        # Like RoboDojo's scripts/eval_policy.sh: its root first, then XPolicyLab (both have a `utils`).
        sys.path[:0] = [str(path) for path in (ROBODOJO_ROOT, ROBODOJO_ROOT / "XPolicyLab") if str(path) not in sys.path]
        # RoboDojo writes its videos and results under the working directory, as when it is run from its root.
        os.chdir(ROBODOJO_ROOT)

        from isaaclab.app import AppLauncher

        parser = argparse.ArgumentParser()
        AppLauncher.add_app_launcher_args(parser)
        argv = ["--enable_cameras", "--kit_args", " ".join(f"--enable {ext}" for ext in KIT_EXTENSIONS)]
        if self.headless:
            argv.append("--headless")
        args = parser.parse_args(argv)
        from env.camera_manager.capture.render_sync import add_zero_delay_kit_args

        add_zero_delay_kit_args(args)
        self.app = AppLauncher(args).app

        # Everything below needs the running app.
        from omegaconf import OmegaConf

        import src.eval_client.eval_env as eval_env
        from env.global_configs import BENCHMARK, ENV_CONFIG_PATH, ROOT_DIR
        from utils.load_file import load_yaml
        from utils.pipeline_utils import process_config, process_randomization

        eval_env.WsModelClient = _NoPolicy
        registry = importlib.import_module(f"task.{BENCHMARK}.task_registry")
        eval_cfg = load_yaml(os.path.join(ENV_CONFIG_PATH, self.env_cfg_name + ".yml"))
        eval_cfg.update(
            task_name=self.task, num_envs=1, device_id=self.device_id, eval_batch=False, policy_name="EMERGE",
            additional_info="emerge", seed=self.layout_seed, physx_monitor_enabled=False,
        )
        sections = {
            part: load_yaml(os.path.join(ENV_CONFIG_PATH, part, eval_cfg["config"][part] + ".yml"))
            for part in ("sim", "scene", "camera", "robot")
        }
        config = OmegaConf.create({
            **sections,
            "task_env": load_yaml(registry.task_config_path(os.path.join(ROOT_DIR, "task", BENCHMARK, "config"), self.task)),
            "eval_cfg": eval_cfg,
            "deploy_cfg": {"policy_name": "EMERGE", "port": 0, "host": "localhost", "protocol": "ws",
                           "policy_server_url": "ws://localhost:0"},
        })
        OmegaConf.update(config, "sim.scene.num_envs", 1, force_add=True)
        config = process_randomization(config)
        config, eval_num = process_config(config, task_name=self.task)
        config.eval_cfg["eval_num"] = eval_num
        OmegaConf.update(config, "camera.default_frequency", eval_cfg["observation"].get("collect_freq", 0), force_add=True)
        config.sim.seed = [0]
        self.env = eval_env.create_eval_env(config, self.app)
        self.env.step_lim = self.step_lim
        logger.info("RoboDojo task %s ready (%d evaluation layouts)", self.task, len(self.env.seed_manager.seed_info))

    def close(self) -> None:
        env, app = self.env, self.app
        self.env = self.app = None
        if env is not None:
            try:
                env.close()
            except Exception:
                logger.exception("closing the RoboDojo environment failed")
        if app is not None:
            app.close()

    def is_finished(self) -> bool:
        return self.finish_on_episode_end and self.env is not None and bool(self.env.end_flag[0])

    # -- one observation ---------------------------------------------------------------

    def get_joint_positions(self, arm: str) -> list[float]:
        env = self._env()
        return [float(x) for x in env.robot_manager.get_joint(self._robot(arm), env_idx_list=[0])[0]]

    def get_ee_pose(self, arm: str) -> list[float]:
        env = self._env()
        pose = env.robot_manager.get_real_endpose(self._robot(arm), env_idx_list=[0], is_relative=False)[0]
        return [float(x) for x in pose]

    def get_gripper(self, arm: str) -> float:
        robot = self._robot(arm)
        value = float(self._env().robot_manager.get_end_effector_real_val(robot, env_idx_list=[0])[0][0])
        return float(np.clip(_normalize_gripper(robot, value), 0.0, 1.0))

    def get_images(self) -> dict[str, np.ndarray]:
        observation = self._env().get_obs()
        return {name: np.asarray(data["color"])[..., :3] for name, data in observation["vision"].items()}

    def get_intrinsics(self, camera: str) -> np.ndarray:
        manager = self._env().camera_manager
        return manager.get_camera_intrinsics(manager.camera_names[0].index(camera), 0)

    def get_extrinsics(self, camera: str) -> np.ndarray:
        manager = self._env().camera_manager
        return manager.get_camera_extrinsics(manager.camera_names[0].index(camera), 0)

    def get_extra(self) -> dict[str, Any]:
        env = self._env()
        return {"instruction": env.obs_manager.instruction[0], "steps": int(env.take_action_cnt[0]),
                "step_lim": int(env.step_lim)}

    # -- execution ---------------------------------------------------------------------

    def execute_action(self, actions: dict[str, dict[str, Any]]) -> dict[str, str]:
        env = self._env()
        results: dict[str, str] = {}
        for arm in list(self._plans):
            if arm not in actions:
                del self._plans[arm]
        for arm, action in actions.items():
            plan = self._plans.get(arm)
            if plan is None or plan["id"] != action["id"]:
                try:
                    self._plans[arm] = self._plan(arm, action)
                except _Refused as e:
                    self._plans.pop(arm, None)
                    results[arm] = f"Failed: {e}"
        command: dict[str, Any] = {}
        for arm in ARMS:
            target = self._last_command(arm)
            plan = self._plans.get(arm)
            if plan is not None:
                plan["done"] += 1
                fraction = plan["done"] / plan["ticks"]
                start, goal = plan["joints"]
                target = {
                    "joints": [a + (b - a) * fraction for a, b in zip(start, goal)],
                    "gripper": plan["gripper"][0] + (plan["gripper"][1] - plan["gripper"][0]) * fraction,
                }
            self._last[arm] = target
            side = arm.split("_")[0]
            command[f"{side}_arm_joint_state"] = np.asarray(target["joints"], dtype=float)
            command[f"{side}_ee_joint_state"] = np.asarray([target["gripper"]], dtype=float)
        env.take_action(command)
        for arm, plan in list(self._plans.items()):
            if plan["done"] >= plan["ticks"]:
                results[arm] = f"{plan['verb']} {arm} in {plan['ticks']} ticks."
                del self._plans[arm]
        return results

    def _plan(self, arm: str, action: dict[str, Any]) -> dict[str, Any]:
        kind, parameters = action["action_type"], action.get("parameters") or {}
        if kind not in DEFAULT_TICKS:
            raise _Refused(f"unknown action type {kind}; the action types are {sorted(DEFAULT_TICKS)}.")
        ticks = parameters.get("ticks", DEFAULT_TICKS[kind])
        if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 1:
            raise _Refused(f"ticks must be a whole number >= 1, got {ticks!r}.")
        last = self._last_command(arm)
        joints, gripper = list(last["joints"]), last["gripper"]
        if "gripper" in parameters or kind == "set_gripper":
            gripper = _fraction(parameters.get("gripper"), "gripper")
        if kind == "move_joints":
            joints = _vector(parameters.get("joints"), 6, "joints")
            verb = "Moved"
        elif kind == "move_ee":
            joints = self._inverse_kinematics(arm, _vector(parameters.get("pose"), 7, "pose"))
            verb = "Moved"
        elif kind == "set_gripper":
            verb = "Set the gripper of"
        else:
            verb = "Kept"
        return {"id": action["id"], "joints": (list(last["joints"]), joints), "gripper": (last["gripper"], gripper),
                "ticks": ticks, "done": 0, "verb": verb}

    def _inverse_kinematics(self, arm: str, pose: list[float]) -> list[float]:
        env = self._env()
        # RoboDojo's planner works in the environment frame; the observation's poses are world poses.
        origin = np.asarray(env.env_origins[0].cpu())
        relative = list(np.asarray(pose[:3]) - origin) + list(pose[3:])
        result = env.robot_manager.solve_ik(target_pose=relative, env_idx=0, robot=self._robot(arm))
        if result.get("status") != "Success":
            raise _Refused(f"no inverse kinematics solution for that pose ({result.get('status')}).")
        return [float(x) for x in result["joint_value"]]

    def _last_command(self, arm: str) -> dict[str, Any]:
        if arm not in self._last:
            # Nothing commanded yet: hold the arm where it is.
            self._last[arm] = {"joints": self.get_joint_positions(arm), "gripper": self.get_gripper(arm)}
        return self._last[arm]

    # -- scenes --------------------------------------------------------------------------

    def load_scene(self, path: Path) -> None:
        scene = json.loads(Path(path).read_text(encoding="utf-8"))
        if scene.get("task") != self.task:
            raise RobotError(f"{path} is a scene of task {scene.get('task')!r}; this simulation runs {self.task!r}")
        layout_id = int(scene["layout_id"])
        env = self._env()
        if layout_id not in env.seed_manager.seed_info:
            raise RobotError(f"task {self.task} has no evaluation layout {layout_id} (it has {len(env.seed_manager.seed_info)})")
        env.reset(seed=[layout_id])
        # What run_eval() does first: register the task's success checks (and score), without which
        # RoboDojo counts an episode with no checks as already successful.
        env.run_reward()
        if hasattr(env, "get_score"):
            env.get_score()
        self.layout_id = layout_id
        self._plans.clear()
        self._last.clear()
        state = scene.get("state")
        if state is not None:
            _restore(env, state, self)

    def save_scene(self, path: Path) -> None:
        env = self._env()
        scene = {"task": self.task, "layout_id": self.layout_id, "state": _capture(env, self)}
        Path(path).write_text(json.dumps(scene), encoding="utf-8")

    # -- internals ---------------------------------------------------------------------------

    def _env(self):
        if self.env is None:
            raise RobotError("the RoboDojo environment is not running")
        return self.env

    def _robot(self, arm: str):
        if arm not in ARMS:
            raise RobotError(f"no arm {arm!r}")
        return self._env().robot_manager.get_robot_by_arm_name(arm)


class _Refused(Exception):
    """An action the driver will not start; it ends at once as failed."""


# -- scene state -------------------------------------------------------------------------------


def _capture(env, driver: RoboDojoDriver) -> dict[str, Any]:
    """What it takes to come back to this moment of the scene, JSON-ready."""
    robots = {}
    for index, robot in enumerate(env.robot_manager.robot_list):
        key = env.robot_manager.robot_key[index]
        robots[robot.arm_name] = {
            "joint_pos": key.data.joint_pos[0].tolist(),
            "joint_vel": key.data.joint_vel[0].tolist(),
        }
    objects: dict[str, Any] = {}
    not_restored: list[str] = ["the task's reward bookkeeping (RoboDojo keeps it inside the task)"]
    seen: set[int] = set()
    for name, obj in env.scene_manager.get_objects(env_ids=[0]).items():
        # RoboDojo lists rigid and dynamic objects in one collection under both names.
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        kind = name.split("_")[1]
        if kind in ("rigid", "dynamic") and hasattr(obj, "get_linear_velocity"):
            position, orientation = obj.get_world_pose()
            objects[name] = {"pose": _list(position) + _list(orientation),
                             "linear_velocity": _list(obj.get_linear_velocity()),
                             "angular_velocity": _list(obj.get_angular_velocity())}
        elif kind == "articulation":
            position, orientation = obj.get_world_pose()
            objects[name] = {"pose": _list(position) + _list(orientation),
                             "joint_positions": _list(obj.get_joint_positions()),
                             "joint_velocities": _list(obj.get_joint_velocities())}
        elif kind == "geometry":
            position, orientation = obj.get_world_pose()
            objects[name] = {"pose": _list(position) + _list(orientation)}
        elif kind == "fluid":
            objects[name] = {"particles": _list(obj.get_particle_positions())}
        else:
            if name not in objects:
                not_restored.append(f"{name} ({type(obj).__name__})")
    return {
        "steps": int(env.take_action_cnt[0]),
        "success": bool(env.success[0]),
        "end_flag": bool(env.end_flag[0]),
        "robots": robots,
        "objects": objects,
        "last_command": deepcopy(driver._last),
        "not_restored": sorted(set(not_restored)),
    }


def _restore(env, state: dict[str, Any], driver: RoboDojoDriver) -> None:
    import torch

    for index, robot in enumerate(env.robot_manager.robot_list):
        saved = state["robots"].get(robot.arm_name)
        if saved is None:
            continue
        key = env.robot_manager.robot_key[index]
        position = torch.tensor([saved["joint_pos"]], dtype=torch.float32, device=key.data.joint_pos.device)
        velocity = torch.tensor([saved["joint_vel"]], dtype=torch.float32, device=key.data.joint_pos.device)
        key.write_joint_state_to_sim(position, velocity)
        key.set_joint_position_target(position)
    objects = env.scene_manager.get_objects(env_ids=[0])
    for name, saved in state["objects"].items():
        obj = objects.get(name)
        if obj is None:
            logger.warning("scene object %s is gone; it stays where the layout put it", name)
            continue
        if "particles" in saved:
            obj.set_particle_positions(np.asarray(saved["particles"]))
            continue
        # Isaac Sim's prims take tensors on the device they report theirs on.
        current, _ = obj.get_world_pose()

        def tensor(values: list[float]):
            return torch.tensor(values, dtype=torch.float32, device=getattr(current, "device", "cpu"))

        pose = saved["pose"]
        obj.set_world_pose(position=tensor(pose[:3]), orientation=tensor(pose[3:]))
        if "linear_velocity" in saved:
            obj.set_linear_velocity(tensor(saved["linear_velocity"]))
            obj.set_angular_velocity(tensor(saved["angular_velocity"]))
        if "joint_positions" in saved:
            obj.set_joint_positions(tensor(saved["joint_positions"]))
            obj.set_joint_velocities(tensor(saved["joint_velocities"]))
    env.take_action_cnt[0] = int(state["steps"])
    # An episode the task ended by succeeding stays ended; one that ran out of steps goes on if the step
    # limit now leaves room (it may have been raised since).
    succeeded = bool(state["end_flag"]) and bool(state["success"])
    env.success[0] = True
    env.end_flag[0] = succeeded or env.take_action_cnt[0] >= env.step_lim
    driver._last = {arm: dict(command) for arm, command in state.get("last_command", {}).items()}
    env.sim_step(render=True)


# -- helpers -----------------------------------------------------------------------------------


def _normalize_gripper(robot, value: float) -> float:
    low, high = robot.gripper_scale
    opening = (value - low) / (high - low)
    return opening if robot.gripper_move["sign"] == 1 else 1.0 - opening


def _list(value: Any) -> list[float]:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    return [float(x) for x in np.asarray(value).reshape(-1)]


def _vector(value: Any, size: int, what: str) -> list[float]:
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError):
        raise _Refused(f"{what} must be {size} numbers, got {value!r}.") from None
    if array.shape != (size,) or not np.isfinite(array).all():
        raise _Refused(f"{what} must be {size} finite numbers, got {value!r}.")
    return array.tolist()


def _fraction(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise _Refused(f"{what} must be a number from 0 to 1, got {value!r}.")
    return float(value)

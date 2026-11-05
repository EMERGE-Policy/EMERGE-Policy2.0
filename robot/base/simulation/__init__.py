"""The simulation's side of the robot: a driver, run on one clock behind the robot folder.

    RobotDriver      abstract: what a simulator implements                       driver
    SceneChoice      one scene a simulation can start from                        driver
    run_simulation   start a driver, serve the robot folder until done, close it  loop
    capture          take one snapshot from a driver, checking what it gives      capture
"""

from robot.base.simulation.driver import RobotDriver, SceneChoice
from robot.base.simulation.loop import run_simulation

__all__ = ["RobotDriver", "SceneChoice", "run_simulation"]

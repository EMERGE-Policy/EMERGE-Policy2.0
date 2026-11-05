"""RoboDojo (third_party/RoboDojo, Isaac Sim 5.1): its dual ARX X5 tasks behind the robot files.

    RoboDojoDriver   runs one RoboDojo task (python -m robot --driver robodojo ...)
    RoboDojoClient   RobotClient with helpers for the RoboDojo actions

Importing this package does not start Isaac Sim; RoboDojoDriver.start() does.
"""

from robot.robodojo.client import RoboDojoClient
from robot.robodojo.driver import RoboDojoDriver

__all__ = ["RoboDojoClient", "RoboDojoDriver"]

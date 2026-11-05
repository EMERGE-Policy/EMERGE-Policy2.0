"""The nodes' side of the robot: `RobotClient`, what a node uses to hold arms, send actions and read the
robot's state and observations through the conversation's robot folder."""

from robot.base.client.robot_client import RobotClient

__all__ = ["RobotClient"]

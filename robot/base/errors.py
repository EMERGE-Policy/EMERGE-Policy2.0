"""The errors of the robot layer, shared by clients, the simulation and drivers."""


class RobotError(Exception):
    """The robot files refuse a request, or a driver broke its contract."""


class ArmHeld(RobotError):
    """The arm is held by another node."""


class ArmBusy(RobotError):
    """The arm still has an action that has not ended."""


class CommandBusy(RobotError):
    """The previous command has not ended."""


class ManifestError(RobotError):
    """The manifest is not well formed, or does not match what the driver has."""


class BodyViolation(RobotError):
    """A body was asked to use something that is not its own: an arm, a camera, an action."""

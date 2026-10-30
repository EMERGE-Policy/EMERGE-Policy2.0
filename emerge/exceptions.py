class EmergeError(Exception):
    pass


class ConfigError(EmergeError):
    pass


class ModelError(EmergeError):
    pass


class ModelAPIModeError(ModelError):
    """The provider does not support the selected API mode."""


class ToolError(EmergeError):
    pass


class GraphValidationError(EmergeError):
    pass


class RegistryError(EmergeError):
    pass


class NodeError(EmergeError):
    pass


class RuntimeControlError(EmergeError):
    pass


class StorageError(EmergeError):
    pass


class CheckpointError(EmergeError):
    pass


class StopRequested(BaseException):
    """A node was asked to stop while blocked.

    A BaseException, like asyncio.CancelledError, so the `except Exception` in tool wrappers and in
    node code cannot turn a stop into an error result and keep running. Catch it by name to clean
    up, then re-raise.
    """


class WaitTimeout(EmergeError):
    """A `NodeContext` wait saw nothing it was waiting for before its timeout."""


class InputClosed(EmergeError):
    """An input will receive nothing more.

    An input closes when the node owning the output port it subscribes to ends, is removed, or the
    run ends. Replacing that node does not close it: the input waits for the new instance.
    """

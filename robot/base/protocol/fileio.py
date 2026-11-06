"""How a file of the robot folder is written, and how one that both sides write is changed.

**A file lands whole.** New content is written to a temporary file next to the old one, then renamed over
it, so a reader sees the old file or the new one and never half of either. Readers need no lock.

**A shared file is changed under a lock.** action.json and command.json are written by two processes, so
each change is a read-modify-write while holding an exclusive lock on a sibling `.<name>.lock` file.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from robot.base.errors import RobotError

# The process umask, read once (it can only be read by setting it).
_UMASK = os.umask(0)
os.umask(_UMASK)


def read_json(path: Path) -> Any:
    """The file's value, or None when there is no such file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return json.loads(text)


def write_bytes(path: Path, data: bytes) -> None:
    """Replace `path` with `data` in one rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        # mkstemp makes the file private (0600); give it the permissions any new file would get.
        os.fchmod(fd, 0o666 & ~_UMASK)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_text(path: Path, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def edit_json(path: Path) -> Iterator[dict[str, Any]]:
    """Read-modify-write a file holding one JSON object, under the file's lock.

    A missing file reads as {}. Nothing is written when the body raises, nor when the object is unchanged,
    so a look that changes nothing costs no write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(f".{path.name}.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        value = read_json(path)
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise RobotError(f"{path} does not hold a JSON object")
        before = json.dumps(value, sort_keys=True)
        yield value
        if json.dumps(value, sort_keys=True) != before:
            write_json(path, value)

"""Where things live in a workspace, and how the complex values are written down.

A workspace holds the settings every conversation shares, and one folder per conversation holding
everything that conversation has. The layout is the contract other packages rely on:

    config.json, AGENTS.md, USER.md, skills/        the settings, shared by every conversation
    sessions/<session_id>/                           one conversation, all of it:
        .emerge/state.db                                 its database
        trace.jsonl                                      its events outside any run
        memory/MEMORY.md, memory/asset/                  its memory
        code/<node_name>/v<version>.py                   node code generated in it
        runs/<run_id>/                                   a run's root, with trace.jsonl
        runs/<run_id>/graph.json                         the main agent's draft of the graph
        runs/<run_id>/nodes/<node_id>/<instance_id>/     one node instance's artifacts
        runs/<run_id>/nodes/<node_id>/<instance_id>/<port>/<key>/   what one `Ref` points at

Every path inside a conversation is derived from its folder, and every path the code hands out there
is relative to that folder, so moving a workspace or a conversation does not invalidate a stored
reference. How the files under a ref directory are arranged is the node's business; this module
stops at the key directory.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from emerge.graph.ports import Ref

_STATE_DIR = ".emerge"
_DB_NAME = "state.db"
_TRACE_FILE = "trace.jsonl"
_CODE_VERSION = re.compile(r"^v?(\d+)$")


# -- the workspace ----------------------------------------------------------


def sessions_dir(workspace: Path) -> Path:
    return workspace / "sessions"


def session_dir(workspace: Path, session_id: str) -> Path:
    """The folder of one conversation, which holds everything it has."""
    if not session_id or any(char in session_id for char in "/\\") or session_id in (".", ".."):
        raise ValueError(f"session id {session_id!r} cannot name a folder")
    return sessions_dir(workspace) / session_id


# -- inside a conversation --------------------------------------------------


def state_db_path(session_dir: Path) -> Path:
    return session_dir / _STATE_DIR / _DB_NAME


def trace_file(session_dir: Path, run_id: str | None) -> Path:
    """Where the events of one run go, or, without a run, the conversation's other events."""
    return (run_dir(session_dir, run_id) if run_id is not None else session_dir) / _TRACE_FILE


def runs_dir(session_dir: Path) -> Path:
    return session_dir / "runs"


def run_dir(session_dir: Path, run_id: str) -> Path:
    return runs_dir(session_dir) / run_id


def graph_file(session_dir: Path, run_id: str) -> Path:
    return run_dir(session_dir, run_id) / "graph.json"


def node_dir(session_dir: Path, run_id: str, node_id: str, instance_id: str) -> Path:
    return run_dir(session_dir, run_id) / "nodes" / node_id / instance_id


def ref_dir(session_dir: Path, run_id: str, ref: Ref) -> Path:
    """The directory behind one reference.

    The instance is part of the path, so an instance that replaced another writes the same key
    somewhere else and the two never overwrite each other.
    """
    return node_dir(session_dir, run_id, ref.producer, ref.producer_instance) / ref.port / ref.key


def memory_dir(session_dir: Path) -> Path:
    """Where the conversation's memory keeps its file and images."""
    return session_dir / "memory"


def memory_file(session_dir: Path) -> Path:
    return memory_dir(session_dir) / "MEMORY.md"


def memory_asset_dir(session_dir: Path) -> Path:
    return memory_dir(session_dir) / "asset"


def code_dir(session_dir: Path) -> Path:
    return session_dir / "code"


def code_path(session_dir: Path, name: str, version: str) -> Path:
    return code_dir(session_dir) / name / f"v{_version_number(version)}.py"


def ensure_run_dir(session_dir: Path, run_id: str) -> Path:
    directory = run_dir(session_dir, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def ensure_node_dir(session_dir: Path, run_id: str, node_id: str, instance_id: str) -> Path:
    directory = node_dir(session_dir, run_id, node_id, instance_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# -- memory images ----------------------------------------------------------


def copy_memory_asset(session_dir: Path, source: Path | str) -> str:
    """Copy an image into the memory's asset folder and return its path relative to the conversation.

    The file is named by the hash of its content, so copying the same image twice leaves one file.
    Copying rather than pointing at the original: a node may overwrite the file it produced, and
    the memory would silently start showing something else.
    """
    src = Path(source)
    data = src.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    directory = memory_asset_dir(session_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}{src.suffix.lower()}"
    if not target.exists():
        target.write_bytes(data)
    return target.relative_to(session_dir).as_posix()


# -- generated code ---------------------------------------------------------


def write_generated_code(session_dir: Path, name: str, version: str, source: str) -> Path:
    """Write one version of a generated node type, replacing that version's file if it exists."""
    path = code_path(session_dir, name, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def read_generated_code(session_dir: Path, name: str, version: str) -> str:
    return code_path(session_dir, name, version).read_text(encoding="utf-8")


def list_generated_names(session_dir: Path) -> tuple[str, ...]:
    """The generated node types of the conversation, by name."""
    directory = code_dir(session_dir)
    if not directory.is_dir():
        return ()
    return tuple(sorted(path.name for path in directory.iterdir() if path.is_dir()))


def list_generated_versions(session_dir: Path, name: str) -> tuple[str, ...]:
    """Every written version of one generated node type, oldest first.

    Numeric versions sort numerically, so v10 comes after v9; anything else sorts after the
    numbers, as text.
    """
    directory = code_dir(session_dir) / name
    if not directory.is_dir():
        return ()
    numbers: list[int] = []
    others: list[str] = []
    for path in directory.glob("v*.py"):
        match = _CODE_VERSION.match(path.stem)
        if match:
            numbers.append(int(match.group(1)))
        else:
            others.append(path.stem[1:])
    return tuple(str(number) for number in sorted(numbers)) + tuple(sorted(others))


def _version_number(version: str) -> str:
    """The number a version is written as: "v2" and "2" are the same file."""
    match = _CODE_VERSION.match(version)
    if match:
        return str(int(match.group(1)))
    if not version or any(char in version for char in "/\\"):
        raise ValueError(f"generated code version {version!r} cannot name a file")
    return version


# -- written values ---------------------------------------------------------


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def dump_model(model: BaseModel) -> str:
    """One stored value.

    No sort_keys: the order the provider gave its fields is part of what has to come back
    unchanged, and dumping the same model twice has to produce the same bytes.
    """
    return dump_json(model.model_dump(mode="json"))


_Model = TypeVar("_Model", bound=BaseModel)


def load_model(model: type[_Model], text: str) -> _Model:
    return model.model_validate_json(text)

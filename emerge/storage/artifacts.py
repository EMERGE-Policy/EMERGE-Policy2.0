"""Where things live in a workspace, and how the complex values are written down.

Every path here is derived from the workspace root, and every path the code agrees to hand out is
workspace-relative, so moving the workspace does not invalidate a stored reference. The layout is
the contract other packages rely on:

    runs/<run_id>/                                a run's root, with trace.jsonl
    runs/<run_id>/graph.json                      the main agent's draft of the graph
    runs/<run_id>/nodes/<node_id>/<instance_id>/  one node instance's artifacts
    runs/<run_id>/nodes/<node_id>/<instance_id>/<port>/<key>/   what one `Ref` points at
    code/<node_name>/v<version>.py                generated node code
    memory/MEMORY.md, memory/asset/               global memory
    sessions/<session_id>/memory/MEMORY.md, ...   one session's memory
    .emerge/state.db                              the database

How the files under a ref directory are arranged is the node's business; this module stops at the
key directory.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from emerge.graph.ports import Ref

GLOBAL_SCOPE = "global"

_STATE_DIR = ".emerge"
_DB_NAME = "state.db"
_CODE_VERSION = re.compile(r"^v?(\d+)$")


# -- paths ------------------------------------------------------------------


def state_db_path(workspace: Path) -> Path:
    return workspace / _STATE_DIR / _DB_NAME


def runs_dir(workspace: Path) -> Path:
    return workspace / "runs"


def run_dir(workspace: Path, run_id: str) -> Path:
    return runs_dir(workspace) / run_id


def graph_file(workspace: Path, run_id: str) -> Path:
    return run_dir(workspace, run_id) / "graph.json"


def node_dir(workspace: Path, run_id: str, node_id: str, instance_id: str) -> Path:
    return run_dir(workspace, run_id) / "nodes" / node_id / instance_id


def ref_dir(workspace: Path, run_id: str, ref: Ref) -> Path:
    """The directory behind one reference.

    The instance is part of the path, so an instance that replaced another writes the same key
    somewhere else and the two never overwrite each other.
    """
    return node_dir(workspace, run_id, ref.producer, ref.producer_instance) / ref.port / ref.key


def memory_dir(workspace: Path, scope: str) -> Path:
    """Where one memory scope keeps its file and images; scope is "global" or a session id."""
    if scope == GLOBAL_SCOPE:
        return workspace / "memory"
    return workspace / "sessions" / scope / "memory"


def memory_file(workspace: Path, scope: str) -> Path:
    return memory_dir(workspace, scope) / "MEMORY.md"


def memory_asset_dir(workspace: Path, scope: str) -> Path:
    return memory_dir(workspace, scope) / "asset"


def code_path(workspace: Path, name: str, version: str) -> Path:
    return workspace / "code" / name / f"v{_version_number(version)}.py"


def ensure_run_dir(workspace: Path, run_id: str) -> Path:
    directory = run_dir(workspace, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def ensure_node_dir(workspace: Path, run_id: str, node_id: str, instance_id: str) -> Path:
    directory = node_dir(workspace, run_id, node_id, instance_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def ensure_memory_dir(workspace: Path, scope: str) -> Path:
    directory = memory_dir(workspace, scope)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def remove_session_memory(workspace: Path, session_id: str) -> None:
    """Delete one session's memory file and images; the rest of the session directory is untouched."""
    shutil.rmtree(workspace / "sessions" / session_id, ignore_errors=True)


# -- memory images ----------------------------------------------------------


def copy_memory_asset(workspace: Path, scope: str, source: Path | str) -> str:
    """Copy an image into one scope's memory directory and return its workspace-relative path.

    The file is named by the hash of its content, so copying the same image twice leaves one file.
    Copying rather than pointing at the original: a node may overwrite the file it produced, and
    the memory would silently start showing something else.
    """
    src = Path(source)
    data = src.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    directory = memory_asset_dir(workspace, scope)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}{src.suffix.lower()}"
    if not target.exists():
        target.write_bytes(data)
    return target.relative_to(workspace).as_posix()


# -- generated code ---------------------------------------------------------


def write_generated_code(workspace: Path, name: str, version: str, source: str) -> Path:
    """Write one version of a generated node type, replacing that version's file if it exists."""
    path = code_path(workspace, name, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def read_generated_code(workspace: Path, name: str, version: str) -> str:
    return code_path(workspace, name, version).read_text(encoding="utf-8")


def list_generated_versions(workspace: Path, name: str) -> tuple[str, ...]:
    """Every written version of one generated node type, oldest first.

    Numeric versions sort numerically, so v10 comes after v9; anything else sorts after the
    numbers, as text.
    """
    directory = workspace / "code" / name
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


def load_model[T: BaseModel](model: type[T], text: str) -> T:
    return model.model_validate_json(text)

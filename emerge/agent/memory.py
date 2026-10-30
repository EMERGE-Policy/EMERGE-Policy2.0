"""The main agent's memory: a global and a per-session MEMORY.md, each with its own asset/ folder.

Global memory (`memory/MEMORY.md`, `memory/asset/`) is seen by every session; session memory
(`sessions/<session_id>/memory/MEMORY.md` and `asset/`) only by its own session, and goes when the
session is deleted. The files are the memory; SQLite only indexes them (full-text entries, the
images each scope holds, the digest of the file last indexed), so a file edited by hand or with the
file tools is picked up the next time it is looked at.

A memory file is cut into entries for the index:

- a top-level list item (a line starting `- `, `* `, `+ ` or `1. ` at column 0) together with the
  lines indented under it, blank lines inside included;
- a paragraph outside any list (lines between blank lines);
- a non-indented line right after a list item belongs to that item (Markdown's lazy continuation);
- a fenced code block is never split; headings and horizontal rules separate entries and are not
  entries themselves.

An image kept in memory is copied into the scope's asset/ (named by content hash, so the original
being overwritten later changes nothing) and referenced by its workspace path in the file. An image
the file no longer mentions is dropped from the index and deleted.

This is the simple version: memory is written and searched on request, nothing is extracted or
merged automatically.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from emerge.storage.artifacts import GLOBAL_SCOPE, copy_memory_asset, memory_file
from emerge.storage.base import MemoryAsset, MemoryHit, Storage

_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_HEADING = re.compile(r"^#{1,6}(?:\s|$)")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True, slots=True)
class MemoryImage:
    """An image to keep with a memory entry: a file inside the workspace and one line about it."""

    path: Path
    description: str = ""
    # Where it came from, as the caller named it (a workspace path or a reference directory).
    source: str = ""


@dataclass(frozen=True, slots=True)
class MemoryResult:
    """One entry found by a search, with the images it mentions."""

    scope: str
    text: str
    images: tuple[MemoryAsset, ...] = ()


def split_entries(text: str) -> list[str]:
    """Cut a MEMORY.md into index entries; see the module docstring for the rules."""
    entries: list[str] = []
    current: list[str] = []
    blank = False
    fence: str | None = None

    def close() -> None:
        entry = "\n".join(current).strip()
        if entry:
            entries.append(entry)
        current.clear()

    for line in text.splitlines():
        if fence is not None:
            current.append(line)
            if line.strip().startswith(fence):
                fence = None
            continue
        if not line.strip():
            blank = True
            continue
        if _HEADING.match(line) or _RULE.match(line):
            close()
            blank = False
            continue
        if _ITEM.match(line):
            close()
            current.append(line)
        elif line[:1] in (" ", "\t") and current:
            if blank:
                current.append("")
            current.append(line)
        elif blank:
            close()
            current.append(line)
        else:
            # The next line of a paragraph, or a lazy continuation of a list item.
            current.append(line)
        opening = _FENCE.match(line)
        if opening is not None:
            fence = opening.group(1)
        blank = False
    close()
    return entries


def file_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MemoryStore:
    """The two memory scopes one session can use: global, and its own."""

    def __init__(self, storage: Storage, workspace: Path, session_id: str) -> None:
        self.storage = storage
        self.workspace = workspace
        self.session_id = session_id

    @property
    def scopes(self) -> tuple[str, str]:
        return (GLOBAL_SCOPE, self.session_id)

    def scope(self, name: str) -> str:
        """The storage scope for "global" or "session"."""
        if name == "global":
            return GLOBAL_SCOPE
        if name == "session":
            return self.session_id
        raise ValueError(f"memory scope must be 'global' or 'session', not {name!r}")

    def path(self, scope: str) -> Path:
        return memory_file(self.workspace, scope)

    def read(self, scope: str) -> str:
        path = self.path(scope)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    async def write(self, scope: str, text: str, images: Sequence[MemoryImage] = ()) -> str:
        """Append one entry to the scope's MEMORY.md, its images copied into asset/; returns the entry."""
        lines = text.strip().splitlines() or [""]
        entry = [f"- {lines[0]}", *(f"  {line}" if line.strip() else "" for line in lines[1:])]
        for image in images:
            relative = copy_memory_asset(self.workspace, scope, image.path)
            digest = hashlib.sha256((self.workspace / relative).read_bytes()).hexdigest()
            await self.storage.add_memory_asset(
                scope, relative, digest, description=image.description, source=image.source
            )
            entry.append(f"  ![{image.description.replace(']', ')')}]({relative})")
        block = "\n".join(entry)
        path = self.path(scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.read(scope)
        path.write_text(existing + _gap(existing) + block + "\n", encoding="utf-8")
        await self.refresh(scope)
        return block

    async def refresh(self, scope: str) -> bool:
        """Re-index the scope if its file changed since it was last indexed; returns whether it did."""
        text = self.read(scope)
        digest = file_digest(text)
        if digest == await self.storage.memory_file_digest(scope):
            return False
        await self.storage.replace_memory_entries(scope, split_entries(text), digest)
        gone = [asset for asset in await self.storage.memory_assets(scope) if asset.path not in text]
        if gone:
            await self.storage.remove_memory_assets(scope, [asset.path for asset in gone])
            for asset in gone:
                (self.workspace / asset.path).unlink(missing_ok=True)
        return True

    async def refresh_all(self) -> None:
        for scope in self.scopes:
            await self.refresh(scope)

    async def search(self, query: str, *, limit: int = 10) -> list[MemoryResult]:
        """Entries matching `query` in global and this session's memory, best first."""
        await self.refresh_all()
        hits: tuple[MemoryHit, ...] = await self.storage.search_memory(query, self.scopes, limit=limit)
        assets = {scope: await self.storage.memory_assets(scope) for scope in {hit.scope for hit in hits}}
        return [
            MemoryResult(
                scope=hit.scope,
                text=hit.text,
                images=tuple(asset for asset in assets[hit.scope] if asset.path in hit.text),
            )
            for hit in hits
        ]


def _gap(existing: str) -> str:
    """What goes between the file so far and a new entry: one blank line, so each entry stays its own."""
    if not existing or existing.endswith("\n\n"):
        return ""
    return "\n" if existing.endswith("\n") else "\n\n"

"""The main agent's memory: one MEMORY.md per conversation, with its images in asset/ beside it.

Both live in the conversation's folder (`memory/MEMORY.md`, `memory/asset/`), seen by that
conversation only, and go with it. The files are the memory; SQLite only indexes them (full-text
entries, the images the file keeps, the digest of the file last indexed), so a file edited by hand
or with the file tools is picked up the next time it is looked at.

A memory file is cut into entries for the index:

- a top-level list item (a line starting `- `, `* `, `+ ` or `1. ` at column 0) together with the
  lines indented under it, blank lines inside included;
- a paragraph outside any list (lines between blank lines);
- a non-indented line right after a list item belongs to that item (Markdown's lazy continuation);
- a fenced code block is never split; headings and horizontal rules separate entries and are not
  entries themselves.

An image kept in memory is copied into asset/ (named by content hash, so the original being
overwritten later changes nothing) and referenced in the file by its path in the conversation's
folder. An image the file no longer mentions is dropped from the index and deleted.

This is the simple version: memory is written and searched on request, nothing is extracted or
merged automatically.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from emerge.storage.artifacts import copy_memory_asset, memory_file
from emerge.storage.base import MemoryAsset, MemoryHit, Storage

_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_HEADING = re.compile(r"^#{1,6}(?:\s|$)")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True, slots=True)
class MemoryImage:
    """An image to keep with a memory entry: a file inside the conversation's folder and one line about it."""

    path: Path
    description: str = ""
    # Where it came from, as the caller named it (a path in the conversation or a reference directory).
    source: str = ""


@dataclass(frozen=True, slots=True)
class MemoryResult:
    """One entry found by a search, with the images it mentions."""

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
    """The memory of one conversation: its MEMORY.md, its images, and their index in its storage."""

    def __init__(self, storage: Storage, session_dir: Path) -> None:
        self.storage = storage
        self.session_dir = session_dir

    @property
    def path(self) -> Path:
        return memory_file(self.session_dir)

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.is_file() else ""

    async def write(self, text: str, images: Sequence[MemoryImage] = ()) -> str:
        """Append one entry to MEMORY.md, its images copied into asset/; returns the entry."""
        lines = text.strip().splitlines() or [""]
        entry = [f"- {lines[0]}", *(f"  {line}" if line.strip() else "" for line in lines[1:])]
        for image in images:
            relative = copy_memory_asset(self.session_dir, image.path)
            digest = hashlib.sha256((self.session_dir / relative).read_bytes()).hexdigest()
            await self.storage.add_memory_asset(relative, digest, description=image.description, source=image.source)
            entry.append(f"  ![{image.description.replace(']', ')')}]({relative})")
        block = "\n".join(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.read()
        self.path.write_text(existing + _gap(existing) + block + "\n", encoding="utf-8")
        await self.refresh()
        return block

    async def refresh(self) -> bool:
        """Re-index MEMORY.md if it changed since it was last indexed; returns whether it did."""
        text = self.read()
        digest = file_digest(text)
        if digest == await self.storage.memory_file_digest():
            return False
        await self.storage.replace_memory_entries(split_entries(text), digest)
        gone = [asset for asset in await self.storage.memory_assets() if asset.path not in text]
        if gone:
            await self.storage.remove_memory_assets([asset.path for asset in gone])
            for asset in gone:
                (self.session_dir / asset.path).unlink(missing_ok=True)
        return True

    async def search(self, query: str, *, limit: int = 10) -> list[MemoryResult]:
        """Entries matching `query`, best first."""
        await self.refresh()
        hits: tuple[MemoryHit, ...] = await self.storage.search_memory(query, limit=limit)
        assets = await self.storage.memory_assets() if hits else ()
        return [
            MemoryResult(text=hit.text, images=tuple(asset for asset in assets if asset.path in hit.text))
            for hit in hits
        ]


def _gap(existing: str) -> str:
    """What goes between the file so far and a new entry: one blank line, so each entry stays its own."""
    if not existing or existing.endswith("\n\n"):
        return ""
    return "\n" if existing.endswith("\n") else "\n\n"

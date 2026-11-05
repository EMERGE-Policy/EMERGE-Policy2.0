"""Writing and searching memory: the conversation's own MEMORY.md, in its folder.

No other conversation's memory can be read or written here.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from emerge.agent.memory import MemoryImage
from emerge.agent.tool import Tool, tool
from emerge.agent.tools import ToolContext
from emerge.agent.tools.files import IMAGE_SUFFIXES, resolve_path
from emerge.exceptions import ToolError


class ImageToKeep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # An image file in the conversation's folder, or a reference directory holding exactly one image.
    path: str
    # One line saying what the image shows.
    description: str


def make_tools(ctx: ToolContext) -> list[Tool]:
    memory = ctx.memory

    @tool
    async def write_memory(text: str, images: list[ImageToKeep] | None = None) -> str:
        """Keep something in memory as one entry of MEMORY.md, optionally with images.

        Images are copied into the memory's asset folder, so later changes to the original file do
        not change what memory shows.

        Args:
            text: The entry: one fact, preference or lesson, written so it makes sense on its own later.
            images: Images to keep with it: the path of an image file, or a node's reference
                directory (runs/<run>/nodes/<node>/<instance>/<port>/<key>/) holding one image, each
                with a one-line description.
        """
        if not text.strip():
            raise ToolError("the entry is empty")
        kept = [MemoryImage(_image_file(ctx, image.path), image.description.strip(), image.path) for image in images or ()]
        block = await memory.write(text, kept)
        return f"Written to {ctx.relative(memory.path)}:\n{block}"

    @tool
    async def search_memory(query: str, limit: int = 10) -> str:
        """Search memory by keywords (words or Chinese text).

        Returns matching entries, best first, with the paths of the images they keep; open an image
        with read_file when you need to see it.

        Args:
            query: Keywords; every one must appear in an entry.
            limit: The most entries to return.
        """
        results = await memory.search(query, limit=max(1, limit))
        if not results:
            return f"No memory entry matches {query!r}."
        lines = []
        for result in results:
            lines.append(result.text)
            lines += [f"    image {asset.path}: {asset.description}" for asset in result.images]
        return "\n".join(lines)

    return [write_memory, search_memory]


def _image_file(ctx: ToolContext, path: str) -> Path:
    target = resolve_path(ctx.session_dir, path)
    if target.is_dir():
        images = sorted(p for p in target.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
        if len(images) != 1:
            raise ToolError(f"{path} holds {len(images)} images; name the image file itself")
        target = images[0]
    if not target.is_file():
        raise ToolError(f"{path} does not exist")
    if target.suffix.lower() not in IMAGE_SUFFIXES:
        raise ToolError(f"{path} is not an image ({', '.join(sorted(IMAGE_SUFFIXES))})")
    return target

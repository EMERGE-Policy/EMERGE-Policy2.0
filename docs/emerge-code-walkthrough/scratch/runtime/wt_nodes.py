"""Tiny nodes for the runtime walkthrough experiments (not part of the repository)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from pydantic import BaseModel

from emerge.events import Event
from emerge.exceptions import StopRequested
from emerge.graph import Node, Outcome, Port


def mark(ctx: Any, name: str, **data: Any) -> None:
    (ctx.artifacts_dir / f"{name}.json").write_text(json.dumps({"t": time.time(), **data}))


class Looper(Node):
    name = "wt_looper"
    outputs = (Port(name="out", type_tag="text"),)
    outcomes = ("done",)

    async def run(self, ctx: Any) -> Outcome:
        while await ctx.wait_for_run_permission():
            await asyncio.sleep(0.05)
        return Outcome("done", "asked to stop")


class Step(Node):
    name = "wt_step"
    outcomes = ("done",)

    async def run(self, ctx: Any) -> Outcome:
        return Outcome("done")


class SlowSetup(Node):
    name = "wt_slow_setup"
    outcomes = ("done",)

    class Params(BaseModel):
        setup: float = 2.0

    async def setup(self, ctx: Any) -> None:
        await asyncio.sleep(self.params.setup)
        mark(ctx, "setup_done")

    async def run(self, ctx: Any) -> Outcome:
        mark(ctx, "run_entered")
        while await ctx.wait_for_run_permission():
            await asyncio.sleep(0.05)
        return Outcome("done")


class AskAfterStop(Node):
    name = "wt_ask_after_stop"
    outcomes = ("done",)

    async def run(self, ctx: Any) -> Outcome:
        while await ctx.wait_for_run_permission():
            await asyncio.sleep(0.05)
        try:
            await ctx.ask_main_agent("still there?")
        except StopRequested:
            mark(ctx, "ask_raised_stop")
            raise
        return Outcome("done")


class Spoofer(Node):
    name = "wt_spoofer"
    outcomes = ("done",)

    async def run(self, ctx: Any) -> Outcome:
        ctx.emitter.emit_nowait(Event(type="node.state", payload={"state": "finished", "spoofed": True}))
        await asyncio.sleep(0.2)
        return Outcome("done")


NODES: tuple[type[Node], ...] = (Looper, Step, SlowSetup, AskAfterStop, Spoofer)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class Forker(Node):
    name = "wt_forker"
    outcomes = ("done",)

    async def run(self, ctx: Any) -> Outcome:
        import multiprocessing

        child = multiprocessing.get_context("fork").Process(target=_sleep, args=(6.0,), daemon=True)
        child.start()
        mark(ctx, "forked", pid=child.pid)
        return Outcome("done")


NODES = (*NODES, Forker)


class _BadClient:
    async def aclose(self) -> None:
        raise RuntimeError("aclose failed")


class BadClose(Node):
    name = "wt_bad_close"
    outcomes = ("done",)

    async def run(self, ctx: Any) -> Outcome:
        ctx._model_client = _BadClient()
        return Outcome("done")


class SyncLoop(Node):
    name = "wt_sync_loop"
    outcomes = ("done",)

    async def run(self, ctx: Any) -> Outcome:
        while await ctx.wait_for_run_permission():
            time.sleep(0.05)  # blocking work between checkpoints, no await that yields
        return Outcome("done")

    async def teardown(self, ctx: Any) -> None:
        mark(ctx, "teardown")


NODES = (*NODES, BadClose, SyncLoop)

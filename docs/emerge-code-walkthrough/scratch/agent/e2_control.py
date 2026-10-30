"""Scratch: Controller.execute with fake agent/runtime: unknown action, no run, runtime error."""
import asyncio
from types import SimpleNamespace

from emerge.agent.control import Controller, TurnControl
from emerge.exceptions import RuntimeControlError


class FakeRuntime:
    def __init__(self, status="running", fail=None):
        self.calls = []
        self.status = status
        self.fail = fail

    def runs(self):
        return (SimpleNamespace(run_id="r1", status=self.status),)

    async def _do(self, name, run_id, node_id):
        self.calls.append((name, run_id, node_id))
        if self.fail:
            raise RuntimeControlError(self.fail)

    async def pause(self, run_id, node_id=None):
        await self._do("pause", run_id, node_id)

    async def resume(self, run_id, node_id=None):
        await self._do("resume", run_id, node_id)

    async def stop(self, run_id, node_id=None):
        await self._do("stop", run_id, node_id)


def agent(runtime, run_id="r1"):
    notes = []
    a = SimpleNamespace(turns=TurnControl(), current_run_id=run_id, runtime=runtime, notes=notes)
    a.note_control = lambda text, run_id: notes.append((text, run_id))
    return a


async def main():
    for label, rt, run_id, action, node in [
        ("unknown action 'foo'", FakeRuntime(), "r1", "foo", None),
        ("unknown action 'Pause' (case)", FakeRuntime(), "r1", "Pause", "n1"),
        ("interrupt, no turn", FakeRuntime(), "r1", "interrupt", "n1"),
        ("pause, no current run", FakeRuntime(), None, "pause", None),
        ("pause, run ended", FakeRuntime(status="finished"), "r1", "pause", None),
        ("pause, run closed", FakeRuntime(status="closed"), "r1", "pause", None),
        ("stop node", FakeRuntime(), "r1", "stop", "n1"),
        ("stop whole run", FakeRuntime(), "r1", "stop", None),
        ("resume whole run", FakeRuntime(), "r1", "resume", None),
        ("runtime raises", FakeRuntime(fail="no node 'zz'"), "r1", "pause", "zz"),
    ]:
        a = agent(rt, run_id)
        result = await Controller(a).execute(action, node)
        print(f"{label}: runtime calls={rt.calls} notes={a.notes}\n   -> {result}")


asyncio.run(main())


import asyncio
from emerge.graph import Node, Outcome


class GenEcho(Node):
    name = "gen_echo"
    outcomes = ("done",)

    async def run(self, ctx):
        await ctx.emit_event({"name": "gen_version", "version": '2', "instance": ctx.instance_id})
        while await ctx.wait_for_run_permission():
            await asyncio.sleep(0.05)
        return Outcome("done")

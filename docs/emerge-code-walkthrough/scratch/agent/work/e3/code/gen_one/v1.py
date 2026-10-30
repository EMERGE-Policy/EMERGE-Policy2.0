
from emerge.graph import Node, Outcome
class Gen(Node):
    name = "gen_one"
    description = "generated"
    outcomes = ("done",)
    async def run(self, ctx):
        return Outcome("done", "ok")

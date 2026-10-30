import asyncio, os, shutil, sys
from pathlib import Path
from emerge.storage import open_storage
ROOT = Path(__file__).parent / "tmp"
async def main():
    ws = ROOT / "mem2"; shutil.rmtree(ws, ignore_errors=True)
    st = await open_storage(ws)
    try:
        await st.replace_memory_entries("global", ["机器人的手臂很灵活", "hello world"], "d")
        for q in ["臂械机", "机臂", "hello !", "hello", "world hello"]:
            print(repr(q), [(h.position) for h in await st.search_memory(q, ["global"])])
    finally:
        await st.aclose()
try:
    asyncio.run(main())
finally:
    sys.stdout.flush(); os._exit(0)

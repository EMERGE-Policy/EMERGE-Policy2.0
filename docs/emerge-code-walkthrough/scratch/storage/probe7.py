"""Cancel a task while it awaits COMMIT inside SqliteStorage._transaction."""
import asyncio, os, shutil, sqlite3, sys, time
from pathlib import Path
from emerge.storage import open_storage

ROOT = Path(__file__).parent / "tmp"
SLOW = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 20000000) SELECT count(*) FROM c"

async def main():
    ws = ROOT / "cancel_commit"
    shutil.rmtree(ws, ignore_errors=True)
    st = await open_storage(ws)
    s = await st.create_session(title="orig")
    real_commit = st._conn.commit
    async def commit():
        asyncio.ensure_future(st._conn.execute(SLOW))   # keeps the worker thread busy
        await asyncio.sleep(0)
        return await real_commit()
    st._conn.commit = commit
    task = asyncio.create_task(st.set_system_snapshot(s.id, "NEW"))
    await asyncio.sleep(0.3)
    task.cancel()
    r = await asyncio.gather(task, return_exceptions=True)
    print("caller saw:", type(r[0]).__name__, "| lock free:", not st._lock.locked())
    st._conn.commit = real_commit
    await asyncio.sleep(0.1)
    print("system after cancel (read through storage):", (await st.get_session(s.id)).system)
    await st.aclose()
    raw = sqlite3.connect(ws / ".emerge" / "state.db")
    print("system in db file:", raw.execute("SELECT system FROM sessions").fetchone())

try:
    asyncio.run(main())
finally:
    sys.stdout.flush(); os._exit(0)

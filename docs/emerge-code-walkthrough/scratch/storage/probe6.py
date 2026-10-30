"""aclose() while another task is inside a transaction; page size and WAL pragmas."""
import asyncio, os, shutil, sqlite3, sys
from pathlib import Path
from emerge.storage import open_storage

ROOT = Path(__file__).parent / "tmp"

async def main():
    ws = ROOT / "close"
    shutil.rmtree(ws, ignore_errors=True)
    st = await open_storage(ws)
    for p in ("page_size", "wal_autocheckpoint", "journal_size_limit", "synchronous", "foreign_keys", "busy_timeout"):
        row = await (await st._conn.execute(f"PRAGMA {p}")).fetchone()
        print(p, row[0])
    s = await st.create_session(title="orig")

    async def writer():
        try:
            async with st._transaction():
                await st._conn.execute("UPDATE sessions SET title='new' WHERE id=?", (s.id,))
                await asyncio.sleep(0.5)
                await st._conn.execute("UPDATE sessions SET status='turn_open' WHERE id=?", (s.id,))
            return "committed"
        except BaseException as e:
            return f"{type(e).__name__}: {e}"

    w = asyncio.create_task(writer())
    await asyncio.sleep(0.1)
    try:
        await st.aclose()
        closed = "aclose ok"
    except BaseException as e:
        closed = f"aclose {type(e).__name__}: {e}"
    print(closed, "| writer:", await w)
    raw = sqlite3.connect(ws / ".emerge" / "state.db")
    print("title in db:", raw.execute("SELECT title, status FROM sessions").fetchall())
    raw.close()

try:
    asyncio.run(main())
finally:
    sys.stdout.flush(); os._exit(0)

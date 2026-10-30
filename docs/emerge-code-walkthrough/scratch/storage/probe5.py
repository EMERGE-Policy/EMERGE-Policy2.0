"""Cancellation inside a transaction, and the gap between a write and its read-back."""

import asyncio
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from emerge.storage import open_storage

ROOT = Path(__file__).parent / "tmp"


async def main() -> None:
    ws = ROOT / "conc"
    shutil.rmtree(ws, ignore_errors=True)
    st = await open_storage(ws)
    try:
        s = await st.create_session(title="orig")

        # 1. cancel inside a transaction
        async def slow_write():
            async with st._transaction():
                await st._conn.execute("UPDATE sessions SET title='half' WHERE id=?", (s.id,))
                await asyncio.sleep(5)

        task = asyncio.create_task(slow_write())
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        print("after cancel: title", (await st.get_session(s.id)).title, "| lock free", not st._lock.locked())

        # 2. two concurrent updates: the first one's return value shows the second write
        r1, r2 = await asyncio.gather(st.update_session(s.id, title="A"), st.update_session(s.id, title="B"))
        print("update(A) returned title", r1.title, "| update(B) returned", r2.title)

        # 3. delete slipping in between a write and its read-back
        s2 = await st.create_session(title="victim")

        async def hold():
            async with st._transaction():
                await asyncio.sleep(0.5)

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0.05)
        deleter = asyncio.create_task(st.delete_session(s2.id))
        await asyncio.sleep(0.05)
        updater = asyncio.create_task(st.update_session(s2.id, title="x"))
        res = await asyncio.gather(holder, deleter, updater, return_exceptions=True)
        print("delete between write and read-back:", [type(x).__name__ if isinstance(x, BaseException) else type(x).__name__ for x in res])

        # 4. delete_session check-then-act: a run created after the check survives as an orphan
        s3 = await st.create_session(title="orphan")

        async def hold2():
            async with st._transaction():
                await asyncio.sleep(0.5)

        holder = asyncio.create_task(hold2())
        await asyncio.sleep(0.05)
        deleter = asyncio.create_task(st.delete_session(s3.id))  # queues runs_of_session
        await asyncio.sleep(0.05)
        creator = asyncio.create_task(st.create_run(s3.id))  # queues after the check
        res = await asyncio.gather(holder, deleter, creator, return_exceptions=True)
        print("check-then-act:", [type(x).__name__ for x in res])
        raw = sqlite3.connect(ws / ".emerge" / "state.db")
        print("runs left for deleted session:", raw.execute("SELECT status FROM runs WHERE session_id=?", (s3.id,)).fetchall(),
              "| session row:", raw.execute("SELECT COUNT(*) FROM sessions WHERE id=?", (s3.id,)).fetchone())
        raw.close()
    finally:
        await st.aclose()


try:
    asyncio.run(main())
finally:
    sys.stdout.flush()
    os._exit(0)

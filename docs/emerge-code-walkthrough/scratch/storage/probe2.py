"""open() failure paths and cross-connection locking. Usage: probe2.py <case>"""

import asyncio
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from emerge.exceptions import StorageError
from emerge.storage import open_storage, artifacts

ROOT = Path(__file__).parent / "tmp"


def make_db(name: str, version: int, journal: str = "delete") -> Path:
    ws = ROOT / name
    shutil.rmtree(ws, ignore_errors=True)
    (ws / ".emerge").mkdir(parents=True)
    raw = sqlite3.connect(ws / ".emerge" / "state.db")
    raw.execute(f"PRAGMA journal_mode={journal}")
    raw.execute("CREATE TABLE marker (x)")
    raw.execute(f"PRAGMA user_version={version}")
    raw.commit()
    raw.close()
    return ws


def journal_of(ws: Path) -> str:
    raw = sqlite3.connect(ws / ".emerge" / "state.db")
    try:
        return raw.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        raw.close()


async def caught(version: int) -> None:
    ws = make_db(f"v{version}", version)
    print("journal before:", journal_of(ws))
    try:
        await open_storage(ws)
    except StorageError as e:
        print("StorageError:", e)
    print("journal after:", journal_of(ws))


async def uncaught(version: int) -> None:
    ws = make_db(f"v{version}u", version)
    await open_storage(ws)


async def foreign_v0() -> None:
    # user_version 0 but a table of the same name with other columns
    ws = ROOT / "foreign"
    shutil.rmtree(ws, ignore_errors=True)
    (ws / ".emerge").mkdir(parents=True)
    raw = sqlite3.connect(ws / ".emerge" / "state.db")
    raw.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
    raw.commit()
    raw.close()
    try:
        st = await open_storage(ws)
        print("opened; user_version", sqlite3.connect(ws / ".emerge" / "state.db").execute("PRAGMA user_version").fetchone())
        await st.aclose()
    except BaseException as e:  # noqa: BLE001
        print(f"{type(e).__module__}.{type(e).__name__}: {e}")


async def busy() -> None:
    ws = ROOT / "busy"
    shutil.rmtree(ws, ignore_errors=True)
    a = await open_storage(ws)
    b = await open_storage(ws)
    s = await a.create_session()

    async def hold():
        async with a._transaction():
            await a._conn.execute("UPDATE sessions SET title='held' WHERE id=?", (s.id,))
            await asyncio.sleep(8)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.2)
    t0 = time.monotonic()
    try:
        await b.get_session(s.id)
        print("read via second connection returned after", round(time.monotonic() - t0, 2), "s")
    except BaseException as e:  # noqa: BLE001
        print(f"second connection read: {type(e).__module__}.{type(e).__name__}: {e} after {round(time.monotonic() - t0, 2)} s")
    await holder
    await a.aclose()
    await b.aclose()


async def port_buffer_zero() -> None:
    try:
        await open_storage(ROOT / "pb0", port_buffer=0)
    except StorageError as e:
        print("StorageError:", e, "| workspace created?", (ROOT / "pb0").exists())


case = sys.argv[1]
coro = {
    "caught1": lambda: caught(1),
    "caught3": lambda: caught(3),
    "uncaught1": lambda: uncaught(1),
    "foreign": foreign_v0,
    "busy": busy,
    "pb0": port_buffer_zero,
}[case]()
asyncio.run(coro)
print("main returned")

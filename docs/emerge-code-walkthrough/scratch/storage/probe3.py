"""Memory index / FTS5 query behaviour on a throwaway database."""

import asyncio
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from emerge.storage import open_storage
from emerge.storage.sqlite import _index_text, _match_expression

ROOT = Path(__file__).parent / "tmp"


async def main() -> None:
    ws = ROOT / "mem"
    shutil.rmtree(ws, ignore_errors=True)
    st = await open_storage(ws)
    try:
        entries = [
            "UR5机械臂抓取红色方块",
            "用户偏好，颜色是蓝色",
            "Robots are great",
            "café au lait",
            "hello world again",
            "版本v2发布",
            "カタカナとひらがな",
            "한국어 문장",
            "符号 *** 和 AND OR NOT",
        ]
        await st.replace_memory_entries("global", entries, "d0")
        await st.replace_memory_entries("S1", ["session only 偏好"], "d1")
        for text in ["UR5机械臂", "用户偏好，颜色", "版本v2发布"]:
            print("index_text", repr(text), "->", repr(_index_text(text)))
        queries = [
            "机械臂", "UR5机械臂", "机械臂UR5", "UR5 机械臂", "偏好", "偏好，颜色", "偏好 颜色", "偏好。", "（偏好）",
            "版本v2", "v2发布", "robot", "ROBOTS", "cafe", "hello,world", "world,hello", "hello world",
            "***", "-", '"', "。", "AND", "NOT", "OR", "NEAR", "(", "a*", "^hello", "", "   ",
            "カタカナ", "한국", "\x00", "ab\ud800",
        ]
        for q in queries:
            try:
                expr = _match_expression(q)
            except Exception as e:  # noqa: BLE001
                expr = f"<{type(e).__name__}>"
            try:
                hits = await st.search_memory(q, ["global", "S1"])
                res = [(h.scope, h.position) for h in hits]
            except BaseException as e:  # noqa: BLE001
                res = f"{type(e).__module__}.{type(e).__name__}: {e}"
            print(f"q={q!r:16} expr={expr!r:40} -> {res}")
        print("scopes=[]", await st.search_memory("偏好", []))
        print("scope S1 only", [(h.scope, h.position) for h in await st.search_memory("偏好", ["S1"])])
        print("limit=1", len(await st.search_memory("偏好", ["global", "S1"], limit=1)))
        print("limit=0", len(await st.search_memory("偏好", ["global", "S1"], limit=0)))
        print("limit=-1", len(await st.search_memory("偏好", ["global", "S1"], limit=-1)))
        # replace: old entries gone
        await st.replace_memory_entries("global", ["新的条目"], "d2")
        print("after replace old 机械臂", await st.search_memory("机械臂", ["global"]))
        print("after replace new", [(h.scope, h.position, h.text) for h in await st.search_memory("条目", ["global"])])
        await st.replace_memory_entries("global", [], "d3")
        print("empty entries -> digest", await st.memory_file_digest("global"), "hits", await st.search_memory("条目", ["global"]))
        print("digest never indexed", await st.memory_file_digest("never"))
        # assets
        a = await st.add_memory_asset("global", "memory/asset/x.png", "h1", description="d", source="s")
        b = await st.add_memory_asset("global", "memory/asset/x.png", "h2")
        print("asset upsert", [(m.path, m.digest, m.description, m.source) for m in await st.memory_assets("global")])
        print("remove []", await st.remove_memory_assets("global", []))
        print("remove other scope", await st.remove_memory_assets("S1", ["memory/asset/x.png"]))
        print("remove", await st.remove_memory_assets("global", ["memory/asset/x.png", "nope"]))
        raw = sqlite3.connect(ws / ".emerge" / "state.db")
        print("fts integrity", raw.execute("INSERT INTO memory_entries_fts(memory_entries_fts) VALUES('integrity-check')").fetchall())
        raw.close()
    finally:
        await st.aclose()


try:
    asyncio.run(main())
finally:
    sys.stdout.flush()
    os._exit(0)

"""Probe storage-layer edge cases on throwaway databases (never touches the real workspace)."""

import asyncio
import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from emerge.exceptions import StorageError
from emerge.graph.ports import MainAgentMessage
from emerge.graph.ports import Message as PortMessage
from emerge.models.types import Message
from emerge.storage import SqliteStorage, artifacts, open_storage

ROOT = Path(__file__).parent / "tmp"
ROOT.mkdir(exist_ok=True)


def fresh(name: str) -> Path:
    d = ROOT / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def show(label, fn_result):
    print(f"[{label}] {fn_result}")


async def attempt(label, coro):
    try:
        r = await coro
        print(f"[{label}] OK -> {r!r}"[:400])
        return r
    except BaseException as e:  # noqa: BLE001
        print(f"[{label}] {type(e).__module__}.{type(e).__name__}: {e}"[:400])
        return None


def pm(node, port, version, ct=1):
    return PortMessage(
        source_node=node, source_instance="i1", source_port=port, version=version, timestamp=time.time(),
        content_time=ct, text=f"v{version}",
    )


async def main():
    # ---------- new database ----------
    ws = fresh("new")
    st = await open_storage(ws)
    db = artifacts.state_db_path(st.workspace)
    raw = sqlite3.connect(db)
    show("user_version", raw.execute("PRAGMA user_version").fetchone())
    show("journal_mode", raw.execute("PRAGMA journal_mode").fetchone())
    show("counters", raw.execute("SELECT * FROM counters ORDER BY key").fetchall())
    show("fk runs", raw.execute("PRAGMA foreign_key_list(runs)").fetchall())
    show("fk messages", raw.execute("PRAGMA foreign_key_list(messages)").fetchall())
    show("tables", [r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")])
    raw.close()

    # ---------- sessions ----------
    await attempt("create int-key metadata", st.create_session(title="ik", metadata={3: "x"}))
    show("int-key session row exists", [x.title for x in await st.list_sessions() if x.title == "ik"])
    s = await st.create_session(title="t", metadata={"a": (1, 2)})
    show("create metadata returned", s.metadata)
    show("create metadata read back", (await st.get_session(s.id)).metadata)
    await attempt("create non-json metadata", st.create_session(metadata={"x": {1, 2}}))
    await attempt("get missing", st.get_session("nope"))
    before = (await st.get_session(s.id)).updated_at
    r = await st.update_session(s.id)
    show("update no fields -> updated_at changed", r.updated_at > before)
    r = await st.update_session(s.id, current_run_id="RUNX")
    show("current_run_id unchecked", r.current_run_id)
    r = await st.update_session(s.id, current_run_id="RUNY", clear_current_run=True)
    show("clear wins over id", r.current_run_id)
    r = await st.update_session(s.id, metadata={})
    show("metadata {} replaces", r.metadata)
    await attempt("update missing", st.update_session("nope", title="x"))
    for _ in range(3):
        await st.create_session()
    show("list limit=0", len(await st.list_sessions(limit=0)))
    show("list limit=-1", len(await st.list_sessions(limit=-1)))
    show("list all", len(await st.list_sessions()))

    # invalid enum written, read back fails
    bad = await st.create_session(title="bad")
    await attempt("update status=bogus", st.update_session(bad.id, status="bogus"))
    await attempt("get after bogus", st.get_session(bad.id))
    await attempt("list after bogus", st.list_sessions())
    await attempt("update intent=-1", st.update_session(s.id, intent_count=-1))
    await attempt("get after intent -1", st.get_session(s.id))
    # repair
    raw = sqlite3.connect(db)
    raw.execute("UPDATE sessions SET status='idle' WHERE id=?", (bad.id,))
    raw.execute("UPDATE sessions SET intent_count=0 WHERE id=?", (s.id,))
    raw.commit()
    raw.close()

    # ---------- conversation ----------
    await attempt("append to missing session", st.append_message("nope", 0, Message.user("x")))
    raw = sqlite3.connect(db)
    show("session_seq after failed append", raw.execute("SELECT value FROM counters WHERE key='session_seq'").fetchone())
    raw.close()
    a = await st.append_message(s.id, 0, Message.user("u0"))
    b = await st.append_message(s.id, 1, Message.user("u1"))
    s2 = await st.create_session()
    c = await st.append_message(s2.id, 0, Message.user("other"))
    show("seq global across sessions", (a.seq, b.seq, c.seq))
    await attempt("append turn=-1", st.append_message(s.id, -1, Message.user("neg")))
    await attempt("load after turn -1", st.load_messages(s.id))
    raw = sqlite3.connect(db)
    raw.execute("DELETE FROM messages WHERE turn=-1")
    raw.commit()
    raw.close()
    show("discard missing session", await st.discard_turn("nope", 0))
    show("discard turn1", await st.discard_turn(s.id, 1))
    show("discard turn1 again", await st.discard_turn(s.id, 1))
    await st.append_message(s.id, 1, Message.user("u1-again"))
    show("load", [(m.seq, m.turn, m.message.text if hasattr(m.message, 'text') else None) for m in await st.load_messages(s.id)])
    rows = await st.compact_messages(s.id, Message.user("SUMMARY"), [])
    show("compact empty kept", [(m.seq, m.turn, m.summary) for m in rows])
    show("load after compact", [(m.seq, m.turn, m.summary) for m in await st.load_messages(s.id)])
    show("discard turn0 (summary)", await st.discard_turn(s.id, 0))
    show("load after discarding summary", [(m.seq, m.turn, m.summary) for m in await st.load_messages(s.id)])
    # summary via append without compaction
    s3 = await st.create_session()
    for t in range(3):
        await st.append_message(s3.id, t, Message.user(f"m{t}"))
    await st.append_message(s3.id, 3, Message.user("S"), summary=True)
    await st.append_message(s3.id, 4, Message.user("after"))
    show("append summary=True w/o compact", [(m.turn, m.summary) for m in await st.load_messages(s3.id)])
    show("load missing session", await st.load_messages("nope"))

    # pending inputs
    await attempt("pending to missing", st.add_pending_input("nope", Message.user("x")))
    p = await st.add_pending_input(s.id, Message.user("p"))
    await attempt("remove pending wrong session", st.remove_pending_input(s2.id, p.seq))
    show("pending still there", [x.seq for x in await st.pending_inputs(s.id)])
    await attempt("remove pending missing seq", st.remove_pending_input(s.id, 9999))

    # ---------- runs ----------
    await attempt("create_run missing session", st.create_run("nope"))
    run = await st.create_run(s.id)
    show("session current_run after create_run", (await st.get_session(s.id)).current_run_id == run.id)
    run2 = await st.create_run(s.id)
    show("second create_run overwrites current", (await st.get_session(s.id)).current_run_id == run2.id)
    r = await st.set_run_status(run.id, "finished")
    show("finished stamps", r.finished_at is not None)
    t1 = r.finished_at
    await asyncio.sleep(0.01)
    r = await st.set_run_status(run.id, "finished")
    show("finished again re-stamps", r.finished_at != t1)
    r = await st.set_run_status(run.id, "running")
    show("finished -> running allowed, finished_at", r.finished_at)
    r = await st.set_run_status(run.id, "closed")
    show("closed finished_at", r.finished_at)
    r = await st.set_run_status(run.id, "running", finished_at=123.0)
    show("running with explicit finished_at", r.finished_at)
    await attempt("set_run_status missing", st.set_run_status("nope", "finished"))
    await attempt("set_run_status bogus", st.set_run_status(run.id, "bogus"))
    await attempt("get_run after bogus", st.get_run(run.id))
    raw = sqlite3.connect(db)
    show("bogus finished_at stamped?", raw.execute("SELECT status, finished_at FROM runs WHERE id=?", (run.id,)).fetchone())
    raw.execute("UPDATE runs SET status='running', finished_at=NULL WHERE id=?", (run.id,))
    raw.commit()
    raw.close()
    await attempt("set_run_paused missing", st.set_run_paused("nope", True))
    r = await st.set_run_paused(run.id, True)
    show("paused independent of status", (r.status, r.paused))
    show("load_run_state missing", await st.load_run_state("nope"))
    show("load_graph missing", await st.load_graph(run.id))

    # ---------- nodes ----------
    ns = await st.set_node_state("NO-SUCH-RUN", "n", "i", "running")
    show("set_node_state no run check", ns.run_id)
    await st.set_node_state(run.id, "n", "i1", "failed", outcome="o", reason="r", error="e")
    await st.set_node_state(run.id, "n", "i1", "running")
    show("defaults overwrite outcome/reason/error", [(x.state, x.outcome, x.reason, x.error) for x in await st.node_states(run.id)])
    await attempt("set_node_state bogus", st.set_node_state(run.id, "n", "i2", "bogus"))
    await attempt("node_states after bogus", st.node_states(run.id))
    await attempt("load_run_state after bogus", st.load_run_state(run.id))
    raw = sqlite3.connect(db)
    raw.execute("DELETE FROM node_states WHERE state='bogus'")
    raw.commit()
    raw.close()
    await st.set_node_snapshot(run.id, "m", "j1", {1: "a", "t": (1, 2)})
    show("snapshot creates pending row", [(x.node_id, x.state) for x in await st.node_states(run.id) if x.node_id == "m"])
    show("snapshot keys/tuples", await st.node_snapshot(run.id, "m", "j1"))
    await st.set_node_snapshot(run.id, "m", "j1", None)
    raw = sqlite3.connect(db)
    show("snapshot None stored as", raw.execute("SELECT snapshot FROM node_states WHERE node_id='m'").fetchone())
    raw.close()
    show("snapshot None read", await st.node_snapshot(run.id, "m", "j1"))
    await attempt("snapshot non-json", st.set_node_snapshot(run.id, "m", "j1", {1, 2}))
    await st.set_node_snapshot(run.id, "m", "j1", float("nan"))
    show("snapshot NaN read", await st.node_snapshot(run.id, "m", "j1"))

    # ---------- port messages ----------
    st.port_buffer = 3
    await attempt("put wrong type", st.put_port_message(Message.user("x"), run_id=run.id))
    for v in range(1, 6):
        await st.put_port_message(pm("a", "out", v), run_id=run.id)
    show("window 3 after v1..5", [x.version for x in await st.port_messages(run.id, "a", "out")])
    await attempt("duplicate version", st.put_port_message(pm("a", "out", 5), run_id=run.id))
    await st.put_port_message(pm("a", "out", 1), run_id=run.id)
    show("out-of-order old version re-inserted", [x.version for x in await st.port_messages(run.id, "a", "out")])
    await st.put_port_message(pm("a", "out", 100), run_id=run.id)
    show("sparse jump to 100", [x.version for x in await st.port_messages(run.id, "a", "out")])
    show("after_version=2 (trimmed cursor)", [x.version for x in await st.port_messages(run.id, "a", "out", after_version=2)])
    await st.put_port_message(pm("b", "out", 0), run_id=run.id)
    show("version 0 accepted", [x.version for x in await st.port_messages(run.id, "b", "out")])
    show("latest", [(x.node, x.port, x.version) for x in await st.latest_port_messages(run.id)])
    rec = await st.put_port_message(pm("z", "out", 1), run_id="NO-SUCH-RUN")
    show("put_port_message no run check", rec.run_id)

    # ---------- queued node messages ----------
    q = MainAgentMessage(seq=1, timestamp=time.time(), content_time=1, text="hi")
    await attempt("queue missing run", st.queue_node_message("nope", "n", "i", q))
    await st.queue_node_message(run.id, "n", "i9", q)
    await attempt("queue duplicate seq", st.queue_node_message(run.id, "n", "i9", q))
    show("mark delivered", await st.mark_node_messages_delivered(run.id, "i9"))
    show("mark delivered again", await st.mark_node_messages_delivered(run.id, "i9"))
    show("queued default excludes delivered", len(await st.queued_node_messages(run.id)))
    show("queued include delivered", len(await st.queued_node_messages(run.id, include_delivered=True)))
    show("mark on missing run", await st.mark_node_messages_delivered("nope", "x"))

    # ---------- asks ----------
    await attempt("add_ask missing run", st.add_ask("nope", "n", "i", "q?"))
    k1 = await st.add_ask(run.id, "n", "i", "q1?")
    k2 = await st.add_ask(run2.id, "n", "i", "q2?")
    show("ask ids global", (k1.id, k2.id))
    v = await st.void_ask(k1.id)
    show("void pending", (v.status, v.answered_at is not None))
    a1 = await st.answer_ask(k1.id, "late")
    show("answer a void ask", (a1.status, a1.answer))
    t_before = a1.answered_at
    v2 = await st.void_ask(k1.id)
    show("void an answered ask (unchanged)", (v2.status, v2.answer, v2.answered_at == t_before))
    a2 = await st.answer_ask(k1.id, "again")
    show("answer twice overwrites", (a2.answer, a2.answered_at != t_before))
    await attempt("answer missing", st.answer_ask("ask999", "x"))
    await attempt("void missing", st.void_ask("ask999"))

    # ---------- timeline ----------
    await attempt("next_content_time missing run", st.next_content_time("nope"))
    values = await asyncio.gather(*(st.next_content_time(run2.id) for _ in range(200)))
    show("200 concurrent unique/contiguous", (len(set(values)), min(values), max(values)))
    show("runs.content_time", (await st.get_run(run2.id)).content_time)

    # ---------- delete session ----------
    await attempt("delete missing", st.delete_session("nope"))
    await attempt("delete with running run", st.delete_session(s.id))
    await st.set_run_status(run.id, "closed")
    await st.set_run_status(run2.id, "stopped")
    mem = artifacts.ensure_memory_dir(st.workspace, s.id)
    (mem / "MEMORY.md").write_text("- x\n")
    (st.workspace / "sessions" / s.id / "other.txt").write_text("y")
    artifacts.ensure_run_dir(st.workspace, run.id)
    await st.replace_memory_entries(s.id, ["条目一", "entry two"], "d1")
    await st.delete_session(s.id)
    raw = sqlite3.connect(db)
    show("after delete: runs", raw.execute("SELECT COUNT(*) FROM runs WHERE session_id=?", (s.id,)).fetchone())
    show("after delete: timeline counters", raw.execute("SELECT key FROM counters WHERE key LIKE 'timeline:%'").fetchall())
    show("after delete: fts rows", raw.execute("SELECT COUNT(*) FROM memory_entries_fts").fetchone())
    show("after delete: orphan port msgs (NO-SUCH-RUN)", raw.execute("SELECT COUNT(*) FROM port_messages").fetchone())
    raw.close()
    show("sessions/<id>/ removed entirely", (st.workspace / "sessions" / s.id).exists())
    show("runs/<id>/ kept", artifacts.run_dir(st.workspace, run.id).exists())

    # ---------- aclose ----------
    await st.aclose()
    await attempt("aclose twice", st.aclose())
    await attempt("call after aclose", st.get_session("x"))
    show("wal after close exists", (db.parent / "state.db-wal").exists())
    if (db.parent / "state.db-wal").exists():
        show("wal size", (db.parent / "state.db-wal").stat().st_size)


import os
try:
    asyncio.run(main())
finally:
    import sys; sys.stdout.flush(); os._exit(0)

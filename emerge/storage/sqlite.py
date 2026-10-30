"""The SQLite implementation of `Storage`.

One database file, one connection, one transaction per write. Every call takes the same lock, so a
multi-step write (compaction, replacing one scope's memory entries, handing out a timeline number
and storing the message stamped with it) is never interleaved with another call's statements, and a
reader never sees half of one. Writers wait on each other, which is what SQLite does anyway.

The schema version lives in `PRAGMA user_version`. Values that belong to one row are columns;
graphs, messages and snapshots are JSON text, written by `artifacts.dump_model` and read back into
the same model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from emerge.exceptions import StorageError
from emerge.graph.graph import Graph
from emerge.graph.ports import MainAgentMessage
from emerge.graph.ports import Message as PortMessage
from emerge.graph.serialization import SCHEMA_VERSION as GRAPH_SCHEMA_VERSION
from emerge.graph.serialization import dumps_graph, loads_graph
from emerge.models.types import Message, Usage
from emerge.storage import artifacts
from emerge.storage.base import (
    AskRecord,
    MemoryAsset,
    MemoryHit,
    NodeState,
    PendingInput,
    PortRecord,
    QueuedNodeMessage,
    RunRecord,
    RunState,
    SessionRecord,
    Storage,
    StoredMessage,
)

logger = logging.getLogger(__name__)

# 2 added `queued_node_messages` and `runs.paused`. Older databases are not migrated.
SCHEMA_VERSION = 2

# Pages in the WAL before SQLite checkpoints on its own (about 80 MB at 4 KiB pages), and the size
# the WAL is cut back to after a checkpoint. See `SqliteStorage.open`.
_WAL_AUTOCHECKPOINT = 20_000
_WAL_SIZE_LIMIT = 64 * 1024 * 1024

_RUNNING_STATES = ("running", "idle", "paused")
# Runs that were closed with the session. Only these may be left behind when the session goes.
_CLOSED_RUN_STATE = "closed"
# Statuses a run may still reach; anything else means it is a record of something over.
_OPEN_RUN_STATES = frozenset(_RUNNING_STATES) | {_CLOSED_RUN_STATE}

# Runs of characters that get indexed one character at a time: Chinese, Japanese kana, Hangul.
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]+")
_TOKEN_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]+|[^\s]+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'idle',
    system TEXT,
    current_run_id TEXT,
    intent_count INTEGER NOT NULL DEFAULT 0,
    usage TEXT NOT NULL DEFAULT '{}',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    turn INTEGER NOT NULL,
    message TEXT NOT NULL,
    is_summary INTEGER NOT NULL DEFAULT 0,
    is_compacted INTEGER NOT NULL DEFAULT 0,
    is_discarded INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_messages_turn ON messages (session_id, turn);

CREATE TABLE IF NOT EXISTS pending_inputs (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    message TEXT NOT NULL,
    received_at REAL NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS memory_entries (
    scope TEXT NOT NULL,
    position INTEGER NOT NULL,
    text TEXT NOT NULL,
    -- The same text with CJK characters separated, so the tokenizer keeps them apart.
    index_text TEXT NOT NULL,
    PRIMARY KEY (scope, position)
);
CREATE INDEX IF NOT EXISTS ix_memory_entries_scope ON memory_entries (scope);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_entries_fts USING fts5(
    index_text,
    content='memory_entries',
    content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS memory_entries_ai AFTER INSERT ON memory_entries BEGIN
    INSERT INTO memory_entries_fts (rowid, index_text) VALUES (new.rowid, new.index_text);
END;
CREATE TRIGGER IF NOT EXISTS memory_entries_ad AFTER DELETE ON memory_entries BEGIN
    INSERT INTO memory_entries_fts (memory_entries_fts, rowid, index_text) VALUES ('delete', old.rowid, old.index_text);
END;

CREATE TABLE IF NOT EXISTS memory_assets (
    scope TEXT NOT NULL,
    path TEXT NOT NULL,
    digest TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    added_at REAL NOT NULL,
    PRIMARY KEY (scope, path)
);

CREATE TABLE IF NOT EXISTS memory_files (
    scope TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    indexed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    content_time INTEGER NOT NULL DEFAULT 0,
    graph_file_digest TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS ix_runs_session ON runs (session_id);

CREATE TABLE IF NOT EXISTS graphs (
    run_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    data TEXT NOT NULL,
    saved_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS node_states (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    state TEXT NOT NULL,
    outcome TEXT,
    reason TEXT NOT NULL DEFAULT '',
    error TEXT,
    snapshot TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_id, node_id, instance_id)
);

CREATE TABLE IF NOT EXISTS port_messages (
    run_id TEXT NOT NULL,
    node TEXT NOT NULL,
    port TEXT NOT NULL,
    version INTEGER NOT NULL,
    message TEXT NOT NULL,
    stored_at REAL NOT NULL,
    PRIMARY KEY (run_id, node, port, version)
);

CREATE TABLE IF NOT EXISTS asks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    answer TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    answered_at REAL
);
CREATE INDEX IF NOT EXISTS ix_asks_run_status ON asks (run_id, status);

-- Main-agent messages sent to a node instance before it started, handed over when it starts.
CREATE TABLE IF NOT EXISTS queued_node_messages (
    run_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    message TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    queued_at REAL NOT NULL,
    delivered_at REAL,
    PRIMARY KEY (run_id, instance_id, seq)
);

-- Monotonic counters: per-session message numbers and per-run timeline numbers. Kept apart from
-- the rows they number so that a number handed out is never handed out twice, not even after rows
-- were deleted.
CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


class SqliteStorage(Storage):
    """The only implementation. Open it with `open_storage()` or `SqliteStorage.open()`."""

    def __init__(self, workspace: Path, conn: aiosqlite.Connection, *, port_buffer: int) -> None:
        self.workspace = workspace
        self._conn = conn
        self.port_buffer = port_buffer
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, workspace: Path, *, port_buffer: int = 64) -> "SqliteStorage":
        if port_buffer < 1:
            raise StorageError(f"port_buffer must be at least 1, got {port_buffer}")
        workspace = Path(workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        path = artifacts.state_db_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        # Each write is its own commit, and with the default 1000-page threshold a burst of small
        # writes hits a checkpoint every few hundred commits, each of which stops the writer while
        # it copies the WAL back. Writes here come in bursts (a publishing port, a compaction), so
        # checkpoints are rare on purpose and the WAL is truncated when the storage is closed.
        await conn.execute(f"PRAGMA wal_autocheckpoint={_WAL_AUTOCHECKPOINT}")
        await conn.execute(f"PRAGMA journal_size_limit={_WAL_SIZE_LIMIT}")
        storage = cls(workspace, conn, port_buffer=port_buffer)
        await storage._init_schema()
        return storage

    async def _init_schema(self) -> None:
        row = await self._fetch_one("PRAGMA user_version")
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise StorageError(
                f"{artifacts.state_db_path(self.workspace)} has schema version {version}; "
                f"this code understands {SCHEMA_VERSION}"
            )
        if 0 < version < SCHEMA_VERSION:
            raise StorageError(
                f"{artifacts.state_db_path(self.workspace)} has schema version {version}, written by older "
                f"code; schema {SCHEMA_VERSION} is not migrated to, so move the file away to start a new one"
            )
        # executescript commits on its own, so it runs outside the transaction helper.
        await self._conn.executescript(_SCHEMA)
        async with self._transaction():
            await self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            if version < SCHEMA_VERSION:
                # A new database, or one written before the counters table existed: carry on from
                # whatever the stored rows already used.
                for key, sql in (
                    ("session_seq", "SELECT COALESCE(MAX(seq), 0) FROM messages"),
                    ("pending_seq", "SELECT COALESCE(MAX(seq), 0) FROM pending_inputs"),
                    ("ask_seq", "SELECT COALESCE(MAX(CAST(substr(id, 4) AS INTEGER)), 0) FROM asks"),
                ):
                    rows = await self._raw_fetch(sql)
                    await self._conn.execute(
                        "INSERT INTO counters (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
                        (key, int(rows[0][0])),
                    )

    # -- plumbing -----------------------------------------------------------

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        """Hold the connection for one unit of work and commit it at the end.

        Everything reachable from inside this block must use the `_raw_*` helpers: the lock is
        already held and a second `acquire` would wait forever.
        """
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                await self._conn.rollback()
                raise
            await self._conn.commit()

    async def _raw_fetch(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        cursor = await self._conn.execute(sql, params)
        return list(await cursor.fetchall())

    async def _raw_one(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        rows = await self._raw_fetch(sql, params)
        return rows[0] if rows else None

    async def _fetch(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._transaction():
            return await self._raw_fetch(sql, params)

    async def _fetch_one(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self._transaction():
            return await self._raw_one(sql, params)

    async def _counter(self, key: str) -> int:
        """Take the next value of a counter. Inside a transaction, like everything else here."""
        await self._conn.execute(
            "INSERT INTO counters (key, value) VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET value = value + 1",
            (key,),
        )
        row = await (await self._conn.execute("SELECT value FROM counters WHERE key = ?", (key,))).fetchone()
        return int(row[0])

    async def _require_session(self, session_id: str) -> None:
        if await self._raw_one("SELECT 1 FROM sessions WHERE id = ?", (session_id,)) is None:
            raise StorageError(f"session {session_id!r} does not exist")

    async def _require_run(self, run_id: str) -> None:
        if await self._raw_one("SELECT 1 FROM runs WHERE id = ?", (run_id,)) is None:
            raise StorageError(f"run {run_id!r} does not exist")

    async def aclose(self) -> None:
        # Fold the WAL back into the database file before letting go, so a closed workspace is one
        # file and the next open starts clean.
        try:
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            await self._conn.close()

    # -- sessions -----------------------------------------------------------

    async def create_session(
        self,
        *,
        title: str = "",
        system: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionRecord:
        session_id = uuid.uuid4().hex
        now = time.time()
        # Serialize first: a metadata value that is not JSON has to be reported here, not at read time.
        metadata_text = artifacts.dump_json(dict(metadata or {}))
        async with self._transaction():
            await self._conn.execute(
                "INSERT INTO sessions (id, title, status, system, current_run_id, intent_count, usage, "
                "metadata, created_at, updated_at) VALUES (?, ?, 'idle', ?, NULL, 0, ?, ?, ?, ?)",
                (session_id, title, system, artifacts.dump_model(Usage()), metadata_text, now, now),
            )
        return SessionRecord(
            id=session_id,
            title=title,
            status="idle",
            system=system,
            current_run_id=None,
            intent_count=0,
            usage=Usage(),
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )

    async def get_session(self, session_id: str) -> SessionRecord | None:
        row = await self._fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return _session(row) if row else None

    async def list_sessions(self, *, limit: int | None = None) -> tuple[SessionRecord, ...]:
        sql = "SELECT * FROM sessions ORDER BY updated_at DESC, id DESC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return tuple(_session(row) for row in await self._fetch(sql, params))

    async def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        status: str | None = None,
        current_run_id: str | None = None,
        clear_current_run: bool = False,
        intent_count: int | None = None,
        usage: Usage | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionRecord:
        sets: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("title", title),
            ("status", status),
            ("intent_count", intent_count),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        # Current run is a nullable field: None means "leave it", so clearing needs its own flag.
        if clear_current_run or current_run_id is not None:
            sets.append("current_run_id = ?")
            params.append(None if clear_current_run else current_run_id)
        if usage is not None:
            sets.append("usage = ?")
            params.append(artifacts.dump_model(usage))
        if metadata is not None:
            sets.append("metadata = ?")
            params.append(artifacts.dump_json(dict(metadata)))
        sets.append("updated_at = ?")
        params.append(time.time())
        async with self._transaction():
            await self._require_session(session_id)
            await self._conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", (*params, session_id))
        session = await self.get_session(session_id)
        assert session is not None
        return session

    async def set_system_snapshot(self, session_id: str, system: str) -> SessionRecord:
        async with self._transaction():
            await self._require_session(session_id)
            await self._conn.execute(
                "UPDATE sessions SET system = ?, updated_at = ? WHERE id = ?",
                (system, time.time(), session_id),
            )
        session = await self.get_session(session_id)
        assert session is not None
        return session

    async def delete_session(self, session_id: str) -> None:
        runs = await self.runs_of_session(session_id)
        busy = [run for run in runs if run.status in _RUNNING_STATES]
        if busy:
            listed = ", ".join(f"{run.id} ({run.status})" for run in busy)
            raise StorageError(f"session {session_id!r} still has open runs: {listed}")
        async with self._transaction():
            await self._require_session(session_id)
            for table in ("messages", "pending_inputs"):
                await self._conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            # The session's own memory scope; the global one is untouched.
            for table in ("memory_entries", "memory_assets", "memory_files"):
                await self._conn.execute(f"DELETE FROM {table} WHERE scope = ?", (session_id,))
            # Its runs go with it: a run nothing points at is not a record of anything any more.
            for run in runs:
                for table in ("graphs", "node_states", "port_messages", "queued_node_messages"):
                    await self._conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run.id,))
                await self._conn.execute("DELETE FROM asks WHERE run_id = ?", (run.id,))
                await self._conn.execute("DELETE FROM counters WHERE key = ?", (f"timeline:{run.id}",))
                await self._conn.execute("DELETE FROM runs WHERE id = ?", (run.id,))
            await self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        artifacts.remove_session_memory(self.workspace, session_id)

    # -- conversation -------------------------------------------------------

    async def append_message(
        self, session_id: str, turn: int, message: Message, *, summary: bool = False
    ) -> StoredMessage:
        text = artifacts.dump_model(message)
        now = time.time()
        async with self._transaction():
            await self._require_session(session_id)
            seq = await self._counter("session_seq")
            await self._conn.execute(
                "INSERT INTO messages (session_id, seq, turn, message, is_summary, is_compacted, "
                "is_discarded, created_at) VALUES (?, ?, ?, ?, ?, 0, 0, ?)",
                (session_id, seq, turn, text, 1 if summary else 0, now),
            )
        return StoredMessage(seq=seq, turn=turn, message=message, summary=summary, created_at=now)

    async def compact_messages(
        self, session_id: str, summary: Message, kept: Sequence[tuple[int, Message]]
    ) -> tuple[StoredMessage, ...]:
        now = time.time()
        appended: list[StoredMessage] = []
        async with self._transaction():
            await self._require_session(session_id)
            # Every row the conversation could have read becomes unreachable; the summary and the
            # kept turns take their place. Nothing is deleted.
            await self._conn.execute(
                "UPDATE messages SET is_compacted = 1 WHERE session_id = ? AND is_compacted = 0",
                (session_id,),
            )
            seq = await self._counter("session_seq")
            await self._conn.execute(
                "INSERT INTO messages (session_id, seq, turn, message, is_summary, is_compacted, "
                "is_discarded, created_at) VALUES (?, ?, 0, ?, 1, 0, 0, ?)",
                (session_id, seq, artifacts.dump_model(summary), now),
            )
            appended.append(StoredMessage(seq=seq, turn=0, message=summary, summary=True, created_at=now))
            for turn, message in kept:
                seq = await self._counter("session_seq")
                await self._conn.execute(
                    "INSERT INTO messages (session_id, seq, turn, message, is_summary, is_compacted, "
                    "is_discarded, created_at) VALUES (?, ?, ?, ?, 0, 0, 0, ?)",
                    (session_id, seq, turn, artifacts.dump_model(message), now),
                )
                appended.append(StoredMessage(seq=seq, turn=turn, message=message, created_at=now))
        return tuple(appended)

    async def discard_turn(self, session_id: str, turn: int) -> int:
        async with self._transaction():
            cursor = await self._conn.execute(
                "UPDATE messages SET is_discarded = 1 "
                "WHERE session_id = ? AND turn = ? AND is_compacted = 0 AND is_discarded = 0",
                (session_id, turn),
            )
            return cursor.rowcount

    async def load_messages(self, session_id: str) -> tuple[StoredMessage, ...]:
        rows = await self._fetch(
            "SELECT * FROM messages WHERE session_id = ? AND is_compacted = 0 AND is_discarded = 0 "
            "ORDER BY seq",
            (session_id,),
        )
        start = 0
        for index, row in enumerate(rows):
            if row["is_summary"]:
                start = index
        return tuple(_stored_message(row) for row in rows[start:])

    # -- queued user messages -----------------------------------------------

    async def add_pending_input(self, session_id: str, message: Message) -> PendingInput:
        text = artifacts.dump_model(message)
        now = time.time()
        async with self._transaction():
            await self._require_session(session_id)
            seq = await self._counter("pending_seq")
            await self._conn.execute(
                "INSERT INTO pending_inputs (session_id, seq, message, received_at) VALUES (?, ?, ?, ?)",
                (session_id, seq, text, now),
            )
        return PendingInput(session_id=session_id, seq=seq, message=message, received_at=now)

    async def pending_inputs(self, session_id: str) -> tuple[PendingInput, ...]:
        rows = await self._fetch(
            "SELECT * FROM pending_inputs WHERE session_id = ? ORDER BY seq", (session_id,)
        )
        return tuple(
            PendingInput(
                session_id=row["session_id"],
                seq=row["seq"],
                message=artifacts.load_model(Message, row["message"]),
                received_at=row["received_at"],
            )
            for row in rows
        )

    async def remove_pending_input(self, session_id: str, seq: int) -> None:
        async with self._transaction():
            await self._conn.execute(
                "DELETE FROM pending_inputs WHERE session_id = ? AND seq = ?", (session_id, seq)
            )

    # -- memory index -------------------------------------------------------

    async def replace_memory_entries(
        self, scope: str, entries: Sequence[str], file_digest: str
    ) -> None:
        now = time.time()
        async with self._transaction():
            await self._conn.execute("DELETE FROM memory_entries WHERE scope = ?", (scope,))
            await self._conn.executemany(
                "INSERT INTO memory_entries (scope, position, text, index_text) VALUES (?, ?, ?, ?)",
                [(scope, position, text, _index_text(text)) for position, text in enumerate(entries)],
            )
            await self._conn.execute(
                "INSERT INTO memory_files (scope, digest, indexed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET digest = excluded.digest, indexed_at = excluded.indexed_at",
                (scope, file_digest, now),
            )

    async def search_memory(
        self, query: str, scopes: Sequence[str], *, limit: int = 20
    ) -> tuple[MemoryHit, ...]:
        match = _match_expression(query)
        if match is None or not scopes:
            return ()
        placeholders = ", ".join("?" for _ in scopes)
        sql = (
            "SELECT e.scope, e.position, e.text FROM memory_entries_fts f "
            "JOIN memory_entries e ON e.rowid = f.rowid "
            f"WHERE memory_entries_fts MATCH ? AND e.scope IN ({placeholders}) "
            "ORDER BY f.rank, e.scope, e.position LIMIT ?"
        )
        try:
            rows = await self._fetch(sql, (match, *scopes, limit))
        except aiosqlite.OperationalError as e:
            # A query made only of FTS5 syntax is not a search; it finds nothing.
            logger.warning("memory search %r could not be parsed: %s", query, e)
            return ()
        return tuple(MemoryHit(scope=row["scope"], position=row["position"], text=row["text"]) for row in rows)

    async def memory_file_digest(self, scope: str) -> str | None:
        row = await self._fetch_one("SELECT digest FROM memory_files WHERE scope = ?", (scope,))
        return row["digest"] if row else None

    async def add_memory_asset(
        self,
        scope: str,
        path: str,
        digest: str,
        *,
        description: str = "",
        source: str = "",
    ) -> MemoryAsset:
        now = time.time()
        async with self._transaction():
            await self._conn.execute(
                "INSERT INTO memory_assets (scope, path, digest, description, source, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(scope, path) DO UPDATE SET digest = excluded.digest, "
                "description = excluded.description, source = excluded.source, added_at = excluded.added_at",
                (scope, path, digest, description, source, now),
            )
        return MemoryAsset(
            scope=scope, path=path, digest=digest, description=description, source=source, added_at=now
        )

    async def memory_assets(self, scope: str) -> tuple[MemoryAsset, ...]:
        rows = await self._fetch(
            "SELECT * FROM memory_assets WHERE scope = ? ORDER BY path", (scope,)
        )
        return tuple(
            MemoryAsset(
                scope=row["scope"],
                path=row["path"],
                digest=row["digest"],
                description=row["description"],
                source=row["source"],
                added_at=row["added_at"],
            )
            for row in rows
        )

    async def remove_memory_assets(self, scope: str, paths: Sequence[str]) -> int:
        if not paths:
            return 0
        placeholders = ", ".join("?" for _ in paths)
        async with self._transaction():
            cursor = await self._conn.execute(
                f"DELETE FROM memory_assets WHERE scope = ? AND path IN ({placeholders})",
                (scope, *paths),
            )
            return cursor.rowcount

    # -- runs ---------------------------------------------------------------

    async def create_run(self, session_id: str, *, graph_file_digest: str | None = None) -> RunRecord:
        run_id = uuid.uuid4().hex
        now = time.time()
        async with self._transaction():
            await self._require_session(session_id)
            await self._conn.execute(
                "INSERT INTO runs (id, session_id, status, content_time, graph_file_digest, paused, started_at, "
                "finished_at) VALUES (?, ?, 'running', 0, ?, 0, ?, NULL)",
                (run_id, session_id, graph_file_digest, now),
            )
            await self._conn.execute(
                "UPDATE sessions SET current_run_id = ?, updated_at = ? WHERE id = ?",
                (run_id, now, session_id),
            )
        return RunRecord(
            id=run_id,
            session_id=session_id,
            status="running",
            content_time=0,
            graph_file_digest=graph_file_digest,
            started_at=now,
        )

    async def set_run_status(
        self, run_id: str, status: str, *, finished_at: float | None = None
    ) -> RunRecord:
        async with self._transaction():
            await self._require_run(run_id)
            if finished_at is not None:
                stamp = finished_at
            elif status in _OPEN_RUN_STATES:
                stamp = None
            else:
                stamp = time.time()
            await self._conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?", (status, stamp, run_id)
            )
        run = await self.get_run(run_id)
        assert run is not None
        return run

    async def get_run(self, run_id: str) -> RunRecord | None:
        row = await self._fetch_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return _run(row) if row else None

    async def runs_of_session(self, session_id: str) -> tuple[RunRecord, ...]:
        rows = await self._fetch(
            "SELECT * FROM runs WHERE session_id = ? ORDER BY started_at, id", (session_id,)
        )
        return tuple(_run(row) for row in rows)

    async def set_graph_file_digest(self, run_id: str, digest: str) -> RunRecord:
        async with self._transaction():
            await self._require_run(run_id)
            await self._conn.execute(
                "UPDATE runs SET graph_file_digest = ? WHERE id = ?", (digest, run_id)
            )
        run = await self.get_run(run_id)
        assert run is not None
        return run

    async def set_run_paused(self, run_id: str, paused: bool) -> RunRecord:
        async with self._transaction():
            await self._require_run(run_id)
            await self._conn.execute("UPDATE runs SET paused = ? WHERE id = ?", (int(paused), run_id))
        run = await self.get_run(run_id)
        assert run is not None
        return run

    # -- graph --------------------------------------------------------------

    async def save_graph(self, run_id: str, graph: Graph) -> None:
        data = dumps_graph(graph)
        async with self._transaction():
            await self._require_run(run_id)
            await self._conn.execute(
                "INSERT INTO graphs (run_id, schema_version, data, saved_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET schema_version = excluded.schema_version, "
                "data = excluded.data, saved_at = excluded.saved_at",
                (run_id, GRAPH_SCHEMA_VERSION, data, time.time()),
            )

    async def load_graph(self, run_id: str) -> Graph | None:
        row = await self._fetch_one("SELECT data FROM graphs WHERE run_id = ?", (run_id,))
        return loads_graph(row["data"]) if row else None

    # -- nodes --------------------------------------------------------------

    async def set_node_state(
        self,
        run_id: str,
        node_id: str,
        instance_id: str,
        state: str,
        *,
        outcome: str | None = None,
        reason: str = "",
        error: str | None = None,
    ) -> NodeState:
        now = time.time()
        async with self._transaction():
            await self._conn.execute(
                "INSERT INTO node_states (run_id, node_id, instance_id, state, outcome, reason, error, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, node_id, instance_id) DO UPDATE SET state = excluded.state, "
                "outcome = excluded.outcome, reason = excluded.reason, error = excluded.error, "
                "updated_at = excluded.updated_at",
                (run_id, node_id, instance_id, state, outcome, reason, error, now),
            )
        return NodeState(
            run_id=run_id,
            node_id=node_id,
            instance_id=instance_id,
            state=state,
            outcome=outcome,
            reason=reason,
            error=error,
            updated_at=now,
        )

    async def node_states(self, run_id: str) -> tuple[NodeState, ...]:
        rows = await self._fetch(
            "SELECT * FROM node_states WHERE run_id = ? ORDER BY node_id, instance_id", (run_id,)
        )
        return tuple(_node_state(row) for row in rows)

    async def set_node_snapshot(
        self, run_id: str, node_id: str, instance_id: str, data: Any
    ) -> None:
        text = artifacts.dump_json(data)
        async with self._transaction():
            await self._conn.execute(
                "INSERT INTO node_states (run_id, node_id, instance_id, state, updated_at, snapshot) "
                "VALUES (?, ?, ?, 'pending', ?, ?) "
                "ON CONFLICT(run_id, node_id, instance_id) DO UPDATE SET "
                "snapshot = excluded.snapshot, updated_at = excluded.updated_at",
                (run_id, node_id, instance_id, time.time(), text),
            )

    async def node_snapshot(self, run_id: str, node_id: str, instance_id: str) -> Any | None:
        row = await self._fetch_one(
            "SELECT snapshot FROM node_states WHERE run_id = ? AND node_id = ? AND instance_id = ?",
            (run_id, node_id, instance_id),
        )
        if row is None or row["snapshot"] is None:
            return None
        return json.loads(row["snapshot"])

    # -- port messages ------------------------------------------------------

    async def put_port_message(self, message: PortMessage, *, run_id: str) -> PortRecord:
        if not isinstance(message, PortMessage):
            raise StorageError(f"port messages hold graph.ports.Message, got {type(message).__name__}")
        text = artifacts.dump_model(message)
        now = time.time()
        async with self._transaction():
            await self._conn.execute(
                "INSERT INTO port_messages (run_id, node, port, version, message, stored_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, message.source_node, message.source_port, message.version, text, now),
            )
            # Keep the newest `port_buffer` messages of this port; anything older is out of reach.
            await self._conn.execute(
                "DELETE FROM port_messages WHERE run_id = ? AND node = ? AND port = ? AND version <= ?",
                (
                    run_id,
                    message.source_node,
                    message.source_port,
                    message.version - self.port_buffer,
                ),
            )
        return PortRecord(
            run_id=run_id,
            node=message.source_node,
            port=message.source_port,
            version=message.version,
            message=message,
            stored_at=now,
        )

    async def port_messages(
        self, run_id: str, node: str, port: str, after_version: int | None = None
    ) -> tuple[PortRecord, ...]:
        sql = "SELECT * FROM port_messages WHERE run_id = ? AND node = ? AND port = ?"
        params: tuple[Any, ...] = (run_id, node, port)
        if after_version is not None:
            sql += " AND version > ?"
            params += (after_version,)
        sql += " ORDER BY version"
        return tuple(_port_message(row) for row in await self._fetch(sql, params))

    async def latest_port_messages(self, run_id: str) -> tuple[PortRecord, ...]:
        rows = await self._fetch(
            "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY node, port ORDER BY version DESC) "
            "AS rn FROM port_messages WHERE run_id = ?) WHERE rn = 1 ORDER BY version, node, port",
            (run_id,),
        )
        return tuple(_port_message(row) for row in rows)

    # -- main-agent messages to nodes not started yet --------------------------

    async def queue_node_message(
        self, run_id: str, node_id: str, instance_id: str, message: MainAgentMessage
    ) -> QueuedNodeMessage:
        text = artifacts.dump_model(message)
        now = time.time()
        async with self._transaction():
            await self._require_run(run_id)
            await self._conn.execute(
                "INSERT INTO queued_node_messages (run_id, instance_id, seq, node_id, message, delivered, "
                "queued_at, delivered_at) VALUES (?, ?, ?, ?, ?, 0, ?, NULL)",
                (run_id, instance_id, message.seq, node_id, text, now),
            )
        return QueuedNodeMessage(
            run_id=run_id, node_id=node_id, instance_id=instance_id, seq=message.seq, message=message, queued_at=now
        )

    async def queued_node_messages(
        self, run_id: str, *, instance_id: str | None = None, include_delivered: bool = False
    ) -> tuple[QueuedNodeMessage, ...]:
        sql = "SELECT * FROM queued_node_messages WHERE run_id = ?"
        params: tuple[Any, ...] = (run_id,)
        if instance_id is not None:
            sql += " AND instance_id = ?"
            params += (instance_id,)
        if not include_delivered:
            sql += " AND delivered = 0"
        sql += " ORDER BY instance_id, seq"
        return tuple(_queued_message(row) for row in await self._fetch(sql, params))

    async def mark_node_messages_delivered(self, run_id: str, instance_id: str) -> int:
        async with self._transaction():
            cursor = await self._conn.execute(
                "UPDATE queued_node_messages SET delivered = 1, delivered_at = ? "
                "WHERE run_id = ? AND instance_id = ? AND delivered = 0",
                (time.time(), run_id, instance_id),
            )
            return cursor.rowcount

    # -- asks ---------------------------------------------------------------

    async def add_ask(self, run_id: str, node_id: str, instance_id: str, question: str) -> AskRecord:
        now = time.time()
        async with self._transaction():
            await self._require_run(run_id)
            seq = await self._counter("ask_seq")
            ask_id = f"ask{seq}"
            await self._conn.execute(
                "INSERT INTO asks (id, run_id, node_id, instance_id, question, status, answer, "
                "created_at, answered_at) VALUES (?, ?, ?, ?, ?, 'pending', '', ?, NULL)",
                (ask_id, run_id, node_id, instance_id, question, now),
            )
        return AskRecord(
            id=ask_id,
            run_id=run_id,
            node_id=node_id,
            instance_id=instance_id,
            question=question,
            status="pending",
            answer="",
            created_at=now,
        )

    async def answer_ask(self, ask_id: str, answer: str) -> AskRecord:
        async with self._transaction():
            if await self._raw_one("SELECT 1 FROM asks WHERE id = ?", (ask_id,)) is None:
                raise StorageError(f"ask {ask_id!r} does not exist")
            await self._conn.execute(
                "UPDATE asks SET status = 'answered', answer = ?, answered_at = ? WHERE id = ?",
                (answer, time.time(), ask_id),
            )
        return await self._ask(ask_id)

    async def void_ask(self, ask_id: str) -> AskRecord:
        async with self._transaction():
            if await self._raw_one("SELECT 1 FROM asks WHERE id = ?", (ask_id,)) is None:
                raise StorageError(f"ask {ask_id!r} does not exist")
            # An answered ask keeps its answer: voiding is for questions nobody will answer.
            await self._conn.execute(
                "UPDATE asks SET status = 'void', answered_at = ? WHERE id = ? AND status = 'pending'",
                (time.time(), ask_id),
            )
        return await self._ask(ask_id)

    async def _ask(self, ask_id: str) -> AskRecord:
        row = await self._fetch_one("SELECT * FROM asks WHERE id = ?", (ask_id,))
        assert row is not None
        return AskRecord(
            id=row["id"],
            run_id=row["run_id"],
            node_id=row["node_id"],
            instance_id=row["instance_id"],
            question=row["question"],
            status=row["status"],
            answer=row["answer"],
            created_at=row["created_at"],
            answered_at=row["answered_at"],
        )

    async def pending_asks(self, run_id: str) -> tuple[AskRecord, ...]:
        rows = await self._fetch(
            "SELECT * FROM asks WHERE run_id = ? AND status = 'pending' ORDER BY created_at, id",
            (run_id,),
        )
        return tuple(
            AskRecord(
                id=row["id"],
                run_id=row["run_id"],
                node_id=row["node_id"],
                instance_id=row["instance_id"],
                question=row["question"],
                status=row["status"],
                answer=row["answer"],
                created_at=row["created_at"],
                answered_at=row["answered_at"],
            )
            for row in rows
        )

    # -- timeline -----------------------------------------------------------

    async def next_content_time(self, run_id: str) -> int:
        async with self._transaction():
            await self._require_run(run_id)
            value = await self._counter(f"timeline:{run_id}")
            await self._conn.execute("UPDATE runs SET content_time = ? WHERE id = ?", (value, run_id))
            return value

    # -- resuming -----------------------------------------------------------

    async def load_run_state(self, run_id: str) -> RunState | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        rows = await self._fetch(
            "SELECT * FROM port_messages WHERE run_id = ? ORDER BY node, port, version", (run_id,)
        )
        return RunState(
            run=run,
            graph=await self.load_graph(run_id),
            node_states=await self.node_states(run_id),
            port_messages=tuple(_port_message(row) for row in rows),
            pending_asks=await self.pending_asks(run_id),
            queued_messages=await self.queued_node_messages(run_id),
        )


# -- row to record ----------------------------------------------------------


def _session(row: aiosqlite.Row) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        system=row["system"],
        current_run_id=row["current_run_id"],
        intent_count=row["intent_count"],
        usage=Usage.model_validate(json.loads(row["usage"])),
        metadata=json.loads(row["metadata"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _stored_message(row: aiosqlite.Row) -> StoredMessage:
    return StoredMessage(
        seq=row["seq"],
        turn=row["turn"],
        message=artifacts.load_model(Message, row["message"]),
        summary=bool(row["is_summary"]),
        created_at=row["created_at"],
    )


def _run(row: aiosqlite.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        session_id=row["session_id"],
        status=row["status"],
        content_time=row["content_time"],
        graph_file_digest=row["graph_file_digest"],
        paused=bool(row["paused"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _node_state(row: aiosqlite.Row) -> NodeState:
    return NodeState(
        run_id=row["run_id"],
        node_id=row["node_id"],
        instance_id=row["instance_id"],
        state=row["state"],
        outcome=row["outcome"],
        reason=row["reason"],
        error=row["error"],
        updated_at=row["updated_at"],
    )


def _queued_message(row: aiosqlite.Row) -> QueuedNodeMessage:
    return QueuedNodeMessage(
        run_id=row["run_id"],
        node_id=row["node_id"],
        instance_id=row["instance_id"],
        seq=row["seq"],
        message=artifacts.load_model(MainAgentMessage, row["message"]),
        delivered=bool(row["delivered"]),
        queued_at=row["queued_at"],
        delivered_at=row["delivered_at"],
    )


def _port_message(row: aiosqlite.Row) -> PortRecord:
    return PortRecord(
        run_id=row["run_id"],
        node=row["node"],
        port=row["port"],
        version=row["version"],
        message=artifacts.load_model(PortMessage, row["message"]),
        stored_at=row["stored_at"],
    )


# -- search text ------------------------------------------------------------


def _index_text(text: str) -> str:
    """The text with a space between CJK characters, so unicode61 indexes them one by one."""
    return _CJK_RE.sub(lambda match: " " + " ".join(match.group()) + " ", text)


def _match_expression(query: str) -> str | None:
    """An FTS5 expression that matches the query's words and characters; None for an empty query.

    CJK characters become separate terms, matching the way they were indexed; everything else is
    matched as written, so a Latin word matches whole and case-insensitively. Terms are quoted, so
    the user's text is never read as FTS5 syntax.
    """
    terms: list[str] = []
    for token in _TOKEN_RE.findall(query):
        if _CJK_RE.fullmatch(token):
            terms.extend(token)
        else:
            terms.append(token)
    seen: list[str] = []
    for term in terms:
        if term not in seen:
            seen.append(term)
    if not seen:
        return None
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in seen)

"""Lite storage: a duck-typed asyncpg pool over SQLite.

Lite mode exists so bucker runs on a machine with NOTHING but Python
installed — no Docker, no Postgres, no Temporal, no uv. The whole
application layer (EventStore, SnapshotStore, activities, API endpoints,
dashboard) is written against the asyncpg pool surface: ``acquire()``,
``fetchrow``, ``fetchval``, ``fetch``, ``execute``, ``$1`` placeholders,
``::jsonb`` casts. ``LitePool`` implements that same surface over a
SQLite file so every existing code path works UNCHANGED.

What is lost vs. the full stack (honest, and by design):

* Durability across process restarts is degraded: SQLite transactions
  survive a crash, but there is no Temporal to reschedule a half-finished
  task after a worker death. Lite mode runs tasks to completion in one
  process or not at all.
* No container isolation: ``LocalSandbox`` runs the worker's code as a
  subprocess on the host in a scratch directory. Use it for code you
  trust. The full Docker sandbox is the default and stays.
* No schedule/retry/replay guarantees from Temporal: schedules and
  deterministic replay pages answer 503 with a clear message.

Everything else — event sourcing, append-only log, snapshots, cost
tracking, verification, the dashboard — behaves identically.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


#: asyncpg ``$1`` placeholders -> sqlite ``?``.
_PLACEHOLDER = re.compile(r"\$\d+")
#: asyncpg type casts appended to literals/params; sqlite has no such thing.
#: ``::interval`` MUST come before ``::int`` (alternation is ordered, and
#: ``::int`` would otherwise match the prefix of ``::interval``).
_CAST = re.compile(r"::jsonb|::json|::text|::interval|::int|::uuid")
#: Postgres date/time expressions -> sqlite equivalents. Timestamps are
#: stored as ISO-8601 text, which sorts lexicographically, so a string
#: comparison is correct.
_DATE_FIXES = [
    (re.compile(r"NOW\(\)\s*-\s*INTERVAL\s*'([^']+)'"), r"datetime('now', '-\1')"),
    (re.compile(r"NOW\(\)\s*-\s*\(\s*\$(\d+)\s*\|\|\s*' minutes'\s*\)"),
     r"datetime('now', '-' || ? || ' minutes')"),
    (re.compile(r"NOW\(\)"), "datetime('now')"),
    (re.compile(r"CURRENT_DATE"), "date('now')"),
]


def translate_sql(sql: str) -> str:
    """Port one query from asyncpg dialect to sqlite dialect.

    Only the surface is translated: placeholders, type casts, and the
    handful of date expressions this codebase uses. The queries are
    deliberately plain (SELECT/INSERT/UPDATE with WHERE), so nothing
    fancier is needed. Anything exotic would fail loudly in the tests
    rather than silently misbehave.
    """
    sql = _CAST.sub("", sql)
    for pattern, replacement in _DATE_FIXES:
        sql = pattern.sub(replacement, sql)
    return _PLACEHOLDER.sub("?", sql)


class LiteRow:
    """A single result row, asyncpg-Record-shaped.

    Supports both ``row["col"]`` and ``row[0]`` access, plus ``.get()``.
    JSONB columns arrive as text and are decoded on access so payload
    access matches asyncpg's dict-shaped jsonb.
    """

    __slots__ = ("_values", "_keys")

    def __init__(self, keys: list[str], values: list[Any]) -> None:
        self._keys = keys
        self._values = [
            _decode_value(v) for v in values
        ]

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        try:
            idx = self._keys.index(key)
        except ValueError:
            raise KeyError(key) from None
        return self._values[idx]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self) -> list[str]:
        return list(self._keys)

    def as_dict(self) -> dict[str, Any]:
        return dict(zip(self._keys, self._values, strict=True))


def _decode_value(value: Any) -> Any:
    """Convert a raw sqlite value into the shape asyncpg would return.

    * JSONB TEXT -> parsed dict/list (matches asyncpg's jsonb codec).
    * ISO-8601 timestamps -> ``datetime`` (asyncpg returns datetimes for
      timestamptz; callers do ``.isoformat()`` on them).
    * uuid-shaped TEXT -> ``UUID`` (asyncpg returns UUID objects; callers
      do ``str(row["id"])``).
    """
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    text = str(value)

    # JSONB payloads: any string that is valid JSON and looks like an
    # object/array is decoded. Plain strings stay strings.
    if text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            pass

    # Timestamps: asyncpg returns tz-aware datetimes. Store ISO-8601.
    if _looks_like_timestamp(text):
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass

    # UUIDs: asyncpg returns UUID objects.
    if _looks_like_uuid(text):
        try:
            return UUID(text)
        except ValueError:
            pass

    return text


def _looks_like_timestamp(text: str) -> bool:
    if len(text) < 19:
        return False
    return text[4] == "-" and text[7] == "-" and text[10] == "T"


def _looks_like_uuid(text: str) -> bool:
    if len(text) != 36:
        return False
    return text[8] == "-" and text[13] == "-" and text[18] == "-" and text[23] == "-"


class LiteConnection:
    """One sqlite connection. asyncpg-Connection-shaped enough for this app."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        #: pending fetch args per cursor row — set by _execute
        self._last_rowcount = 0

    # ------------------------------------------------------------- exec ----
    async def execute(self, sql: str, *args: Any) -> str:
        """Run a statement; return a pseudo-command-tag like asyncpg."""
        sql = translate_sql(sql)
        sqlite_args = [_encode_param(a) for a in args]
        await asyncio.to_thread(self._run, sql, sqlite_args)
        return f"OK {self._last_rowcount}"

    def _run(self, sql: str, args: list[Any]) -> None:
        with self._conn:
            cur = self._conn.execute(sql, args)
            self._last_rowcount = cur.rowcount

    async def fetchrow(self, sql: str, *args: Any) -> LiteRow | None:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = await self.fetchrow(sql, *args)
        if row is None:
            return None
        return row[0]

    async def fetch(self, sql: str, *args: Any) -> list[LiteRow]:
        sql = translate_sql(sql)
        sqlite_args = [_encode_param(a) for a in args]
        return await asyncio.to_thread(self._fetch, sql, sqlite_args)

    def _fetch(self, sql: str, args: list[Any]) -> list[LiteRow]:
        cur = self._conn.execute(sql, args)
        try:
            keys = [d[0] for d in cur.description] if cur.description else []
            return [LiteRow(keys, list(row)) for row in cur.fetchall()]
        finally:
            cur.close()

    async def close(self) -> None:
        await asyncio.to_thread(self._conn.close)

    def __enter__(self) -> LiteConnection:
        return self

    def __exit__(self, *exc_info) -> None:
        return None


def _encode_param(value: Any) -> Any:
    """asyncpg accepts UUIDs, datetimes, dicts, lists; sqlite needs scalars."""
    if value is None or isinstance(value, (int, float, str, bytes, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value)
    return str(value)


class LitePool:
    """A sqlite-backed stand-in for ``asyncpg.Pool``.

    Only the methods this codebase actually uses are implemented —
    deliberately. Anything missing fails with AttributeError in the tests,
    which is the correct way to learn that lite mode needs a new method.
    """

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self._path = str(path)
        self._read_only = read_only
        self._lock = asyncio.Lock()

    @property
    def path(self) -> str:
        return self._path

    # ------------------------------------------------------------ acquire ----
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[LiteConnection]:
        """asyncpg-compatible: ``async with pool.acquire() as conn:``"""
        async with self._lock:
            conn = await asyncio.to_thread(self._connect)
            try:
                yield LiteConnection(conn)
            finally:
                await asyncio.to_thread(conn.close)

    def _connect(self) -> sqlite3.Connection:
        # URI must use forward slashes; a Windows backslash path would
        # break the file: URI parser.
        uri_path = self._path.replace("\\", "/")
        mode = "ro" if self._read_only else "rwc"
        conn = sqlite3.connect(
            f"file:{uri_path}?mode={mode}", uri=True, timeout=30,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # Autocommit: python's sqlite3 opens an implicit transaction on
        # DML, and without a commit every write is rolled back when the
        # per-acquire connection closes. This app never needs a
        # multi-statement transaction (idempotency keys do the dedup), so
        # autocommit is both correct and safe here.
        conn.isolation_level = None
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # ------------------------------------------------ pool-level shortcuts ----
    async def fetchrow(self, sql: str, *args: Any) -> LiteRow | None:
        async with self.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetchval(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[LiteRow]:
        async with self.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        async with self.acquire() as conn:
            return await conn.execute(sql, *args)

    async def close(self) -> None:
        return None  # connections are per-acquire; nothing to pool-close

    # -------------------------------------------------------------- schema ---
    async def init_schema(self) -> None:
        """Create the lite schema (idempotent). Mirrors migrations/*.sql."""
        from bucker.lite.schema import SCHEMA_SQL

        async with self.acquire() as conn:
            for statement in SCHEMA_SQL.split(";"):
                stmt = statement.strip()
                if stmt:
                    await conn.execute(stmt)


def sqlite_url_to_path(dsn: str) -> str:
    """``sqlite:///abs/path.db`` (or ``sqlite://relative.db``) -> path."""
    rest = dsn.removeprefix("sqlite:///")
    if rest.startswith("/") and len(rest) > 1 and rest[1] != "/":
        # absolute path on unix-style: sqlite:////tmp/x.db -> /tmp/x.db
        return rest
    return rest

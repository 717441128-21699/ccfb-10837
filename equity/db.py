"""SQLite 小工具：线程局部连接 + 同步驱动的异步封装。

SQLite 的 async 驱动不在标准库内，统一用 ``asyncio.to_thread`` 包一层。
关键：``sqlite3.Connection`` 默认只允许在创建它的线程使用，而线程池会把
不同调用派给不同线程，因此这里用 ``threading.local`` 让每个工作线程持有
独立连接（WAL 模式下多读单写、busy_timeout 自动等锁），事件循环不被阻塞。
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any


def _make_connection(path: str | Path, readonly: bool) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        uri = f"file:{p.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """同步连接（仿真站服务端、分析服务等单线程场景使用）。"""
    return _make_connection(path, readonly)


class AsyncDB:
    def __init__(self, path: str | Path, *, readonly: bool = False):
        self.path = str(path)
        self.readonly = readonly
        self._local = threading.local()
        self._write_lock = asyncio.Lock()  # 串行化写事务，减少 WAL 写锁竞争
        self._open = True

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _make_connection(self.path, self.readonly)
            self._local.conn = conn
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """同步上下文（同一线程内）使用的连接。"""
        return self._conn()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        self._open = False

    async def query(self, sql: str,
                    params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        def run() -> list[sqlite3.Row]:
            return self._conn().execute(sql, params).fetchall()
        return await asyncio.to_thread(run)

    async def query_one(self, sql: str,
                        params: Sequence[Any] = ()) -> sqlite3.Row | None:
        def run() -> sqlite3.Row | None:
            return self._conn().execute(sql, params).fetchone()
        return await asyncio.to_thread(run)

    async def execute(self, sql: str,
                      params: Sequence[Any] = ()) -> int:
        async with self._write_lock:
            def run() -> int:
                conn = self._conn()
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.lastrowid
            return await asyncio.to_thread(run)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        async with self._write_lock:
            def run() -> int:
                conn = self._conn()
                cur = conn.executemany(sql, list(rows))
                conn.commit()
                return cur.rowcount
            return await asyncio.to_thread(run)

    async def transaction(
            self, statements: Iterable[tuple[str, Sequence[Any]]]) -> None:
        """在单个事务里顺序执行多条语句。"""
        async with self._write_lock:
            def run() -> None:
                conn = self._conn()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    for sql, params in statements:
                        conn.execute(sql, params)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            await asyncio.to_thread(run)

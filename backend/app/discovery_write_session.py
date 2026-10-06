"""One callback's SQLite connection, with a separate transaction per identity."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from .database import Database


class DiscoveryWriteSession:
    """Lazy, explicitly owned connection; never a pool or a batch transaction.

    Construction performs no I/O. The callback owns this object before its first
    cancellable thread submission and closes it only after admitted work settles.
    Async worker calls may run on different threads, so a private guard protects
    connection ownership in addition to the database's normal writer lock.
    """

    def __init__(self, database: Database) -> None:
        self._database = database
        self._guard = threading.Lock()
        self._connection: sqlite3.Connection | None = None
        self._closed = False

    @contextmanager
    def write(self, database: Database) -> Iterator[sqlite3.Connection]:
        if database is not self._database:
            raise ValueError("Discovery session belongs to a different database")
        with self._guard, database._write_lock:
            if self._closed:
                raise RuntimeError("Discovery session is already closed")
            if self._connection is None:
                self._connection = database._connect_discovery()
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def close(self) -> None:
        with self._guard, self._database._write_lock:
            if self._closed:
                return
            self._closed = True
            connection, self._connection = self._connection, None
            if connection is not None:
                connection.close()

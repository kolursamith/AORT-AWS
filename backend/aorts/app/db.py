"""Database engine/session helpers (synchronous SQLAlchemy 2.x over psycopg 3)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


class Database:
    def __init__(self, url: str, pool_size: int = 10):
        self.engine: Engine = create_engine(
            url,
            pool_size=pool_size,
            max_overflow=pool_size * 2,
            pool_pre_ping=True,
            pool_timeout=10,
        )
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """One ACID transaction: commit on success, roll back on any exception."""
        session = self._factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()

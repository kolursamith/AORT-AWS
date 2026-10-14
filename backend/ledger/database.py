"""Database engine factory; PostgreSQL is required outside unit tests."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def database_url() -> str:
    return os.getenv(
        "AORT_DATABASE_URL",
        "postgresql+psycopg://aort:aort_local_only@localhost:5432/aort",
    )


engine = create_engine(database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

import hashlib
import json
import os
import secrets
from datetime import datetime
from typing import TypedDict

import valkey
from sqlalchemy import DateTime, Engine, Index, create_engine, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://shortener:shortener@localhost:5432/shortener"
)
VALKEY_URL = os.getenv("VALKEY_URL", "redis://localhost:6379/0")


class Base(DeclarativeBase):
    pass


class Url(Base):
    __tablename__ = "urls"

    id: Mapped[str] = mapped_column(primary_key=True)
    url: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# id lookups use the primary key. url lookups use this index; it hashes the url
# because btree entries are capped (~2.7kB) and urls can be longer. The unique
# constraint also guarantees one id per url.
Index("urls_url_md5_key", func.md5(Url.url), unique=True)


def init_postgres() -> Engine:
    # SQLAlchemy defaults "postgresql://" to psycopg2; we use psycopg 3.
    url = make_url(DATABASE_URL).set(drivername="postgresql+psycopg")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return engine


def init_valkey() -> valkey.Valkey:
    client = valkey.Valkey.from_url(VALKEY_URL, decode_responses=True)
    client.ping()
    return client


class DatabaseUrlRecord(TypedDict):
    id: str
    url: str
    created_at: str

def _url_key(url: str) -> str:
    return f"url:{hashlib.sha256(url.encode()).hexdigest()}"


def get_id_by_url_from_valkey(client: valkey.Valkey, url: str) -> str | None:
    """Return the cached id for a raw url, or None on a cache miss."""
    return client.get(_url_key(url))


def get_row_by_url_id_from_valkey(
    client: valkey.Valkey, url_id: str
) -> DatabaseUrlRecord | None:
    """Return the cached row (stored as JSON under `id:<url_id>`), or None on a miss."""
    raw = client.get(f"id:{url_id}")
    return json.loads(raw) if raw else None


def _to_record(row: Url | None) -> DatabaseUrlRecord | None:
    if row is None:
        return None
    return DatabaseUrlRecord(
        id=row.id, url=row.url, created_at=row.created_at.isoformat()
    )


def get_row_by_id_from_postgres(
    engine: Engine, url_id: str
) -> DatabaseUrlRecord | None:
    with Session(engine) as session:
        return _to_record(session.get(Url, url_id))


def get_row_by_url_from_postgres(engine: Engine, url: str) -> DatabaseUrlRecord | None:
    # md5(url) matches the unique index; `url = ...` guards against hash collisions.
    stmt = select(Url).where(func.md5(Url.url) == func.md5(url), Url.url == url)
    with Session(engine) as session:
        return _to_record(session.scalars(stmt).one_or_none())


def insert_row_into_postgres(engine: Engine, url: str) -> DatabaseUrlRecord:
    """Insert a new url with a random id, or return the row that already has it.

    ON CONFLICT DO NOTHING covers both an id collision and another request
    inserting the same url first; we tell them apart by looking the url up again.
    """
    while True:
        stmt = (
            insert(Url)
            .values(id=secrets.token_urlsafe(6), url=url)
            .on_conflict_do_nothing()
            .returning(Url)
        )
        with Session(engine) as session, session.begin():
            row = session.scalars(stmt).one_or_none()
            record = _to_record(row)
        if record is not None:
            return record
        existing = get_row_by_url_from_postgres(engine, url)
        if existing is not None:
            return existing


CACHE_TTL_SECONDS = 24 * 60 * 60


def cache_put_in_valkey(client: valkey.Valkey, record: DatabaseUrlRecord) -> None:
    """Cache a row under `id:<id>` and its `url:<hash>` pointer, atomically."""
    pipe = client.pipeline(transaction=True)
    pipe.set(f"id:{record['id']}", json.dumps(record), ex=CACHE_TTL_SECONDS)
    pipe.set(_url_key(record["url"]), record["id"], ex=CACHE_TTL_SECONDS)
    pipe.execute()


def delete_row_from_postgres(
    engine: Engine, url_id: str
) -> DatabaseUrlRecord | None:
    """Delete a row by id and return it, or None if there was no such row."""
    stmt = delete(Url).where(Url.id == url_id).returning(Url)
    with Session(engine) as session, session.begin():
        return _to_record(session.scalars(stmt).one_or_none())


def delete_from_valkey(client: valkey.Valkey, record: DatabaseUrlRecord) -> None:
    client.delete(f"id:{record['id']}", _url_key(record["url"]))

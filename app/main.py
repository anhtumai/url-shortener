from contextlib import asynccontextmanager

import valkey
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import Engine
from pydantic import BaseModel, HttpUrl

from app.db import (
    DatabaseUrlRecord,
    cache_put_in_valkey,
    delete_from_valkey,
    delete_row_from_postgres,
    get_id_by_url_from_valkey,
    get_row_by_id_from_postgres,
    get_row_by_url_from_postgres,
    get_row_by_url_id_from_valkey,
    init_postgres,
    init_valkey,
    insert_row_into_postgres,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pg = init_postgres()
    app.state.valkey = init_valkey()
    yield
    app.state.valkey.close()
    app.state.pg.dispose()


app = FastAPI(title="URL Shortener", lifespan=lifespan)


class ShortenRequest(BaseModel):
    url: HttpUrl


class ShortenResponse(BaseModel):
    id: str


def get_valkey(request: Request) -> valkey.Valkey:
    return request.app.state.valkey


def get_pg(request: Request) -> Engine:
    return request.app.state.pg


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@app.post("/shortener")
def shortener(
    body: ShortenRequest,
    valkey_client: valkey.Valkey = Depends(get_valkey),
    pg: Engine = Depends(get_pg),
) -> ShortenResponse:
    # If url exists in the cache, reuse its id
    cached_id = get_id_by_url_from_valkey(valkey_client, str(body.url))
    if cached_id:
        return ShortenResponse(id=cached_id)

    # If url exists in the database, reuse its id; otherwise build a new row
    row = get_row_by_url_from_postgres(pg, str(body.url))
    if row is None:
        row = insert_row_into_postgres(pg, str(body.url))

    cache_put_in_valkey(valkey_client, row)
    return ShortenResponse(id=row["id"])


def find_row_by_id(
    valkey_client: valkey.Valkey, pg: Engine, url_id: str
) -> DatabaseUrlRecord | None:
    """Cache first, then Postgres (refilling the cache on a hit)."""
    row = get_row_by_url_id_from_valkey(valkey_client, url_id)
    if row is None:
        row = get_row_by_id_from_postgres(pg, url_id)
        if row is not None:
            cache_put_in_valkey(valkey_client, row)
    return row


@app.get("/shortener/{url_id}")
def get_by_id(
    url_id: str,
    valkey_client: valkey.Valkey = Depends(get_valkey),
    pg: Engine = Depends(get_pg),
) -> DatabaseUrlRecord:
    row = find_row_by_id(valkey_client, pg, url_id)
    if row is None:
        raise HTTPException(status_code=404, detail="id not found")
    return row


@app.get("/shortener")
def get_by_url(
    url: HttpUrl,
    valkey_client: valkey.Valkey = Depends(get_valkey),
    pg: Engine = Depends(get_pg),
) -> DatabaseUrlRecord:
    cached_id = get_id_by_url_from_valkey(valkey_client, str(url))
    row = find_row_by_id(valkey_client, pg, cached_id) if cached_id else None
    if row is None:
        row = get_row_by_url_from_postgres(pg, str(url))
        if row is not None:
            cache_put_in_valkey(valkey_client, row)
    if row is None:
        raise HTTPException(status_code=404, detail="url not found")
    return row


@app.delete("/shortener/{url_id}", status_code=204)
def delete(
    url_id: str,
    valkey_client: valkey.Valkey = Depends(get_valkey),
    pg: Engine = Depends(get_pg),
) -> Response:
    row = delete_row_from_postgres(pg, url_id)
    if row is None:
        raise HTTPException(status_code=404, detail="id not found")
    delete_from_valkey(valkey_client, row)
    return Response(status_code=204)


# Registered last so it can't shadow the fixed routes like /health and /docs.
@app.get("/{url_id}")
def redirect(
    url_id: str,
    valkey_client: valkey.Valkey = Depends(get_valkey),
    pg: Engine = Depends(get_pg),
) -> RedirectResponse:
    row = find_row_by_id(valkey_client, pg, url_id)
    if row is None:
        raise HTTPException(status_code=404, detail="id not found")
    # 307 (not 301) so browsers don't cache it and deleted links stop working.
    return RedirectResponse(row["url"], status_code=307)

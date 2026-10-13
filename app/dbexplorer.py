"""
Read-only database explorer: lists every table in the `public` schema with its
columns and row count, and serves any table's rows a page at a time.

Safety: table names are validated against information_schema before being
used as a quoted identifier (never interpolated from user input directly),
every statement is a SELECT, and page size is capped. If ADMIN_TOKEN is set,
requests must carry ?token=<value>, same as /admin/reset-sale.

Serves the frontend's /database page.
"""
import base64
import datetime as dt
import decimal
import os
import uuid

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from app.database import SessionLocal

router = APIRouter(prefix="/db-explorer", tags=["db-explorer"])
MAX_PAGE = 500


def _check_token(token: str):
    required = os.getenv("ADMIN_TOKEN", "")
    if required and token != required:
        raise HTTPException(403, "Bad or missing admin token.")


def _jsonable(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (uuid.UUID, decimal.Decimal)):
        return str(v)
    if isinstance(v, (bytes, memoryview)):
        return "0x" + base64.b16encode(bytes(v)).decode()[:32]
    return str(v)


def _tables(db):
    rows = db.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
    )).fetchall()
    return [r[0] for r in rows]


def _columns(db, table):
    pk = {r[0] for r in db.execute(text(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary"
    ), {"t": f'public."{table}"'})}
    cols = db.execute(text(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t ORDER BY ordinal_position"
    ), {"t": table}).fetchall()
    return [{"name": c[0], "type": c[1], "nullable": c[2] == "YES", "primary_key": c[0] in pk} for c in cols]


@router.get("/tables")
def list_tables(token: str = ""):
    _check_token(token)
    db = SessionLocal()
    try:
        out = []
        for t in _tables(db):
            n = db.execute(text(f'SELECT count(*) FROM public."{t}"')).scalar()
            out.append({"name": t, "rows": n, "columns": _columns(db, t)})
        return {"tables": out}
    finally:
        db.close()


@router.get("/tables/{name}")
def table_rows(
    name: str,
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    token: str = "",
):
    _check_token(token)
    db = SessionLocal()
    try:
        if name not in _tables(db):
            raise HTTPException(404, "No such table.")
        cols = _columns(db, name)
        order = ", ".join(f'"{c["name"]}"' for c in cols if c["primary_key"]) or "1"
        total = db.execute(text(f'SELECT count(*) FROM public."{name}"')).scalar()
        rows = db.execute(
            text(f'SELECT * FROM public."{name}" ORDER BY {order} LIMIT :l OFFSET :o'),
            {"l": limit, "o": offset},
        ).fetchall()
        return {
            "table": name, "total": total, "limit": limit, "offset": offset,
            "columns": cols,
            "rows": [[_jsonable(v) for v in r] for r in rows],
        }
    finally:
        db.close()


def install(app):
    app.include_router(router)

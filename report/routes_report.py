"""
routes_report.py
────────────────
Cursor-paginated HTTP access to a *_report.parquet file.

    GET /api/report/{file_id}/page?page_size=20&cursor=<token>&raw=false

Pagination contract
-------------------
- First page: omit `cursor`.
- Each response carries pagination.next_cursor.
- Fetch the next page by passing that token back as `cursor`.
- next_cursor == null  →  last page reached.
The cursor is opaque; clients echo next_cursor back unchanged, never parse it.

Performance
-----------
Each parquet is loaded + sorted once, then cached in-process keyed by
(path, mtime). Repeated page requests for an unchanged file are served from
memory. If the file is rewritten, its mtime changes and the cache rebuilds.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from output_writer import OUTPUT_DIR as REPORT_DIR
from report.pagination import load_report, get_page

router = APIRouter()

# load-once cache: path -> (mtime, sorted DataFrame)
_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_cache_lock = threading.Lock()


def _report_path(file_id: str) -> Path:
    if "/" in file_id or "\\" in file_id or ".." in file_id:
        raise HTTPException(400, "Invalid file_id.")
    return REPORT_DIR / f"{file_id}_report.parquet"


def _get_sorted_df(file_id: str) -> pd.DataFrame:
    path = _report_path(file_id)
    if not path.exists():
        raise HTTPException(404, f"No report parquet for file_id '{file_id}'.")

    mtime = path.stat().st_mtime
    key = str(path)

    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]

    try:
        df = load_report(path)
    except (KeyError, FileNotFoundError) as e:
        raise HTTPException(422, f"Could not read report: {e}")

    with _cache_lock:
        _cache[key] = (mtime, df)
    return df


@router.get("/{file_id}/page", summary="Fetch one cursor-paginated page of the report")
async def get_report_page(
    file_id: str,
    page_size: int = Query(default=20, ge=1, le=500, description="Rows per page"),
    cursor: Optional[str] = Query(default=None, description="Opaque token from a previous page's next_cursor"),
    raw: bool = Query(default=False, description="Return JSON columns as raw strings instead of nested objects"),
):
    df = _get_sorted_df(file_id)
    try:
        page = get_page(df, page_size=page_size, cursor=cursor, decode_json=not raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    page["file_id"] = file_id
    return JSONResponse(page)


@router.get("/", summary="List available report file_ids")
async def list_reports():
    files = sorted(REPORT_DIR.glob("*_report.parquet"))
    return {
        "reports": [
            {"file_id": p.stem.replace("_report", ""),
             "file": p.name,
             "size_mb": round(p.stat().st_size / 1024 / 1024, 2)}
            for p in files
        ],
        "report_dir": str(REPORT_DIR),
    }


@router.delete("/{file_id}/cache", summary="Evict a report from the in-process cache")
async def evict_cache(file_id: str):
    key = str(_report_path(file_id))
    with _cache_lock:
        existed = _cache.pop(key, None) is not None
    return {"file_id": file_id, "evicted": existed}

"""
routes_report.py
────────────────
Cursor-paginated HTTP access to a *_report.parquet file, addressed by the
same (data_type, ip_name, file_id) layout used for storage:

    GET /api/report/{data_type}/{ip_name}/{file_id}/page   (beneficiary / banks / financials)
    GET /api/report/{data_type}/{file_id}/page             (certificates — no ip_name)

    ?page_size=20&cursor=<token>&raw=false

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

from output_writer import resolve_dir, iter_all_outputs, InvalidDataLocation
from report.pagination import load_report, get_page

router = APIRouter()

# load-once cache: path -> (mtime, sorted DataFrame)
_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_cache_lock = threading.Lock()


def _report_path(data_type: str, file_id: str, ip_name: Optional[str] = None) -> Path:
    if "/" in file_id or "\\" in file_id or ".." in file_id:
        raise HTTPException(400, "Invalid file_id.")
    try:
        folder = resolve_dir(data_type, ip_name, create=False)
    except InvalidDataLocation as e:
        raise HTTPException(400, str(e))
    return folder / f"{file_id}_report.parquet"


def _get_sorted_df(data_type: str, file_id: str, ip_name: Optional[str] = None) -> pd.DataFrame:
    path = _report_path(data_type, file_id, ip_name)
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


@router.get("/{data_type}/{ip_name}/{file_id}/page", summary="Fetch one cursor-paginated page (beneficiary / banks / financials)")
@router.get("/{data_type}/{file_id}/page", summary="Fetch one cursor-paginated page (certificates)")
async def get_report_page(
    data_type: str,
    file_id: str,
    ip_name: Optional[str] = None,
    page_size: int = Query(default=20, ge=1, le=5000, description="Rows per page"),
    cursor: Optional[str] = Query(default=None, description="Opaque token from a previous page's next_cursor"),
    page: Optional[int] = Query(default=None, ge=1, description="Jump directly to this 1-based page number, computed from page_size — e.g. page_size=500 on 2,000 rows gives 4 pages, and page=4 jumps straight there without paging through 1-3. Takes precedence over cursor if both are given."),
    raw: bool = Query(default=False, description="Return JSON columns as raw strings instead of nested objects"),
):
    df = _get_sorted_df(data_type, file_id, ip_name)
    try:
        page_result = get_page(df, page_size=page_size, cursor=cursor, decode_json=not raw, page=page)
    except ValueError as e:
        raise HTTPException(400, str(e))
    page["file_id"]   = file_id
    page["data_type"] = data_type.lower()
    page["ip_name"]   = ip_name

    # Attach validation summary from sidecar JSON if present
    import json as _json
    try:
        from output_writer import resolve_dir
        out_dir = resolve_dir(data_type, ip_name, create=False)
        sidecar = out_dir / f"{file_id}_validation_summary.json"
        if sidecar.exists():
            page["validation_summary"] = _json.loads(sidecar.read_text())
    except Exception:
        pass

    return JSONResponse(page)


@router.get("/", summary="List available report file_ids across every type/IP folder")
async def list_reports():
    files = sorted(iter_all_outputs(kind="report"), key=lambda t: t[2])
    return {
        "reports": [
            {"data_type": data_type, "ip_name": ip_name, "file_id": file_id,
             "file": p.name, "size_mb": round(p.stat().st_size / 1024 / 1024, 2)}
            for data_type, ip_name, file_id, p in files
        ],
    }


@router.delete("/{data_type}/{ip_name}/{file_id}/cache", summary="Evict a report from the in-process cache")
@router.delete("/{data_type}/{file_id}/cache", summary="Evict a report from the in-process cache (certificates)")
async def evict_cache(data_type: str, file_id: str, ip_name: Optional[str] = None):
    key = str(_report_path(data_type, file_id, ip_name))
    with _cache_lock:
        existed = _cache.pop(key, None) is not None
    return {"data_type": data_type.lower(), "ip_name": ip_name, "file_id": file_id, "evicted": existed}

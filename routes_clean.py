"""
routes_clean.py

Every endpoint is now addressed by (data_type, ip_name, file_id) so that
outputs land in the matching type/IP folder on disk:

    beneficiary | banks | financials  -> /{data_type}/{ip_name}/{file_id}/...
    certificates                       -> /{data_type}/{file_id}/...          (no ip_name)

POST /api/clean/{data_type}/{ip_name}/{file_id}        – run pipeline (beneficiary/banks/financials)
POST /api/clean/{data_type}/{file_id}                  – run pipeline (certificates)
GET  .../rows                – paginated per-row report (from parquet)
GET  .../summary              – summary stats
GET  .../download/cleaned     – cleaned dataset parquet
GET  .../download/report      – per-record report parquet
GET  .../download/excel       – cleaned XLSX (on-demand, slow)
GET  /api/clean/logs          – list past runs across every type/IP folder
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from cleaning_engine import clean_dataframe_fast
from file_handler import load_dataframe, save_cleaned_excel, delete_file
from output_writer import (
    write_outputs,
    read_report,
    resolve_dir,
    iter_all_outputs,
    InvalidDataLocation,
)

router = APIRouter()

# in-memory session, keyed by "{data_type}:{ip_name or ''}:{file_id}"
_cache: dict[str, dict] = {}


def _cache_key(data_type: str, ip_name: Optional[str], file_id: str) -> str:
    return f"{data_type.lower()}:{(ip_name or '').lower()}:{file_id}"


def _location_or_400(data_type: str, ip_name: Optional[str]):
    """Validate the (data_type, ip_name) combo, raising a friendly 400 on bad input."""
    try:
        resolve_dir(data_type, ip_name, create=False)
    except InvalidDataLocation as e:
        raise HTTPException(400, str(e))


# ── summary builder ───────────────────────────────────────────────────────────

def _summarise(result: dict) -> dict:
    total       = len(result)
    clean_rows  = 0; review_rows = 0; dup_rows = 0
    clean_cells = 0; review_cells = 0
    step_counts: dict[str, int] = {}
    review_cols: dict[str, int] = {}

    for v in result.values():
        cv = v["cleaned_values"]; rv = v["manual_reviews_required"]
        if cv:  clean_rows  += 1
        if rv:  review_rows += 1
        if v["IS DUPLICATED UUID"]: dup_rows += 1
        for _, (__, step) in cv.items():
            clean_cells += 1
            for s in step.split(" | "):
                step_counts[s] = step_counts.get(s, 0) + 1
        for col in rv:
            review_cells += 1
            review_cols[col] = review_cols.get(col, 0) + 1

    return {
        "total_rows":          total,
        "rows_auto_cleaned":   clean_rows,
        "rows_need_review":    review_rows,
        "duplicate_uuid_rows": dup_rows,
        "cells_auto_cleaned":  clean_cells,
        "cells_flagged":       review_cells,
        "step_breakdown":      dict(sorted(step_counts.items(), key=lambda x: -x[1])),
        "review_by_column":    dict(sorted(review_cols.items(),  key=lambda x: -x[1])),
    }


# ── POST /api/clean/{data_type}/{ip_name}/{file_id}  (or .../{data_type}/{file_id} for certificates) ──

@router.post("/{data_type}/{ip_name}/{file_id}", summary="Run cleaning (beneficiary / banks / financials) — saves to <type>/<ip_name>/")
@router.post("/{data_type}/{file_id}", summary="Run cleaning (certificates) — saves to <type>/")
async def clean(
    data_type: str,
    file_id: str,
    ip_name: Optional[str] = None,
    uuid_column: Optional[str] = Query(default=None),
):
    _location_or_400(data_type, ip_name)

    try:
        df, _ = load_dataframe(file_id)
    except HTTPException:
        raise

    try:
        cleaned_df, result = clean_dataframe_fast(df, uuid_column=uuid_column)
    except Exception as e:
        raise HTTPException(500, f"Cleaning failed: {e}")

    # auto-save both parquet files into the type/ip folder
    try:
        meta = write_outputs(file_id, cleaned_df, result, data_type=data_type, ip_name=ip_name)
    except InvalidDataLocation as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to write output files: {e}")

    # source file has been consumed — remove the uploaded copy to free disk
    delete_file(file_id)

    summary = _summarise(result)
    key = _cache_key(data_type, ip_name, file_id)
    _cache[key] = {"summary": summary, "result": result, "cleaned_df": cleaned_df}

    base = f"/api/clean/{meta['data_type']}" + (f"/{ip_name}" if ip_name else "")

    return JSONResponse({
        "file_id":   file_id,
        "data_type": meta["data_type"],
        "ip_name":   ip_name,
        "summary":   summary,
        "output_files": {
            "cleaned":          str(meta["cleaned_path"].name),
            "report":           str(meta["report_path"].name),
            "format":           meta["ext"],
            "cleaned_size_mb":  meta["cleaned_size_mb"],
            "report_size_mb":   meta["report_size_mb"],
            "total_size_mb":    meta["total_size_mb"],
            "saved_to":         str(meta["output_dir"]),
        },
        "download_urls": {
            "cleaned_dataset":   f"{base}/{file_id}/download/cleaned",
            "per_record_report": f"{base}/{file_id}/download/report",
            "excel":             f"{base}/{file_id}/download/excel",
        },
    })


# ── GET rows ───────────────────────────────────────────────────────────────────

@router.get("/{data_type}/{ip_name}/{file_id}/rows", summary="Paginated per-record report (beneficiary / banks / financials)")
@router.get("/{data_type}/{file_id}/rows", summary="Paginated per-record report (certificates)")
async def get_rows(
    data_type: str,
    file_id: str,
    ip_name: Optional[str] = None,
    page:      int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    filter:    str = Query(default="all", description="all | cleaned | review | dup"),
):
    _location_or_400(data_type, ip_name)
    key = _cache_key(data_type, ip_name, file_id)

    if key not in _cache:
        # try loading from saved parquet
        try:
            report_df = read_report(file_id, data_type=data_type, ip_name=ip_name)
        except InvalidDataLocation as e:
            raise HTTPException(400, str(e))
        if report_df is None:
            raise HTTPException(404, "Run POST /api/clean/{data_type}/[ip_name]/{file_id} first.")
        # rebuild result from parquet
        result = {}
        for _, row in report_df.iterrows():
            result[row["uuid"]] = {
                "original_values":         json.loads(row["original_values"] or "{}"),
                "cleaned_values":          json.loads(row["cleaned_values"]  or "{}"),
                "manual_reviews_required": json.loads(row["manual_reviews"]  or "{}"),
                "IS DUPLICATED UUID":      bool(row["is_dup"]),
            }
        _cache[key] = {"summary": _summarise(result), "result": result, "cleaned_df": None}

    result = _cache[key]["result"]
    keys   = list(result.keys())

    if filter == "cleaned": keys = [k for k in keys if result[k]["cleaned_values"]]
    elif filter == "review": keys = [k for k in keys if result[k]["manual_reviews_required"]]
    elif filter == "dup":    keys = [k for k in keys if result[k]["IS DUPLICATED UUID"]]

    total = len(keys)
    start = (page - 1) * page_size
    rows  = {k: result[k] for k in keys[start: start + page_size]}

    return JSONResponse({
        "pagination": {
            "page": page, "page_size": page_size,
            "total_rows": total,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        },
        "rows": rows,
    })


# ── GET summary ───────────────────────────────────────────────────────────────

@router.get("/{data_type}/{ip_name}/{file_id}/summary")
@router.get("/{data_type}/{file_id}/summary")
async def get_summary(data_type: str, file_id: str, ip_name: Optional[str] = None):
    _location_or_400(data_type, ip_name)
    key = _cache_key(data_type, ip_name, file_id)
    if key not in _cache:
        raise HTTPException(404, "Run cleaning first.")
    return JSONResponse({"file_id": file_id, "data_type": data_type.lower(), "ip_name": ip_name,
                          "summary": _cache[key]["summary"]})


# ── downloads ─────────────────────────────────────────────────────────────────

@router.get("/{data_type}/{ip_name}/{file_id}/download/cleaned", summary="Download cleaned dataset parquet")
@router.get("/{data_type}/{file_id}/download/cleaned", summary="Download cleaned dataset parquet (certificates)")
async def dl_cleaned(data_type: str, file_id: str, ip_name: Optional[str] = None):
    try:
        folder = resolve_dir(data_type, ip_name, create=False)
    except InvalidDataLocation as e:
        raise HTTPException(400, str(e))
    for ext in (".parquet", ".csv"):
        p = folder / f"{file_id}_cleaned{ext}"
        if p.exists():
            return FileResponse(p, filename=p.name,
                                media_type="application/octet-stream" if ext==".parquet" else "text/csv")
    raise HTTPException(404, "File not found. Run cleaning first.")


@router.get("/{data_type}/{ip_name}/{file_id}/download/report", summary="Download per-record report parquet")
@router.get("/{data_type}/{file_id}/download/report", summary="Download per-record report parquet (certificates)")
async def dl_report(data_type: str, file_id: str, ip_name: Optional[str] = None):
    try:
        folder = resolve_dir(data_type, ip_name, create=False)
    except InvalidDataLocation as e:
        raise HTTPException(400, str(e))
    for ext in (".parquet", ".csv"):
        p = folder / f"{file_id}_report{ext}"
        if p.exists():
            return FileResponse(p, filename=p.name,
                                media_type="application/octet-stream" if ext==".parquet" else "text/csv")
    raise HTTPException(404, "File not found. Run cleaning first.")


@router.get("/{data_type}/{ip_name}/{file_id}/download/excel", summary="Download cleaned XLSX (slow for large files)")
@router.get("/{data_type}/{file_id}/download/excel", summary="Download cleaned XLSX (certificates)")
async def dl_excel(data_type: str, file_id: str, ip_name: Optional[str] = None):
    _location_or_400(data_type, ip_name)
    key = _cache_key(data_type, ip_name, file_id)
    if key not in _cache or _cache[key]["cleaned_df"] is None:
        raise HTTPException(404, "Run cleaning first.")
    path = save_cleaned_excel(_cache[key]["cleaned_df"], file_id)
    return FileResponse(path, filename=path.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ── logs ──────────────────────────────────────────────────────────────────────

@router.get("/logs", summary="List past cleaning runs across every type/IP folder")
async def logs():
    runs = sorted(
        iter_all_outputs(kind="report"),
        key=lambda t: t[3].stat().st_mtime,
        reverse=True,
    )
    return JSONResponse({
        "runs": [{
            "data_type": data_type,
            "ip_name":   ip_name,
            "file_id":   file_id,
            "report":    p.name,
            "cleaned":   p.name.replace("_report", "_cleaned"),
            "size_mb":   round(p.stat().st_size / 1024 / 1024, 2),
            "saved_at":  datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "path":      str(p.parent),
        } for data_type, ip_name, file_id, p in runs[:50]],
    })

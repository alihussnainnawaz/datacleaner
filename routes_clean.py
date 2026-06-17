"""
routes_clean.py

POST /api/clean/{file_id}                   – run pipeline, auto-save both parquets
GET  /api/clean/{file_id}/rows              – paginated per-row report (from parquet)
GET  /api/clean/{file_id}/summary           – summary stats
GET  /api/clean/{file_id}/download/cleaned  – cleaned dataset parquet
GET  /api/clean/{file_id}/download/report   – per-record report parquet
GET  /api/clean/{file_id}/download/excel    – cleaned XLSX (on-demand, slow)
GET  /api/clean/logs                        – list past runs
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from cleaning_engine import clean_dataframe_fast
from file_handler import load_dataframe, save_cleaned_excel, delete_file
from output_writer import OUTPUT_DIR, write_outputs, read_report

router = APIRouter()

# in-memory session: file_id → {summary, result, cleaned_df}
_cache: dict[str, dict] = {}


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


# ── POST /api/clean/{file_id} ─────────────────────────────────────────────────

@router.post("/{file_id}", summary="Run cleaning — saves cleaned + report parquet to beneficiary/")
async def clean(
    file_id: str,
    uuid_column: Optional[str] = Query(default=None),
):
    try:
        df, _ = load_dataframe(file_id)
    except HTTPException:
        raise

    try:
        cleaned_df, result = clean_dataframe_fast(df, uuid_column=uuid_column)
    except Exception as e:
        raise HTTPException(500, f"Cleaning failed: {e}")

    # auto-save both parquet files
    try:
        meta = write_outputs(file_id, cleaned_df, result)
    except Exception as e:
        raise HTTPException(500, f"Failed to write output files: {e}")

    # source file has been consumed — remove the uploaded copy to free disk
    delete_file(file_id)

    summary = _summarise(result)
    _cache[file_id] = {"summary": summary, "result": result, "cleaned_df": cleaned_df}

    return JSONResponse({
        "file_id": file_id,
        "summary": summary,
        "output_files": {
            "cleaned":          str(meta["cleaned_path"].name),
            "report":           str(meta["report_path"].name),
            "format":           meta["ext"],
            "cleaned_size_mb":  meta["cleaned_size_mb"],
            "report_size_mb":   meta["report_size_mb"],
            "total_size_mb":    meta["total_size_mb"],
            "saved_to":         "beneficiary/",
        },
        "download_urls": {
            "cleaned_dataset":  f"/api/clean/{file_id}/download/cleaned",
            "per_record_report":f"/api/clean/{file_id}/download/report",
            "excel":            f"/api/clean/{file_id}/download/excel",
        },
    })


# ── GET /api/clean/{file_id}/rows ─────────────────────────────────────────────

@router.get("/{file_id}/rows", summary="Paginated per-record report")
async def get_rows(
    file_id: str,
    page:      int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    filter:    str = Query(default="all", description="all | cleaned | review | dup"),
):
    if file_id not in _cache:
        # try loading from saved parquet
        report_df = read_report(file_id)
        if report_df is None:
            raise HTTPException(404, "Run POST /api/clean/{file_id} first.")
        # rebuild result from parquet
        result = {}
        for _, row in report_df.iterrows():
            result[row["uuid"]] = {
                "original_values":         json.loads(row["original_values"] or "{}"),
                "cleaned_values":          json.loads(row["cleaned_values"]  or "{}"),
                "manual_reviews_required": json.loads(row["manual_reviews"]  or "{}"),
                "IS DUPLICATED UUID":      bool(row["is_dup"]),
            }
        _cache[file_id] = {"summary": _summarise(result), "result": result, "cleaned_df": None}

    result = _cache[file_id]["result"]
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

@router.get("/{file_id}/summary")
async def get_summary(file_id: str):
    if file_id not in _cache:
        raise HTTPException(404, "Run cleaning first.")
    return JSONResponse({"file_id": file_id, "summary": _cache[file_id]["summary"]})


# ── downloads ─────────────────────────────────────────────────────────────────

@router.get("/{file_id}/download/cleaned", summary="Download cleaned dataset parquet")
async def dl_cleaned(file_id: str):
    for ext in (".parquet", ".csv"):
        p = OUTPUT_DIR / f"{file_id}_cleaned{ext}"
        if p.exists():
            return FileResponse(p, filename=p.name,
                                media_type="application/octet-stream" if ext==".parquet" else "text/csv")
    raise HTTPException(404, "File not found. Run cleaning first.")


@router.get("/{file_id}/download/report", summary="Download per-record report parquet")
async def dl_report(file_id: str):
    for ext in (".parquet", ".csv"):
        p = OUTPUT_DIR / f"{file_id}_report{ext}"
        if p.exists():
            return FileResponse(p, filename=p.name,
                                media_type="application/octet-stream" if ext==".parquet" else "text/csv")
    raise HTTPException(404, "File not found. Run cleaning first.")


@router.get("/{file_id}/download/excel", summary="Download cleaned XLSX (slow for large files)")
async def dl_excel(file_id: str):
    if file_id not in _cache or _cache[file_id]["cleaned_df"] is None:
        raise HTTPException(404, "Run cleaning first.")
    path = save_cleaned_excel(_cache[file_id]["cleaned_df"], file_id)
    return FileResponse(path, filename=path.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ── logs ──────────────────────────────────────────────────────────────────────

@router.get("/logs", summary="List past cleaning runs in beneficiary/")
async def logs():
    runs = sorted(OUTPUT_DIR.glob("*_report.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return JSONResponse({
        "runs": [{
            "file_id":   p.stem.replace("_report", ""),
            "report":    p.name,
            "cleaned":   p.name.replace("_report", "_cleaned"),
            "size_mb":   round(p.stat().st_size / 1024 / 1024, 2),
            "saved_at":  datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
        } for p in runs[:50]],
        "saved_to": str(OUTPUT_DIR),
    })
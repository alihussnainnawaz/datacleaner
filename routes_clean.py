"""
routes_clean.py

beneficiary / certificates  → POST /api/clean/{data_type}/{ip_name}/{file_id}
banks / financials          → POST /api/clean/{data_type}/{file_id}
download                    → GET  .../download/cleaned  (stem in URL, not file_id)
"""
from __future__ import annotations

from typing import Optional

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from cleaning_engine import clean_dataframe_fast

# One shared thread pool for CPU-bound cleaning work.
# max_workers=2 prevents RAM exhaustion if two large files are submitted together.
_CLEAN_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cleaner")
from file_handler import load_dataframe, delete_file
from output_writer import (
    write_outputs,
    resolve_dir,
    output_stem,
    InvalidDataLocation,
)

router = APIRouter()


def _location_or_400(data_type: str, ip_name: Optional[str]):
    try:
        resolve_dir(data_type, ip_name, create=False)
    except InvalidDataLocation as e:
        raise HTTPException(400, str(e))


# ── summary builder ────────────────────────────────────────────────────────────

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


# ── POST — with ip_name (beneficiary / certificates) ──────────────────────────

@router.post(
    "/{data_type}/{ip_name}/{file_id}",
    summary="Run cleaning — beneficiary / certificates (IP required)",
)
async def clean_with_ip(
    data_type: str,
    ip_name: str,
    file_id: str,
    uuid_column: Optional[str] = Query(default=None),
):
    return await _run_clean(data_type, file_id, ip_name, uuid_column)


# ── POST — without ip_name (banks / financials) ───────────────────────────────

@router.post(
    "/{data_type}/{file_id}",
    summary="Run cleaning — banks / financials (no IP)",
)
async def clean_no_ip(
    data_type: str,
    file_id: str,
    uuid_column: Optional[str] = Query(default=None),
):
    return await _run_clean(data_type, file_id, None, uuid_column)


# ── shared pipeline ───────────────────────────────────────────────────────────

async def _run_clean(
    data_type: str,
    file_id: str,
    ip_name: Optional[str],
    uuid_column: Optional[str],
):
    _location_or_400(data_type, ip_name)

    try:
        df, _ = load_dataframe(file_id)
    except HTTPException:
        raise

    try:
        loop = asyncio.get_event_loop()
        cleaned_df, result = await loop.run_in_executor(
            _CLEAN_POOL,
            partial(clean_dataframe_fast, df, uuid_column=uuid_column),
        )
    except Exception as e:
        raise HTTPException(500, f"Cleaning failed: {e}")

    try:
        meta = write_outputs(file_id, cleaned_df, result, data_type=data_type, ip_name=ip_name)
    except InvalidDataLocation as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to write output files: {e}")

    delete_file(file_id)

    summary = _summarise(result)
    stem    = meta["stem"]
    dtype   = meta["data_type"]

    # Download URL:  /api/clean/{type}/{ip_name}/{stem}/download/cleaned
    #             or /api/clean/{type}/{stem}/download/cleaned
    base = f"/api/clean/{dtype}" + (f"/{ip_name}" if ip_name else "")

    return JSONResponse({
        "file_id":   file_id,
        "data_type": dtype,
        "ip_name":   ip_name,
        "summary":   summary,
        "output_files": {
            "cleaned":         str(meta["cleaned_path"].name),
            "report":          str(meta["report_path"].name),
            "format":          meta["ext"],
            "cleaned_size_mb": meta["cleaned_size_mb"],
            "report_size_mb":  meta["report_size_mb"],
            "total_size_mb":   meta["total_size_mb"],
            "saved_to":        str(meta["output_dir"]),
        },
        "download_urls": {
            "cleaned_dataset": f"{base}/{stem}/download/cleaned",
        },
    })


# ── GET download/cleaned — with ip_name ───────────────────────────────────────

@router.get(
    "/{data_type}/{ip_name}/{stem}/download/cleaned",
    summary="Download cleaned parquet (beneficiary / certificates)",
)
async def dl_cleaned_with_ip(data_type: str, ip_name: str, stem: str):
    return _serve_cleaned(data_type, ip_name, stem)


# ── GET download/cleaned — without ip_name ────────────────────────────────────

@router.get(
    "/{data_type}/{stem}/download/cleaned",
    summary="Download cleaned parquet (banks / financials)",
)
async def dl_cleaned_no_ip(data_type: str, stem: str):
    return _serve_cleaned(data_type, None, stem)


def _serve_cleaned(data_type: str, ip_name: Optional[str], stem: str):
    try:
        folder = resolve_dir(data_type, ip_name, create=False)
    except InvalidDataLocation as e:
        raise HTTPException(400, str(e))
    for ext in (".parquet", ".csv"):
        p = folder / f"{stem}_cleaned{ext}"
        if p.exists():
            return FileResponse(
                p, filename=p.name,
                media_type="application/octet-stream" if ext == ".parquet" else "text/csv",
            )
    raise HTTPException(404, f"No cleaned file found for '{stem}'. Run cleaning first.")

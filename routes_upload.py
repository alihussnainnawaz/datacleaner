# routes_upload.py
from fastapi import APIRouter, UploadFile, File, HTTPException, Request
from file_handler import save_upload, load_dataframe, delete_file, detect_column_dtypes, write_dataframe_inplace, get_file_name

router = APIRouter()


@router.post("/", summary="Upload CSV / XLSX / XLS file")
async def upload_file(file: UploadFile = File(...)):
    saved = await save_upload(file)
    df, _ = load_dataframe(saved["file_id"])
    # Detect + cache per-column datatypes right away, so the "Datatype"
    # filter's All-Columns drop target (Column Rule Preview) is an instant
    # cache read later instead of scanning the file at drop time.
    dtypes = detect_column_dtypes(saved["file_id"], df)
    return {
        "success":      True,
        "file_id":      saved["file_id"],
        "file_name":    saved["file_name"],
        "row_count":    len(df),
        "column_count": len(df.columns),
        "columns":      df.columns.tolist(),
        "dtypes":       dtypes,
        "message":      f"'{saved['file_name']}' uploaded — {len(df):,} rows, {len(df.columns)} columns.",
    }


@router.get("/{file_id}/dtypes", summary="Get auto-detected datatypes for every column")
async def file_dtypes(file_id: str):
    """
    Cached read — computed once at upload time (see upload_file above) and
    re-scanned only if the underlying file changes (e.g. after apply-mapping
    edits it in place). Powers dragging the "Datatype" filter onto
    All Columns in the Column Rule Preview screen.
    """
    return {"file_id": file_id, "dtypes": detect_column_dtypes(file_id)}


@router.get("/{file_id}", summary="Get file metadata")
async def file_info(file_id: str):
    df, _ = load_dataframe(file_id)
    return {
        "file_id":      file_id,
        "file_name":    get_file_name(file_id),
        "row_count":    len(df),
        "column_count": len(df.columns),
        "columns":      df.columns.tolist(),
    }


@router.delete("/{file_id}", summary="Delete uploaded file")
async def delete(file_id: str):
    if not delete_file(file_id):
        raise HTTPException(404, f"No file for '{file_id}'.")
    return {"success": True, "file_id": file_id}


@router.post("/{file_id}/apply-mapping/{column}", summary="Apply a value mapping to a column in the raw uploaded file")
async def apply_mapping(file_id: str, column: str, request: Request):
    """
    Apply a user-confirmed {old_value: new_value} mapping directly to a
    column in the raw uploaded file, updating the in-memory copy.

    This exists for the "Regex Clean" rule queued from the upload-time
    Column Rule Preview screen: that rule is meant to run BEFORE the main
    cleaning pipeline (which only operates on an already-saved cleaned
    parquet), so it needs to edit the raw upload directly rather than going
    through /api/clean/tools/*, which requires a cleaned parquet to already
    exist.
    """
    from urllib.parse import unquote

    body    = await request.json()
    mapping = body.get("mapping") or {}

    df, _ = load_dataframe(file_id)
    col = column if column in df.columns else unquote(column)
    if col not in df.columns:
        raise HTTPException(404, f"Column '{column}' not found.")

    if not mapping:
        return {"success": True, "column": col, "changes": 0}

    # PERF: .map(lambda v: mapping.get(v, v)) called a Python function once
    # per ROW — the exact per-cell overhead the rest of this app's pipeline
    # was rewritten to eliminate. .map(dict) (no lambda) uses pandas' C
    # hash-join path instead — same semantics (missing keys stay
    # unchanged), far faster on a large column. Still offloaded to the
    # thread pool: the mapping transform itself is real CPU work on a large
    # column and would otherwise block the event loop (including live
    # progress polls for an unrelated in-flight pipeline run) for its
    # duration — the in-memory store update itself is O(1) and not the
    # reason for the offload anymore (no disk write happens here now).
    import asyncio
    from routes_clean import _CLEAN_POOL

    def _do_mapping():
        before = df[col].astype(str)
        after  = before.map(mapping).where(lambda s: s.notna(), before)
        changed = int((before != after).sum())
        df[col] = after
        write_dataframe_inplace(df, file_id)
        return changed

    changed = await asyncio.get_event_loop().run_in_executor(_CLEAN_POOL, _do_mapping)
    return {"success": True, "column": col, "changes": changed}


@router.post("/{file_id}/apply-mappings-batch", summary="Apply value mappings to several columns in ONE read/write pass")
async def apply_mappings_batch(file_id: str, request: Request):
    """
    Batched version of apply_mapping: applies mappings for multiple columns
    in a single load + single write, instead of the caller looping over
    apply_mapping once per column.

    PERF: batches N "Regex Clean" column mappings into a single in-memory
    transform + single store update, instead of the caller making N
    sequential /apply-mapping calls (each with its own thread-pool
    round trip). Historically this also collapsed N full-file disk
    read/write cycles into one; the upload is in-memory now (see
    file_handler.py), so the remaining win is fewer request round trips
    and one transform pass instead of N.

    Body: { "mappings": { "<column>": {"<old>": "<new>", ...}, ... } }
    """
    from urllib.parse import unquote

    body     = await request.json()
    mappings = body.get("mappings") or {}
    if not mappings:
        return {"success": True, "results": {}}

    df, _ = load_dataframe(file_id)

    # PERF: vectorised dict-map instead of a per-row Python lambda, offloaded
    # to the thread pool since the batched transform across N columns is
    # real CPU work that would otherwise block the event loop (including
    # live progress polls for an unrelated in-flight pipeline run) for the
    # whole duration. No disk write happens here — the in-memory store
    # update at the end is O(1) — so this is purely about not blocking the
    # loop during the transform itself, not about a slow write anymore.
    import asyncio
    from routes_clean import _CLEAN_POOL

    def _do_batch():
        results: dict[str, int] = {}
        for column, mapping in mappings.items():
            if not mapping:
                results[column] = 0
                continue
            col = column if column in df.columns else unquote(column)
            if col not in df.columns:
                results[column] = 0
                continue
            before = df[col].astype(str)
            after  = before.map(mapping).where(lambda s: s.notna(), before)
            results[col] = int((before != after).sum())
            df[col] = after
        write_dataframe_inplace(df, file_id)
        return results

    results = await asyncio.get_event_loop().run_in_executor(_CLEAN_POOL, _do_batch)
    return {"success": True, "results": results}


@router.get("/{file_id}/unique/{column}", summary="Get unique values for a column")
async def unique_values(file_id: str, column: str):
    """Return sorted unique non-null values for a column in the uploaded file."""
    from fastapi import HTTPException
    df, _ = load_dataframe(file_id)
    # Decode column name (URL-encoded)
    col = column
    if col not in df.columns:
        # Try URL-decoded
        from urllib.parse import unquote
        col = unquote(column)
    if col not in df.columns:
        raise HTTPException(404, f"Column '{column}' not found.")
    null_tokens = {"", "nan", "none", "null", "n/a", "na", "nil", "-", "--"}
    vals = sorted({
        str(v) for v in df[col].dropna().tolist()
        if str(v).strip().lower() not in null_tokens
    })
    return {"column": col, "values": vals[:500]}  # cap at 500 for perf


@router.get("/{file_id}/clusters/{column}", summary="Quick-cluster a column's unique values")
async def column_clusters(file_id: str, column: str):
    """
    Fast, lightweight clustering of a column's unique values — powers the
    interactive Regex Clean bubble popup in the upload-time Column Rule
    Preview screen. No heavy fuzzy scoring: case/whitespace normalisation
    plus a cheap edit-distance-1 merge (see cleaning_engine.quick_cluster_values),
    designed to be fast and fully correctable by the user via drag-and-drop
    rather than a final answer on its own.

    Unlike /tools/regex-clusters (which reads from an already-cleaned
    parquet), this reads directly from the freshly uploaded file — it's
    meant to run BEFORE the pipeline has ever executed for this project.
    """
    from fastapi import HTTPException
    from urllib.parse import unquote
    from cleaning_engine import quick_cluster_values

    df, _ = load_dataframe(file_id)
    col = column if column in df.columns else unquote(column)
    if col not in df.columns:
        raise HTTPException(404, f"Column '{column}' not found.")

    null_tokens = {"", "nan", "none", "null", "n/a", "na", "nil", "-", "--"}
    values = sorted({
        str(v) for v in df[col].dropna().tolist()
        if str(v).strip().lower() not in null_tokens
    })[:500]  # same cap as unique_values, for perf and a usable popup size

    clusters = quick_cluster_values(values)
    return {
        "column":        col,
        "clusters":      clusters,
        "cluster_count": len(clusters),
        "value_count":   len(values),
    }

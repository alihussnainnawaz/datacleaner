# upload.py

import sqlite3

import pandas as pd
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from file_handler import save_upload, load_dataframe, save_imported_dataframe
from schemas import UploadResponse, ErrorResponse, DatabaseImportRequest

router = APIRouter()


def _validate_read_query(query: str) -> str:
    cleaned = (query or "").strip()
    lowered = cleaned.lower()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Database query is required.")
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise HTTPException(status_code=400, detail="Only SELECT/WITH read queries are allowed.")
    if ";" in cleaned.rstrip(";"):
        raise HTTPException(status_code=400, detail="Multiple SQL statements are not allowed.")
    return cleaned.rstrip(";")


def _connect_database(req: DatabaseImportRequest):
    db_type = (req.db_type or "sqlite").lower().strip()

    if req.connection_uri:
        if req.connection_uri.startswith("sqlite:///"):
            return sqlite3.connect(req.connection_uri.replace("sqlite:///", "", 1))
        raise HTTPException(
            status_code=400,
            detail="Connection URI is currently supported for sqlite:/// paths only. Use fields for PostgreSQL, MySQL, or MSSQL.",
        )

    if db_type == "sqlite":
        if not req.sqlite_path:
            raise HTTPException(status_code=400, detail="SQLite database path is required.")
        return sqlite3.connect(req.sqlite_path)

    if db_type in {"postgres", "postgresql"}:
        try:
            import psycopg2
        except ImportError:
            raise HTTPException(status_code=400, detail="PostgreSQL support requires installing psycopg2.")
        return psycopg2.connect(
            host=req.host,
            port=req.port or 5432,
            dbname=req.database,
            user=req.username,
            password=req.password,
        )

    if db_type in {"mysql", "mariadb"}:
        try:
            import pymysql
        except ImportError:
            raise HTTPException(status_code=400, detail="MySQL support requires installing pymysql.")
        return pymysql.connect(
            host=req.host,
            port=req.port or 3306,
            database=req.database,
            user=req.username,
            password=req.password,
        )

    if db_type in {"mssql", "sqlserver", "sql_server"}:
        try:
            import pyodbc
        except ImportError:
            raise HTTPException(status_code=400, detail="SQL Server support requires installing pyodbc.")
        driver = "{ODBC Driver 17 for SQL Server}"
        conn_str = (
            f"DRIVER={driver};SERVER={req.host},{req.port or 1433};"
            f"DATABASE={req.database};UID={req.username};PWD={req.password}"
        )
        return pyodbc.connect(conn_str)

    raise HTTPException(status_code=400, detail=f"Unsupported database type: {req.db_type}")


# ── POST /api/upload ──────────────────────────────────────

@router.post(
    "/",
    response_model = UploadResponse,
    summary        = "Upload an Excel file",
    description    = "Accepts .xlsx or .xls files. Saves to disk and returns file metadata.",
)
async def upload_file(file: UploadFile = File(...)):
    """
    Endpoint to upload a single Excel file.
    - Validates extension and file size
    - Saves file to UPLOAD_DIR with a UUID filename
    - Returns metadata: file_id, row count, column names
    """

    # 1. Save file to disk
    try:
        saved = await save_upload(file)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error during upload: {str(e)}")

    # 2. Load into DataFrame to extract metadata
    try:
        df, _ = load_dataframe(saved["file_id"])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"File saved but could not be read: {str(e)}")

    # 3. Return metadata
    return UploadResponse(
        success      = True,
        file_name    = saved["file_name"],
        file_id      = saved["file_id"],
        row_count    = len(df),
        column_count = len(df.columns),
        columns      = df.columns.tolist(),
        message      = f"'{saved['file_name']}' uploaded successfully. {len(df)} rows detected.",
    )


@router.post(
    "/database",
    response_model = UploadResponse,
    summary        = "Import data from a database query",
    description    = "Runs a read-only SELECT/WITH query, saves the result as a pipeline CSV, and returns file metadata.",
)
async def import_database(request: DatabaseImportRequest):
    query = _validate_read_query(request.query)

    conn = None
    try:
        conn = _connect_database(request)
        df = pd.read_sql_query(query, conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Database import failed: {str(e)}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if df.empty:
        raise HTTPException(status_code=422, detail="Query returned no rows.")

    saved = save_imported_dataframe(df, request.source_name or "database_query")

    return UploadResponse(
        success      = True,
        file_name    = saved["file_name"],
        file_id      = saved["file_id"],
        row_count    = len(df),
        column_count = len(df.columns),
        columns      = [str(c) for c in df.columns.tolist()],
        message      = f"Database query imported successfully. {len(df)} rows detected.",
    )


# ── GET /api/upload/{file_id} ─────────────────────────────

@router.get(
    "/{file_id}",
    summary     = "Get metadata of an uploaded file",
    description = "Returns row count, column names, and basic info for a previously uploaded file.",
)
async def get_file_info(file_id: str):
    """
    Returns metadata of a previously uploaded file without re-processing it.
    Useful for frontend to confirm a file is ready before triggering clean/transform.
    """

    try:
        df, file_path = load_dataframe(file_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load file: {str(e)}")

    return JSONResponse(content={
        "success"      : True,
        "file_id"      : file_id,
        "file_name"    : file_path.name,
        "row_count"    : len(df),
        "column_count" : len(df.columns),
        "columns"      : df.columns.tolist(),
    })


# ── DELETE /api/upload/{file_id} ──────────────────────────

@router.delete(
    "/{file_id}",
    summary     = "Delete an uploaded file",
    description = "Deletes both the original and cleaned version of a file from disk.",
)
async def delete_file(file_id: str):
    """
    Deletes all files associated with a file_id (original + cleaned).
    Useful for cleanup after processing is complete.
    """

    from file_handler import delete_file as _delete

    deleted = _delete(file_id)

    if not deleted:
        raise HTTPException(
            status_code = 404,
            detail      = f"No file found for file_id '{file_id}'."
        )

    return JSONResponse(content={
        "success" : True,
        "file_id" : file_id,
        "message" : f"All files for '{file_id}' deleted successfully.",
    })

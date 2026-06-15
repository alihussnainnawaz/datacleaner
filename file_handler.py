# file_handler.py

import uuid
import aiofiles
from pathlib import Path
from typing import Optional
import pandas as pd
from fastapi import UploadFile, HTTPException

from config import UPLOAD_DIR, ALLOWED_EXTENSIONS, MAX_FILE_SIZE_BYTES


# ── Save Uploaded File to Disk ────────────────────────────

async def save_upload(file: UploadFile) -> dict:
    """
    Validates and saves an uploaded Excel file to UPLOAD_DIR.
    Returns metadata: file_id, saved path, original filename.
    """

    # 1. Extension check
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{suffix}'. Only {ALLOWED_EXTENSIONS} are allowed."
        )

    # 2. Read file bytes
    contents = await file.read()

    # 3. Size check
    if len(contents) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum allowed size of {MAX_FILE_SIZE_BYTES // (1024*1024)} MB."
        )

    # 4. Assign unique ID & save
    file_id   = str(uuid.uuid4())
    save_path = UPLOAD_DIR / f"{file_id}{suffix}"

    async with aiofiles.open(save_path, "wb") as out:
        await out.write(contents)

    return {
        "file_id":   file_id,
        "file_path": save_path,
        "file_name": file.filename,
    }


def save_imported_dataframe(df: pd.DataFrame, source_name: str = "database_query") -> dict:
    """
    Saves an imported DataFrame to UPLOAD_DIR as CSV so the normal pipeline
    can reuse the same file_id-based flow as uploaded files.
    """

    file_id = str(uuid.uuid4())
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in source_name).strip("_")
    if not safe_name:
        safe_name = "database_query"
    file_name = f"{safe_name}.csv"
    save_path = UPLOAD_DIR / f"{file_id}.csv"

    try:
        df.to_csv(save_path, index=False)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save imported database result: {str(e)}"
        )

    return {
        "file_id": file_id,
        "file_path": save_path,
        "file_name": file_name,
    }


# ── Load Excel File into DataFrame ───────────────────────

def load_dataframe(file_id: str) -> tuple[pd.DataFrame, Path]:
    """
    Locates a previously uploaded file by file_id and loads it
    into a pandas DataFrame. Supports .xlsx and .xls.
    Returns (dataframe, file_path).
    """

    # Find the file regardless of extension
    matches = list(UPLOAD_DIR.glob(f"{file_id}.*"))

    if not matches:
        raise HTTPException(
            status_code=404,
            detail=f"No file found for file_id '{file_id}'. It may not have been uploaded yet."
        )

    file_path = matches[0]
    suffix    = file_path.suffix.lower()

    try:
        if suffix == ".xlsx":
            df = pd.read_excel(file_path, engine="openpyxl", dtype=str)
        elif suffix == ".xls":
            df = pd.read_excel(file_path, engine="xlrd", dtype=str)
        elif suffix == ".csv":
            df = pd.read_csv(file_path, dtype=str)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported file format: {suffix}")
    except Exception as e:
        raise HTTPException(
            status_code=422,
            detail=f"Failed to parse Excel file: {str(e)}"
        )

    # Strip whitespace from all string cells immediately on load
    df = df.map(lambda x: x.strip() if isinstance(x, str) else x)

    return df, file_path


# ── Save Cleaned DataFrame back to Excel ─────────────────

def save_dataframe(df: pd.DataFrame, file_id: str, suffix: str = ".xlsx") -> Path:
    """
    Saves a cleaned/transformed DataFrame as a new Excel file.
    Uses a '_cleaned' suffix to distinguish from the original.
    Returns the output file path.
    """

    output_path = UPLOAD_DIR / f"{file_id}_cleaned{suffix}"

    try:
        df.to_excel(output_path, index=False, engine="openpyxl")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save output file: {str(e)}"
        )

    return output_path


# ── Delete a File by file_id ──────────────────────────────

def delete_file(file_id: str) -> bool:
    """
    Deletes all files (original + cleaned) associated with a file_id.
    Returns True if at least one file was deleted.
    """

    matches = list(UPLOAD_DIR.glob(f"{file_id}*"))

    if not matches:
        return False

    for f in matches:
        f.unlink(missing_ok=True)

    return True


# ── Get File Metadata ─────────────────────────────────────

def get_file_meta(file_id: str) -> Optional[dict]:
    """
    Returns basic metadata about an uploaded file without loading it fully.
    Useful for quick status checks.
    """

    matches = list(UPLOAD_DIR.glob(f"{file_id}.*"))

    if not matches:
        return None

    file_path = matches[0]
    size_kb   = round(file_path.stat().st_size / 1024, 2)

    return {
        "file_id":   file_id,
        "file_name": file_path.name,
        "size_kb":   size_kb,
        "extension": file_path.suffix.lower(),
    }

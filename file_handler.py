# file_handler.py
import re
import aiofiles
from pathlib import Path
import pandas as pd
from fastapi import UploadFile, HTTPException
from config import UPLOAD_DIR, ALLOWED_EXTENSIONS, MAX_FILE_SIZE_BYTES


def _slugify(name: str) -> str:
    """
    Turn an original filename stem into a safe, filesystem-friendly slug.
    e.g. "TRDP-Profile - Copy" → "TRDP-Profile-Copy"
         "my file (2).csv"     → "my-file-2"
    Keeps letters, digits, hyphens, underscores. Collapses runs of
    separators into a single hyphen and strips leading/trailing hyphens.
    """
    slug = re.sub(r"[^\w\-]+", "-", name.strip())  # replace non-word chars with -
    slug = re.sub(r"-{2,}", "-", slug)              # collapse multiple hyphens
    slug = slug.strip("-")                           # strip leading/trailing
    return slug or "file"


async def save_upload(file: UploadFile) -> dict:
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Invalid file type '{suffix}'. Allowed: {ALLOWED_EXTENSIONS}")

    stem    = Path(file.filename).stem
    file_id = _slugify(stem)

    # Resolve final path before streaming so we never hold the whole file in RAM
    save_path = UPLOAD_DIR / f"{file_id}{suffix}"
    counter   = 1
    while save_path.exists():
        save_path = UPLOAD_DIR / f"{file_id}_{counter}{suffix}"
        counter  += 1
    file_id = save_path.stem

    # Stream directly to disk in 1 MB chunks — never loads the whole file into RAM
    CHUNK    = 1024 * 1024
    received = 0
    async with aiofiles.open(save_path, "wb") as out:
        while True:
            chunk = await file.read(CHUNK)
            if not chunk:
                break
            received += len(chunk)
            if received > MAX_FILE_SIZE_BYTES:
                await out.close()
                save_path.unlink(missing_ok=True)
                raise HTTPException(413, f"File exceeds {MAX_FILE_SIZE_BYTES // (1024*1024)} MB limit.")
            await out.write(chunk)

    return {"file_id": file_id, "file_path": save_path, "file_name": file.filename}


def load_dataframe(file_id: str) -> tuple[pd.DataFrame, Path]:
    matches = list(UPLOAD_DIR.glob(f"{file_id}.*"))
    # prefer original over _cleaned
    matches = [m for m in matches if "_cleaned" not in m.stem] or matches
    if not matches:
        raise HTTPException(404, f"No file found for file_id '{file_id}'.")
    file_path = matches[0]
    suffix    = file_path.suffix.lower()
    try:
        if suffix == ".xlsx":
            df = pd.read_excel(file_path, engine="openpyxl", dtype=str)
        elif suffix == ".xls":
            df = pd.read_excel(file_path, engine="xlrd", dtype=str)
        elif suffix == ".csv":
            df = pd.read_csv(file_path, dtype=str, low_memory=False)
        else:
            raise HTTPException(400, f"Unsupported format: {suffix}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(422, f"Failed to parse file: {e}")
    return df, file_path


def save_cleaned_excel(df: pd.DataFrame, file_id: str) -> Path:
    """Save cleaned DataFrame as Excel (slow — only on explicit download request)."""
    out = UPLOAD_DIR / f"{file_id}_cleaned.xlsx"
    df.to_excel(out, index=False, engine="openpyxl")
    return out


def delete_file(file_id: str) -> bool:
    matches = list(UPLOAD_DIR.glob(f"{file_id}.*"))
    matches = [m for m in matches if "_cleaned" not in m.stem]
    if not matches:
        return False
    for f in matches:
        f.unlink(missing_ok=True)
    return True

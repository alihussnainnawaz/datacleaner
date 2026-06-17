# routes_upload.py
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from file_handler import save_upload, load_dataframe, delete_file

router = APIRouter()


@router.post("/", summary="Upload CSV / XLSX / XLS file")
async def upload_file(file: UploadFile = File(...)):
    saved = await save_upload(file)
    df, _ = load_dataframe(saved["file_id"])
    return {
        "success":      True,
        "file_id":      saved["file_id"],
        "file_name":    saved["file_name"],
        "row_count":    len(df),
        "column_count": len(df.columns),
        "columns":      df.columns.tolist(),
        "message":      f"'{saved['file_name']}' uploaded — {len(df):,} rows, {len(df.columns)} columns.",
    }


@router.get("/{file_id}", summary="Get file metadata")
async def file_info(file_id: str):
    df, path = load_dataframe(file_id)
    return {
        "file_id":      file_id,
        "file_name":    path.name,
        "row_count":    len(df),
        "column_count": len(df.columns),
        "columns":      df.columns.tolist(),
    }


@router.delete("/{file_id}", summary="Delete uploaded file")
async def delete(file_id: str):
    if not delete_file(file_id):
        raise HTTPException(404, f"No file for '{file_id}'.")
    return {"success": True, "file_id": file_id}

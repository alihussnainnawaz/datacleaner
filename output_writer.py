"""
output_writer.py
────────────────
Writes TWO output files to beneficiary/ after every cleaning run.

FILE 1 — {file_id}_cleaned.parquet   (~10 MB)
    The full cleaned dataset. Open in pandas, Excel Power Query, or DuckDB.

FILE 2 — {file_id}_report.parquet    (~16 MB vs 451 MB JSON)
    Per-record cleaning report — same logical structure as the reference JSON
    but 96% smaller. One row per beneficiary record with 5 columns:

        uuid             – DA_UUID (or ROW_N fallback)
        original_values  – JSON string: only the cols that were touched
        cleaned_values   – JSON string: {col: [new_val, step_tags]}
        manual_reviews   – JSON string: {col: original_val}
        is_dup           – bool: duplicate UUID flag

    Read back in Python:
        df = pd.read_parquet("beneficiary/xxx_report.parquet")
        import json
        record = df[df.uuid == "1546264"].iloc[0]
        print(json.loads(record.cleaned_values))

Why not full JSON?
    Original JSON stored all 47 columns per row even when only 5 changed.
    That alone was 280 MB of the 451 MB. Here we store only touched columns.

Output format is parquet ONLY. If fastparquet is missing, the module refuses
to import rather than silently degrading to CSV.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

try:
    import fastparquet as _fp  # noqa: F401
except ImportError as e:
    raise RuntimeError(
        "fastparquet is not installed in this environment — outputs would "
        "silently fall back to CSV. Run `pip install -r requirements.txt` "
        "into the venv you launch the server with, then restart."
    ) from e

OUTPUT_DIR = Path(__file__).parent / "beneficiary"
OUTPUT_DIR.mkdir(exist_ok=True)


def _write(df: pd.DataFrame, path: Path) -> None:
    import fastparquet as fp
    fp.write(str(path), df.fillna(""), compression="ZSTD")


def write_outputs(
    file_id: str,
    cleaned_df: pd.DataFrame,
    result: dict[str, Any],
) -> dict[str, Any]:
    """
    Save cleaned dataset + per-record report. Auto-called after every clean run.
    Returns metadata dict (sizes, paths, urls).
    """
    ext = ".parquet"

    # ── FILE 1: full cleaned dataset ──────────────────────────────────────────
    cleaned_path = OUTPUT_DIR / f"{file_id}_cleaned{ext}"
    clean_str = cleaned_df.astype(str).replace({"None": "", "nan": "", "<NA>": ""})
    _write(clean_str, cleaned_path)

    # ── FILE 2: per-record report (compact, same shape as reference JSON) ─────
    report_rows: list[dict] = []
    for uuid_key, v in result.items():
        ov           = v["original_values"]
        cv           = v["cleaned_values"]
        rv           = v["manual_reviews_required"]
        is_dup       = bool(v["IS DUPLICATED UUID"])

        # only store original values for columns that were actually touched
        touched      = set(cv.keys()) | set(rv.keys())
        orig_touched = {c: ov.get(c) for c in touched if c in ov}

        report_rows.append({
            "uuid":            uuid_key,
            "original_values": json.dumps(orig_touched, default=str, ensure_ascii=False),
            "cleaned_values":  json.dumps(cv,           default=str, ensure_ascii=False),
            "manual_reviews":  json.dumps(rv,           default=str, ensure_ascii=False),
            "is_dup":          is_dup,
        })

    report_df   = pd.DataFrame(report_rows)
    report_path = OUTPUT_DIR / f"{file_id}_report{ext}"
    _write(report_df, report_path)

    cleaned_mb = round(cleaned_path.stat().st_size / 1024 / 1024, 2)
    report_mb  = round(report_path.stat().st_size  / 1024 / 1024, 2)

    return {
        "cleaned_path":    cleaned_path,
        "report_path":     report_path,
        "ext":             ext,
        "cleaned_size_mb": cleaned_mb,
        "report_size_mb":  report_mb,
        "total_size_mb":   round(cleaned_mb + report_mb, 2),
    }


def read_report(file_id: str) -> pd.DataFrame | None:
    p = OUTPUT_DIR / f"{file_id}_report.parquet"
    return pd.read_parquet(p, engine="fastparquet") if p.exists() else None


def read_cleaned(file_id: str) -> pd.DataFrame | None:
    p = OUTPUT_DIR / f"{file_id}_cleaned.parquet"
    return pd.read_parquet(p, engine="fastparquet") if p.exists() else None
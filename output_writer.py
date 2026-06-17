"""
output_writer.py
────────────────
Writes outputs into a type + IP folder layout instead of one flat folder:

    beneficiary/<ip_name>/<file_id>_cleaned.parquet
    beneficiary/<ip_name>/<file_id>_report.parquet
    banks/<ip_name>/<file_id>_cleaned.parquet
    banks/<ip_name>/<file_id>_report.parquet
    financials/<ip_name>/<file_id>_cleaned.parquet
    financials/<ip_name>/<file_id>_report.parquet
    certificates/<file_id>_cleaned.parquet      (no ip_name subfolder)
    certificates/<file_id>_report.parquet

`data_type` is one of: beneficiary, banks, certificates, financials
(case-insensitive). `ip_name` (implementing partner) is required for every
type except certificates — see config.TYPES_WITH_IP_SUBFOLDER.

FILE 1 — {file_id}_cleaned.parquet   (~10 MB)
    The full cleaned dataset. Open in pandas, Excel Power Query, or DuckDB.

FILE 2 — {file_id}_report.parquet    (~16 MB vs 451 MB JSON)
    Per-record cleaning report — same logical structure as the reference JSON
    but 96% smaller. One row per record with 5 columns:

        uuid             – DA_UUID (or ROW_N fallback)
        original_values  – JSON string: only the cols that were touched
        cleaned_values   – JSON string: {col: [new_val, step_tags]}
        manual_reviews   – JSON string: {col: original_val}
        is_dup           – bool: duplicate UUID flag

    Read back in Python:
        df = pd.read_parquet("beneficiary/Hands/xxx_report.parquet")
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
import re
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from config import DATA_ROOT, DATA_TYPE_FOLDERS, TYPES_WITH_IP_SUBFOLDER

try:
    import fastparquet as _fp  # noqa: F401
except ImportError as e:
    raise RuntimeError(
        "fastparquet is not installed in this environment — outputs would "
        "silently fall back to CSV. Run `pip install -r requirements.txt` "
        "into the venv you launch the server with, then restart."
    ) from e

# kept for backward compatibility with any code importing the old constant —
# points at the same place it always did (beneficiary/), still auto-created.
OUTPUT_DIR = DATA_ROOT / DATA_TYPE_FOLDERS["beneficiary"]
OUTPUT_DIR.mkdir(exist_ok=True)

_BAD_SEGMENT_RE = re.compile(r"[\\/]|\.\.")


class InvalidDataLocation(ValueError):
    """Raised when (data_type, ip_name) doesn't describe a valid storage location."""


# ── path resolution ────────────────────────────────────────────────────────

def normalise_type(data_type: str) -> str:
    key = (data_type or "").strip().lower()
    if key not in DATA_TYPE_FOLDERS:
        allowed = ", ".join(sorted(DATA_TYPE_FOLDERS))
        raise InvalidDataLocation(f"Unknown data type '{data_type}'. Allowed: {allowed}.")
    return key


def requires_ip_subfolder(data_type: str) -> bool:
    return normalise_type(data_type) in TYPES_WITH_IP_SUBFOLDER


def _safe_segment(name: str, label: str) -> str:
    name = (name or "").strip()
    if not name:
        raise InvalidDataLocation(f"{label} must not be empty.")
    if _BAD_SEGMENT_RE.search(name):
        raise InvalidDataLocation(f"{label} '{name}' contains invalid path characters.")
    return name


def resolve_dir(data_type: str, ip_name: Optional[str] = None, *, create: bool = True) -> Path:
    """
    Resolve (and optionally create) the on-disk folder for a given data type / IP.

        beneficiary | banks | financials  -> DATA_ROOT/<type>/<ip_name>/
        certificates                       -> DATA_ROOT/<type>/   (no ip_name)

    Raises InvalidDataLocation on bad input (unknown type, missing/extra
    ip_name, path-traversal characters, etc).
    """
    key    = normalise_type(data_type)
    folder = DATA_ROOT / DATA_TYPE_FOLDERS[key]

    if key in TYPES_WITH_IP_SUBFOLDER:
        if not ip_name:
            raise InvalidDataLocation(
                f"Data type '{key}' requires an ip_name subfolder — "
                f"use /{key}/<ip_name>/<file_id>."
            )
        folder = folder / _safe_segment(ip_name, "ip_name")
    else:
        if ip_name:
            raise InvalidDataLocation(
                f"Data type '{key}' does not use an ip_name subfolder — "
                f"use /{key}/<file_id> (no ip_name)."
            )

    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def ensure_all_directories() -> None:
    """Create the 4 top-level type folders (call once at app startup)."""
    for folder_name in DATA_TYPE_FOLDERS.values():
        (DATA_ROOT / folder_name).mkdir(parents=True, exist_ok=True)


# ── parquet I/O ────────────────────────────────────────────────────────────

def _write(df: pd.DataFrame, path: Path) -> None:
    import fastparquet as fp
    fp.write(str(path), df.fillna(""), compression="ZSTD")


def write_outputs(
    file_id: str,
    cleaned_df: pd.DataFrame,
    result: dict[str, Any],
    data_type: str,
    ip_name: Optional[str] = None,
) -> dict[str, Any]:
    """
    Save cleaned dataset + per-record report under the folder for
    (data_type, ip_name). Auto-called after every clean run.
    Returns metadata dict (sizes, paths, urls).
    """
    out_dir = resolve_dir(data_type, ip_name)
    ext = ".parquet"

    # ── FILE 1: full cleaned dataset ──────────────────────────────────────────
    cleaned_path = out_dir / f"{file_id}_cleaned{ext}"
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
    report_path = out_dir / f"{file_id}_report{ext}"
    _write(report_df, report_path)

    cleaned_mb = round(cleaned_path.stat().st_size / 1024 / 1024, 2)
    report_mb  = round(report_path.stat().st_size  / 1024 / 1024, 2)

    return {
        "data_type":       normalise_type(data_type),
        "ip_name":         ip_name,
        "output_dir":      out_dir,
        "cleaned_path":    cleaned_path,
        "report_path":     report_path,
        "ext":             ext,
        "cleaned_size_mb": cleaned_mb,
        "report_size_mb":  report_mb,
        "total_size_mb":   round(cleaned_mb + report_mb, 2),
    }


def read_report(file_id: str, data_type: str, ip_name: Optional[str] = None) -> pd.DataFrame | None:
    p = resolve_dir(data_type, ip_name, create=False) / f"{file_id}_report.parquet"
    return pd.read_parquet(p, engine="fastparquet") if p.exists() else None


def read_cleaned(file_id: str, data_type: str, ip_name: Optional[str] = None) -> pd.DataFrame | None:
    p = resolve_dir(data_type, ip_name, create=False) / f"{file_id}_cleaned.parquet"
    return pd.read_parquet(p, engine="fastparquet") if p.exists() else None


# ── cross-folder listing (used by /logs and /api/report/) ────────────────────

def iter_all_outputs(kind: str = "report"):
    """
    Yield (data_type, ip_name, file_id, path) for every {kind} parquet
    ('report' or 'cleaned') found across all 4 type folders.
    """
    for key, folder_name in DATA_TYPE_FOLDERS.items():
        base = DATA_ROOT / folder_name
        if not base.exists():
            continue
        if key in TYPES_WITH_IP_SUBFOLDER:
            for ip_dir in sorted(p for p in base.iterdir() if p.is_dir()):
                for p in ip_dir.glob(f"*_{kind}.parquet"):
                    yield key, ip_dir.name, p.stem.replace(f"_{kind}", ""), p
        else:
            for p in base.glob(f"*_{kind}.parquet"):
                yield key, None, p.stem.replace(f"_{kind}", ""), p

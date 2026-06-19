"""
output_writer.py
────────────────
Final storage layout:

    beneficiary/<ip_name>/<ip_name>_cleaned.parquet   IP required
    beneficiary/<ip_name>/<ip_name>_report.parquet
    certificates/<ip_name>/<ip_name>_cleaned.parquet  IP required
    certificates/<ip_name>/<ip_name>_report.parquet
    Banks_Financials/Banks_Financials_cleaned.parquet  no IP, fixed stem
    Banks_Financials/Banks_Financials_report.parquet

`output_dir` returned as a relative path from DATA_ROOT (e.g. "beneficiary/TRDP").
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from config import (
    DATA_ROOT, DATA_TYPE_FOLDERS,
    TYPES_WITH_IP_SUBFOLDER, FIXED_STEM,
)

try:
    import fastparquet as _fp  # noqa: F401
except ImportError as e:
    raise RuntimeError(
        "fastparquet is not installed. Run `pip install -r requirements.txt` "
        "into the venv you launch the server with, then restart."
    ) from e

OUTPUT_DIR = DATA_ROOT / DATA_TYPE_FOLDERS["beneficiary"]
OUTPUT_DIR.mkdir(exist_ok=True)

_BAD_SEGMENT_RE = re.compile(r"[\\/]|\.\.")


class InvalidDataLocation(ValueError):
    """Raised when (data_type, ip_name) combo is invalid."""


# ── helpers ───────────────────────────────────────────────────────────────────

def normalise_type(data_type: str) -> str:
    key = (data_type or "").strip().lower()
    if key not in DATA_TYPE_FOLDERS:
        allowed = ", ".join(sorted(DATA_TYPE_FOLDERS))
        raise InvalidDataLocation(f"Unknown data type '{data_type}'. Allowed: {allowed}.")
    return key


def _safe_segment(name: str, label: str) -> str:
    name = (name or "").strip()
    if not name:
        raise InvalidDataLocation(f"{label} must not be empty.")
    if _BAD_SEGMENT_RE.search(name):
        raise InvalidDataLocation(f"{label} '{name}' contains invalid path characters.")
    return name


def resolve_dir(data_type: str, ip_name: Optional[str] = None, *, create: bool = True) -> Path:
    """
    Resolve the on-disk output folder:

        beneficiary  → DATA_ROOT/beneficiary/<ip_name>/   (ip required)
        certificates → DATA_ROOT/certificates/<ip_name>/  (ip required)
        banks        → DATA_ROOT/Banks_Financials/         (no ip)
        financials   → DATA_ROOT/Banks_Financials/         (no ip)

    Raises InvalidDataLocation on bad input.
    """
    key    = normalise_type(data_type)
    folder = DATA_ROOT / DATA_TYPE_FOLDERS[key]

    if key in TYPES_WITH_IP_SUBFOLDER:
        if not ip_name:
            raise InvalidDataLocation(
                f"Data type '{key}' requires an ip_name — "
                f"use /{key}/<ip_name>/<file_id>."
            )
        folder = folder / _safe_segment(ip_name, "ip_name")
    else:
        if ip_name:
            raise InvalidDataLocation(
                f"Data type '{key}' does not use an ip_name — "
                f"use /{key}/<file_id> (no ip_name)."
            )

    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def output_stem(data_type: str, file_id: str, ip_name: Optional[str] = None) -> str:
    """
    Return the filename stem for output parquets:
        beneficiary / certificates  → ip_name          (e.g. "TRDP")
        banks / financials          → "Banks_Financials" (fixed)
    """
    key = normalise_type(data_type)
    if key in FIXED_STEM:
        return FIXED_STEM[key]          # always "Banks_Financials"
    return ip_name or file_id           # ip_name for beneficiary/certificates


def ensure_all_directories() -> None:
    """Create all top-level type folders at startup."""
    seen: set[str] = set()
    for folder_name in DATA_TYPE_FOLDERS.values():
        if folder_name not in seen:
            (DATA_ROOT / folder_name).mkdir(parents=True, exist_ok=True)
            seen.add(folder_name)


# ── parquet I/O ───────────────────────────────────────────────────────────────

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
    Save cleaned dataset + per-record report.

    Output examples:
        beneficiary/TRDP/TRDP_cleaned.parquet
        certificates/Hands/Hands_cleaned.parquet
        Banks_Financials/Banks_Financials_cleaned.parquet
    """
    out_dir = resolve_dir(data_type, ip_name)
    ext     = ".parquet"
    stem    = output_stem(data_type, file_id, ip_name)

    # FILE 1 — cleaned dataset
    # Fill NA per-column instead of astype(str) on the whole frame — avoids
    # creating a full string copy of 100MB+ data just to normalise nulls.
    cleaned_path = out_dir / f"{stem}_cleaned{ext}"
    clean_out = cleaned_df.copy()
    for col in clean_out.columns:
        clean_out[col] = clean_out[col].where(clean_out[col].notna(), other="")
    _write(clean_out, cleaned_path)
    del clean_out

    # FILE 2 — per-record report
    report_rows: list[dict] = []
    for uuid_key, v in result.items():
        ov     = v["original_values"]
        cv     = v["cleaned_values"]
        rv     = v["manual_reviews_required"]
        is_dup = bool(v["IS DUPLICATED UUID"])

        touched      = set(cv.keys()) | set(rv.keys())
        orig_touched = {c: ov.get(c) for c in touched if c in ov}

        report_rows.append({
            "uuid":            uuid_key,
            "original_values": json.dumps(orig_touched, default=str, ensure_ascii=False),
            "cleaned_values":  json.dumps(cv,           default=str, ensure_ascii=False),
            "manual_reviews":  json.dumps(rv,           default=str, ensure_ascii=False),
            "is_dup":          is_dup,
            "is_dup_cnic":     bool(v.get("IS DUPLICATED CNIC", False)),
        })

    report_df   = pd.DataFrame(report_rows)
    report_path = out_dir / f"{stem}_report{ext}"
    _write(report_df, report_path)

    cleaned_mb = round(cleaned_path.stat().st_size / 1024 / 1024, 2)
    report_mb  = round(report_path.stat().st_size  / 1024 / 1024, 2)

    try:
        rel_dir = out_dir.relative_to(DATA_ROOT)
    except ValueError:
        rel_dir = out_dir

    return {
        "data_type":       normalise_type(data_type),
        "ip_name":         ip_name,
        "stem":            stem,
        "output_dir":      rel_dir,
        "cleaned_path":    cleaned_path,
        "report_path":     report_path,
        "ext":             ext,
        "cleaned_size_mb": cleaned_mb,
        "report_size_mb":  report_mb,
        "total_size_mb":   round(cleaned_mb + report_mb, 2),
    }


def read_report(file_id: str, data_type: str, ip_name: Optional[str] = None) -> pd.DataFrame | None:
    stem = output_stem(data_type, file_id, ip_name)
    p    = resolve_dir(data_type, ip_name, create=False) / f"{stem}_report.parquet"
    return pd.read_parquet(p, engine="fastparquet") if p.exists() else None


def read_cleaned(file_id: str, data_type: str, ip_name: Optional[str] = None) -> pd.DataFrame | None:
    stem = output_stem(data_type, file_id, ip_name)
    p    = resolve_dir(data_type, ip_name, create=False) / f"{stem}_cleaned.parquet"
    return pd.read_parquet(p, engine="fastparquet") if p.exists() else None


def iter_all_outputs(kind: str = "report"):
    """Yield (data_type, ip_name, stem, path) for every {kind} parquet found."""
    seen_folders: set[str] = set()
    for key, folder_name in DATA_TYPE_FOLDERS.items():
        base = DATA_ROOT / folder_name
        if not base.exists():
            continue
        if key in TYPES_WITH_IP_SUBFOLDER:
            for ip_dir in sorted(p for p in base.iterdir() if p.is_dir()):
                for p in ip_dir.glob(f"*_{kind}.parquet"):
                    yield key, ip_dir.name, p.stem.replace(f"_{kind}", ""), p
        else:
            # banks and financials share Banks_Financials/ — only scan once
            if folder_name not in seen_folders:
                seen_folders.add(folder_name)
                for p in base.glob(f"*_{kind}.parquet"):
                    yield key, None, p.stem.replace(f"_{kind}", ""), p

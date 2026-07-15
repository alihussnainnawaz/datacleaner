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

Predefined validation integration
──────────────────────────────────
Predefined rules (R01–R12) are now OPT-IN — they only run when
clean_dataframe_*(run_predefined=True) is used. By default they are skipped,
so __validation_status__ / validation_status reflect only user-configured
filters from the Validate tab.

write_outputs() handles this transparently:
  • Pops "__predefined_validation_summary__" from result before iterating rows
    (zeroed out when predefined rules didn't run)
  • Merges it with any user-filter validation_summary passed by routes_clean.py
  • Writes a unified validation_summary.json
  • Each report parquet row's "validation_status" column carries the FULL
    per-filter detail (every configured filter, pass or fail, with column,
    condition, and actual value) as JSON — not just failures. The standalone
    predefined_rules_failed / predefined_rules_colors / predefined_fail_count
    columns were removed since they were always empty once predefined rules
    stopped running automatically and nothing in the frontend read them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import numpy as np

# PERF: orjson (C implementation) for the per-touched-row report JSON —
# ~4x faster than stdlib json; falls back safely on non-serialisable values.
try:
    import orjson as _orjson

    def _fast_json_dumps_safe(obj: Any) -> str:
        try:
            return _orjson.dumps(obj).decode("utf-8")
        except Exception:
            return json.dumps(obj, default=str, ensure_ascii=True)
except ImportError:
    def _fast_json_dumps_safe(obj: Any) -> str:
        return json.dumps(obj, default=str, ensure_ascii=True)

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

# Colour map for predefined rule prefixes — must stay in sync with
# validation_engine.PREDEFINED_RULE_COLORS
_PREDEFINED_RULE_COLORS: dict[str, str] = {
    "R01": "red",
    "R02": "red",
    "R03": "red",
    "R04": "orange",
    "R05": "orange",
    "R06": "red",
    "R07": "purple",
    "R08": "purple",
    "R09": "yellow",
    "R10": "yellow",
    "R11": "orange",
    "R12": "yellow",
}


def _rule_color(rule_id: str) -> str:
    return _PREDEFINED_RULE_COLORS.get(rule_id[:3], "red")


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
        beneficiary / certificates  → ip_name           (e.g. "TRDP")
        banks / financials          → "Banks_Financials" (fixed)
    """
    key = normalise_type(data_type)
    if key in FIXED_STEM:
        return FIXED_STEM[key]
    return ip_name or file_id


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


# ── predefined summary helpers ────────────────────────────────────────────────
# (per-row predefined-rule extraction for the report was removed along with the
#  predefined_rules_failed/colors/fail_count report columns — see write_outputs.
#  The full per-filter detail, including any predefined entries if opted in via
#  run_predefined=true, still lives in validation_status / __validation_status__.)



def _merge_validation_summaries(
    predefined_summary: dict | None,
    user_summary: dict | None,
) -> dict:
    """
    Merge the predefined rule summary (from cleaning_engine) with the
    user-filter summary (from validation_engine.run_validation).

    The merged summary is what gets written to _validation_summary.json.
    It is the authoritative record of all failures for a pipeline run.

    Structure of returned dict
    ──────────────────────────
    {
      "total_rows":     int,
      "passed":         int,
      "failed":         int,
      "filter_results": [
        {
          "label":         str,
          "cond":          str,          # "predefined" | filter cond key
          "flagged_count": int,
          "color":         str,          # colour token for frontend
        }, ...
      ],
      "has_predefined":  bool,
      "has_user_filters": bool,
    }
    """
    pre  = predefined_summary or {}
    user = user_summary or {}

    # Use whichever has row counts (user summary is authoritative if both ran,
    # because run_validation re-computes PASS/FAIL after merging both sources)
    total  = user.get("total_rows") or pre.get("total_rows") or 0
    passed = user.get("passed")     if user else pre.get("passed", total)
    failed = user.get("failed")     if user else pre.get("failed", 0)

    # Collect filter_results: predefined first (sorted by rule ID), user after
    pre_results  = sorted(
        pre.get("filter_results", []),
        key=lambda r: r.get("label", ""),
    )
    user_results = user.get("filter_results", [])

    # Tag predefined entries with colour if not already set
    for r in pre_results:
        if "color" not in r:
            r["color"] = _rule_color(r.get("label", ""))
        r.setdefault("cond", "predefined")

    # Tag user filter entries with a default colour if not already set
    for r in user_results:
        r.setdefault("color", "red")

    # Deduplicate: if run_validation already included predefined entries
    # (because it read existing __validation_status__), avoid double-counting.
    # Keep only the first occurrence of each (label, cond) pair.
    seen: set[tuple[str, str]] = set()
    merged_results: list[dict] = []
    for r in pre_results + user_results:
        key = (r.get("label", ""), r.get("cond", ""))
        if key not in seen:
            seen.add(key)
            merged_results.append(r)

    return {
        "total_rows":      total,
        "passed":          passed if passed is not None else total,
        "failed":          failed if failed is not None else 0,
        "filter_results":  merged_results,
        "has_predefined":  bool(pre_results),
        "has_user_filters": bool(user_results),
    }


# ── main write function ───────────────────────────────────────────────────────

def write_outputs(
    file_id: str,
    cleaned_df: pd.DataFrame,
    result: dict[str, Any],
    data_type: str,
    ip_name: Optional[str] = None,
    validation_summary: Optional[dict] = None,
) -> dict[str, Any]:
    """
    Save cleaned+validated dataset + per-record report + validation summary.

    Parameters
    ----------
    file_id            : UUID string from the upload session
    cleaned_df         : output of clean_dataframe_* — may already contain
                         __validation_status__ written by cleaning_engine
    result             : response dict from clean_dataframe_* — may contain
                         "__predefined_validation_summary__" key
    data_type          : "beneficiary" | "certificates" | "banks" | "financials"
    ip_name            : implementing partner subfolder (beneficiary/certs only)
    validation_summary : summary returned by validation_engine.run_validation(),
                         or {} if no user filters were configured

    Output files
    ────────────
    <stem>_cleaned.parquet          – cleaned dataset with __validation_status__
    <stem>_report.parquet           – per-record change log + predefined rule fields
    <stem>_validation_summary.json  – unified summary of predefined + user rules
    """
    out_dir = resolve_dir(data_type, ip_name)
    ext     = ".parquet"
    stem    = output_stem(data_type, file_id, ip_name)

    # ── Pop predefined summary from result before row iteration ───────────────
    # cleaning_engine stores it under a string key; result rows use integer keys.
    predefined_summary: dict = result.pop("__predefined_validation_summary__", {}) or {}
    fast_meta: dict | None  = result.get("__fast_meta__") if isinstance(result, dict) else None

    # ── FILE 1: cleaned dataset (always includes a readable status column) ────
    cleaned_path = out_dir / f"{stem}_cleaned{ext}"
    # Shallow copy: every modification below REPLACES whole columns on
    # clean_out (never mutates shared arrays), so sharing blocks with
    # cleaned_df is safe and avoids another full-dataset copy in memory.
    clean_out    = cleaned_df.copy(deep=False)

    # NOTE: this used to add a human-readable "Validation Status" column
    # (PASS / FAIL: <labels>) alongside the raw "__validation_status__" JSON
    # column, both baked into the downloadable cleaned file. Per explicit
    # request, neither belongs in the file a user opens in Excel — both are
    # dropped below before writing. The in-app dataset viewer and
    # validation-results endpoints still show PASS/FAIL correctly; they now
    # source it from the report file's validation_status/validation_result
    # columns instead of from the cleaned parquet (see main.py).
    for col in clean_out.columns:
        if clean_out[col].dtype == object:
            clean_out[col] = clean_out[col].where(clean_out[col].notna(), other="")
        elif clean_out[col].dtype == bool or str(clean_out[col].dtype) == "boolean":
            clean_out[col] = clean_out[col].astype(str)
    clean_out = clean_out.drop(columns=["Validation Status", "__validation_status__"], errors="ignore")
    _write(clean_out, cleaned_path)
    del clean_out

    # ── FILE 2: per-record report ─────────────────────────────────────────────
    val_status_list: list[str]
    if "__validation_status__" in cleaned_df.columns:
        val_status_list = [str(v or "PASS") for v in cleaned_df["__validation_status__"].tolist()]
    else:
        val_status_list = ["PASS"] * len(cleaned_df)

    def _row_result(status: str) -> str:
        if status.startswith("{"):
            try:
                return json.loads(status).get("result", "PASS")
            except Exception:
                return "PASS" if status == "PASS" else "FAIL"
        return status if status in ("PASS", "FAIL") else "PASS"

    duplicate_positions: list[int] = []

    if fast_meta is not None:
        # ── FAST PATH: columnar build, JSON only for touched rows ─────────────
        n_rows      = int(fast_meta["n_rows"])
        uuid_keys   = fast_meta["uuid_keys"]
        dup_uuid    = fast_meta["dup_uuid"]
        dup_cnic    = fast_meta["dup_cnic"]
        changes_map = fast_meta["changes"].rows()    # {pos: {col: [val, step]}} — touched rows only
        reviews_map = fast_meta["reviews"].rows()    # {pos: {col: val}}         — touched rows only
        orig_map    = fast_meta["originals"].rows()  # {pos: {col: original_val}} — touched cells only

        duplicate_positions = np.flatnonzero(dup_uuid | dup_cnic).tolist()

        empty = "{}"
        ov_col = np.full(n_rows, empty, dtype=object)
        cv_col = np.full(n_rows, empty, dtype=object)
        rv_col = np.full(n_rows, empty, dtype=object)

        touched = set(changes_map) | set(reviews_map)
        for pos in touched:
            cv = changes_map.get(pos)
            rv = reviews_map.get(pos)
            ov = orig_map.get(pos)
            if ov: ov_col[pos] = _fast_json_dumps_safe(ov)
            if cv: cv_col[pos] = _fast_json_dumps_safe(cv)
            if rv: rv_col[pos] = _fast_json_dumps_safe(rv)

        report_df = pd.DataFrame({
            "uuid":              uuid_keys,
            "original_values":   ov_col,
            "cleaned_values":    cv_col,
            "manual_reviews":    rv_col,
            "is_dup":            np.where(dup_uuid, "true", "false"),
            "is_dup_cnic":       np.where(dup_cnic, "true", "false"),
            "validation_status": val_status_list,
            "validation_result": [_row_result(s) for s in val_status_list],
        })
    else:
        # ── LEGACY PATH: per-row dict result (unchanged behaviour) ────────────
        val_status_map: dict[int, str] = dict(enumerate(val_status_list))
        report_rows: list[dict] = []
        for row_i, (uuid_key, v) in enumerate(
            (kv for kv in result.items() if isinstance(kv[1], dict) and "cleaned_values" in kv[1])
        ):
            ov     = v["original_values"]
            cv     = v["cleaned_values"]
            rv     = v["manual_reviews_required"]
            is_dup      = bool(v["IS DUPLICATED UUID"])
            is_dup_cnic = bool(v.get("IS DUPLICATED CNIC", False))
            if is_dup or is_dup_cnic:
                duplicate_positions.append(row_i)

            touched      = set(cv.keys()) | set(rv.keys())
            orig_touched = {c: ov.get(c) for c in touched if c in ov}
            row_val_status = val_status_map.get(row_i, "PASS")

            report_rows.append({
                "uuid":                   uuid_key,
                "original_values":        json.dumps(orig_touched,  default=str, ensure_ascii=True),
                "cleaned_values":         json.dumps(cv,            default=str, ensure_ascii=True),
                "manual_reviews":         json.dumps(rv,            default=str, ensure_ascii=True),
                "is_dup":                 "true" if is_dup else "false",
                "is_dup_cnic":            "true" if is_dup_cnic else "false",
                "validation_status":      row_val_status,
                "validation_result":      _row_result(row_val_status),
            })
        report_df = pd.DataFrame(report_rows)

    report_path = out_dir / f"{stem}_report{ext}"
    _write(report_df, report_path)

    # ── FILE 3: duplicate-UUID / duplicate-CNIC rows only ─────────────────────
    dup_path = out_dir / f"{stem}_duplicates{ext}"
    if duplicate_positions:
        dup_df = cleaned_df.iloc[duplicate_positions].copy()
        for col in dup_df.columns:
            if dup_df[col].dtype == object:
                dup_df[col] = dup_df[col].where(dup_df[col].notna(), other="")
            elif dup_df[col].dtype == bool or str(dup_df[col].dtype) == "boolean":
                dup_df[col] = dup_df[col].astype(str)
    else:
        dup_df = cleaned_df.iloc[0:0].copy()  # empty, same schema
    # Same as the cleaned file — neither status column belongs in a file the
    # user downloads and opens directly.
    dup_df = dup_df.drop(columns=["Validation Status", "__validation_status__"], errors="ignore")
    _write(dup_df, dup_path)

    # ── FILE 3: unified validation summary sidecar JSON ───────────────────────
    # Merge predefined summary (from cleaning_engine) + user-filter summary
    # (from validation_engine.run_validation) into a single authoritative record.
    merged_summary = _merge_validation_summaries(predefined_summary, validation_summary)

    # Always write the sidecar — even if no user filters ran, predefined results
    # still need to be persisted so Load Results in the Validate tab can read them.
    sidecar = out_dir / f"{stem}_validation_summary.json"
    sidecar.write_text(
        json.dumps(merged_summary, default=str, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )

    # ── Return metadata ───────────────────────────────────────────────────────
    cleaned_mb = round(cleaned_path.stat().st_size / 1024 / 1024, 2)
    report_mb  = round(report_path.stat().st_size  / 1024 / 1024, 2)
    dup_mb     = round(dup_path.stat().st_size     / 1024 / 1024, 2)

    try:
        rel_dir = out_dir.relative_to(DATA_ROOT)
    except ValueError:
        rel_dir = out_dir

    return {
        "data_type":        normalise_type(data_type),
        "ip_name":          ip_name,
        "stem":             stem,
        "output_dir":       rel_dir,
        "cleaned_path":     cleaned_path,
        "report_path":      report_path,
        "duplicates_path":  dup_path,
        "ext":              ext,
        "cleaned_size_mb":  cleaned_mb,
        "report_size_mb":   report_mb,
        "duplicates_size_mb": dup_mb,
        "duplicate_rows":   len(duplicate_positions),
        "total_size_mb":    round(cleaned_mb + report_mb + dup_mb, 2),
        # Expose summary stats in the return so routes_clean can surface them
        "predefined_failed":   merged_summary.get("failed", 0),
        "predefined_rules_run": merged_summary.get("has_predefined", False),
    }


# ── read helpers ──────────────────────────────────────────────────────────────

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
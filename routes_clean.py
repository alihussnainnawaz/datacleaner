"""
routes_clean.py  —  Unified Clean + Validate pipeline
──────────────────────────────────────────────────────
beneficiary / certificates  → POST /api/clean/{data_type}/{ip_name}/{file_id}
banks / financials          → POST /api/clean/{data_type}/{file_id}
download                    → GET  .../download/cleaned

Accepts an optional JSON body with validation filters:
  {
    "filters": [ { "cond": "empty", "colIdx": 2, ... }, ... ]
  }
If no filters are supplied the pipeline runs cleaning + predefined validation
(rules R01–R12 from cleaning_engine.py) only.

Predefined validation is always run — it is part of the cleaning pipeline itself.
User-configured filters from the frontend are additive on top of predefined rules.
"""
from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from cleaning_engine import (
    clean_dataframe_fast,
    clean_dataframe_banks,
    clean_dataframe_certificates,
)
from validation_engine import run_validation
from file_handler import load_dataframe
import progress_store as _progress
from output_writer import (
    write_outputs,
    resolve_dir,
    InvalidDataLocation,
)

_CLEAN_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cleaner")

router = APIRouter()

# Sentinel key written by cleaning_engine into result dict — must be skipped
# in all row-iterating code paths.
_PREDEFINED_SUMMARY_KEY = "__predefined_validation_summary__"


def _location_or_400(data_type: str, ip_name: Optional[str]):
    try:
        resolve_dir(data_type, ip_name, create=False)
    except InvalidDataLocation as e:
        raise HTTPException(400, str(e))


# ── summary builder ────────────────────────────────────────────────────────────

def _summarise(result: dict) -> dict:
    """
    Build a cleaning summary from the result dict returned by clean_dataframe_*.

    Fast contract: if the engine returned a "__fast_meta__" payload (columnar
    change/review logs), compute every count from numpy arrays — no 690k-row
    Python iteration. Falls back to the legacy per-row-dict walk otherwise.
    """
    meta = result.get("__fast_meta__") if isinstance(result, dict) else None
    if meta is not None:
        changes = meta["changes"]
        reviews = meta["reviews"]
        n       = int(meta["n_rows"])
        step_counts = changes.step_counts()
        review_cols = reviews.column_counts()
        return {
            "total_rows":          n,
            "rows_auto_cleaned":   int(changes.touched_row_mask(n).sum()),
            "rows_need_review":    int(reviews.touched_row_mask(n).sum()),
            "duplicate_uuid_rows": int(meta["dup_uuid"].sum()),
            "cells_auto_cleaned":  int(changes.cell_count()),
            "cells_flagged":       int(reviews.cell_count()),
            "step_breakdown":      dict(sorted(step_counts.items(), key=lambda x: -x[1])),
            "review_by_column":    dict(sorted(review_cols.items(),  key=lambda x: -x[1])),
        }

    total       = 0
    clean_rows  = 0
    review_rows = 0
    dup_rows    = 0
    clean_cells = 0
    review_cells = 0
    step_counts: dict[str, int] = {}
    review_cols: dict[str, int] = {}

    for key, v in result.items():
        # Skip sentinel / non-row entries
        if not isinstance(v, dict) or "cleaned_values" not in v:
            continue
        total += 1

        cv = v["cleaned_values"]
        rv = v["manual_reviews_required"]

        if cv:
            clean_rows += 1
        if rv:
            review_rows += 1
        if v.get("IS DUPLICATED UUID"):
            dup_rows += 1

        for col_key, val in cv.items():
            # cleaned_values entries are [new_val, step_string]
            if not isinstance(val, (list, tuple)) or len(val) < 2:
                continue
            clean_cells += 1
            step_str = val[1] if isinstance(val[1], str) else str(val[1])
            for s in step_str.split(" | "):
                s = s.strip()
                if s:
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


# NOTE: the predefined_validation response block (rules_run / failed / colors)
# was removed — predefined auto-rules (R01–R12) no longer run by default, so
# that block was always empty/zero and added nothing to the API response.


# ── CLEAN TOOLS (operate directly on the saved parquet) ───────────────────────

from fastapi import Request as _Request
import pandas as _pd


def _load_parquet(data_type: str, ip_name: Optional[str]) -> tuple:
    """Return (df, path) for the most-recent cleaned parquet, or raise 404."""
    import glob as _glob
    try:
        folder = resolve_dir(data_type, ip_name, create=False)
    except InvalidDataLocation as e:
        raise HTTPException(400, str(e))
    files = _glob.glob(str(folder / "*_cleaned.parquet"))
    if not files:
        raise HTTPException(404, "No cleaned parquet found. Run the pipeline first.")
    path = max(files, key=lambda p: __import__("pathlib").Path(p).stat().st_mtime)
    df   = _pd.read_parquet(path, engine="fastparquet")
    return df, __import__("pathlib").Path(path)


def _find_report_parquet(data_type: str, ip_name: Optional[str]):
    """Return the most-recent report parquet path for a project, or None."""
    import glob as _glob
    from pathlib import Path as _Path
    try:
        folder = resolve_dir(data_type, ip_name, create=False)
    except InvalidDataLocation:
        return None
    files = _glob.glob(str(folder / "*_report.parquet"))
    if not files:
        return None
    return _Path(max(files, key=lambda p: _Path(p).stat().st_mtime))


def _sync_tool_edit_to_report(
    data_type: str, ip_name: Optional[str], column: str,
    before_vals: list, after_vals: list, step_label: str,
) -> None:
    """
    Standalone Clean Tools actions (Regex Clean, Title Case, Trim, etc.)
    used to only ever update the cleaned parquet on disk (plus a sidecar
    JSON that makes the in-app grid show a "cleaned" flag) — the actual
    downloadable Report file never learned about these edits at all, so a
    regex/mapping change applied via the toolbar was invisible in the
    Report even though it was genuinely applied to the data. This merges
    each such edit into the existing report parquet's per-row
    original_values / cleaned_values JSON, the same shape the main
    pipeline writes, so the Report file reflects EVERY change regardless
    of whether it came from the pipeline or a toolbar tool afterward.

    before_vals / after_vals must be full-length, row-order-aligned with
    the report file (same order the cleaned parquet is in).
    """
    import json as _json
    import fastparquet as _fp

    report_path = _find_report_parquet(data_type, ip_name)
    if not report_path or not report_path.exists():
        return
    try:
        rdf = _pd.read_parquet(report_path, engine="fastparquet")
    except Exception:
        return

    cv_col = rdf["cleaned_values"].tolist()
    ov_col = rdf["original_values"].tolist()
    n = min(len(rdf), len(before_vals), len(after_vals))
    changed = False
    for i in range(n):
        b, a = before_vals[i], after_vals[i]
        if b == a:
            continue
        try:
            cv = _json.loads(cv_col[i]) if cv_col[i] else {}
        except Exception:
            cv = {}
        try:
            ov = _json.loads(ov_col[i]) if ov_col[i] else {}
        except Exception:
            ov = {}
        if column not in ov:
            # First time this column changes for this row (in the report's
            # history) — record the value as it stood right before THIS
            # tool's edit, matching what "original_values" means elsewhere.
            ov[column] = b
        prev_step = cv.get(column, [None, ""])
        prev_step = prev_step[1] if isinstance(prev_step, list) and len(prev_step) > 1 else ""
        new_step = f"{prev_step} | {step_label}" if prev_step else step_label
        cv[column] = [a, new_step]
        cv_col[i] = _json.dumps(cv, default=str)
        ov_col[i] = _json.dumps(ov, default=str)
        changed = True

    if changed:
        rdf["cleaned_values"]   = cv_col
        rdf["original_values"]  = ov_col
        _fp.write(str(report_path), rdf, compression="ZSTD")


def _save_parquet(df: _pd.DataFrame, path) -> None:
    import fastparquet as _fp
    _fp.write(str(path), df.fillna(""), compression="ZSTD")


def _load_tool_flags(path) -> dict:
    """Load the tool-flags sidecar JSON {row_index: {col: 'cleaned'|'review'}}."""
    import json as _json
    sidecar = __import__("pathlib").Path(str(path).replace(".parquet", "__flags__.json"))
    if sidecar.exists():
        try:
            return {int(k): v for k, v in _json.loads(sidecar.read_text()).items()}
        except Exception:
            pass
    return {}


def _save_tool_flags(path, flags: dict) -> None:
    """Merge and persist tool flags sidecar."""
    import json as _json
    sidecar = __import__("pathlib").Path(str(path).replace(".parquet", "__flags__.json"))
    existing = _load_tool_flags(path)
    for row_i, cols in flags.items():
        row_i = int(row_i)
        if row_i not in existing:
            existing[row_i] = {}
        existing[row_i].update(cols)
    sidecar.write_text(_json.dumps(existing))


@router.post("/tools/unique", tags=["Clean Tools"])
async def tools_unique(request: _Request):
    """Return sorted unique non-null values for a column in the cleaned parquet."""
    body      = await request.json()
    data_type = body.get("data_type", "")
    ip_name   = body.get("ip_name") or None
    column    = body.get("column", "")

    df, _ = _load_parquet(data_type, ip_name)

    if column not in df.columns:
        raise HTTPException(400, f"Column '{column}' not found.")

    null_tokens = {"", "nan", "none", "null", "n/a", "na", "nil", "-", "--"}
    vals = sorted({
        str(v) for v in df[column].dropna().tolist()
        if str(v).strip().lower() not in null_tokens
    })
    return {"column": column, "values": vals, "count": len(vals)}


def _dataset_wide_cell_flags(data_type: str, ip_name: Optional[str]) -> tuple:
    """
    Build a dataset-wide {row_index: {col: 'cleaned'|'review'}} map, mirroring
    the per-page logic in main.py's /api/dataset endpoint, but scanning every
    row rather than just the current page. Returns (df, cell_flags, columns).
    Read-only — does not touch the saved parquet.
    """
    import json as _json

    df, path = _load_parquet(data_type, ip_name)
    columns  = list(df.columns)
    cell_flags: dict[int, dict[str, str]] = {}

    # Source 1: pipeline report parquet (cleaned_values + manual_reviews per row)
    try:
        report_files = __import__("glob").glob(str(path.parent / "*_report.parquet"))
        if report_files:
            rpath = max(report_files, key=lambda p: __import__("pathlib").Path(p).stat().st_mtime)
            rdf = _pd.read_parquet(rpath, engine="fastparquet")
            rdf = rdf[rdf["uuid"] != "__validation_summary__"].reset_index(drop=True)
            for row_i, rrow in rdf.iterrows():
                per_col: dict[str, str] = {}
                try:
                    cv = _json.loads(rrow["cleaned_values"]) if rrow["cleaned_values"] else {}
                    for col in cv:
                        per_col[col] = "cleaned"
                except Exception:
                    pass
                try:
                    rv = _json.loads(rrow["manual_reviews"]) if rrow["manual_reviews"] else {}
                    for col, v in rv.items():
                        if v is not None and col not in per_col:
                            per_col[col] = "review"
                except Exception:
                    pass
                if per_col:
                    cell_flags[int(row_i)] = per_col
    except Exception:
        pass

    # Source 2: tool-flags sidecar (written by toolbar tools) — overrides
    sidecar_flags = _load_tool_flags(path)
    for row_i, cols in sidecar_flags.items():
        if row_i not in cell_flags:
            cell_flags[row_i] = {}
        cell_flags[row_i].update(cols)

    return df, cell_flags, columns


_NULL_TOKENS = {"", "nan", "none", "null", "n/a", "na", "nil", "-", "--"}
_SPECIAL_CHARS = [("@","at"),("!","bang"),("?","question"),("<","angle"),
                  ("{","curly"),("#","hash"),("$","dollar"),("%","percent"),
                  ("^","caret"),("*","star"),("|","pipe"),("~","tilde")]


@router.post("/tools/flagged-values", tags=["Clean Tools"])
async def tools_flagged_values(request: _Request):
    """
    Dataset-wide (all pages) lookup of flagged values, scoped either:
      - by column (mode='column')      -> values needing review / validation fail in that column
      - by flag type (mode='flag')     -> values for a given flag bubble (auto-cleaned, needs-review,
                                          null, validation-fail, special-char) across all columns

    Powers the column-header popup and the footer-bubble popup in the Clean view.
    Read-only. Caps results at `limit` distinct value groups to stay responsive
    on large datasets.
    """
    body      = await request.json()
    data_type = body.get("data_type", "")
    ip_name   = body.get("ip_name") or None
    mode      = body.get("mode", "column")        # "column" | "flag"
    column    = body.get("column") or None
    flag_key  = body.get("flag") or None           # cell-auto-cleaned | cell-needs-review | null_value | flag-val-fail | special_at
    limit     = min(int(body.get("limit", 500)), 2000)

    df, cell_flags, columns = _dataset_wide_cell_flags(data_type, ip_name)

    if mode == "column" and (not column or column not in columns):
        raise HTTPException(400, f"Column '{column}' not found.")

    val_col = "__validation_status__"

    def _row_validation_failed(row) -> bool:
        if val_col not in df.columns:
            return False
        raw = str(row.get(val_col, "") or "")
        if not raw:
            return False
        try:
            if raw.startswith("{"):
                import json as _j
                return _j.loads(raw).get("result", "PASS") != "PASS"
            return raw not in ("PASS", "")
        except Exception:
            return raw not in ("PASS", "")

    groups: dict[str, dict] = {}
    target_cols = [column] if mode == "column" else [c for c in columns if c != val_col]

    for row_i, row in df.iterrows():
        cf = cell_flags.get(int(row_i), {})
        val_fail = _row_validation_failed(row)
        for col in target_cols:
            raw = "" if row[col] is None else str(row[col])
            is_null = raw.strip().lower() in _NULL_TOKENS
            has_special = any(ch in raw for ch, _n in _SPECIAL_CHARS)
            cflag = cf.get(col)  # 'cleaned' | 'review' | None

            include = False
            if mode == "column":
                include = bool(cflag) or val_fail or is_null
            else:
                if flag_key == "cell-auto-cleaned":
                    include = cflag == "cleaned"
                elif flag_key == "cell-needs-review":
                    include = cflag == "review"
                elif flag_key == "null_value":
                    include = is_null
                elif flag_key == "flag-val-fail":
                    include = val_fail
                elif flag_key == "special_at":
                    include = has_special
                else:
                    include = False

            if not include:
                continue

            key = f"{col}\u241f{raw}" if mode == "flag" else raw
            g = groups.get(key)
            if g is None:
                if len(groups) >= limit:
                    continue
                status = ("review" if cflag == "review" else
                          "cleaned" if cflag == "cleaned" else
                          "validation-fail" if val_fail else
                          "null" if is_null else "flagged")
                g = {"value": raw, "column": col, "count": 0, "rows": [], "status": status}
                groups[key] = g
            g["count"] += 1
            if len(g["rows"]) < 20:
                g["rows"].append(int(row_i) + 2)  # +2: 1-indexed + header row

    results = sorted(groups.values(), key=lambda g: -g["count"])[:limit]
    return {
        "mode":         mode,
        "column":       column,
        "flag":         flag_key,
        "total_groups": len(results),
        "values":       results,
    }


@router.post("/tools/standardize", tags=["Clean Tools"])
async def tools_standardize(request: _Request):
    """Apply a value mapping to a column and save the parquet in-place."""
    body      = await request.json()
    data_type = body.get("data_type", "")
    ip_name   = body.get("ip_name") or None
    column    = body.get("column", "")
    mapping   = body.get("mapping", {})

    df, path = _load_parquet(data_type, ip_name)

    if column not in df.columns:
        raise HTTPException(400, f"Column '{column}' not found.")

    changes      = int(df[column].isin(mapping.keys()).sum())
    changed_mask = df[column].isin(mapping.keys())
    df[column]   = df[column].map(lambda v: mapping.get(str(v), v) if _pd.notna(v) else v)
    _save_parquet(df, path)

    tool_flags = {int(i): {column: "cleaned"} for i in df.index[changed_mask]}
    if tool_flags:
        _save_tool_flags(path, tool_flags)

    return {"success": True, "column": column, "changes": changes}


@router.post("/tools/trim", tags=["Clean Tools"])
async def tools_trim(request: _Request):
    """Trim leading/trailing whitespace from all or selected columns."""
    body      = await request.json()
    data_type = body.get("data_type", "")
    ip_name   = body.get("ip_name") or None
    columns   = body.get("columns") or None

    df, path    = _load_parquet(data_type, ip_name)
    target_cols = columns if columns else df.columns.tolist()
    trim_flags: dict = {}

    for col in target_cols:
        if col in df.columns:
            before = df[col].astype(str)
            df[col] = before.str.strip().replace({"nan": "", "None": ""})
            changed = df.index[before != df[col].astype(str)]
            for i in changed:
                trim_flags.setdefault(int(i), {})[col] = "cleaned"

    _save_parquet(df, path)
    if trim_flags:
        _save_tool_flags(path, trim_flags)

    return {"success": True, "message": f"Whitespace trimmed from {len(target_cols)} column(s)."}


@router.post("/tools/not-null", tags=["Clean Tools"])
async def tools_not_null(request: _Request):
    """
    Flag null / empty cells in one or more columns for manual review.

    This is distinct from the Unique + Not Null primary-key handling that
    already runs inside the main cleaning pipeline for UUID/CNIC-type
    columns (which also checks uniqueness). This tool is a plain null check
    only — no uniqueness/duplicate logic — for any column the user picks,
    e.g. "Father Name should never be blank" without caring whether values
    repeat across rows.

    There is nothing to "clean" a missing value into, so this tool flags
    matching cells with 'review' (the same convention every other toolbar
    tool uses for cells that need a human decision) rather than mutating
    the dataset.
    """
    body      = await request.json()
    data_type = body.get("data_type", "")
    ip_name   = body.get("ip_name") or None
    columns   = body.get("columns") or None

    df, path    = _load_parquet(data_type, ip_name)
    target_cols = [c for c in (columns or df.columns.tolist()) if c in df.columns]
    if not target_cols:
        raise HTTPException(400, "No valid columns specified.")

    review_flags: dict = {}
    per_column_counts: dict[str, int] = {}

    for col in target_cols:
        raw = df[col].astype("string")
        is_null = (
            raw.isna() |
            raw.str.strip().str.lower().isin(_NULL_TOKENS)
        )
        flagged_rows = df.index[is_null]
        per_column_counts[col] = int(len(flagged_rows))
        for i in flagged_rows:
            review_flags.setdefault(int(i), {})[col] = "review"

    if review_flags:
        _save_tool_flags(path, review_flags)

    total_flagged = sum(per_column_counts.values())
    return {
        "success":            True,
        "columns":            target_cols,
        "total_flagged":      total_flagged,
        "per_column_counts":  per_column_counts,
        "message": (
            f"{total_flagged} null/empty cell(s) flagged for review across {len(target_cols)} column(s)."
            if total_flagged else
            f"No null/empty values found in {len(target_cols)} column(s)."
        ),
    }


@router.post("/tools/dates", tags=["Clean Tools"])
async def tools_dates(request: _Request):
    """Reformat date columns to the specified strftime format."""
    body      = await request.json()
    data_type = body.get("data_type", "")
    ip_name   = body.get("ip_name") or None
    columns   = body.get("columns", [])
    fmt       = body.get("fmt", "%d-%m-%Y")

    df, path = _load_parquet(data_type, ip_name)
    failed   = []

    for col in columns:
        if col not in df.columns:
            continue
        parsed = _pd.to_datetime(
            df[col].astype(str).str.strip(),
            errors="coerce", format="mixed", dayfirst=False,
        )
        for i, (orig, ts) in enumerate(zip(df[col], parsed)):
            if _pd.isna(ts):
                if str(orig).strip() not in {"", "nan", "None"}:
                    failed.append({"col": col, "row": i + 2, "value": str(orig)})
            else:
                df.at[df.index[i], col] = ts.strftime(fmt)

    _save_parquet(df, path)
    return {"success": True, "columns": columns, "format_applied": fmt, "failed_cells": failed}


def _apply_case_style(s: str, style: str) -> str:
    """Apply one case style to a single string value."""
    if style == "upper":
        return s.upper()
    if style == "lower":
        return s.lower()
    if style == "camel":
        words = re.split(r"\s+", s.strip())
        words = [w for w in words if w]
        if not words:
            return s
        first = words[0].lower()
        rest  = "".join(w[:1].upper() + w[1:].lower() if w else "" for w in words[1:])
        return first + rest
    # default: "title"
    return s.title()


@router.post("/tools/title-case", tags=["Clean Tools"])
async def tools_title_case(request: _Request):
    """
    Apply a case style to all or selected text columns.

    Backward compatible: a plain {"columns": [...]} body (no case_style/
    column_styles) behaves exactly as before — Title Case on every column.

    New, optional:
      "case_style":     "title" | "upper" | "lower" | "camel"
                         — applies one style to every column in `columns`
                           (or all text columns if `columns` is omitted).
      "column_styles":  { "ColumnName": "upper", "OtherColumn": "camel", ... }
                         — per-column style, for when different columns need
                           different casing in the same request. Takes
                           precedence over case_style for any column it lists;
                           case_style (or "title" if also absent) is used for
                           any selected column not listed here.
    """
    body          = await request.json()
    data_type     = body.get("data_type", "")
    ip_name       = body.get("ip_name") or None
    columns       = body.get("columns") or None
    default_style = (body.get("case_style") or "title").strip().lower()
    column_styles = body.get("column_styles") or {}

    valid_styles = {"title", "upper", "lower", "camel"}
    if default_style not in valid_styles:
        raise HTTPException(400, f"Invalid case_style '{default_style}'. Must be one of {sorted(valid_styles)}.")
    for col, style in column_styles.items():
        if style not in valid_styles:
            raise HTTPException(400, f"Invalid case_style '{style}' for column '{col}'. Must be one of {sorted(valid_styles)}.")

    df, path  = _load_parquet(data_type, ip_name)
    skip_cols = {"__validation_status__", "__changes__", "__reviews__"}

    if columns:
        target_cols = [c for c in columns if c in df.columns and c not in skip_cols]
    else:
        target_cols = [
            c for c in df.columns
            if c not in skip_cols and df[c].dtype == object
        ]

    null_tokens = {"", "nan", "none", "null", "n/a"}
    tool_flags: dict = {}
    total_changes = 0
    per_column_changes: dict[str, int] = {}

    for col in target_cols:
        style  = column_styles.get(col, default_style)
        before = df[col].astype(str)
        after  = before.apply(
            lambda v: _apply_case_style(v, style) if v.strip().lower() not in null_tokens else v
        )
        changed = df.index[before != after]
        per_column_changes[col] = int(len(changed))
        if len(changed):
            df.loc[changed, col] = after.loc[changed]
            total_changes += len(changed)
            for i in changed:
                tool_flags.setdefault(int(i), {})[col] = "cleaned"

    _save_parquet(df, path)
    if tool_flags:
        _save_tool_flags(path, tool_flags)

    return {
        "success":             True,
        "columns":             target_cols,
        "changes":             total_changes,
        "per_column_changes":  per_column_changes,
    }


@router.post("/tools/regex", tags=["Clean Tools"])
async def tools_regex(request: _Request):
    """Apply a regex find-replace to a column and save the parquet in-place."""
    import re as _re

    body         = await request.json()
    data_type    = body.get("data_type", "")
    ip_name      = body.get("ip_name") or None
    column       = body.get("column", "")
    pattern      = body.get("pattern", "")
    replacement  = body.get("replacement", "")
    flags_str    = body.get("flags", "")
    preview_only = bool(body.get("preview_only", False))

    if not pattern:
        raise HTTPException(400, "Pattern is required.")

    re_flags = 0
    if "i" in flags_str: re_flags |= _re.IGNORECASE
    if "m" in flags_str: re_flags |= _re.MULTILINE

    try:
        compiled = _re.compile(pattern, re_flags)
    except _re.error as e:
        raise HTTPException(400, f"Invalid regex: {e}")

    df, path = _load_parquet(data_type, ip_name)

    if column not in df.columns:
        raise HTTPException(400, f"Column '{column}' not found.")

    null_tokens = {"", "nan", "none", "null", "n/a"}
    before_vals = df[column].astype(str).tolist()
    after_vals  = [
        compiled.sub(replacement, v) if v.strip().lower() not in null_tokens else v
        for v in before_vals
    ]

    changes = sum(1 for b, a in zip(before_vals, after_vals) if b != a)

    preview = []
    for i, (b, a) in enumerate(zip(before_vals, after_vals)):
        if b != a:
            preview.append({"row": i + 2, "before": b, "after": a})
        if len(preview) >= 50:
            break

    if preview_only:
        return {"changes": changes, "preview": preview}

    df[column] = after_vals
    _save_parquet(df, path)
    _sync_tool_edit_to_report(data_type, ip_name, column, before_vals, after_vals, "REGEX_CLEAN")

    tool_flags = {i: {column: "cleaned"} for i, (b, a) in enumerate(zip(before_vals, after_vals)) if b != a}
    if tool_flags:
        _save_tool_flags(path, tool_flags)

    return {"success": True, "column": column, "changes": changes, "preview": preview}


@router.post("/tools/regex-clusters", tags=["Clean Tools"])
async def tools_regex_clusters(request: _Request):
    """
    Read-only: analyze a column's unique values and return them grouped into
    editable clusters (e.g. "bdin", "badin", "BADin" -> suggested target
    "Badin"), instead of a flat list of row-level before/after pairs.

    This powers the interactive Regex Clean preview: the user can rename a
    cluster's canonical target, or remove a wrongly-grouped member, before
    anything is applied — nothing is written to the dataset by this call.
    """
    from cleaning_engine import analyze_value_clusters

    body      = await request.json()
    data_type = body.get("data_type", "")
    ip_name   = body.get("ip_name") or None
    column    = body.get("column", "")

    df, _ = _load_parquet(data_type, ip_name)

    if column not in df.columns:
        raise HTTPException(400, f"Column '{column}' not found.")

    try:
        clusters = analyze_value_clusters(df, column)
    except Exception as e:
        raise HTTPException(500, f"Cluster analysis failed: {e}")

    total_affected = sum(len(c["members"]) for c in clusters)
    return {
        "column":         column,
        "clusters":       clusters,
        "cluster_count":  len(clusters),
        "total_affected": total_affected,
    }


@router.post("/tools/regex-apply-clusters", tags=["Clean Tools"])
async def tools_regex_apply_clusters(request: _Request):
    """
    Apply a USER-CONFIRMED (possibly hand-edited) set of value clusters from
    /tools/regex-clusters. The mapping is taken exactly as given — renamed
    canonicals and removed members are respected precisely, nothing is
    re-derived from the fuzzy matcher at this stage.

    Body:
      { "data_type", "ip_name", "column",
        "clusters": [
          { "canonical": "Badin", "original_canonical": "badin",
            "members": [{"value": "bdin"}, {"value": "BADin"}, ...] },
          ...
        ] }
      "original_canonical" should be the canonical value as it was returned
      by /tools/regex-clusters, BEFORE any rename — send it back unchanged
      even if the user renamed "canonical", so rows already holding that
      original literal value get remapped too. Omit it (or make it equal to
      "canonical") if the user didn't rename anything.
      A cluster with an empty members list (everything removed by the user)
      is simply skipped — nothing to apply.
    """
    from cleaning_engine import apply_value_cluster_mapping

    body      = await request.json()
    data_type = body.get("data_type", "")
    ip_name   = body.get("ip_name") or None
    column    = body.get("column", "")
    clusters  = body.get("clusters") or []

    if not column:
        raise HTTPException(400, "Column is required.")

    # Flatten the (edited) clusters back into a single {value: canonical} map.
    # Two sources of remapping per cluster:
    #   1. every listed member -> the cluster's canonical
    #   2. if the user renamed the canonical, the ORIGINAL canonical value
    #      (sent back as "original_canonical") must also map to the new one —
    #      rows already holding that literal value otherwise never get
    #      remapped, since the original canonical is the implicit target, not
    #      a listed member.
    mapping: dict[str, str] = {}
    for c in clusters:
        canonical = str(c.get("canonical", "")).strip()
        if not canonical:
            continue
        original_canonical = str(c.get("original_canonical", "")).strip()
        if original_canonical and original_canonical != canonical:
            mapping[original_canonical] = canonical
        for m in c.get("members", []):
            value = str(m.get("value", "")).strip() if isinstance(m, dict) else str(m).strip()
            if value and value != canonical:
                mapping[value] = canonical

    if not mapping:
        return {"success": True, "column": column, "changes": 0, "message": "No changes to apply."}

    df, path = _load_parquet(data_type, ip_name)
    if column not in df.columns:
        raise HTTPException(400, f"Column '{column}' not found.")

    try:
        cleaned_df, changes, reviews = apply_value_cluster_mapping(df, column, mapping)
    except Exception as e:
        raise HTTPException(500, f"Apply failed: {e}")

    before_full = df[column].astype(str).tolist()
    _save_parquet(cleaned_df, path)
    after_full = cleaned_df[column].astype(str).tolist()
    _sync_tool_edit_to_report(data_type, ip_name, column, before_full, after_full, "REGEX_CLUSTER_CLEAN")

    tool_flags: dict = {}
    for row_i, cols in changes.items():
        if column in cols:
            tool_flags.setdefault(row_i, {})[column] = "cleaned"
    if tool_flags:
        _save_tool_flags(path, tool_flags)

    return {
        "success": True,
        "column":  column,
        "changes": len(changes),
        "message": f"{len(changes)} cell(s) updated across {len(mapping)} value(s) standardised.",
    }


@router.post("/tools/regex-auto", tags=["Clean Tools"])
async def tools_regex_auto(request: _Request):
    """Auto-detect value clusters in a column and standardise them."""
    from cleaning_engine import auto_regex_clean_column

    body         = await request.json()
    data_type    = body.get("data_type", "")
    ip_name      = body.get("ip_name") or None
    column       = body.get("column", "")
    preview_only = bool(body.get("preview_only", False))

    df, path = _load_parquet(data_type, ip_name)

    if column not in df.columns:
        raise HTTPException(400, f"Column '{column}' not found.")

    try:
        cleaned_df, changes, reviews = auto_regex_clean_column(df, column)
    except Exception as e:
        raise HTTPException(500, f"Auto regex clean failed: {e}")

    preview = []
    for row_i, cols in changes.items():
        if column in cols:
            new_val = cols[column][0]
            old_val = df.iloc[row_i][column] if row_i < len(df) else ""
            preview.append({"row": row_i + 2, "before": str(old_val), "after": str(new_val)})
        if len(preview) >= 50:
            break

    review_rows = []
    for row_i, cols in reviews.items():
        if column in cols:
            review_rows.append({"row": row_i + 2, "value": str(cols[column])})
        if len(review_rows) >= 50:
            break

    total_changes = len(changes)
    total_reviews = len(reviews)

    if preview_only:
        return {
            "changes":      total_changes,
            "reviews":      total_reviews,
            "preview":      preview,
            "review_rows":  review_rows,
        }

    _save_parquet(cleaned_df, path)

    # Sync into the downloadable report file — previously only the sidecar
    # (in-app grid flag) was updated here, the Report file itself never
    # learned about these changes.
    before_full = df[column].astype(str).tolist()
    after_full  = cleaned_df[column].astype(str).tolist()
    _sync_tool_edit_to_report(data_type, ip_name, column, before_full, after_full, "REGEX_AUTO_CLEAN")

    tool_flags: dict = {}
    for row_i, cols in changes.items():
        if column in cols:
            tool_flags.setdefault(row_i, {})[column] = "cleaned"
    for row_i, cols in reviews.items():
        if column in cols:
            tool_flags.setdefault(row_i, {})[column] = "review"
    if tool_flags:
        _save_tool_flags(path, tool_flags)

    return {
        "success":      True,
        "column":       column,
        "changes":      total_changes,
        "reviews":      total_reviews,
        "preview":      preview,
        "review_rows":  review_rows,
    }


# NOTE: the "/tools/auto-clean" endpoint that used to live here has been
# removed. It called the cleaning engine with no enabled_rules argument at
# all, which bypassed Column Rules gating entirely and re-applied the old
# always-on legacy behaviour (every column matching the static schema got
# CNIC formatting / gender normalisation / bank fuzzy-matching / geo
# canonicalisation / casing applied regardless of what was configured on the
# Column Rule Preview screen). Nothing runs now except what the user
# explicitly configures via the main "Run Pipeline" flow.

# ── POST — with ip_name ────────────────────────────────────────────────────────

@router.post(
    "/{data_type}/{ip_name}/{file_id}",
    summary="Run clean+validate — beneficiary / certificates (IP required)",
)
async def clean_with_ip(
    data_type: str,
    ip_name:   str,
    file_id:   str,
    request:   Request,
    uuid_column: Optional[str] = Query(default=None),
):
    return await _run_clean(data_type, file_id, ip_name, uuid_column, request)


# ── POST — without ip_name ────────────────────────────────────────────────────

@router.post(
    "/{data_type}/{file_id}",
    summary="Run clean+validate — banks / financials (no IP)",
)
async def clean_no_ip(
    data_type: str,
    file_id:   str,
    request:   Request,
    uuid_column: Optional[str] = Query(default=None),
):
    return await _run_clean(data_type, file_id, None, uuid_column, request)


# ── shared pipeline ────────────────────────────────────────────────────────────

async def _run_clean(
    data_type:   str,
    file_id:     str,
    ip_name:     Optional[str],
    uuid_column: Optional[str],
    request:     Request,
):
    _location_or_400(data_type, ip_name)

    # Parse optional validation filters + global rule toggles from JSON body
    filters: list[dict] = []
    global_rules: dict  = {}
    run_predefined: bool = False   # auto rules R01–R12 are OFF by default now
    column_rules: dict[str, str] = {}   # per-column case style overrides
    reco_column_rules: dict[str, list] | None = None   # {col: [ruleKey,...]} from Column Rule Preview — gates which schema steps run at all
    regex_rules: dict = {}   # {col: {"mapping":...} | {"pattern","replacement","flags"} | {"auto": true}} — "Regex Clean" rules, now run AS a pipeline step instead of a separate pre-flight phase
    dtype_rules: dict[str, str] = {}   # {col: expectedType} — DATATYPE_CHECK column rule from Column Rule Preview
    dataset2_file_id: str | None = None   # Cross Check / Double Cross Check reference dataset, uploaded separately
    try:
        body         = await request.json()
        filters      = body.get("filters", []) or []
        global_rules = body.get("global_rules", {}) or {}
        run_predefined = bool(body.get("run_predefined", False))
        column_rules = body.get("column_rules", {}) or {}
        regex_rules  = body.get("regex_rules", {}) or {}
        dtype_rules  = body.get("dtype_rules", {}) or {}
        dataset2_file_id = body.get("dataset2_file_id") or None
        # Present only when the frontend sends it (i.e. this build); absent
        # for any older/other caller, which keeps the pre-existing
        # always-on schema behaviour rather than silently cleaning nothing.
        if "reco_column_rules" in body:
            reco_column_rules = body.get("reco_column_rules") or {}
    except Exception:
        pass  # no body or not JSON — cleaning only

    _progress.new(file_id)
    # NOTE: this used to wrap a "file" stage start/end around load_dataframe
    # here, shown in the popup as a "Validating file" step. Removed: the
    # file is already parsed and cached at upload time — by the time this
    # runs it's just a dict lookup (confirmed by direct measurement: 0.0s),
    # not real validation work, so it no longer gets its own step.
    try:
        df, _ = load_dataframe(file_id)
    except HTTPException:
        _progress.finish(file_id, error="Failed to load file")
        raise

    # An explicitly-selected UUID/ID column that doesn't exist in THIS
    # file's columns used to be silently dropped — the pipeline would just
    # fall back to synthetic ROW_2/ROW_3/... row identifiers with no
    # duplicate detection, giving no indication anything was wrong. Almost
    # always caused by a stale selection carried over from a previously
    # uploaded file (fixed on the frontend too), but failing loudly here
    # closes the gap for any other path that could still send a mismatched
    # value, instead of quietly doing the wrong thing.
    if uuid_column and uuid_column not in df.columns:
        _progress.finish(file_id, error=f"UUID/ID column '{uuid_column}' not found")
        raise HTTPException(
            400,
            f"Selected UUID/ID column '{uuid_column}' was not found in this file's "
            f"columns. Re-select it from the dropdown for this file, or clear it "
            f"to use fallback row numbering.",
        )

    import time as _time
    _stage_t: dict[str, float] = {}
    _t_pipeline0 = _time.perf_counter()
    _t0 = _time.perf_counter()

    # ── Step 1: Cleaning (CPU-bound) ──────────────────────────────────────────
    # Predefined rules R01–R12 no longer run automatically — they are opt-in via
    # the request body's "run_predefined": true. User-configured filters in
    # Step 2 are the source of truth for validation results by default.
    try:
        loop = asyncio.get_event_loop()
        _dt  = data_type.lower()
        _progress_cb = _progress.clean_step_cb(file_id)
        if _dt in {"banks", "financials"}:
            _clean_fn = partial(clean_dataframe_banks,        df, uuid_column=uuid_column, global_rules=global_rules, run_predefined=run_predefined, case_overrides=column_rules, enabled_rules=reco_column_rules, progress_cb=_progress_cb, regex_rules=regex_rules, dtype_rules=dtype_rules)
        elif _dt == "certificates":
            _clean_fn = partial(clean_dataframe_certificates, df, uuid_column=uuid_column, global_rules=global_rules, run_predefined=run_predefined, case_overrides=column_rules, enabled_rules=reco_column_rules, progress_cb=_progress_cb, regex_rules=regex_rules, dtype_rules=dtype_rules)
        else:
            _clean_fn = partial(clean_dataframe_fast,         df, uuid_column=uuid_column, global_rules=global_rules, run_predefined=run_predefined, case_overrides=column_rules, enabled_rules=reco_column_rules, progress_cb=_progress_cb, regex_rules=regex_rules, dtype_rules=dtype_rules)
        cleaned_df, result = await loop.run_in_executor(_CLEAN_POOL, _clean_fn)
    except Exception as e:
        _progress.finish(file_id, error=str(e))
        raise HTTPException(500, f"Cleaning failed: {e}")
    _stage_t["clean"] = _time.perf_counter() - _t0

    # Real per-step timings measured inside the cleaning engine (keyed by the
    # frontend pipeline-popup catalog keys: trim/null/special/cnic/cell/...).
    _engine_steps: dict[str, float] = {}
    if isinstance(result, dict) and "__fast_meta__" in result:
        _engine_steps = dict(result["__fast_meta__"].get("step_timings") or {})

    # NOTE: this used to evict file_handler._DF_CACHE[file_id] here to save
    # memory during a single big run. That was a mistake in practice: it
    # meant every SUBSEQUENT "Run Pipeline" click on the same upload paid
    # the full file-read-and-parse cost again from scratch (the "Validating
    # file" step), even though nothing on disk had changed. Uploading once
    # should mean parsing once — the cache is keyed by the file's mtime, so
    # it's already correctly invalidated whenever the file actually changes
    # (e.g. a regex/mapping rule rewrites it). We only drop our own local
    # reference to the DataFrame object (`del df` below); the cache entry
    # itself stays live so the next run is instant.
    del df
    _clean_fn = None   # the partial also holds a df reference — release it

    # Snapshot predefined summary before write_outputs pops it from result
    predefined_summary: dict = result.get(_PREDEFINED_SUMMARY_KEY, {})

    # ── Step 2: User-configured validation (additive on top of predefined) ────
    # run_validation merges predefined results already in __validation_status__
    # with the user filter results, producing a unified status column.
    validation_summary: dict = {}
    _t0 = _time.perf_counter()
    if filters:
        # Dataset 2 — only needed for cross/doublecross filters, so only pay
        # to load it when a dataset2_file_id was actually sent. Loaded from
        # the same in-memory upload store as the main file (see
        # file_handler.py) — it's never cleaned or written anywhere itself,
        # only read as a reference pool.
        df2 = None
        if dataset2_file_id:
            try:
                df2, _ = load_dataframe(dataset2_file_id)
            except HTTPException:
                _progress.finish(file_id, error="Dataset 2 file not found")
                raise
        try:
            _val_fn = partial(run_validation, cleaned_df, filters, progress_cb=_progress.filter_cb(file_id), df2=df2)
            validated_df, validation_summary = await loop.run_in_executor(
                _CLEAN_POOL, _val_fn
            )
        except Exception as e:
            _progress.finish(file_id, error=str(e))
            raise HTTPException(500, f"Validation failed: {e}")
    else:
        # No user filters — cleaned_df already has predefined results embedded.
        # validation_summary stays {} here; write_outputs will use predefined_summary.
        validated_df = cleaned_df
    _stage_t["validate"] = _time.perf_counter() - _t0

    # ── Step 3: Write outputs ─────────────────────────────────────────────────
    # write_outputs pops __predefined_validation_summary__ from result,
    # merges it with validation_summary, and writes the unified sidecar JSON.
    #
    # PERF/CORRECTNESS: this used to call write_outputs() directly, inline,
    # on the event loop — unlike the clean and validate stages above, which
    # both already run on the worker pool. A synchronous call here blocks
    # the entire event loop for the whole write duration, which meant the
    # "start" SSE push (scheduled via call_soon_threadsafe) couldn't
    # actually be delivered to the browser until write_outputs() returned —
    # so the popup's "Writing parquet output" row only ever received
    # "start" and "end" back-to-back, AFTER the write had already finished.
    # That's the direct cause of "no live timer, it just appears when done".
    # It also meant one big write blocked progress polling/SSE for every
    # OTHER concurrent pipeline run on the server, not just this one.
    # Offloading to the same pool used for cleaning/validation fixes both.
    _t0 = _time.perf_counter()
    _progress.event(file_id, "stage", "start", key="write")
    try:
        _write_fn = partial(
            write_outputs,
            file_id, validated_df, result,
            data_type=data_type, ip_name=ip_name,
            validation_summary=validation_summary,
        )
        meta = await loop.run_in_executor(_CLEAN_POOL, _write_fn)
    except InvalidDataLocation as e:
        _progress.finish(file_id, error=str(e))
        raise HTTPException(400, str(e))
    except Exception as e:
        _progress.finish(file_id, error=str(e))
        raise HTTPException(500, f"Failed to write output files: {e}")
    _progress.event(file_id, "stage", "end", key="write")
    _stage_t["write"] = _time.perf_counter() - _t0
    _stage_t["total"] = _time.perf_counter() - _t_pipeline0

    _progress.finish(file_id)
    # NOTE: this used to call delete_file(file_id) here, deleting the raw
    # uploaded file from disk right after every successful run. That meant
    # a SECOND "Run Pipeline" click on the same upload — after tweaking a
    # filter or column rule — 404'd outright ("No file found for file_id"),
    # forcing a full re-upload (network transfer + re-parse) just to try
    # again. That reupload-every-time cost is almost certainly the "extra
    # minutes" this was costing on repeated runs. The file is intentionally
    # left in place now — upload once, iterate and re-run as many times as
    # needed. It's still deletable on request via DELETE /api/upload/{file_id}
    # (routes_upload.py), which a "remove this dataset" action can call
    # explicitly when the user is actually done with it.

    # ── Step 4: Build response ────────────────────────────────────────────────
    cleaning_summary  = _summarise(result)
    stem              = meta["stem"]
    dtype             = meta["data_type"]
    base              = f"/api/clean/{dtype}" + (f"/{ip_name}" if ip_name else "")

    # Validation summary to surface in the response: driven by user-configured
    # filters (predefined auto-rules no longer run by default — predefined_summary
    # stays at its zeroed default unless run_predefined=true was explicitly set).
    response_val_summary = validation_summary if validation_summary else {
        "total_rows":     predefined_summary.get("total_rows", 0),
        "passed":         predefined_summary.get("passed", 0),
        "failed":         predefined_summary.get("failed", 0),
        "filter_results": predefined_summary.get("filter_results", []),
    }

    return JSONResponse({
        "file_id":   file_id,
        "data_type": dtype,
        "ip_name":   ip_name,

        # REAL measured wall-clock timings (seconds) — replaces the frontend's
        # weight-based fabricated splits in the pipeline progress popup.
        #   stages : coarse {clean, validate, write, total}
        #   steps  : per cleaning step, keyed by the popup catalog keys
        #   filters: per user-configured validation filter [{label, cond, seconds}]
        "step_timings": {
            "stages":  {k: round(v, 3) for k, v in _stage_t.items()},
            "steps":   {k: round(v, 3) for k, v in _engine_steps.items()},
            "filters": (validation_summary or {}).get("filter_timings", []),
        },

        # Cleaning step summary (auto-cleaned cells, review flags, duplicates)
        "summary": cleaning_summary,

        # Combined validation summary — driven entirely by user-configured
        # filters now that the predefined auto-rules (R01–R12) no longer run
        # automatically. Mirrors the shape of the _validation_summary.json sidecar.
        "validation_summary": response_val_summary,

        "output_files": {
            "cleaned":            str(meta["cleaned_path"].name),
            "report":             str(meta["report_path"].name),
            "duplicates":         str(meta["duplicates_path"].name),
            "format":             meta["ext"],
            "cleaned_size_mb":    meta["cleaned_size_mb"],
            "report_size_mb":     meta["report_size_mb"],
            "duplicates_size_mb": meta["duplicates_size_mb"],
            "duplicate_rows":     meta["duplicate_rows"],
            "total_size_mb":      meta["total_size_mb"],
            "saved_to":           str(meta["output_dir"]),
        },
        "download_urls": {
            "cleaned_dataset": f"{base}/{stem}/download/cleaned",
            "report":          f"{base}/{stem}/download/report",
            "duplicates":      f"{base}/{stem}/download/duplicates",
        },
    })


# ── GET live pipeline progress ───────────────────────────────────────────────
#
# Real replacement for the frontend's old weight-based fake ticker. Poll this
# while a /clean/{...}/{file_id} call is in flight to get the ACTUAL current
# step and ACTUAL elapsed seconds for every step that has started or
# finished so far — every number here comes from a real time.perf_counter()
# measurement taken as that step ran, not a simulated animation.
#
# Response shape:
#   current         : {"type","key"?,"cond"?} | null — the step in progress
#                      right now (or null if between steps / not started).
#   done            : [{"type","key"?/"cond"?,"seconds"}] — every step that
#                      has finished so far, with its real measured duration.
#   elapsed_current : seconds elapsed on the current step so far (live).
#   finished        : true once the whole pipeline call has returned
#                      (successfully or with an error).
#   error           : error message string if the run failed, else null.
#   known           : false if file_id isn't a run we're tracking (e.g. the
#                      pipeline hasn't started yet, or already expired).
@router.get("/progress/{file_id}", summary="Poll live real-time pipeline progress (fallback)")
async def get_clean_progress(file_id: str):
    return JSONResponse(_progress.snapshot(file_id))


# ── GET live pipeline progress, PUSHED (Server-Sent Events) ────────────────
#
# Preferred over the polling endpoint above: ONE connection for the whole
# run, and the server sends a message only when something real actually
# happens (a step starts, a step ends, the run finishes) — no repeated
# "are you done yet?" requests. The frontend keeps its own local clock
# ticking for whichever step is currently active between messages; this
# stream is only responsible for telling it when to start a new one or
# freeze the current one, exactly like a real event notification instead
# of a status poll.
#
# Message shape is identical to the polling endpoint's response body — see
# the docstring above. The stream closes itself once `finished` is true.
@router.get("/progress-stream/{file_id}", summary="Live pipeline progress via Server-Sent Events")
async def stream_clean_progress(file_id: str):
    import json as _json

    async def _events():
        # The frontend opens this connection right when the pipeline
        # request is about to be sent — it can genuinely arrive at the
        # server microseconds before that POST does. Wait briefly for the
        # run to register instead of giving up immediately; this is a
        # short server-side wait loop, not client-facing polling — the
        # browser still only holds the one open connection throughout.
        known_deadline = asyncio.get_event_loop().time() + 30
        snap = _progress.snapshot(file_id)
        while not snap.get("known") and asyncio.get_event_loop().time() < known_deadline:
            await asyncio.sleep(0.1)
            snap = _progress.snapshot(file_id)

        # Atomic: read current state AND subscribe to future events in one
        # locked operation, so nothing that happens in between is missed.
        snap, q = _progress.snapshot_and_subscribe(file_id)
        yield f"data: {_json.dumps(snap)}\n\n"
        if snap.get("finished") or not snap.get("known") or q is None:
            return

        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    # Comment ping so intermediary proxies/load balancers
                    # don't time out an idle connection — not a data event.
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {_json.dumps(item)}\n\n"
                if item.get("finished"):
                    break
        finally:
            _progress.unsubscribe(file_id, q)

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── GET download/cleaned ───────────────────────────────────────────────────────

@router.get(
    "/{data_type}/{ip_name}/{stem}/download/cleaned",
    summary="Download cleaned+validated parquet (beneficiary / certificates)",
)
async def dl_cleaned_with_ip(data_type: str, ip_name: str, stem: str):
    return _serve_cleaned(data_type, ip_name, stem)


@router.get(
    "/{data_type}/{stem}/download/cleaned",
    summary="Download cleaned+validated parquet (banks / financials)",
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
    raise HTTPException(404, "Cleaned file not found")


# ── GET download/report ──────────────────────────────────────────────────────

@router.get(
    "/{data_type}/{ip_name}/{stem}/download/report",
    summary="Download per-record report parquet (beneficiary / certificates)",
)
async def dl_report_with_ip(data_type: str, ip_name: str, stem: str):
    return _serve_named(data_type, ip_name, stem, "report")


@router.get(
    "/{data_type}/{stem}/download/report",
    summary="Download per-record report parquet (banks / financials)",
)
async def dl_report_no_ip(data_type: str, stem: str):
    return _serve_named(data_type, None, stem, "report")


# ── GET download/duplicates ──────────────────────────────────────────────────

@router.get(
    "/{data_type}/{ip_name}/{stem}/download/duplicates",
    summary="Download duplicate-UUID/CNIC rows parquet (beneficiary / certificates)",
)
async def dl_duplicates_with_ip(data_type: str, ip_name: str, stem: str):
    return _serve_named(data_type, ip_name, stem, "duplicates")


@router.get(
    "/{data_type}/{stem}/download/duplicates",
    summary="Download duplicate-UUID/CNIC rows parquet (banks / financials)",
)
async def dl_duplicates_no_ip(data_type: str, stem: str):
    return _serve_named(data_type, None, stem, "duplicates")


def _serve_named(data_type: str, ip_name: Optional[str], stem: str, kind: str):
    try:
        folder = resolve_dir(data_type, ip_name, create=False)
    except InvalidDataLocation as e:
        raise HTTPException(400, str(e))
    p = folder / f"{stem}_{kind}.parquet"
    if p.exists():
        return FileResponse(p, filename=p.name, media_type="application/octet-stream")
    raise HTTPException(404, f"{kind.capitalize()} file not found")
    raise HTTPException(404, f"No cleaned file found for '{stem}'. Run cleaning first.")
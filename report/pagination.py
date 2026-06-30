"""
pagination.py
─────────────
Cursor-based pagination over a *_report.parquet file (per-record cleaning audit).

The report parquet has 6 columns:
    uuid             – unique record id (the cursor key)
    original_values  – JSON string
    cleaned_values   – JSON string
    manual_reviews   – JSON string
    is_dup           – bool  (duplicate UUID)
    is_dup_cnic      – bool  (duplicate CNIC)

Cursor model
------------
Rows are ordered by uuid (numerically when uuids are numeric, else lexically).
A cursor is an opaque base64 token wrapping the sort key of the last row of the
previous page. The next page returns the first `page_size` rows whose sort key
is strictly greater than the cursor's. Because uuid is unique and ordering is
stable, page boundaries never skip or duplicate rows.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd

CURSOR_COLUMN = "uuid"
JSON_COLUMNS = ("original_values", "cleaned_values", "manual_reviews","validation_status")


# ── cursor encode / decode ─────────────────────────────────────────────────────

def encode_cursor(sort_key: Any) -> str:
    """Opaque, URL-safe token wrapping the last row's sort key."""
    raw = json.dumps({"k": sort_key}, ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(token: str) -> Any:
    """Reverse of encode_cursor. Raises ValueError on a malformed token."""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
        return obj["k"]
    except Exception as e:  # noqa: BLE001 - any decode failure is a bad cursor
        raise ValueError(f"Invalid cursor token: {e}") from e


# ── parquet loading + sorting ──────────────────────────────────────────────────

def load_report(path: Path) -> pd.DataFrame:
    """Read the report parquet and return it sorted by the cursor key.

    Adds a hidden __sort_key__ column used for ordering and cursor search.
    Sorts uuids numerically when they all parse as numbers, else lexically.
    """
    if not path.exists():
        raise FileNotFoundError(f"Parquet file not found: {path}")
    df = pd.read_parquet(path, engine="fastparquet")
    if CURSOR_COLUMN not in df.columns:
        raise KeyError(
            f"Report parquet is missing the '{CURSOR_COLUMN}' column. "
            f"Found columns: {list(df.columns)}"
        )
    df[CURSOR_COLUMN] = df[CURSOR_COLUMN].astype(str)

    # No __validation_summary__ row in the parquet — it's stored as a sidecar JSON

    numeric = pd.to_numeric(df[CURSOR_COLUMN], errors="coerce")
    if numeric.notna().all():
        df["__sort_key__"] = numeric.astype("int64")
    else:
        df["__sort_key__"] = df[CURSOR_COLUMN]

    df = df.sort_values("__sort_key__", kind="stable").reset_index(drop=True)
    return df


# ── pagination core ────────────────────────────────────────────────────────────

def get_page(
    df: pd.DataFrame,
    page_size: int,
    cursor: Optional[str] = None,
    decode_json: bool = True,
    page: Optional[int] = None,
) -> dict[str, Any]:
    """Return one page of rows, plus a next_cursor token (None when the file
    is exhausted) and full page-count metadata for direct page-number jumps.

    Two ways to choose which page:
      - cursor  (existing, forward-only): resumes after the last row of a
        previous page. Cheap, stable under concurrent appends, but can only
        move forward one page at a time.
      - page    (new, 1-based): jumps directly to any page by offset —
        start = (page - 1) * page_size — against the already-sorted,
        already-in-memory dataframe, so this is a plain O(1) slice, not a
        walk through every preceding page. Lets a client request "page 4"
        directly instead of paging through 1 → 2 → 3 → 4.

    If both are omitted, returns page 1. If both are given, `page` wins
    (it's the more explicit request). `cursor`-based clients are completely
    unaffected — this is purely additive.
    """
    if page_size < 1:
        raise ValueError("page_size must be >= 1")

    total = len(df)
    total_pages = max(1, -(-total // page_size))  # ceil(total / page_size), never 0

    if page is not None:
        if page < 1:
            raise ValueError("page must be >= 1")
        start = (page - 1) * page_size
    elif cursor is None:
        start = 0
    else:
        last_key = decode_cursor(cursor)
        start = int(df["__sort_key__"].searchsorted(last_key, side="right"))

    # Current page number from whichever start we ended up with (so cursor-
    # based callers also get an accurate current_page in the response).
    current_page = (start // page_size) + 1 if total else 1

    window = df.iloc[start : start + page_size]
    rows: list[dict[str, Any]] = []
    for _, r in window.iterrows():
        row = {
            "uuid":            r["uuid"],
            "original_values": r.get("original_values", ""),
            "cleaned_values":  r.get("cleaned_values", ""),
            "manual_reviews":  r.get("manual_reviews", ""),
            "is_dup":          bool(r.get("is_dup",      False)),
            "is_dup_cnic":     bool(r.get("is_dup_cnic", False)),
        }
        if decode_json:
            for c in JSON_COLUMNS:
                val = row.get(c)
                row[c] = json.loads(val) if isinstance(val, str) and val else {}
        rows.append(row)

    has_more = (start + page_size) < total
    if rows and has_more:
        last_key = df["__sort_key__"].iloc[start + len(rows) - 1]
        if hasattr(last_key, "item"):
            last_key = last_key.item()
        next_cursor = encode_cursor(last_key)
    else:
        next_cursor = None

    # ── Overall summary (full-file counts, not page-scoped) ──────────────────
    # df is already in memory, so these are just 4 vectorised column ops.
    cleaned_col = df["cleaned_values"] if "cleaned_values" in df.columns else pd.Series(dtype=str)
    review_col  = df["manual_reviews"] if "manual_reviews" in df.columns else pd.Series(dtype=str)

    def _nonempty(col: pd.Series) -> int:
        """Count rows where the JSON string is a non-empty object/array."""
        s = col.astype(str).str.strip()
        return int((~s.isin(["", "null", "None", "{}", "[]"])).sum())

    summary = {
        "total_cleaned":        _nonempty(cleaned_col),
        "total_review":         _nonempty(review_col),
        "total_duplicate_uuid": int(df["is_dup"].astype(bool).sum())     if "is_dup"      in df.columns else 0,
        "total_duplicate_cnic": int(df["is_dup_cnic"].astype(bool).sum()) if "is_dup_cnic" in df.columns else 0,
    }

    return {
        "pagination": {
            "page_size":    page_size,
            "returned":     len(rows),
            "total_rows":   total,
            # New: lets the client render "Page X of Y" and a jump-to-page
            # control instead of only Next/Prev.
            "total_pages":  total_pages,
            "current_page": current_page,
            "has_more":     has_more,
            "has_prev":     current_page > 1,
            "next_cursor":  next_cursor,
            "cursor":       cursor,
        },
        "summary": summary,
        "rows": rows,
    }

"""
pagination.py
─────────────
Cursor-based pagination over a *_report.parquet file (per-record cleaning audit).

The report parquet has 5 columns:
    uuid             – unique record id (the cursor key)
    original_values  – JSON string
    cleaned_values   – JSON string
    manual_reviews   – JSON string
    is_dup           – bool

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
JSON_COLUMNS = ("original_values", "cleaned_values", "manual_reviews")


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
) -> dict[str, Any]:
    """Return one page of rows after `cursor`, plus a next_cursor token
    (None when the file is exhausted)."""
    if page_size < 1:
        raise ValueError("page_size must be >= 1")

    if cursor is None:
        start = 0
    else:
        last_key = decode_cursor(cursor)
        start = int(df["__sort_key__"].searchsorted(last_key, side="right"))

    window = df.iloc[start : start + page_size]
    rows: list[dict[str, Any]] = []
    for _, r in window.iterrows():
        row = {
            "uuid":            r["uuid"],
            "original_values": r.get("original_values", ""),
            "cleaned_values":  r.get("cleaned_values", ""),
            "manual_reviews":  r.get("manual_reviews", ""),
            "is_dup":          bool(r.get("is_dup", False)),
        }
        if decode_json:
            for c in JSON_COLUMNS:
                val = row.get(c)
                row[c] = json.loads(val) if isinstance(val, str) and val else {}
        rows.append(row)

    total = len(df)
    has_more = (start + page_size) < total
    if rows and has_more:
        last_key = df["__sort_key__"].iloc[start + len(rows) - 1]
        if hasattr(last_key, "item"):
            last_key = last_key.item()
        next_cursor = encode_cursor(last_key)
    else:
        next_cursor = None

    return {
        "pagination": {
            "page_size":   page_size,
            "returned":    len(rows),
            "total_rows":  total,
            "has_more":    has_more,
            "next_cursor": next_cursor,
            "cursor":      cursor,
        },
        "rows": rows,
    }

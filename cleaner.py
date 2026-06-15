"""Optimized dynamic cleaner.

This version avoids hardcoded dataset schemas and avoids per-row Python writes.
Core ideas:
- detect columns by name patterns
- vectorized pandas string operations
- fuzzy/cache mappings only on unique values
- issue reporting is capped for UI safety, while summary counts stay exact
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd
from rapidfuzz import process, fuzz

from schemas import CleaningReport, InconsistencyItem

try:
    from config import BANK_ALIAS_MAP, BANK_NAMES, GEO_COLUMNS, AUTO_DATE_FORMAT, FUZZY_EXACT_THRESHOLD
except Exception:  # keeps module importable in tests
    BANK_ALIAS_MAP, BANK_NAMES, GEO_COLUMNS = {}, {}, {}
    AUTO_DATE_FORMAT = "%m/%d/%Y"
    FUZZY_EXACT_THRESHOLD = 90

MAX_FLAGGED_CELLS = 5000
NULL_TOKENS = {"", "nan", "none", "null", "n/a", "na", "nil", "-", "--", "#n/a"}
CNIC_FAKE_VALUES = {
    "0000000000000", "1111111111111", "2222222222222", "3333333333333",
    "4444444444444", "5555555555555", "6666666666666", "7777777777777",
    "8888888888888", "9999999999999", "1234567890123", "4330190000000",
}
YES_VALUES = {"y", "yes", "true", "t", "1", "active", "available", "present", "ok"}
NO_VALUES = {"n", "no", "false", "f", "0", "inactive", "not available", "absent", "none"}
GENDER_MAP = {
    "m": "Male", "male": "Male", "man": "Male", "masculine": "Male",
    "f": "Female", "female": "Female", "woman": "Female", "fem": "Female",
    "t": "Transgender", "trans": "Transgender", "transgender": "Transgender", "third gender": "Transgender",
}
TEXT_EXCLUDE_HINTS = ("uuid", "cnic", "phone", "mobile", "contact", "iban", "date", "time", "amount", "income", "count", "number", "code", "lat", "lon", "longitude", "latitude")
TEXT_FORCE_HINTS = ("name", "title", "beneficiary", "father", "mother", "spouse", "owner", "guardian")
BOOL_HINTS = ("is_", "has_", "have_", "was_", "were_", "bool", "verified", "approved", "eligible", "available", "present", "disable", "disabled", "widow", "hazard", "pucca", "kacha")
BANK_HINTS = ("bank", "payment_by", "ifis", "ifi", "donor", "funding")
GEO_HINTS = ("district", "tehsil", "taluka", "town", "union_council", "uc", "jh_uc", "deh")
UUID_HINTS = ("uuid", "uu_id", "beneficiary_id", "household_id")
CNIC_HINTS = ("cnic",)

_WORD_RE = re.compile(r"[^A-Za-z0-9\s]+")
_MULTI_SPACE_RE = re.compile(r"\s+")


def _norm_col(c: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_")


def resolve_column_name(df: pd.DataFrame, column: str) -> str | None:
    target = _norm_col(column)
    for c in df.columns:
        if _norm_col(c) == target:
            return c
    return None


def _cols_with(df: pd.DataFrame, hints: tuple[str, ...]) -> list[str]:
    out = []
    for c in df.columns:
        n = _norm_col(c)
        if any(h in n for h in hints):
            out.append(c)
    return out


def _string_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if df[c].dtype == object or pd.api.types.is_string_dtype(df[c])]


def _is_text_clean_column(col: str, s: pd.Series) -> bool:
    """Detect true free-text/name columns without damaging IDs, amounts or account numbers."""
    n = _norm_col(col)
    if any(h in n for h in ("cnic", "uuid", "phone", "mobile", "iban", "date", "time", "ip", "email")):
        return False
    tokens = set(n.split("_"))
    if any(h in n for h in ("account_number", "account_no", "bank_account_num", "bank_account_number", "latitude", "longitude")):
        return False
    if tokens & {"amount", "income", "count", "number", "total"}:
        return False
    sample = s.dropna().astype(str).str.strip().head(500)
    if sample.empty:
        return False
    if any(h in n for h in TEXT_FORCE_HINTS):
        return True
    alpha_ratio = sample.str.contains(r"[A-Za-z]", regex=True).mean()
    numeric_ratio = sample.str.fullmatch(r"[0-9 .,/:-]+", na=False).mean()
    return alpha_ratio >= 0.70 and numeric_ratio < 0.30


def _null_mask(s: pd.Series) -> pd.Series:
    ss = s.astype("string")
    return s.isna() | ss.str.strip().str.lower().isin(NULL_TOKENS)


def _identifier_digits(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if not text or text.lower() in NULL_TOKENS:
        return ""
    try:
        if re.fullmatch(r"[+-]?\d+(\.0+)?", text) or re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)[eE][+-]?\d+", text):
            return str(int(Decimal(text).to_integral_value()))
    except (InvalidOperation, ValueError):
        pass
    return re.sub(r"\D", "", text)


def _to_display_value(value: Any, column: str | None = None) -> str:
    if pd.isna(value):
        return "None"
    text = str(value).strip()
    if text.lower() in NULL_TOKENS:
        return "None"
    if column and any(h in _norm_col(column) for h in CNIC_HINTS):
        digits = _identifier_digits(value)
        if digits:
            return digits
    return str(value)


def _issue(row: int, col: str, issue_type: str, original: Any, fix: Any = None, conf: float = 1.0) -> InconsistencyItem:
    return InconsistencyItem(
        row=int(row) + 2, column=str(col), original_value=_to_display_value(original, col),
        suggested_fix=None if fix is None or pd.isna(fix) else _to_display_value(fix, col), confidence=float(conf), issue_type=issue_type,
    )


def _append_issues(issues: list[InconsistencyItem], rows, col, typ, before, after=None, conf=1.0):
    for r in list(rows):
        old = before.loc[r] if isinstance(before, pd.Series) and r in before.index else before
        new = after.loc[r] if isinstance(after, pd.Series) and after is not None and r in after.index else after
        issues.append(_issue(r, col, typ, old, new, conf))


def _standardize_nulls(df: pd.DataFrame, issues: list[InconsistencyItem]) -> int:
    changes = 0
    for c in df.columns:
        mask = _null_mask(df[c])
        changes += int(mask.sum())
        _append_issues(issues, df.index[mask], c, "missing", df[c], None, 0.0)
        if mask.any():
            df.loc[mask, c] = None
    return changes


def _trim(df: pd.DataFrame, issues: list[InconsistencyItem]) -> int:
    changes = 0
    for c in _string_cols(df):
        before = df[c].copy()
        # strip leading/trailing whitespace and collapse repeated internal whitespace
        after = before.astype("string").str.replace(r"\s+", " ", regex=True).str.strip()
        mask = before.notna() & (before.astype("string") != after)
        changes += int(mask.sum())
        _append_issues(issues, df.index[mask], c, "trim", before, after)
        if mask.any():
            df.loc[mask, c] = after[mask].astype(object)
    return changes


def _clean_text_columns(df: pd.DataFrame, issues: list[InconsistencyItem]) -> int:
    changes = 0
    for c in _string_cols(df):
        if not _is_text_clean_column(c, df[c]):
            continue
        before = df[c].copy()
        after = (before.astype("string")
                 .str.replace(_WORD_RE, " ", regex=True)
                 .str.replace(_MULTI_SPACE_RE, " ", regex=True)
                 .str.strip()
                 .str.title())
        mask = before.notna() & (before.astype("string") != after)
        changes += int(mask.sum())
        _append_issues(issues, df.index[mask], c, "text_cleaning", before, after)
        if mask.any():
            df.loc[mask, c] = after[mask].astype(object)
    return changes


def _standardize_cnics(df: pd.DataFrame, issues: list[InconsistencyItem]) -> int:
    changes = 0
    for c in _cols_with(df, CNIC_HINTS):
        raw = df[c].copy()
        digits = raw.map(_identifier_digits).astype("string")
        nonnull = ~_null_mask(raw)
        valid_len = digits.str.len().eq(13)
        changed = nonnull & valid_len & (raw.astype("string").str.strip() != digits)
        changes += int(changed.sum())
        _append_issues(issues, df.index[changed], c, "cnic_format", raw, digits)
        if valid_len.any():
            df.loc[nonnull & valid_len, c] = digits[nonnull & valid_len].astype(object)
        bad = nonnull & (~valid_len | digits.isin(CNIC_FAKE_VALUES) | digits.str.fullmatch(r"(\d)\1{12}").fillna(False))
        _append_issues(issues, df.index[bad], c, "cnic_invalid", raw, None, 0.0)
        dup = valid_len & digits.duplicated(keep=False) & nonnull
        _append_issues(issues, df.index[dup], c, "cnic_duplicate", digits, None, 0.0)
    return changes


def _standardize_gender(df: pd.DataFrame, issues: list[InconsistencyItem]) -> int:
    changes = 0
    for c in _cols_with(df, ("gender", "sex")):
        before = df[c].copy()
        key = before.astype("string").str.lower().str.replace(r"[^a-z]+", " ", regex=True).str.strip()
        after = key.map(GENDER_MAP)
        mask = before.notna() & after.notna() & (before.astype("string") != after.astype("string"))
        changes += int(mask.sum())
        _append_issues(issues, df.index[mask], c, "gender_standardization", before, after)
        if mask.any():
            df.loc[mask, c] = after[mask].astype(object)
        invalid = before.notna() & after.isna() & ~_null_mask(before)
        _append_issues(issues, df.index[invalid], c, "gender_invalid", before, None, 0.0)
    return changes


def _standardize_bool(df: pd.DataFrame, issues: list[InconsistencyItem]) -> int:
    changes = 0
    for c in df.columns:
        n = _norm_col(c)
        if not any(h in n for h in BOOL_HINTS):
            continue
        if any(x in n for x in ("count", "amount", "number", "no_of", "total")):
            continue
        before = df[c].copy()
        key = before.astype("string").str.lower().str.strip()
        after = pd.Series(pd.NA, index=df.index, dtype="string")
        after[key.isin(YES_VALUES)] = "Yes"
        after[key.isin(NO_VALUES)] = "No"
        mask = before.notna() & after.notna() & (before.astype("string") != after)
        changes += int(mask.sum())
        _append_issues(issues, df.index[mask], c, "bool_standardization", before, after)
        if mask.any():
            df.loc[mask, c] = after[mask].astype(object)
    return changes


def _build_exact_map(canonical_values: list[str]) -> dict[str, str]:
    return {str(v).strip().lower(): str(v).strip() for v in canonical_values if str(v).strip()}


@lru_cache(maxsize=256)
def _cached_fuzzy(value: str, choices_key: tuple[str, ...], threshold: int) -> str | None:
    if not value:
        return None
    result = process.extractOne(value, choices_key, scorer=fuzz.WRatio)
    if result and result[1] >= threshold:
        return result[0]
    return None


def _standardize_from_canon(df: pd.DataFrame, col: str, canonical: list[str], issues: list[InconsistencyItem], issue_type: str) -> int:
    if not canonical:
        return 0
    before = df[col].copy()
    exact = _build_exact_map(canonical)
    choices = tuple(sorted(set(exact.values())))
    unique = before.dropna().astype(str).str.strip().unique().tolist()
    mapping = {}
    for val in unique:
        key = val.lower()
        fixed = exact.get(key)
        if fixed is None:
            fixed = _cached_fuzzy(key, choices, int(FUZZY_EXACT_THRESHOLD))
        if fixed and fixed != val:
            mapping[val] = fixed
    if not mapping:
        return 0
    stripped = before.astype("string").str.strip()
    after = stripped.map(mapping)
    mask = after.notna()
    _append_issues(issues, df.index[mask], col, issue_type, before, after)
    df.loc[mask, col] = after[mask].astype(object)
    return int(mask.sum())


def _standardize_banks(df: pd.DataFrame, issues: list[InconsistencyItem]) -> int:
    canonical = list(BANK_NAMES.keys()) if isinstance(BANK_NAMES, dict) else list(BANK_NAMES or [])
    # include aliases as exact map via canonical map
    changes = 0
    for c in df.columns:
        if not any(h in _norm_col(c) for h in BANK_HINTS):
            continue
        before = df[c].copy()
        stripped = before.astype("string").str.strip()
        unique = stripped.dropna().unique().tolist()
        mapping = {}
        alias_norm = {re.sub(r"[^a-z0-9]+", " ", k.lower()).strip(): v for k, v in BANK_ALIAS_MAP.items()}
        choices = tuple(sorted(canonical))
        for val in unique:
            low = str(val).strip().lower()
            norm = re.sub(r"[^a-z0-9]+", " ", low).strip()
            short_norm = re.sub(r"\b(ltd|limited|bank)\b", "", norm).strip()
            fixed = BANK_ALIAS_MAP.get(low) or alias_norm.get(norm) or alias_norm.get(short_norm)
            if not fixed and canonical:
                fixed = _cached_fuzzy(norm, choices, 88)
            if fixed and fixed != val:
                mapping[str(val)] = fixed
        after = stripped.map(mapping)
        mask = after.notna()
        changes += int(mask.sum())
        _append_issues(issues, df.index[mask], c, "bank_standardization", before, after)
        if mask.any():
            df.loc[mask, c] = after[mask].astype(object)
    return changes


def _standardize_geo(df: pd.DataFrame, issues: list[InconsistencyItem]) -> int:
    changes = 0
    for c in df.columns:
        n = _norm_col(c)
        if not any(h == n or h in n for h in GEO_HINTS):
            continue
        # try best matching configured canonical list based on column name
        canon = []
        for key, vals in (GEO_COLUMNS or {}).items():
            if _norm_col(key) in n or n in _norm_col(key):
                canon = list(vals)
                break
        if canon:
            changes += _standardize_from_canon(df, c, canon, issues, "geo_standardization")
        else:
            # no configured list: at least normalize spacing/title case for geo text
            before = df[c].copy()
            after = before.astype("string").str.replace(r"[^A-Za-z0-9\s]+", " ", regex=True).str.replace(r"\s+", " ", regex=True).str.strip().str.title()
            mask = before.notna() & (before.astype("string") != after)
            changes += int(mask.sum())
            _append_issues(issues, df.index[mask], c, "geo_standardization", before, after)
            if mask.any():
                df.loc[mask, c] = after[mask].astype(object)
    return changes


_DATE_SIGNAL_RE = re.compile(
    r"("
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"      # 2026-05-29 / 2026/05/29
    r"|\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b"  # 05-29-2026 / 5/29/26
    r"|\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}\b"
    r"|\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}\b"
    r")"
)
_TIME_ONLY_RE = re.compile(r"^\s*\d{1,2}:\d{2}(:\d{2})?\s*([AaPp][Mm])?\s*$")


def _detect_date_columns(df: pd.DataFrame, sample_size: int = 2000) -> list[str]:
    """Detect date-like columns from values, not only from names.

    This intentionally rejects pure time columns such as `14:27` even if the
    column is called CREATED_AT/UPDATED_AT. Datetime strings are accepted and
    normalized to the requested output date format.
    """
    cols: list[str] = []
    for c in df.columns:
        n = _norm_col(c)
        if any(x in n for x in ("uuid", "cnic", "phone", "mobile", "account", "amount", "count", "number", "ip")):
            continue
        sample = df[c].dropna().astype(str).str.strip()
        sample = sample[~sample.str.lower().isin(NULL_TOKENS)].head(sample_size)
        if sample.empty:
            continue

        time_only_rate = sample.str.match(_TIME_ONLY_RE, na=False).mean()
        date_signal_rate = sample.str.contains(_DATE_SIGNAL_RE, regex=True, na=False).mean()
        if time_only_rate >= 0.80 and date_signal_rate < 0.20:
            continue

        parsed = pd.to_datetime(sample, errors="coerce", dayfirst=False, format="mixed")
        parse_rate = parsed.notna().mean()
        name_hint = any(h in n for h in ("date", "dob", "created", "updated")) or n.endswith("_at")

        if date_signal_rate >= 0.60 and parse_rate >= 0.70:
            cols.append(c)
        elif name_hint and date_signal_rate >= 0.25 and parse_rate >= 0.60:
            cols.append(c)
    return cols


def _standardize_dates(df: pd.DataFrame, issues: list[InconsistencyItem]) -> tuple[int, list[dict]]:
    changes, failures = 0, []
    for c in _detect_date_columns(df):
        before = df[c].copy()
        parsed = pd.to_datetime(before, errors="coerce", dayfirst=False, format="mixed")
        valid = before.notna() & parsed.notna()
        formatted = parsed.dt.strftime(AUTO_DATE_FORMAT)
        mask = valid & (before.astype("string") != formatted.astype("string"))
        changes += int(mask.sum())
        _append_issues(issues, df.index[mask], c, "date_standardization", before, formatted)
        if mask.any():
            df.loc[mask, c] = formatted[mask].astype(object)
        bad = before.notna() & parsed.isna() & ~_null_mask(before)
        if bad.any():
            _append_issues(issues, df.index[bad], c, "date_invalid", before, None, 0.0)
            failures.extend({"row": int(i) + 2, "column": c, "value": _to_display_value(before.loc[i], c)} for i in df.index[bad])
    return changes, failures


def _flag_duplicates(df: pd.DataFrame, issues: list[InconsistencyItem]) -> int:
    count = 0
    dup_rows = df.duplicated(keep=False)
    if dup_rows.any():
        rows = df.index[dup_rows]
        _append_issues(issues, rows, "ROW", "duplicate_record", "Duplicate row", None, 0.0)
        count += int(dup_rows.sum())
    for c in _cols_with(df, UUID_HINTS):
        s = df[c].astype("string").str.strip()
        mask = s.notna() & ~s.str.lower().isin(NULL_TOKENS) & s.duplicated(keep=False)
        if mask.any():
            _append_issues(issues, df.index[mask], c, "uuid_duplicate", df[c], None, 0.0)
            count += int(mask.sum())
    return count


def _find_repeating_digit_cells(df: pd.DataFrame) -> list[tuple[int, str]]:
    flags: list[tuple[int, str]] = []
    for c in _cols_with(df, CNIC_HINTS + UUID_HINTS + ("account", "phone", "mobile")):
        s = df[c].astype("string")
        mask = s.str.replace(r"\D", "", regex=True).str.contains(r"(\d)\1{6,}", regex=True, na=False)
        flags.extend((int(i), str(c)) for i in df.index[mask][:MAX_FLAGGED_CELLS])
    return flags[:MAX_FLAGGED_CELLS]


def auto_clean(file_id: str, df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    df = df.copy()
    issues: list[InconsistencyItem] = []
    steps = []

    def log(step, changes, detail):
        steps.append({"step": step, "changes": int(changes), "detail": detail})

    log("trim_whitespace", _trim(df, issues), "Vectorized trim on all text cells")
    log("null_standardization", _standardize_nulls(df, issues), "Null-like values standardized to None")
    log("text_cleaning", _clean_text_columns(df, issues), "Text columns capitalized and special characters removed")
    log("cnic_validation", _standardize_cnics(df, issues), "CNIC columns normalized to 13 digits; invalid/fake/duplicate CNICs flagged")
    log("uuid_duplicate_check", _flag_duplicates(df, issues), "Duplicate rows and duplicate UUID-like columns flagged")
    log("bank_standardization", _standardize_banks(df, issues), "Bank/payment columns standardized using cached unique-value matching")
    log("geo_standardization", _standardize_geo(df, issues), "District/tehsil/UC/deh columns standardized dynamically")
    n_dates, failed_dates = _standardize_dates(df, issues)
    log("date_standardization", n_dates, f"Date columns standardized; {len(failed_dates)} invalid date samples flagged")
    log("bool_standardization", _standardize_bool(df, issues), "Boolean-like columns standardized to Yes/No")
    log("gender_standardization", _standardize_gender(df, issues), "Gender columns standardized to Male/Female/Transgender")

    repeating = _find_repeating_digit_cells(df)
    log("repeating_digit_flags", len(repeating), "Repeating digit patterns flagged")

    quarantined_rows = {i.row for i in issues if i.issue_type in {"missing", "cnic_invalid", "cnic_duplicate", "uuid_duplicate", "duplicate_record"}}
    auto_corrected = sum(1 for i in issues if i.suggested_fix is not None)
    flagged_review = max(0, len(issues) - auto_corrected)
    report = CleaningReport(
        file_id=file_id, total_rows=len(df), total_issues=len(issues), auto_corrected=auto_corrected,
        flagged_review=flagged_review, quarantined_rows=len(quarantined_rows), issues=issues,
    )
    return df, {
        "file_id": file_id,
        "steps": steps,
        "total_changes": sum(s["changes"] for s in steps),
        "repeating_digit_cells": repeating,
        "cleaning_report": report,
    }


def clean_dataframe(file_id: str, df: pd.DataFrame) -> tuple[pd.DataFrame, CleaningReport]:
    cleaned, summary = auto_clean(file_id, df)
    return cleaned, summary["cleaning_report"]


def trim_whitespace(df: pd.DataFrame, columns: list[str] | None = None) -> pd.DataFrame:
    df = df.copy()
    cols = columns or _string_cols(df)
    for c in cols:
        rc = resolve_column_name(df, c) or c
        if rc in df.columns:
            df[rc] = df[rc].astype("string").str.strip().astype(object)
    return df


def standardize_dates(df: pd.DataFrame, columns: list[str], fmt: str):
    df = df.copy(); failures = []
    for c in columns:
        rc = resolve_column_name(df, c) or c
        if rc not in df.columns:
            continue
        parsed = pd.to_datetime(df[rc], errors="coerce", dayfirst=False, format="mixed")
        good = df[rc].notna() & parsed.notna()
        df.loc[good, rc] = parsed[good].dt.strftime(fmt)
        bad = df[rc].notna() & parsed.isna()
        failures.extend({"row": int(i) + 2, "column": rc, "value": _to_display_value(df.loc[i, rc], rc)} for i in df.index[bad])
    return df, failures


def standardize_values(df: pd.DataFrame, column: str, mapping: dict):
    df = df.copy(); rc = resolve_column_name(df, column) or column
    if rc not in df.columns:
        return df, 0
    before = df[rc].copy()
    df[rc] = df[rc].map(lambda x: mapping.get(str(x), mapping.get(str(x).strip(), x)) if pd.notna(x) else x)
    return df, int((before.astype(str) != df[rc].astype(str)).sum())


def get_column_unique_values(df: pd.DataFrame, column: str) -> list[str]:
    rc = resolve_column_name(df, column) or column
    if rc not in df.columns:
        return []
    vals = df[rc].dropna().astype(str).str.strip().unique().tolist()
    return sorted(v for v in vals if v.lower() not in NULL_TOKENS)[:5000]


def _detect_special_characters(df: pd.DataFrame) -> dict[tuple[int, str], str]:
    out = {}
    for c in _string_cols(df):
        if any(h in _norm_col(c) for h in TEXT_EXCLUDE_HINTS):
            continue
        mask = df[c].astype("string").str.contains(r"[^A-Za-z0-9\s]", regex=True, na=False)
        for i in df.index[mask][:MAX_FLAGGED_CELLS - len(out)]:
            out[(int(i), str(c))] = "special_character"
        if len(out) >= MAX_FLAGGED_CELLS:
            break
    return out


def _detect_name_issues(df: pd.DataFrame) -> list[tuple[int, str]]:
    cols = [c for c in df.columns if "name" in _norm_col(c)]
    out=[]
    for c in cols:
        mask = df[c].astype("string").str.contains(r"[^A-Za-z\s]", regex=True, na=False)
        out.extend((int(i), str(c)) for i in df.index[mask][:MAX_FLAGGED_CELLS-len(out)])
    return out


def _detect_cnic_issues(df: pd.DataFrame) -> list[tuple[int, str]]:
    out=[]
    for c in _cols_with(df, CNIC_HINTS):
        digits = df[c].map(_identifier_digits).astype("string")
        mask = df[c].notna() & (digits.str.len().ne(13) | digits.isin(CNIC_FAKE_VALUES) | digits.str.fullmatch(r"(\d)\1{12}").fillna(False) | digits.duplicated(keep=False))
        out.extend((int(i), str(c)) for i in df.index[mask][:MAX_FLAGGED_CELLS-len(out)])
    return out


def _detect_gender_issues(df: pd.DataFrame) -> list[tuple[int, str]]:
    out=[]
    for c in _cols_with(df, ("gender", "sex")):
        key = df[c].astype("string").str.lower().str.replace(r"[^a-z]+", " ", regex=True).str.strip()
        mask = df[c].notna() & ~key.isin(set(GENDER_MAP.keys()) | {"male", "female", "transgender"})
        out.extend((int(i), str(c)) for i in df.index[mask][:MAX_FLAGGED_CELLS-len(out)])
    return out


def _detect_duplicate_rows(df: pd.DataFrame) -> list[int]:
    return [int(i) for i in df.index[df.duplicated(keep=False)][:MAX_FLAGGED_CELLS]]


def get_full_dataset(
    df: pd.DataFrame,
    uuid_column: str | None = None,
    extra_flags: dict[tuple[int, str], list[str]] | None = None,
    page: int = 1,
    page_size: int = 100,
    flag_filter: str = "all",
) -> dict:
    # UI-safe: compute flag indexes once, then return only one page of rows.
    cols = [str(c) for c in df.columns]
    special = _detect_special_characters(df)
    cnic = set(_detect_cnic_issues(df)); gender = set(_detect_gender_issues(df)); dup_rows = set(_detect_duplicate_rows(df)); repeat = set(_find_repeating_digit_cells(df))
    uuid_dupes = set()
    target_uuid_cols = [uuid_column] if uuid_column else _cols_with(df, UUID_HINTS)
    for c in target_uuid_cols:
        rc = resolve_column_name(df, c) or c
        if rc in df.columns:
            s = df[rc].astype("string").str.strip()
            mask = s.notna() & s.duplicated(keep=False)
            uuid_dupes.update((int(i), str(rc)) for i in df.index[mask][:MAX_FLAGGED_CELLS])
    extra_flags = extra_flags or {}

    null_cells = set()
    null_mask = df.isna()
    for c in df.columns:
        null_cells.update((int(i), str(c)) for i in df.index[null_mask[c]])

    flag_cells = {
        "special": set(special.keys()),
        "cnic": set(cnic),
        "gender": set(gender),
        "uuid": set(uuid_dupes),
        "null": null_cells,
        "dupe": {(int(i), str(c)) for i in dup_rows for c in df.columns},
        "repeat": set(repeat),
        "resolved": {key for key, flags in extra_flags.items() if "resolved_change" in flags},
    }
    flag_counts = {key: len(value) for key, value in flag_cells.items()}

    if flag_filter != "all" and flag_filter in flag_cells:
        row_indexes = sorted({idx for idx, _ in flag_cells[flag_filter]})
    else:
        row_indexes = [int(i) for i in df.index]

    total_rows = len(row_indexes)
    page = max(1, int(page or 1))
    if page_size is None or int(page_size) <= 0:
        page_size = max(1, total_rows)
        total_pages = 1
        page = 1
    else:
        page_size = min(500, max(25, int(page_size or 100)))
        total_pages = max(1, (total_rows + page_size - 1) // page_size)
        page = min(page, total_pages)
    page_indexes = row_indexes[(page - 1) * page_size: page * page_size]

    rows = []
    row_numbers = []
    for i, row in df.loc[page_indexes].iterrows():
        row_numbers.append(int(i) + 2)
        outrow=[]
        for c in df.columns:
            flags=[]
            if (int(i), str(c)) in special: flags.append("special_character")
            if (int(i), str(c)) in cnic: flags.append("cnic_error")
            if (int(i), str(c)) in gender: flags.append("gender_error")
            if (int(i), str(c)) in repeat: flags.append("repeating_digit")
            if (int(i), str(c)) in uuid_dupes: flags.append("uuid_duplicate")
            if int(i) in dup_rows: flags.append("duplicate_row")
            flags.extend(extra_flags.get((int(i), str(c)), []))
            val = row[c]
            if pd.isna(val): flags.append("null_value")
            outrow.append({"value": _to_display_value(val, str(c)), "flags": flags})
        rows.append(outrow)
    return {
        "columns": cols,
        "rows": rows,
        "row_numbers": row_numbers,
        "row_count": len(rows),
        "total_rows": total_rows,
        "all_rows": len(df),
        "column_count": len(cols),
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "flag_filter": flag_filter,
        "flag_counts": flag_counts,
    }

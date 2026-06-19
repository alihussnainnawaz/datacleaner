"""
cleaning_engine.py  –  Hardcoded schema-aware cleaning pipeline.

Performance principles
──────────────────────
• Every transform is vectorised. No per-row Python loops.
• Column string cache: each column cast to StringDtype ONCE per pipeline run.
• Null mask cache: computed ONCE per column, reused across steps.
• Fuzzy matching on UNIQUE values only (lru_cache keyed on value+choices tuple).
• _add_clean / _add_review use .reindex() — one array read, not N .loc[] calls.
• Response builder: NumPy-level to_dict + pre-vectorised _json_scalar on arrays.
• to_dict(orient="records") replaced with column-wise extraction to avoid 4s overhead.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

try:
    from config import (
        BANK_ALIAS_MAP, BANK_NAMES,
        GEO_COLUMNS, AUTO_DATE_FORMAT,
        FUZZY_EXACT_THRESHOLD,
    )
except Exception:
    BANK_ALIAS_MAP, BANK_NAMES, GEO_COLUMNS = {}, {}, {}
    AUTO_DATE_FORMAT      = "%m/%d/%Y"
    FUZZY_EXACT_THRESHOLD = 95

# Force numpy-backed StringDtype — prevents pyarrow from hijacking string ops
import pandas as pd
try:
    pd.options.future.infer_string = False
except Exception:
    pass
try:
    pd.options.mode.string_storage = "python"
except Exception:
    pass


# ── Constants ─────────────────────────────────────────────────────────────────

NULL_TOKENS = {"", "nan", "none", "null", "n/a", "na", "nil", "-", "--", "#n/a"}

CNIC_FAKE_VALUES = {
    "0000000000000", "1111111111111", "2222222222222", "3333333333333",
    "4444444444444", "5555555555555", "6666666666666", "7777777777777",
    "8888888888888", "9999999999999", "1234567890123", "4330190000000",
}

YES_VALUES = {"y", "yes", "true", "t", "1", "active", "available", "present", "ok"}
NO_VALUES  = {"n", "no", "false", "f", "0", "inactive", "not available", "absent", "none"}

GENDER_MAP = {
    "m": "Male",   "male": "Male",   "man": "Male",   "masculine": "Male",
    "f": "Female", "female": "Female", "woman": "Female", "fem": "Female",
    "t": "Transgender", "trans": "Transgender",
    "transgender": "Transgender", "third gender": "Transgender",
}

# ── Hardcoded column schema ───────────────────────────────────────────────────
_SCHEMA: dict[str, dict] = {
    "DA_UUID":  {"type": "numeric",  "non_null": True, "unique": True},
    "DA_CNIC":  {"type": "cnic",     "non_null": True, "unique": True},
    "Cell No":  {"type": "cell_no"},
    "District": {"type": "geo",    "title": True, "canonical": None},
    "Tehsil":   {"type": "geo",    "title": True, "canonical": None},
    "JH UC":    {"type": "geo",    "title": True, "canonical": None},
    "JH Deh":   {"type": "geo",    "title": True, "canonical": None},
    "EY Village": {"type": "string", "title": True},
    "DA Occupant Name": {"type": "string", "title": True},
    "DA Father Name":   {"type": "string", "title": True},
    "DA Spouse Name":   {"type": "string", "title": True},
    "IP Name":          {"type": "string", "upper": True},
    "Address":          {"type": "string", "title": True, "special_chars_ok": True},
    "Payment By - IFIs": {"type": "string", "upper": True},
    "DA Type":            {"type": "string", "title": True, "canonical": ["Kacha", "Pucca", "Hybrid"]},
    "DA_Damage Category": {"type": "string", "title": True, "canonical": ["Collapsed", "WashedAway", "Visible", "Intact"]},
    "Eng Status":         {"type": "string", "title": True, "canonical": ["Approved", "Rejected", "Pending", "Purged"]},
    "Status of Land":     {"type": "string", "title": True, "canonical": [
        "State Land", "Self / Owned Private Land",
        "Village Land / Community", "Government Department Land",
    ]},
    "Occupany Agreement": {"type": "string", "title": True, "canonical": []},
    "Block List":         {"type": "string", "title": True, "special_chars_ok": True, "canonical": []},
    "Marital Status":     {"type": "string", "title": True, "canonical": ["Married", "Widow", "Single", "Divorced"]},
    "Decision":           {"type": "string", "title": True, "canonical": ["Disbursed", "Under Review", "Cleared Case"]},
    "Main Source of hh Income": {"type": "string", "title": True, "canonical": [
        "Farmer", "Laborer Un-Skilled", "Laborer Skilled",
        "No Income", "Un-Employed", "Retired",
        "Shop Keeping", "Home Maker", "Transportation",
        "Animal Husbandry", "Pottery",
    ]},
    "Gender": {"type": "gender"},
    "Constituency No": {"type": "string", "upper": True},
    "Constituency":    {"type": "string", "upper": True},
    "Winning Party":   {"type": "string", "upper": True},
    "Long": {"type": "float", "lat_lon": "lon"},
    "Lat":  {"type": "float", "lat_lon": "lat"},
    "is_hazardous_location":    {"type": "bool"},
    "is_located_in_flood_plain": {"type": "bool"},
    "Disbursement Status":      {"type": "bool"},
    "Widow":                    {"type": "bool"},
    "Women with disable husband": {"type": "bool"},
    "Women with households Divorced / abandoned women\\n / unmarried older women dependent on others": {"type": "bool"},
    "Unaccompained elders":     {"type": "bool"},
    "Unaccompained minors i.e. orphans": {"type": "bool"},
    "Purged":                   {"type": "bool"},
    "Adult Female Count":         {"type": "numeric"},
    "Adult Male Count":           {"type": "numeric"},
    "Disable Adult Female Count": {"type": "numeric"},
    "Disable Adult Male Count":   {"type": "numeric"},
    "Disable Child Female Count": {"type": "numeric"},
    "Disable Child Male Count":   {"type": "numeric"},
    "Child Female Count":         {"type": "numeric"},
    "Child Male Count":           {"type": "numeric"},
    "Monthly Income":             {"type": "numeric"},
}

# ── Compiled regexes (module-level) ──────────────────────────────────────────
_RE_WHITESPACE    = re.compile(r"\s+")
_RE_SPECIAL_SAFE  = re.compile(r"[^A-Za-z0-9\s\-.,#/&'()]")
_RE_SPECIAL_STRIP = re.compile(r"[^A-Za-z0-9\s\-.]")
_RE_NON_DIGIT     = re.compile(r"\D")
_RE_REPEAT_DIGIT  = re.compile(r"([0-9])\1{6,}")
_DATE_SIGNAL_RE   = re.compile(
    r"(?:\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"
    r"|\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b"
    r"|\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}\b"
    r"|\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}\b)"
)
_TIME_ONLY_RE = re.compile(r"^\s*\d{1,2}:\d{2}(:\d{2})?\s*([AaPp][Mm])?\s*$")

_LAT_RANGE   = (20.0, 40.0)
_LON_RANGE   = (60.0, 80.0)
_CELL_LEN    = 10
_CELL_PREFIX = "3"
_FUZZY_AUTO  = 88
_FUZZY_REVIEW = 70


# ── Column-level cache (built once per pipeline run) ──────────────────────────

class _ColCache:
    """Holds per-column string Series and null mask, computed once."""
    __slots__ = ("_s", "_null")

    def __init__(self) -> None:
        self._s:    dict[str, pd.Series] = {}
        self._null: dict[str, pd.Series] = {}

    def s(self, col: str, series: pd.Series) -> pd.Series:
        if col not in self._s:
            self._s[col] = series.astype("string")
        return self._s[col]

    def null(self, col: str, series: pd.Series) -> pd.Series:
        if col not in self._null:
            sc = self.s(col, series)
            self._null[col] = series.isna() | sc.str.strip().str.lower().isin(NULL_TOKENS)
        return self._null[col]

    def invalidate(self, col: str) -> None:
        """Call after a column is modified so cache is refreshed next access."""
        self._s.pop(col, None)
        self._null.pop(col, None)


# ── Scalar helpers ────────────────────────────────────────────────────────────

def _norm_col(c: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_")


# Vectorised JSON scalar — avoids calling Python function per cell in response builder
_NP_INT_TYPES   = (np.integer,)
_NP_FLOAT_TYPES = (np.floating,)

def _json_scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, _NP_INT_TYPES):
        return int(value)
    if isinstance(value, _NP_FLOAT_TYPES):
        return None if np.isnan(value) else float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


def _series_to_json_list(s: pd.Series) -> list:
    """Convert a Series to a list of JSON-safe scalars without per-cell overhead."""
    arr = s.to_numpy(dtype=object, na_value=None)
    out = [None] * len(arr)
    for i, v in enumerate(arr):
        if v is None:
            continue
        if isinstance(v, _NP_INT_TYPES):
            out[i] = int(v)
        elif isinstance(v, _NP_FLOAT_TYPES):
            out[i] = None if np.isnan(v) else float(v)
        elif isinstance(v, pd.Timestamp):
            out[i] = v.isoformat()
        elif isinstance(v, float) and np.isnan(v):
            out[i] = None
        else:
            out[i] = str(v) if v is not None else None
    return out


# ── Change / review recorders ─────────────────────────────────────────────────

def _merge_step(existing: Any, step: str) -> str:
    parts: list[str] = []
    if existing:
        raw = existing if isinstance(existing, list) else re.split(r"\s*[|,]\s*", str(existing))
        for item in raw:
            item = str(item).strip()
            if item and item not in parts:
                parts.append(item)
    if step not in parts:
        parts.append(step)
    return " | ".join(parts)


def _add_clean(changes: dict, idxs, col: str, new_values: Any, step: str) -> None:
    col = str(col)
    if isinstance(new_values, pd.Series):
        vals  = new_values.reindex(idxs)
        items = [(int(i), _json_scalar(v)) for i, v in vals.items()]
    else:
        nv    = _json_scalar(new_values)
        items = [(int(i), nv) for i in idxs]
    for i, nv in items:
        row = changes.setdefault(i, {})
        if col in row:
            row[col] = [nv, _merge_step(row[col][1], step)]
        else:
            row[col] = [nv, step]


def _add_review(reviews: dict, idxs, col: str, values: Any) -> None:
    col = str(col)
    if isinstance(values, pd.Series):
        vals = values.reindex(idxs)
        for i, v in vals.items():
            reviews.setdefault(int(i), {})[col] = _json_scalar(v)
    else:
        v = _json_scalar(values)
        for i in idxs:
            reviews.setdefault(int(i), {})[col] = v


# ── Fuzzy (unique-value LRU cache) ────────────────────────────────────────────

@lru_cache(maxsize=8192)
def _fuzzy_best(value: str, choices: tuple[str, ...]) -> tuple[str, int] | None:
    if not value or not choices:
        return None
    result = process.extractOne(value, choices, scorer=fuzz.WRatio)
    return (result[0], int(result[1])) if result else None


def _build_fuzzy_map(
    series: pd.Series, canonical: list[str],
) -> tuple[dict[str, str], dict[str, str]]:
    exact_lower = {str(v).lower(): str(v) for v in canonical}
    choices     = tuple(sorted(exact_lower.values()))
    auto_map: dict[str, str]   = {}
    review_map: dict[str, str] = {}
    for raw in series.dropna().unique().tolist():
        raw_str = str(raw).strip()
        raw_low = raw_str.lower()
        if raw_low in exact_lower:
            target = exact_lower[raw_low]
            if target != raw_str:
                auto_map[raw_str] = target
            continue
        result = _fuzzy_best(raw_low, choices)
        if result:
            matched, score = result
            if score >= _FUZZY_AUTO:
                auto_map[raw_str] = matched
            elif score >= _FUZZY_REVIEW:
                review_map[raw_str] = matched
    return auto_map, review_map


def _apply_fuzzy(
    cleaned: pd.DataFrame, col: str,
    canonical: list[str],
    changes: dict, reviews: dict,
    step: str, cc: _ColCache,
) -> None:
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    if not nonnull.any():
        return
    s = cc.s(col, before).str.strip()
    auto_map, review_map = _build_fuzzy_map(s[nonnull], canonical)
    if auto_map:
        after = s.map(auto_map)
        mask  = after.notna() & nonnull
        if mask.any():
            idxs = cleaned.index[mask]
            cleaned.loc[idxs, col] = after.loc[idxs].astype(object)
            _add_clean(changes, idxs, col, after, step)
            cc.invalidate(col)
    if review_map:
        review_mask = s.isin(review_map) & nonnull
        if review_mask.any():
            _add_review(reviews, cleaned.index[review_mask], col, before)


# ── Cleaning steps ────────────────────────────────────────────────────────────

def _step_trim(cleaned: pd.DataFrame, changes: dict, cc: _ColCache) -> None:
    """STEP 1 – Trim & collapse whitespace across the ENTIRE dataset."""
    for c in cleaned.columns:
        if cleaned[c].dtype != object and not pd.api.types.is_string_dtype(cleaned[c]):
            continue
        before = cleaned[c].copy()
        s      = cc.s(c, before)
        after  = s.str.replace(_RE_WHITESPACE, " ", regex=True).str.strip()
        mask   = before.notna() & (s != after).fillna(False)
        if mask.any():
            idxs = cleaned.index[mask]
            cleaned.loc[idxs, c] = after.loc[idxs].astype(object)
            _add_clean(changes, idxs, c, after, "TRIM")
            cc.invalidate(c)


def _step_null_standardize(cleaned: pd.DataFrame, changes: dict, reviews: dict, cc: _ColCache) -> None:
    """STEP 2 – Null tokens → real NULL."""
    for c in cleaned.columns:
        before = cleaned[c].copy()
        mask   = cc.null(c, before)
        if not mask.any():
            continue
        idxs    = cleaned.index[mask]
        changed = idxs[before.loc[idxs].notna()]
        if len(changed):
            _add_clean(changes, changed, c, None, "NULL_STANDARDIZED")
        _add_review(reviews, idxs, c, before)
        cleaned.loc[idxs, c] = None
        cc.invalidate(c)


def _step_special_chars(
    cleaned: pd.DataFrame, changes: dict,
    col: str, safe: bool, cc: _ColCache,
) -> None:
    """STEP 3a – Strip illegal special characters."""
    pat     = _RE_SPECIAL_SAFE if safe else _RE_SPECIAL_STRIP
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    s       = cc.s(col, before)
    after   = (
        s.str.replace(pat, " ", regex=True)
         .str.replace(_RE_WHITESPACE, " ", regex=True)
         .str.strip()
    )
    mask = nonnull & (s != after).fillna(False)
    if mask.any():
        idxs = cleaned.index[mask]
        cleaned.loc[idxs, col] = after.loc[idxs].astype(object)
        _add_clean(changes, idxs, col, after, "SPECIAL_CHARS_CLEANED")
        cc.invalidate(col)


def _step_casing(
    cleaned: pd.DataFrame, changes: dict,
    col: str, profile: dict, cc: _ColCache,
) -> None:
    """STEP 3b – Title Case or UPPER."""
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    s       = cc.s(col, before)
    if profile.get("upper"):
        after = s.str.upper()
        step  = "UPPER_CASED"
    elif profile.get("title"):
        after = s.str.title()
        step  = "TITLE_CASED"
    else:
        return
    mask = nonnull & (s != after).fillna(False)
    if mask.any():
        idxs = cleaned.index[mask]
        cleaned.loc[idxs, col] = after.loc[idxs].astype(object)
        _add_clean(changes, idxs, col, after, step)
        cc.invalidate(col)


def _step_bool(cleaned: pd.DataFrame, changes: dict, reviews: dict, col: str, cc: _ColCache) -> None:
    """STEP 4 – Standardise Yes / No."""
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    key     = cc.s(col, before).str.lower().str.strip()
    after   = pd.Series(pd.NA, index=cleaned.index, dtype="string")
    after[key.isin(YES_VALUES)] = "Yes"
    after[key.isin(NO_VALUES)]  = "No"
    auto = nonnull & after.notna() & (cc.s(col, before) != after).fillna(False)
    if auto.any():
        idxs = cleaned.index[auto]
        cleaned.loc[idxs, col] = after.loc[idxs].astype(object)
        _add_clean(changes, idxs, col, after, "BOOL_STANDARDIZED")
        cc.invalidate(col)
    invalid = nonnull & after.isna()
    if invalid.any():
        _add_review(reviews, cleaned.index[invalid], col, before)


def _step_gender(cleaned: pd.DataFrame, changes: dict, reviews: dict, col: str, cc: _ColCache) -> None:
    """STEP 5 – Standardise Gender."""
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    key     = cc.s(col, before).str.lower().str.replace(r"[^a-z\s]+", " ", regex=True).str.strip()
    after   = key.map(GENDER_MAP)
    auto    = nonnull & after.notna() & (cc.s(col, before) != after.astype("string")).fillna(False)
    if auto.any():
        idxs = cleaned.index[auto]
        cleaned.loc[idxs, col] = after.loc[idxs].astype(object)
        _add_clean(changes, idxs, col, after, "GENDER_STANDARDIZED")
        cc.invalidate(col)
    invalid = nonnull & after.isna()
    if invalid.any():
        _add_review(reviews, cleaned.index[invalid], col, before)


def _step_cnic(
    cleaned: pd.DataFrame, changes: dict, reviews: dict,
    col: str, profile: dict, cc: _ColCache,
) -> pd.Series:
    """STEP 6 – CNIC: 13 digits, non-null, unique, no fakes. Returns duplicate mask."""
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    if profile.get("non_null"):
        _add_review(reviews, cleaned.index[~nonnull], col, before)

    digits = cc.s(col, before).str.replace(_RE_NON_DIGIT, "", regex=True).str.strip()

    # handle scientific notation stored as string
    sci = before.astype("string").str.match(r"[+-]?\d+\.?\d*[eE][+-]?\d+", na=False)
    if sci.any():
        def _sci_to_str(v: Any) -> str:
            try:
                return str(int(float(str(v))))
            except Exception:
                return ""
        digits[sci] = before[sci].map(_sci_to_str)

    valid_len = digits.str.len() == 13
    fmt_mask  = nonnull & valid_len & (cc.s(col, before).str.strip() != digits).fillna(False)
    if fmt_mask.any():
        idxs = cleaned.index[fmt_mask]
        cleaned.loc[idxs, col] = digits.loc[idxs].astype(object)
        _add_clean(changes, idxs, col, digits, "CNIC_FORMAT")
        cc.invalidate(col)

    repeat = digits.str.fullmatch(r"(\d)\1{12}").fillna(False)
    invalid = nonnull & (~valid_len | digits.isin(CNIC_FAKE_VALUES) | repeat)
    if invalid.any():
        _add_review(reviews, cleaned.index[invalid], col, before)

    duplicate_cnic_mask = pd.Series(False, index=cleaned.index)
    if profile.get("unique"):
        dup = nonnull & valid_len & digits.duplicated(keep=False)
        if dup.any():
            _add_review(reviews, cleaned.index[dup], col, before)
            duplicate_cnic_mask = dup
    return duplicate_cnic_mask


def _step_uuid(
    cleaned: pd.DataFrame, changes: dict, reviews: dict,
    col: str, profile: dict, cc: _ColCache,
) -> pd.Series:
    """STEP 7 – UUID / numeric ID: non-null, unique, numeric-type check."""
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    if profile.get("non_null"):
        _add_review(reviews, cleaned.index[~nonnull], col, before)
    duplicate_mask = pd.Series(False, index=cleaned.index)
    if profile.get("unique"):
        s   = cc.s(col, before).str.strip()
        dup = nonnull & s.duplicated(keep=False)
        if dup.any():
            _add_review(reviews, cleaned.index[dup], col, before)
            duplicate_mask = dup
    bad_type = nonnull & ~cc.s(col, before).str.strip().str.fullmatch(r"\d+(\.\d+)?", na=False)
    if bad_type.any():
        _add_review(reviews, cleaned.index[bad_type], col, before)
    return duplicate_mask


def _step_cell_no(
    cleaned: pd.DataFrame, changes: dict, reviews: dict,
    col: str, cc: _ColCache,
) -> None:
    """STEP 8 – Cell No → 03XXXXXXXXX (11 chars: leading 0 + 10 digits starting with 3)."""
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    s       = cc.s(col, before).str.strip()
    # strip country prefix / spaces / dashes → bare 10-digit number starting with 3
    digits  = (
        s.str.replace(r"^\+92", "", regex=True)
         .str.replace(r"^0092", "", regex=True)
         .str.replace(r"^92",   "", regex=True)
         .str.replace(r"^0",    "", regex=True)
         .str.replace(r"\D",    "", regex=True)
    )
    valid = nonnull & (digits.str.len() == _CELL_LEN) & digits.str.startswith(_CELL_PREFIX).fillna(False)
    # final normalised form: prepend "0" → 03XXXXXXXXX
    normalised = "0" + digits
    auto  = valid & (s != normalised).fillna(False)
    if auto.any():
        idxs = cleaned.index[auto]
        cleaned.loc[idxs, col] = normalised.loc[idxs].astype(object)
        _add_clean(changes, idxs, col, normalised, "CELL_NO_NORMALIZED")
        cc.invalidate(col)
    if (nonnull & ~valid).any():
        _add_review(reviews, cleaned.index[nonnull & ~valid], col, before)


def _step_numeric_type(cleaned: pd.DataFrame, reviews: dict, col: str, cc: _ColCache) -> None:
    """STEP 9 – Flag non-numeric values in numeric count columns."""
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    bad     = nonnull & ~cc.s(col, before).str.strip().str.fullmatch(r"[+-]?\d+(\.\d+)?", na=False)
    if bad.any():
        _add_review(reviews, cleaned.index[bad], col, before)


def _step_float_coord(
    cleaned: pd.DataFrame, reviews: dict,
    col: str, lat_lon: str, cc: _ColCache,
) -> None:
    """STEP 10 – Float format + Pakistan coordinate bounds."""
    before    = cleaned[col].copy()
    nonnull   = ~cc.null(col, before)
    s         = cc.s(col, before).str.strip()
    bad_fmt   = nonnull & ~s.str.fullmatch(r"[+-]?\d+(\.\d+)?", na=False)
    if bad_fmt.any():
        _add_review(reviews, cleaned.index[bad_fmt], col, before)
    parseable = nonnull & ~bad_fmt
    if parseable.any():
        nums = pd.to_numeric(before[parseable], errors="coerce")
        lo, hi = _LAT_RANGE if lat_lon == "lat" else _LON_RANGE
        oob    = parseable.copy()
        oob[parseable] = (nums < lo) | (nums > hi)
        if oob.any():
            _add_review(reviews, cleaned.index[oob], col, before)


def _step_geo(
    cleaned: pd.DataFrame, changes: dict, reviews: dict,
    col: str, profile: dict, cc: _ColCache,
) -> None:
    """STEP 11 – Geo: special-char clean → Title Case → fuzzy canonical."""
    _step_special_chars(cleaned, changes, col, False, cc)
    _step_casing(cleaned, changes, col, {"title": True}, cc)
    canonical = profile.get("canonical")
    if canonical is None:
        n = _norm_col(col)
        for key, vals in (GEO_COLUMNS or {}).items():
            if _norm_col(key) in n or n in _norm_col(key):
                canonical = list(vals)
                break
    if canonical:
        _apply_fuzzy(cleaned, col, canonical, changes, reviews, "GEO_STANDARDIZED", cc)


def _step_string_category(
    cleaned: pd.DataFrame, changes: dict, reviews: dict,
    col: str, profile: dict, cc: _ColCache,
) -> None:
    """STEP 12 – String/category: special-char clean → casing → fuzzy."""
    _step_special_chars(cleaned, changes, col, profile.get("special_chars_ok", False), cc)
    _step_casing(cleaned, changes, col, profile, cc)
    canonical = profile.get("canonical")
    if canonical:
        _apply_fuzzy(cleaned, col, canonical, changes, reviews, "CATEGORY_STANDARDIZED", cc)


def _step_date(cleaned: pd.DataFrame, changes: dict, reviews: dict) -> None:
    """STEP 13 – Auto-detect and standardise date columns."""
    skip_hints = ("uuid", "cnic", "phone", "mobile", "account",
                  "amount", "count", "number", "ip", "long", "lat")
    for c in cleaned.columns:
        n = _norm_col(c)
        if any(x in n for x in skip_hints):
            continue
        sample = cleaned[c].dropna().astype(str).str.strip()
        sample = sample[~sample.str.lower().isin(NULL_TOKENS)].head(2000)
        if sample.empty:
            continue
        time_only   = sample.str.match(_TIME_ONLY_RE, na=False).mean()
        date_signal = sample.str.contains(_DATE_SIGNAL_RE, regex=True, na=False).mean()
        if time_only >= 0.80 and date_signal < 0.20:
            continue
        parsed_sample = pd.to_datetime(sample, errors="coerce", dayfirst=False, format="mixed")
        parse_rate    = parsed_sample.notna().mean()
        name_hint     = any(h in n for h in ("date", "dob", "created", "updated")) or n.endswith("_at")
        if not ((date_signal >= 0.60 and parse_rate >= 0.70)
                or (name_hint and date_signal >= 0.25 and parse_rate >= 0.60)):
            continue
        before    = cleaned[c].copy()
        parsed    = pd.to_datetime(before, errors="coerce", dayfirst=False, format="mixed")
        valid     = before.notna() & parsed.notna()
        formatted = parsed.dt.strftime(AUTO_DATE_FORMAT)
        mask      = valid & (before.astype("string") != formatted.astype("string")).fillna(False)
        if mask.any():
            idxs = cleaned.index[mask]
            cleaned.loc[idxs, c] = formatted.loc[idxs].astype(object)
            _add_clean(changes, idxs, c, formatted, "DATE_STANDARDIZED")
        bad = before.notna() & parsed.isna() & ~_null_mask_plain(before)
        if bad.any():
            _add_review(reviews, cleaned.index[bad], c, before)


def _null_mask_plain(s: pd.Series) -> pd.Series:
    ss = s.astype("string")
    return s.isna() | ss.str.strip().str.lower().isin(NULL_TOKENS)


def _step_bank(cleaned: pd.DataFrame, changes: dict) -> None:
    """STEP 14 – Bank name standardisation."""
    canonical_banks = list(BANK_NAMES.keys()) if isinstance(BANK_NAMES, dict) else list(BANK_NAMES or [])
    if not canonical_banks:
        return
    alias_norm = {re.sub(r"[^a-z0-9]+", " ", str(k).lower()).strip(): v for k, v in BANK_ALIAS_MAP.items()}
    choices    = tuple(sorted(canonical_banks))
    bank_hints = ("bank", "payment_by", "ifis", "ifi", "donor", "funding")
    for c in cleaned.columns:
        if not any(h in _norm_col(c) for h in bank_hints):
            continue
        before  = cleaned[c].copy()
        s       = before.astype("string").str.strip()
        mapping: dict[str, str] = {}
        for val in s.dropna().unique().tolist():
            raw        = str(val)
            low        = raw.lower().strip()
            norm       = re.sub(r"[^a-z0-9]+", " ", low).strip()
            short_norm = re.sub(r"\b(ltd|limited|bank)\b", "", norm).strip()
            fixed      = BANK_ALIAS_MAP.get(low) or alias_norm.get(norm) or alias_norm.get(short_norm)
            if not fixed:
                result = _fuzzy_best(norm, choices)
                if result and result[1] >= _FUZZY_AUTO:
                    fixed = result[0]
            if fixed and fixed != raw:
                mapping[raw] = fixed
        if mapping:
            after = s.map(mapping)
            mask  = after.notna()
            idxs  = cleaned.index[mask]
            cleaned.loc[idxs, c] = after.loc[idxs].astype(object)
            _add_clean(changes, idxs, c, after, "BANK_STANDARDIZED")


def _step_duplicate_rows(cleaned: pd.DataFrame, reviews: dict) -> None:
    """STEP 15 – Flag fully duplicate rows."""
    mask = cleaned.duplicated(keep=False)
    if mask.any():
        _add_review(reviews, cleaned.index[mask], "_record", "Duplicate row")


def _step_type_mismatch(cleaned: pd.DataFrame, reviews: dict, cc: _ColCache) -> None:
    """STEP 16 – Type-mismatch check for columns NOT in the schema."""
    TEXT_SKIP  = ("uuid", "cnic", "phone", "mobile", "account", "iban",
                  "date", "time", "email", "ip", "long", "lat")
    NUM_HINTS  = ("amount", "income", "count", "number", "total",
                  "age", "qty", "quantity", "salary", "balance")
    TEXT_HINTS = ("name", "title", "beneficiary", "father", "mother",
                  "spouse", "owner", "guardian", "district", "tehsil",
                  "village", "address", "status", "decision", "category")
    schema_cols = set(_SCHEMA.keys())
    for c in cleaned.columns:
        if c in schema_cols:
            continue
        n       = _norm_col(c)
        if any(h in n for h in TEXT_SKIP):
            continue
        before  = cleaned[c].copy()
        nonnull = ~cc.null(c, before)
        if nonnull.sum() < 10:
            continue
        s          = cc.s(c, before).str.strip()
        is_numeric = s[nonnull].str.fullmatch(r"[+-]?\d+(\.\d+)?", na=False)
        num_ratio  = float(is_numeric.mean())
        num_intent  = any(h in n for h in NUM_HINTS) or num_ratio >= 0.85
        text_intent = any(h in n for h in TEXT_HINTS) or (num_ratio <= 0.05 and not num_intent)
        if num_intent and num_ratio < 1.0:
            offenders = nonnull & ~is_numeric.reindex(cleaned.index, fill_value=False)
            if offenders.any() and offenders.sum() / max(nonnull.sum(), 1) < 0.50:
                _add_review(reviews, cleaned.index[offenders], c, before)
        elif text_intent:
            offenders = nonnull & is_numeric.reindex(cleaned.index, fill_value=False)
            if offenders.any():
                _add_review(reviews, cleaned.index[offenders], c, before)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def clean_dataframe_fast(
    df: pd.DataFrame,
    uuid_column: str | None = None,
) -> tuple[pd.DataFrame, dict]:
    original = df.copy(deep=True)
    cleaned  = df.copy(deep=True)
    changes: dict[int, dict] = {}
    reviews: dict[int, dict] = {}
    cc = _ColCache()

    # STEP 1 – Trim entire dataset
    _step_trim(cleaned, changes, cc)

    # STEP 2 – Null standardisation
    _step_null_standardize(cleaned, changes, reviews, cc)

    # STEPS 3–12 – Schema-driven per-column
    duplicate_uuid_mask  = pd.Series(False, index=cleaned.index)
    duplicate_cnic_mask  = pd.Series(False, index=cleaned.index)
    uuid_col: str | None = None

    for col, profile in _SCHEMA.items():
        if col not in cleaned.columns:
            continue
        col_type = profile.get("type", "string")

        if col_type == "cnic":
            cnic_dup_mask       = _step_cnic(cleaned, changes, reviews, col, profile, cc)
            duplicate_cnic_mask = duplicate_cnic_mask | cnic_dup_mask

        elif col_type == "numeric":
            if profile.get("non_null") or profile.get("unique"):
                dup_mask = _step_uuid(cleaned, changes, reviews, col, profile, cc)
                if profile.get("unique"):
                    duplicate_uuid_mask = dup_mask
                    uuid_col = uuid_column or col
            else:
                _step_numeric_type(cleaned, reviews, col, cc)

        elif col_type == "float":
            _step_float_coord(cleaned, reviews, col, profile.get("lat_lon", "lat"), cc)

        elif col_type == "bool":
            _step_bool(cleaned, changes, reviews, col, cc)

        elif col_type == "gender":
            _step_gender(cleaned, changes, reviews, col, cc)

        elif col_type == "cell_no":
            _step_cell_no(cleaned, changes, reviews, col, cc)

        elif col_type == "geo":
            _step_geo(cleaned, changes, reviews, col, profile, cc)

        elif col_type == "string":
            _step_string_category(cleaned, changes, reviews, col, profile, cc)

    # STEP 13 – Date auto-detect
    _step_date(cleaned, changes, reviews)

    # STEP 14 – Bank standardisation
    _step_bank(cleaned, changes)

    # STEP 15 – Duplicate rows
    _step_duplicate_rows(cleaned, reviews)

    # STEP 16 – Type-mismatch (non-schema columns)
    _step_type_mismatch(cleaned, reviews, cc)

    # ── Build response — vectorised, no iterrows ──────────────────────────────
    if uuid_col and uuid_col in cleaned.columns:
        uuid_values = original[uuid_col].astype("string").str.strip()
    else:
        uuid_values = pd.Series(
            [f"ROW_{i + 2}" for i in range(len(original))],
            index=original.index, dtype="string",
        )

    # Convert each column to JSON-safe list ONCE (avoids per-cell _json_scalar overhead)
    col_names = [str(c) for c in original.columns]
    col_arrays: dict[str, list] = {
        str(c): _series_to_json_list(original[c]) for c in original.columns
    }

    uuid_arr      = uuid_values.tolist()
    dup_arr       = duplicate_uuid_mask.tolist()
    cnic_dup_arr  = duplicate_cnic_mask.tolist()
    seen: dict[str, int] = {}
    response: dict[str, dict] = {}
    n_rows = len(original)

    for i in range(n_rows):
        raw       = uuid_arr[i]
        base_uuid = (
            str(raw) if raw is not None and str(raw).strip().lower() not in NULL_TOKENS
            else f"ROW_{i + 2}"
        )
        seen[base_uuid] = seen.get(base_uuid, 0) + 1
        key = base_uuid if seen[base_uuid] == 1 else f"{base_uuid}__duplicate_row_{i + 2}"
        response[key] = {
            "original_values":         {c: col_arrays[c][i] for c in col_names if c in (changes.get(i, {}).keys() | reviews.get(i, {}).keys())},
            "cleaned_values":          changes.get(i, {}),
            "manual_reviews_required": reviews.get(i, {}),
            "IS DUPLICATED UUID":      dup_arr[i],
            "IS DUPLICATED CNIC":      cnic_dup_arr[i],
        }

    return cleaned, response

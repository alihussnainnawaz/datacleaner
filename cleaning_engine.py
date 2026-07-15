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

Predefined validation rules (run at end of every pipeline, results written into
__validation_status__ column alongside any user-configured filters):

  Rule 1  – UUID mandatory + valid format (no all-same-digit patterns)
  Rule 2  – UUID unique per row (one housing unit = one UUID)
  Rule 3  – CNIC mandatory, numeric, exactly 13 digits
  Rule 4  – UUID ↔ CNIC one-to-one (each UUID maps to exactly one CNIC)
  Rule 5  – CNIC not shared across multiple UUIDs (one household per CNIC)
  Rule 6  – Mandatory fields not empty (UUID, CNIC, district, tehsil, UC, IP, bank, stage)
  Rule 7  – All date columns valid and in DD-MM-YYYY format
  Rule 8  – No future dates (verification, construction, disbursement, withdrawal)
  Rule 9  – Stage date ordering (earlier stage date < later stage date)
  Rule 10 – Construction stage dependency (lintel needs plinth; roof needs both)
  Rule 11 – Completed stage must have required ITVC/certificate verification
  Rule 12 – Stage status ↔ date consistency (completed status needs date; incomplete must not)
"""
from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from typing import Any, Callable

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process
from rapidfuzz.distance import Levenshtein

# PERF: __validation_status__ is built by calling json.dumps() once per row
# (see _apply_predefined_validation_to_df below) — at 340k+ rows that's a
# real cost. orjson's C implementation benchmarks ~4x faster than stdlib
# json for this exact shape of payload (dict of small dicts), with no
# behaviour difference that matters here (its non-ASCII output is still
# valid UTF-8 JSON, and callers always re-parse via json.loads). Falls back
# to stdlib json if orjson isn't installed, so this never breaks — it's
# purely a speed optimization.
try:
    import orjson
    def _fast_json_dumps(obj: Any) -> str:
        return orjson.dumps(obj).decode("utf-8")
except ImportError:
    def _fast_json_dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=True)

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
try:
    pd.options.future.infer_string = False
except Exception:
    pass
try:
    # PERF: pyarrow-backed StringDtype runs .str ops (strip/replace/title/
    # lower/upper/isin/fullmatch) in Arrow's C++ kernels instead of a Python
    # lambda per cell — benchmarked 10-40x faster on this pipeline's hot
    # steps at 690k rows. Every regex used against arrow-backed series in
    # this module is RE2-compatible (no backreferences/lookarounds — the one
    # backreference pattern was replaced with an isin() set, see _step_cnic).
    # Falls back to python storage if pyarrow isn't installed.
    import pyarrow  # noqa: F401
    pd.options.mode.string_storage = "pyarrow"
except Exception:
    try:
        pd.options.mode.string_storage = "python"
    except Exception:
        pass


# ── Constants ─────────────────────────────────────────────────────────────────

NULL_TOKENS = {"", "nan", "none", "null", "n/a", "na", "nil", "-", "--", "#n/a"}

# 13-char all-same-digit strings ("0000000000000" … "9999999999999") —
# RE2-safe replacement for the backreference regex r"(\d)\1{12}".
_ALL_SAME_DIGIT_13 = {str(d) * 13 for d in range(10)}

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
_RE_SPECIAL_SAFE  = re.compile(r"[^A-Za-z0-9\s\-.,#/&'():]")
_RE_SPECIAL_STRIP = re.compile(r"[^A-Za-z0-9\s\-.:]")
_RE_NON_DIGIT     = re.compile(r"\D")
_RE_REPEAT_DIGIT  = re.compile(r"([0-9])\1{6,}")
_DATE_SIGNAL_RE   = re.compile(
    r"(?:\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"
    r"|\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b"
    r"|\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}\b"
    r"|\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}\b)"
)
_TIME_ONLY_RE = re.compile(r"^\s*\d{1,2}:\d{2}(:\d{2})?\s*([AaPp][Mm])?\s*$")
_TZ_SUFFIX_RE = re.compile(r"\s+[A-Z]{2,4}$")

_LAT_RANGE   = (20.0, 40.0)
_LON_RANGE   = (60.0, 80.0)
_CELL_LEN    = 10
_CELL_PREFIX = "3"
_FUZZY_AUTO  = 88
_FUZZY_REVIEW = 70

# ── DD-MM-YYYY output format for predefined validation ────────────────────────
_DD_MM_YYYY = "%d-%m-%Y"

# ── Column name aliases for predefined rules (normalised → canonical role) ────
# UUID aliases: column names that carry the housing unit identifier
_UUID_HINTS    = ("uuid", "uu_id", "da_uuid", "sid", "hru", "ref_no", "ref no",
                  "unit_id", "unit id", "house_id", "house id", "id")
# CNIC aliases
_CNIC_HINTS    = ("cnic", "da_cnic", "national_id", "nic", "id_card")
# District aliases
_DISTRICT_HINTS = ("district", "dist", "zila")
# Tehsil/Taluka aliases
_TEHSIL_HINTS  = ("tehsil", "taluka", "taluqa", "sub_district", "sub district")
# UC aliases
_UC_HINTS      = ("uc", "union_council", "union council", "jh_uc", "jh uc")
# IP/Implementing Partner aliases
_IP_HINTS      = ("ip_name", "ip name", "implementing_partner", "ip", "partner")
# Bank aliases
_BANK_HINTS    = ("bank", "bankname", "bank_name", "payment_by", "ifis",
                  "payment by", "payment by - ifis")
# Stage/status aliases
_STAGE_HINTS   = ("stage", "tranche", "eng_status", "eng status", "status",
                  "instalment", "installment")

# Construction stage column sets for Banks/Financials (ordered early→late)
_STAGE_DATE_SEQUENCE = [
    # (plinth_col, lintel_col, roof_col, house_completion_col)
    ("plinth_completion_date", "lintel_completion_date",
     "roof_completion_date",   "house_completion_date"),
]
_TRANCHE_DATE_SEQUENCE = [
    ("tranche_1_released_date", "tranche_2_released_date",
     "tranche_3_released_date", "tranche_4_released_date"),
    ("tranche_1_withdraw_date", "tranche_2_withdraw_date",
     "tranche_3_withdraw_date", "tranche_4_withdraw_date"),
]

# Certificate stage columns (ordered)
_CERT_DATE_SEQUENCE = [
    ("plinth_submitted_at", "lintel_submitted_at", "roof_submitted_at"),
]

# Stage dependency: later stage → required earlier stages
_STAGE_DEPENDENCIES = {
    "lintel_completion_date":  ["plinth_completion_date"],
    "roof_completion_date":    ["plinth_completion_date", "lintel_completion_date"],
    "house_completion_date":   ["plinth_completion_date", "lintel_completion_date",
                                "roof_completion_date"],
    "lintel_submitted_at":     ["plinth_submitted_at"],
    "roof_submitted_at":       ["plinth_submitted_at", "lintel_submitted_at"],
    "tranche_2_released_date": ["tranche_1_released_date"],
    "tranche_3_released_date": ["tranche_1_released_date", "tranche_2_released_date"],
    "tranche_4_released_date": ["tranche_1_released_date", "tranche_2_released_date",
                                "tranche_3_released_date"],
}

# ITVC/certificate verification columns required when stage date present
_ITVC_REQUIREMENTS = {
    # stage date col → required verification col
    "plinth_completion_date":  "plinth_over_all_certificate",
    "lintel_completion_date":  "lintel_over_all_certificate",
    "roof_completion_date":    "roof_over_all_certificate",
    "plinth_submitted_at":     "plinth_over_all_certificate",
    "lintel_submitted_at":     "lintel_over_all_certificate",
    "roof_submitted_at":       "roof_over_all_certificate",
}

# Stage status ↔ date consistency pairs
# Maps a status column → its corresponding date column
# "completed" values → date must exist; "incomplete" values → date must be absent
_STAGE_STATUS_DATE_PAIRS: list[tuple[str, str, set, set]] = [
    # (status_col, date_col, completed_values, incomplete_values)
    ("Instalment 1 Processed", "tranche_1_released_date",
     {"disbursed", "cleared", "processing"}, {"pending", "returned"}),
    ("Instalment 2 Processed", "tranche_2_released_date",
     {"disbursed", "cleared", "processing"}, {"pending", "returned"}),
    ("Instalment 3 Processed", "tranche_3_released_date",
     {"disbursed", "cleared", "processing"}, {"pending", "returned"}),
    ("Instalment 4 Processed", "tranche_4_released_date",
     {"disbursed", "cleared", "processing"}, {"pending", "returned"}),
    # Beneficiary eng status ↔ disbursement
    ("Eng Status", "Disbursement Status",
     {"approved"}, {"rejected", "pending", "purged"}),
]


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
        self._s.pop(col, None)
        self._null.pop(col, None)

    def update(self, col: str, mask: pd.Series, after: pd.Series) -> None:
        """
        PERF: refresh the cached string view in place after a masked column
        write, instead of dropping it and paying a full object->arrow
        re-conversion in every later step that touches the column. The
        refreshed view is exactly what a fresh astype("string") of the
        post-write column would produce: *after* where mask, else the old
        cached view. Null mask is dropped (cheap to rebuild, and only some
        steps need it).
        """
        cached = self._s.get(col)
        if cached is None:
            self._null.pop(col, None)
            return
        try:
            self._s[col] = cached.mask(mask, after.astype("string"))
        except Exception:
            self._s.pop(col, None)
        self._null.pop(col, None)


# ── Scalar helpers ────────────────────────────────────────────────────────────

def _norm_col(c: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_")


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


class _CleanLog:
    """
    Columnar change/review recorder — O(1) per step call instead of a Python
    loop over every changed cell. Records are (col, idx_int64_array,
    values_list_or_scalar, step). Per-row dicts (the shape the report and the
    predefined-validation path expect) are materialised lazily via .rows(),
    touching only cells that actually changed.
    """
    __slots__ = ("records", "_rows_cache", "kind")

    def __init__(self, kind: str = "clean") -> None:
        self.records: list[tuple] = []
        self._rows_cache: dict | None = None
        self.kind = kind  # "clean" -> rows as {col: [val, step]}, "review" -> {col: val}

    def add(self, idxs, col: str, values: Any, step: str | None) -> None:
        try:
            idx_arr = np.asarray(idxs, dtype=np.int64)
        except Exception:
            idx_arr = np.asarray([int(i) for i in idxs], dtype=np.int64)
        if len(idx_arr) == 0:
            return
        if isinstance(values, pd.Series):
            # MEMORY: store factorised (codes, uniques) instead of one Python
            # string object per changed cell — cleaned values repeat heavily
            # ("Dadu" x 40k, "Yes" x 200k...), so this collapses hundreds of
            # MB of duplicate strings into a small uniques list + int32 codes.
            sub = values.reindex(idxs)
            codes, uniq = pd.factorize(sub, use_na_sentinel=True)
            uniq_list = [
                _json_scalar(u) for u in np.asarray(uniq, dtype=object).tolist()
            ]
            vals = ("F", codes.astype(np.int32), uniq_list)
        else:
            vals = _json_scalar(values)
        self.records.append((str(col), idx_arr, vals, step))
        self._rows_cache = None

    # ── aggregate helpers (all vectorised / touched-cells-only) ──────────────
    def cell_count(self) -> int:
        return sum(len(r[1]) for r in self.records)

    def touched_row_mask(self, n: int) -> np.ndarray:
        mask = np.zeros(n, dtype=bool)
        for _, idx_arr, _, _ in self.records:
            mask[idx_arr] = True
        return mask

    def step_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, idx_arr, _, step in self.records:
            if step:
                out[step] = out.get(step, 0) + len(idx_arr)
        return out

    def column_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for col, idx_arr, _, _ in self.records:
            out[col] = out.get(col, 0) + len(idx_arr)
        return out

    def rows(self, with_step: bool | None = None) -> dict[int, dict]:
        """{row_pos: {col: [val, step] | val}} — built once, only for touched cells."""
        if with_step is None:
            with_step = self.kind == "clean"
        if self._rows_cache is not None:
            return self._rows_cache
        rows: dict[int, dict] = {}
        for col, idx_arr, vals, step in self.records:
            if isinstance(vals, tuple) and len(vals) == 3 and vals[0] == "F":
                codes, uniq_list = vals[1], vals[2]
                decoded = [uniq_list[c] if c >= 0 else None for c in codes.tolist()]
            else:
                decoded = None
            scalar = decoded is None
            for k in range(len(idx_arr)):
                i = int(idx_arr[k])
                v = vals if scalar else decoded[k]
                row = rows.setdefault(i, {})
                if with_step:
                    if col in row:
                        row[col] = [v, _merge_step(row[col][1], step or "")]
                    else:
                        row[col] = [v, step or ""]
                else:
                    row[col] = v
        self._rows_cache = rows
        return rows

    # dict-compat shims (predefined-validation path reads reviews like a dict)
    def get(self, i, default=None):
        return self.rows().get(i, default)

    def setdefault(self, i, default):
        return self.rows().setdefault(i, default)

    def __contains__(self, i) -> bool:
        return i in self.rows()

    def __bool__(self) -> bool:
        return bool(self.records)


def _add_clean(changes, idxs, col: str, new_values: Any, step: str) -> None:
    if isinstance(changes, _CleanLog):
        changes.add(idxs, col, new_values, step)
        return
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


def _add_review(reviews, idxs, col: str, values: Any) -> None:
    if isinstance(reviews, _CleanLog):
        reviews.add(idxs, col, values, None)
        return
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
            _assign_masked(cleaned, col, mask, after, cc)
            _add_clean(changes, idxs, col, after, step)
    if review_map:
        review_mask = s.isin(review_map) & nonnull
        if review_mask.any():
            _add_review(reviews, cleaned.index[review_mask], col, before)


# ── Cleaning steps ────────────────────────────────────────────────────────────


def _memo_transform(s: pd.Series, fn) -> pd.Series:
    """
    Apply a per-value string transform via unique-value memoisation. Output
    is element-for-element IDENTICAL to ``fn(s)`` for any per-element
    transform (regex replace / strip / casing are all per-element), but the
    transform only touches distinct values.

    PERF: stays inside pyarrow end-to-end — dictionary_encode (C hash) finds
    the distinct values, *fn* runs on that (usually tiny) dictionary, and
    take() gathers results back by code with nulls propagating — so no
    object-array round trip is paid on either side. Falls back to a plain
    ``fn(s)`` if the arrow fast path isn't available for this series.
    """
    try:
        import pyarrow as pa
        arr = s.array._pa_array.combine_chunks()          # pyarrow StringArray
        enc = arr.dictionary_encode()
        uniq_ser = pd.Series(
            pd.arrays.ArrowStringArray(pa.chunked_array([enc.dictionary])),
            copy=False,
        )
        out_u = fn(uniq_ser)
        out_pa = out_u.array._pa_array.combine_chunks()
        gathered = out_pa.take(enc.indices)               # null indices -> null
        return pd.Series(
            pd.arrays.ArrowStringArray(pa.chunked_array([gathered])),
            index=s.index, copy=False,
        )
    except Exception:
        return fn(s)


def _assign_masked(cleaned: pd.DataFrame, col, mask: pd.Series, after: pd.Series, cc: "_ColCache | None" = None) -> None:
    """Replace cleaned[col] where mask is True with values from *after*.

    PERF: a full-column np.where swap is markedly cheaper than
    ``cleaned.loc[idxs, col] = after.loc[idxs]`` when many rows change
    (row-indexed .loc assignment pays per-row alignment costs). Values at
    masked positions are guaranteed non-null by the callers' masks, so the
    na_value placeholder never lands in the output.
    """
    mask_np   = mask.to_numpy(dtype=bool)
    before_np = cleaned[col].to_numpy(dtype=object, na_value=None) if cleaned[col].dtype != object else cleaned[col].to_numpy()
    after_np  = after.to_numpy(dtype=object, na_value=None)
    cleaned[col] = pd.Series(np.where(mask_np, after_np, before_np), index=cleaned.index, dtype=object)
    if cc is not None:
        cc.update(col, mask, after)


def _step_regex_rules(
    cleaned: pd.DataFrame, changes, reviews,
    regex_rules: dict | None, cc: _ColCache,
) -> None:
    """
    Apply user-configured "Regex Clean" rules — one shared step, covering
    every configured column, run as the FIRST step in the pipeline (these
    operate on raw values, before trim/casing/anything else touches them).

    Previously these ran as a separate pre-flight phase from the frontend —
    a handful of extra API calls sent BEFORE the real pipeline request even
    started, invisible to progress tracking and outside the same
    execution/timing hierarchy as every other step. Folded in here: same
    rule shapes (`mapping` / `pattern`+`replacement`+`flags` / `auto`),
    same underlying logic (the plain dict-map and _step_regex_auto reuse
    exactly what the standalone /tools/regex* endpoints use), just running
    inside the pipeline as a real, timed, tracked step like any other.
    """
    if not regex_rules:
        return
    import re as _re
    for col, cfg in regex_rules.items():
        if col not in cleaned.columns or not isinstance(cfg, dict):
            continue
        if cfg.get("mapping"):
            mapping = cfg["mapping"]
            if not mapping:
                continue
            before = cleaned[col].astype(str)
            # Vectorised dict-map (was a per-row Python lambda in the old
            # standalone endpoint) — same semantics: unmapped values pass
            # through unchanged.
            after = before.map(mapping)
            after = after.where(after.notna(), before)
            mask  = before.ne(after)
            if mask.any():
                idxs = cleaned.index[mask]
                _assign_masked(cleaned, col, mask, after, cc)
                _add_clean(changes, idxs, col, after, "REGEX_CLEAN")

        elif cfg.get("auto"):
            # Legacy queued rule (pre-dates the editable-cluster popup) —
            # reuses the exact fuzzy-cluster auto-clean step directly, so
            # it feeds into the same changes/reviews log everything else
            # here uses instead of building a throwaway one.
            _step_regex_auto(cleaned, changes, reviews, col, cc)

        elif cfg.get("pattern"):
            pattern     = str(cfg.get("pattern") or "")
            replacement = str(cfg.get("replacement") or "")
            flags_str   = str(cfg.get("flags") or "")
            if not pattern:
                continue
            re_flags = 0
            if "i" in flags_str: re_flags |= _re.IGNORECASE
            if "m" in flags_str: re_flags |= _re.MULTILINE
            try:
                _re.compile(pattern, re_flags)
            except _re.error:
                continue
            before  = cleaned[col].astype("string")
            nonnull = before.notna() & ~before.str.strip().str.lower().isin(NULL_TOKENS)
            if not nonnull.any():
                continue
            try:
                replaced = before.where(~nonnull).combine_first(
                    before[nonnull].str.replace(pattern, replacement, regex=True, flags=re_flags)
                )
            except Exception:
                continue
            mask = nonnull & (before != replaced).fillna(False)
            if mask.any():
                idxs = cleaned.index[mask]
                _assign_masked(cleaned, col, mask, replaced, cc)
                _add_clean(changes, idxs, col, replaced, "REGEX_CLEAN")


def _resolve_explicit_uuid(
    cleaned: pd.DataFrame, uuid_column: str | None,
) -> tuple[str | None, "pd.Series | None"]:
    """
    If the user explicitly picked a UUID/ID column (the picker in Settings —
    NOT a schema guess), honour it directly and unconditionally: it becomes
    uuid_col for row-identifier/report-key purposes, and gets its own
    duplicate check, independent of whether it happens to also match a
    hardcoded schema column name or whether Column Rules were dragged onto
    it. Previously uuid_col was ONLY ever set as a side effect of the
    schema-driven column loop finding a column profiled "unique" (in
    practice just the literal column named "DA_UUID") — so a user-selected
    ID column that wasn't gated on or didn't match that exact name was
    silently dropped, falling back to synthetic ROW_2/ROW_3/... identifiers
    that looked like the system had "guessed" and ignored the real choice.

    Returns (uuid_col, duplicate_mask) — duplicate_mask is None if there's
    nothing to check yet (schema-driven duplicate detection can still layer
    on top of this via the normal column loop, unaffected).
    """
    if not uuid_column or uuid_column not in cleaned.columns:
        return None, None
    s = cleaned[uuid_column].astype("string").str.strip()
    nonnull = s.notna() & ~s.str.lower().isin(NULL_TOKENS)
    dup = nonnull & s.duplicated(keep=False)
    return uuid_column, dup


def _step_trim(cleaned: pd.DataFrame, changes: dict, cc: _ColCache) -> None:
    """STEP 1 – Trim & collapse whitespace across the ENTIRE dataset."""
    for c in cleaned.columns:
        if cleaned[c].dtype != object and not pd.api.types.is_string_dtype(cleaned[c]):
            continue
        before = cleaned[c].copy()
        s      = cc.s(c, before)
        after  = _memo_transform(s, lambda u: u.str.replace(_RE_WHITESPACE.pattern, " ", regex=True).str.strip())
        mask   = before.notna() & (s != after).fillna(False)
        if mask.any():
            idxs = cleaned.index[mask]
            _assign_masked(cleaned, c, mask, after, cc)
            _add_clean(changes, idxs, c, after, "TRIM")


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
        # Vectorised null-out (was a row-indexed .loc assignment per column)
        mask_np = mask.to_numpy(dtype=bool)
        col_np  = cleaned[c].to_numpy(dtype=object) if cleaned[c].dtype == object else cleaned[c].to_numpy(dtype=object, na_value=None)
        cleaned[c] = pd.Series(np.where(mask_np, None, col_np), index=cleaned.index, dtype=object)
        cc.invalidate(c)


def _step_special_chars(
    cleaned: pd.DataFrame, changes: dict,
    col: str, safe: bool, cc: _ColCache,
) -> None:
    """Strip illegal special characters from a single column."""
    pat     = _RE_SPECIAL_SAFE if safe else _RE_SPECIAL_STRIP
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    s       = cc.s(col, before)
    after   = _memo_transform(
        s,
        lambda u: (
            u.str.replace(pat.pattern, " ", regex=True)
             .str.replace(_RE_WHITESPACE.pattern, " ", regex=True)
             .str.strip()
        ),
    )
    mask = nonnull & (s != after).fillna(False)
    if mask.any():
        idxs = cleaned.index[mask]
        _assign_masked(cleaned, col, mask, after, cc)
        _add_clean(changes, idxs, col, after, "SPECIAL_CHARS_CLEANED")


def _step_special_chars_all(cleaned: pd.DataFrame, changes: dict, cc: _ColCache) -> None:
    """STEP 3 – Strip illegal special characters across the ENTIRE dataset,
    same shape as _step_trim/_step_null_standardize: one pass, every
    string column, gated only by the "special" global toggle — not nested
    inside per-column schema steps (geo/catdate) anymore. That nesting used
    to fire this once per geo/catdate column, which made the pipeline
    popup's "Cleaning special chars" row flip done→running repeatedly (one
    start/end pair per column) instead of completing once like every other
    global-rule step. A handful of schema columns (e.g. "Address") that
    legitimately contain characters like # / & ' ( ) still get the more
    permissive pattern via their schema profile's "special_chars_ok" flag —
    everything else uses the standard strip pattern.
    """
    for c in cleaned.columns:
        if cleaned[c].dtype != object and not pd.api.types.is_string_dtype(cleaned[c]):
            continue
        safe = bool(_SCHEMA.get(c, {}).get("special_chars_ok", False))
        _step_special_chars(cleaned, changes, c, safe, cc)


def _step_casing(
    cleaned: pd.DataFrame, changes: dict,
    col: str, profile: dict, cc: _ColCache,
) -> None:
    """STEP 3b – apply a text-casing style to a column.

    `profile["case_style"]` (one of "title" | "upper" | "lower" | "camel")
    takes precedence when present — this is how a user's drag-and-drop
    column-rule choice overrides the schema default. Falls back to the
    legacy boolean flags ("title"/"upper") used by the static schema.
    """
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    s       = cc.s(col, before)

    style = profile.get("case_style")
    if not style:
        style = "upper" if profile.get("upper") else "title" if profile.get("title") else None

    if style == "upper":
        after = _memo_transform(s, lambda u: u.str.upper())
        step  = "UPPER_CASED"
    elif style == "lower":
        after = _memo_transform(s, lambda u: u.str.lower())
        step  = "LOWER_CASED"
    elif style == "camel":
        def _camel(u):
            _titled = u.str.title().str.replace(r"[^0-9A-Za-z]+", "", regex=True)
            return _titled.str.slice(0, 1).str.lower() + _titled.str.slice(1)
        after = _memo_transform(s, _camel)
        step  = "CAMEL_CASED"
    elif style == "title":
        after = _memo_transform(s, lambda u: u.str.title())
        step  = "TITLE_CASED"
    else:
        return
    mask = nonnull & (s != after).fillna(False)
    if mask.any():
        idxs = cleaned.index[mask]
        _assign_masked(cleaned, col, mask, after, cc)
        _add_clean(changes, idxs, col, after, step)


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
        _assign_masked(cleaned, col, auto, after, cc)
        _add_clean(changes, idxs, col, after, "BOOL_STANDARDIZED")
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
        _assign_masked(cleaned, col, auto, after, cc)
        _add_clean(changes, idxs, col, after, "GENDER_STANDARDIZED")
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

    digits = cc.s(col, before).str.replace(_RE_NON_DIGIT.pattern, "", regex=True).str.strip()

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

    # "(\d)\1{12}" (13 identical digits) — written as an isin() set because
    # backreferences aren't supported by RE2 (the pyarrow string engine).
    # Exactly equivalent: a 13-char all-same-digit string is one of these 10.
    repeat = digits.isin(_ALL_SAME_DIGIT_13).fillna(False)
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
    """STEP 8 – Cell No → 03XXXXXXXXX."""
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    s       = cc.s(col, before).str.strip()
    digits  = (
        s.str.replace(r"^\+92", "", regex=True)
         .str.replace(r"^0092", "", regex=True)
         .str.replace(r"^92",   "", regex=True)
         .str.replace(r"^0",    "", regex=True)
         .str.replace(r"\D",    "", regex=True)
    )
    valid = nonnull & (digits.str.len() == _CELL_LEN) & digits.str.startswith(_CELL_PREFIX).fillna(False)
    normalised = "0" + digits
    auto  = valid & (s != normalised).fillna(False)
    if auto.any():
        idxs = cleaned.index[auto]
        # Ensure column is object dtype before writing strings
        if cleaned[col].dtype != object:
            cleaned[col] = cleaned[col].astype(object)
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


# Same patterns file_handler.py uses to auto-suggest a column's datatype at
# upload time — reused here so "check this column against type X" (the
# DATATYPE_CHECK column rule, dragged onto a column in Column Rule Preview)
# validates against the exact same definition of each type the frontend
# showed the user when suggesting it.
_DTYPE_CHECK_PATTERNS: dict[str, "re.Pattern"] = {
    "integer":  re.compile(r"^-?\d+$"),
    "decimal":  re.compile(r"^-?\d+\.\d+$"),
    "boolean":  re.compile(r"^(true|false|yes|no|y|n|0|1)$", re.I),
    "email":    re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    "phone":    re.compile(r"^\+?\d[\d\-\s()]{6,}\d$"),
    "cnic":     re.compile(r"^\d{5}-?\d{7}-?\d{1}$"),
    "iban":     re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$", re.I),
    "date":     re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}$"),
    # "text"/"unknown" intentionally absent — every non-null string is
    # valid text, so there's nothing to flag for either.
}


def _step_dtype_check(cleaned: pd.DataFrame, reviews: dict, col: str, expected_type: str, cc: _ColCache) -> None:
    """Flag values in `col` that don't match the user-selected expected
    datatype. Non-mutating (review-only, like _step_numeric_type/
    _step_float_coord above) — a value not matching the expected type is a
    judgment call for a human, not something safe to auto-correct."""
    pattern = _DTYPE_CHECK_PATTERNS.get((expected_type or "").lower())
    if pattern is None:   # "text"/"unknown"/unrecognised — nothing to check
        return
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    if not nonnull.any():
        return
    bad = nonnull & ~cc.s(col, before).str.strip().str.fullmatch(pattern, na=False)
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


def _compute_value_clusters(
    freq: dict[str, int],
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Pure clustering computation, extracted from _step_regex_auto so it can be
    reused both by the actual cleaning step (which applies the result) and by
    analyze_value_clusters() (which surfaces it to the frontend for editing
    before anything is applied). Behaviourally identical to the inline logic
    that used to live directly inside _step_regex_auto.

    Parameters
    ----------
    freq : {original_value: occurrence_count} for all non-null values in a column

    Returns
    -------
    (auto_map, review_map) : {original_value: canonical_value} dicts.
      auto_map   — high-confidence matches (typos/case/whitespace variants)
      review_map — lower-confidence fuzzy matches that need a human decision
    """
    if len(freq) <= 1:
        return {}, {}

    def _norm(v: str) -> str:
        return re.sub(r"\s+", " ", v.strip().lower())

    norm_groups: dict[str, list[str]] = {}
    for orig in freq:
        norm_groups.setdefault(_norm(orig), []).append(orig)

    auto_map:   dict[str, str] = {}
    review_map: dict[str, str] = {}
    norm_freq: dict[str, int] = {
        n: sum(freq[o] for o in origs)
        for n, origs in norm_groups.items()
    }

    for norm, originals in norm_groups.items():
        if len(originals) <= 1:
            continue
        canonical = max(originals, key=lambda o: (freq[o], o[0].isupper()))
        for o in originals:
            if o != canonical:
                auto_map[o] = canonical

    distinct_norms = sorted(norm_groups.keys(), key=lambda n: norm_freq[n], reverse=True)
    choices = tuple(distinct_norms)
    assigned_norms: set[str] = set(auto_map.values()) | set(auto_map.keys())

    norm_to_canonical: dict[str, str] = {}
    for n, origs in norm_groups.items():
        norm_to_canonical[n] = max(origs, key=lambda o: (freq[o], o[0].isupper()))

    for n in distinct_norms:
        if n in assigned_norms:
            continue
        assigned_norms.add(n)
        peers = [c for c in choices if c != n and c not in assigned_norms]
        if not peers:
            continue
        result = _fuzzy_best(n, tuple(peers))
        if not result:
            continue
        matched_norm, score = result
        assigned_norms.add(matched_norm)

        if norm_freq[n] >= norm_freq[matched_norm]:
            canonical_norm, variant_norm = n, matched_norm
        else:
            canonical_norm, variant_norm = matched_norm, n

        canonical_orig = norm_to_canonical[canonical_norm]

        if score >= _FUZZY_AUTO:
            for o in norm_groups.get(variant_norm, []):
                if o != canonical_orig:
                    auto_map[o] = canonical_orig
            for o in norm_groups.get(canonical_norm, []):
                if o != canonical_orig:
                    auto_map[o] = canonical_orig
        elif score >= _FUZZY_REVIEW:
            for o in (norm_groups.get(canonical_norm, []) + norm_groups.get(variant_norm, [])):
                if o != canonical_orig and o not in auto_map:
                    review_map[o] = canonical_orig

    return auto_map, review_map


def quick_cluster_values(values: list[str]) -> list[dict]:
    """
    Fast, lightweight clustering of a list of unique values into suggested
    groups — built to seed an INTERACTIVE, fully user-editable popup, not to
    be a final answer on its own. No heavy fuzzy scoring (no rapidfuzz
    WRatio/process.extractOne over the whole value set): just
      1. group by case/whitespace-normalised key (free, instant), then
      2. merge normalised-group keys that are within edit-distance 1 of each
         other (Levenshtein distance is ~free — only run over the small set
         of DISTINCT normalised keys, not every raw value).
    This catches things like "badin"/"Badin"/"bdin" (a single missing
    letter) without the cost of all-pairs fuzzy similarity scoring, so it
    stays fast even on a few hundred to a few thousand unique values, and
    the result is meant to be corrected by the user (drag a value between
    clusters, rename the target) rather than trusted outright.

    Parameters
    ----------
    values : list of raw unique string values (e.g. from a column's unique
             value list — no row data or counts required).

    Returns
    -------
    List of clusters, each:
      { "canonical": str,            # suggested target (highest-frequency-looking
                                      #  original within the group — here just the
                                      #  first value seen, since input has no counts)
        "members":   [str, ...] }    # ALL original values in this group,
                                      #  including the canonical itself, so the
                                      #  frontend can render every bubble and let
                                      #  the user drag any of them, including the
                                      #  canonical, between clusters.
    Clusters with only one member (nothing to merge) are still included —
    the caller decides whether to show singletons (e.g. "tando -> tando")
    or only the ones with >1 member.
    """
    cleaned_values = [str(v).strip() for v in values if str(v).strip()]
    if not cleaned_values:
        return []

    def _norm(v: str) -> str:
        return re.sub(r"\s+", " ", v.strip().lower())

    # Step 1: exact normalised-key grouping (instant)
    norm_groups: dict[str, list[str]] = {}
    for orig in cleaned_values:
        norm_groups.setdefault(_norm(orig), []).append(orig)

    norm_keys = list(norm_groups.keys())

    # Step 2: cheap edit-distance-1 merge over DISTINCT normalised keys only.
    # Union-find over indices into norm_keys.
    parent = list(range(len(norm_keys)))

    def _find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def _union(i: int, j: int) -> None:
        ri, rj = _find(i), _find(j)
        if ri != rj:
            parent[rj] = ri

    n = len(norm_keys)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = norm_keys[i], norm_keys[j]
            # Skip pairs whose length differs by more than 1 — they can't be
            # within edit distance 1, so don't even bother computing it.
            if abs(len(a) - len(b)) > 1:
                continue
            if Levenshtein.distance(a, b) <= 1:
                _union(i, j)

    # Step 3: materialise final groups from the union-find result
    final_groups: dict[int, list[str]] = {}
    for i, key in enumerate(norm_keys):
        root = _find(i)
        final_groups.setdefault(root, []).extend(norm_groups[key])

    clusters: list[dict] = []
    for members in final_groups.values():
        # Prefer the most "title-cased-looking" original as the suggested
        # canonical (e.g. "Badin" over "badin"/"BADin") purely as a nicer
        # starting suggestion — the user can always rename it.
        canonical = max(members, key=lambda v: (v[:1].isupper() and not v.isupper(), v == v.title()))
        clusters.append({"canonical": canonical, "members": sorted(set(members))})

    # Multi-member clusters first (the ones needing a decision), singletons after.
    clusters.sort(key=lambda c: (-len(c["members"]), c["canonical"].lower()))
    return clusters


def _step_regex_auto(
    cleaned: pd.DataFrame, changes: dict, reviews: dict,
    col: str, cc: _ColCache,
) -> None:
    """Auto Regex Clean – two-phase normalization and fuzzy clustering."""
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    if not nonnull.any():
        return

    s = cc.s(col, before).str.strip()

    freq: dict[str, int] = {}
    for v in s[nonnull]:
        sv = str(v).strip()
        if sv:
            freq[sv] = freq.get(sv, 0) + 1

    auto_map, review_map = _compute_value_clusters(freq)

    if auto_map:
        after = s.map(auto_map)
        mask  = after.notna() & nonnull
        if mask.any():
            idxs = cleaned.index[mask]
            _assign_masked(cleaned, col, mask, after, cc)
            _add_clean(changes, idxs, col, after, "REGEX_AUTO_CLEAN")

    if review_map:
        s2   = cc.s(col, cleaned[col]).str.strip()
        mask = s2.isin(review_map) & nonnull
        if mask.any():
            _add_review(reviews, cleaned.index[mask], col, before)


def analyze_value_clusters(df: pd.DataFrame, column: str) -> list[dict]:
    """
    Read-only: compute the same value clusters _step_regex_auto would apply,
    but return them as an editable structure instead of mutating the
    dataframe. Powers the interactive Regex Clean preview, where the user can
    rename a cluster's canonical target or remove a wrongly-grouped member
    before anything is written to the dataset.

    Returns a list of clusters, each:
      {
        "canonical": str,                 # suggested target value (editable by the user)
        "status":    "auto" | "review",   # confidence tier from the fuzzy matcher
        "members": [
          { "value": str, "count": int }, # original values that would be remapped
          ...
        ],
      }
    Only clusters with at least one member (i.e. at least one variant that
    would actually change) are included. Sorted by total affected row count,
    descending, so the highest-impact clusters are reviewed first.
    """
    if column not in df.columns:
        raise ValueError(f'Column "{column}" not found.')

    s = df[column].astype("string").str.strip()
    nonnull = s.notna() & (s.str.lower() != "")

    freq: dict[str, int] = {}
    for v in s[nonnull]:
        sv = str(v).strip()
        if sv:
            freq[sv] = freq.get(sv, 0) + 1

    auto_map, review_map = _compute_value_clusters(freq)

    # Group by canonical target so the UI shows "these N variants -> target"
    clusters: dict[tuple[str, str], dict] = {}  # (status, canonical) -> cluster
    for variant, canonical in auto_map.items():
        key = ("auto", canonical)
        clusters.setdefault(key, {"canonical": canonical, "original_canonical": canonical, "status": "auto", "members": []})
        clusters[key]["members"].append({"value": variant, "count": freq.get(variant, 0)})
    for variant, canonical in review_map.items():
        key = ("review", canonical)
        clusters.setdefault(key, {"canonical": canonical, "original_canonical": canonical, "status": "review", "members": []})
        clusters[key]["members"].append({"value": variant, "count": freq.get(variant, 0)})

    result = list(clusters.values())
    for c in result:
        c["members"].sort(key=lambda m: -m["count"])
    result.sort(key=lambda c: -sum(m["count"] for m in c["members"]))
    return result


def apply_value_cluster_mapping(
    df: pd.DataFrame, column: str, mapping: dict[str, str],
) -> tuple[pd.DataFrame, dict, dict]:
    """
    Apply a USER-SUPPLIED (possibly hand-edited) value mapping to a column —
    the counterpart to analyze_value_clusters(). Unlike auto_regex_clean_column,
    this does not re-derive the mapping from the fuzzy matcher; it applies
    exactly the {original_value: new_value} pairs given, so edits made in the
    interactive preview (renamed canonical, removed members) are respected
    precisely rather than being recomputed and potentially overridden.

    Returns (cleaned_df, changes, reviews) in the same shape as
    auto_regex_clean_column, so the existing /tools/regex-auto response/flag
    plumbing works unchanged.
    """
    if column not in df.columns:
        raise ValueError(f'Column "{column}" not found.')
    cleaned = df.copy()
    changes: dict = {}
    reviews: dict = {}
    if not mapping:
        return cleaned, changes, reviews

    s = cleaned[column].astype("string").str.strip()
    nonnull = s.notna()

    after = s.map(mapping)
    mask  = after.notna() & nonnull
    if mask.any():
        idxs = cleaned.index[mask]
        cleaned.loc[idxs, column] = after.loc[idxs].astype(object)
        _add_clean(changes, idxs, column, after, "REGEX_AUTO_CLEAN")

    return cleaned, changes, reviews




def _step_geo(
    cleaned: pd.DataFrame, changes: dict, reviews: dict,
    col: str, profile: dict, cc: _ColCache,
) -> None:
    """STEP 11 – Geo: Title Case → fuzzy canonical. (Special-char stripping
    now runs once for the whole dataset beforehand, see
    _step_special_chars_all — no longer a sub-step here.)"""
    _step_casing(cleaned, changes, col, {"title": True}, cc)
    canonical = profile.get("canonical")
    if canonical is None:
        n = _norm_col(col)
        if n in (GEO_COLUMNS or {}):
            canonical = list(GEO_COLUMNS[n])
        else:
            for key, vals in (GEO_COLUMNS or {}).items():
                nk = _norm_col(key)
                if nk in n or n in nk:
                    canonical = list(vals)
                    break
        if canonical is None:
            if "district" in n:
                try:
                    from config import SINDH_DISTRICTS
                    canonical = list(SINDH_DISTRICTS)
                except Exception:
                    pass
            elif any(h in n for h in ("tehsil", "_uc", "union", "taluka", "council", "_deh", "deh")):
                try:
                    from config import SINDH_TEHSILS
                    canonical = list(SINDH_TEHSILS)
                except Exception:
                    pass
    if canonical:
        _apply_fuzzy(cleaned, col, canonical, changes, reviews, "GEO_STANDARDIZED", cc)


def _step_string_category(
    cleaned: pd.DataFrame, changes: dict, reviews: dict,
    col: str, profile: dict, cc: _ColCache,
) -> None:
    """STEP 12 – String/category: casing → fuzzy. (Special-char stripping
    now runs once for the whole dataset beforehand, see
    _step_special_chars_all — no longer a sub-step here.)"""
    _step_casing(cleaned, changes, col, profile, cc)
    canonical = profile.get("canonical")
    if canonical:
        _apply_fuzzy(cleaned, col, canonical, changes, reviews, "CATEGORY_STANDARDIZED", cc)


def _step_date(cleaned: pd.DataFrame, changes: dict, reviews: dict, enabled_rules: dict[str, list] | None = None) -> None:
    """STEP 13 – Auto-detect and standardise date columns.

    Scans every column heuristically rather than a fixed schema (dates can
    live under any column name), so unlike the _SCHEMA-driven steps this
    can't be gated by "is this column in the schema" — instead it checks
    enabled_rules directly: a column only gets rewritten if the user
    actually dragged the "Date" rule onto it in Column Rule Preview.
    """
    skip_hints = ("uuid", "cnic", "phone", "mobile", "account",
                  "amount", "count", "number", "ip", "long", "lat")
    for c in cleaned.columns:
        if enabled_rules is not None and "DATE_STANDARDIZED" not in (enabled_rules.get(c) or []):
            continue
        n = _norm_col(c)
        if any(x in n for x in skip_hints):
            continue
        # PERF: build the 2000-row detection sample from CHUNKS of the column
        # instead of dropna().astype(str) over all 690k rows for every column
        # — exact same sample (first 2000 qualifying values in row order).
        col_full = cleaned[c]
        parts = []
        collected = 0
        for lo in range(0, len(col_full), 20_000):
            chunk = col_full.iloc[lo:lo + 20_000].dropna().astype(str).str.strip()
            chunk = chunk[~chunk.str.lower().isin(NULL_TOKENS)]
            if len(chunk):
                parts.append(chunk)
                collected += len(chunk)
            if collected >= 2000:
                break
        sample = pd.concat(parts).head(2000) if parts else pd.Series([], dtype=str)
        if sample.empty:
            continue
        time_only   = sample.str.match(_TIME_ONLY_RE, na=False).mean()
        date_signal = sample.str.contains(_DATE_SIGNAL_RE, regex=True, na=False).mean()
        if time_only >= 0.80 and date_signal < 0.20:
            continue
        sample_clean  = sample.str.replace(_TZ_SUFFIX_RE, "", regex=True)
        parsed_sample = pd.to_datetime(sample_clean, errors="coerce", dayfirst=False, format="mixed")
        parse_rate    = parsed_sample.notna().mean()
        name_hint     = any(h in n for h in ("date", "dob", "created", "updated")) or n.endswith("_at")
        if not ((date_signal >= 0.60 and parse_rate >= 0.70)
                or (name_hint and date_signal >= 0.25 and parse_rate >= 0.60)):
            continue
        before    = cleaned[c].copy()
        before_stripped = before.astype("string").str.replace(_TZ_SUFFIX_RE.pattern, "", regex=True)

        # PERF: parse UNIQUE values then gather back via factorize codes —
        # date columns repeat heavily (a few thousand distinct dates across
        # 690k rows), and format="mixed" infers the format per element
        # anyway, so parsing each distinct string once yields IDENTICAL
        # results to parsing the full column, at a tiny fraction of the cost.
        codes, uniq = pd.factorize(before_stripped)
        uniq_ser    = pd.Series(np.asarray(uniq, dtype=object))
        parsed_u    = pd.to_datetime(uniq_ser, errors="coerce", dayfirst=False, format="mixed")
        formatted_u = parsed_u.dt.strftime(AUTO_DATE_FORMAT)

        ok_u  = parsed_u.notna().to_numpy()
        fmt_u = formatted_u.to_numpy(dtype=object)
        parsed_ok_np = np.zeros(len(codes), dtype=bool)
        formatted_np = np.full(len(codes), None, dtype=object)
        hit = codes >= 0
        parsed_ok_np[hit] = ok_u[codes[hit]]
        formatted_np[hit] = fmt_u[codes[hit]]

        parsed_ok = pd.Series(parsed_ok_np, index=before.index)
        formatted = pd.Series(formatted_np, index=before.index)
        valid     = before.notna() & parsed_ok
        mask      = valid & (before.astype("string") != formatted.astype("string")).fillna(False)
        if mask.any():
            idxs = cleaned.index[mask]
            cleaned.loc[idxs, c] = formatted.loc[idxs].astype(object)
            _add_clean(changes, idxs, c, formatted, "DATE_STANDARDIZED")
        bad = before.notna() & ~parsed_ok & ~_null_mask_plain(before)
        if bad.any():
            _add_review(reviews, cleaned.index[bad], c, before)


def _null_mask_plain(s: pd.Series) -> pd.Series:
    ss = s.astype("string")
    return s.isna() | ss.str.strip().str.lower().isin(NULL_TOKENS)


def _step_bank(cleaned: pd.DataFrame, changes: dict, enabled_rules: dict[str, list] | None = None) -> None:
    """STEP 14 – Bank name standardisation.

    Same rationale as _step_date: bank-name columns are found by heuristic
    name-hint, not a fixed schema, so gating happens per-column against
    enabled_rules directly rather than a col-in-schema check.
    """
    canonical_banks = list(BANK_NAMES.keys()) if isinstance(BANK_NAMES, dict) else list(BANK_NAMES or [])
    if not canonical_banks:
        return
    alias_norm = {re.sub(r"[^a-z0-9]+", " ", str(k).lower()).strip(): v for k, v in BANK_ALIAS_MAP.items()}
    choices    = tuple(sorted(canonical_banks))
    bank_hints = ("bank", "payment_by", "ifis", "ifi", "donor", "funding")
    for c in cleaned.columns:
        if enabled_rules is not None and "BANK_STANDARDIZED" not in (enabled_rules.get(c) or []):
            continue
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


# ═══════════════════════════════════════════════════════════════════════════════
# PREDEFINED VALIDATION RULES
# ═══════════════════════════════════════════════════════════════════════════════

def _find_col(df: pd.DataFrame, hints: tuple[str, ...]) -> str | None:
    """Return the first column whose normalised name matches any hint. Case-insensitive."""
    cols_norm = {_norm_col(c): c for c in df.columns}
    for hint in hints:
        h = _norm_col(hint)
        if h in cols_norm:
            return cols_norm[h]
        # partial match: hint contained in col name
        for cn, orig in cols_norm.items():
            if h in cn or cn in h:
                return orig
    return None


def _parse_date_series(s: pd.Series) -> pd.Series:
    """Parse a string/object Series to datetime64, accepting multiple formats."""
    stripped = s.fillna("").astype(str).str.strip().str.replace(_TZ_SUFFIX_RE, "", regex=True)
    return pd.to_datetime(stripped, errors="coerce", dayfirst=True, format="mixed")


def _is_null_vec(s: pd.Series) -> pd.Series:
    """Vectorised null check consistent with NULL_TOKENS."""
    return s.isna() | s.fillna("").astype(str).str.strip().str.lower().isin(NULL_TOKENS)


def run_predefined_validation(
    df: pd.DataFrame,
    dataset_type: str,
    existing_reviews: dict | None = None,
) -> tuple[dict[str, Any], list[dict]]:
    """
    Run all 12 predefined validation rules against a cleaned DataFrame.

    Parameters
    ----------
    df             : cleaned DataFrame (output of any clean_dataframe_* function)
    dataset_type   : "beneficiary" | "banks" | "financials" | "certificates"
    existing_reviews : optional reviews dict already accumulated during cleaning
                       — used to merge flagged cells so the report is complete

    Returns
    -------
    rule_failures  : dict mapping rule_id (str) → list of row indices that failed
    filter_results : list of dicts in the same shape as validation_engine output:
                     [{"label": str, "cond": str, "flagged_count": int}, ...]
                     Row-index lists are intentionally not included — rows are
                     identified by UUID (already unique) in the report/cleaned
                     output, so a redundant Excel row-index list per filter
                     was pure overhead.
    """
    today      = pd.Timestamp(date.today())

    # Per-cell flag accumulator: row_index → {col: "RULE_<id>"}
    # These get merged into the __validation_status__ column downstream.
    cell_flags: dict[int, dict[str, str]] = {}
    filter_results: list[dict] = []

    def _flag(mask: pd.Series, col: str, rule_id: str) -> None:
        """Record rule failure for every True row in mask."""
        idxs = df.index[mask].tolist()
        for i in idxs:
            cell_flags.setdefault(int(i), {})[col] = rule_id
        filter_results.append({
            "label":         rule_id,
            "cond":          "predefined",
            "flagged_count": int(mask.sum()),
        })

    # ── Resolve key columns ────────────────────────────────────────────────────
    uuid_col     = _find_col(df, _UUID_HINTS)
    cnic_col     = _find_col(df, _CNIC_HINTS)
    district_col = _find_col(df, _DISTRICT_HINTS)
    tehsil_col   = _find_col(df, _TEHSIL_HINTS)
    uc_col       = _find_col(df, _UC_HINTS)
    ip_col       = _find_col(df, _IP_HINTS)
    bank_col     = _find_col(df, _BANK_HINTS)
    stage_col    = _find_col(df, _STAGE_HINTS)

    # ── RULE 1: UUID mandatory + valid format ──────────────────────────────────
    # Format: all-digit, not all-same-digit (e.g. 000000, 111111…), not empty.
    if uuid_col:
        s      = df[uuid_col].fillna("").astype(str).str.strip()
        is_null = _is_null_vec(df[uuid_col])
        digits  = s.str.replace(_RE_NON_DIGIT.pattern, "", regex=True)

        # Scientific notation → integer string
        sci = s.str.match(r"[+-]?\d+\.?\d*[eE][+-]?\d+", na=False)
        if sci.any():
            digits = digits.copy()
            digits[sci] = s[sci].apply(
                lambda v: str(int(float(v))) if v else ""
            )

        all_same  = digits.str.fullmatch(r"(\d)\1+", na=False)   # 000…, 111…, 999…
        non_digit = is_null | ~s.str.fullmatch(r"\d+(\.\d+)?", na=False) & ~sci
        bad_fmt   = is_null | all_same | non_digit

        if bad_fmt.any():
            _flag(bad_fmt, uuid_col, "R01_UUID_INVALID_FORMAT")

    # ── RULE 2: UUID unique per housing unit ───────────────────────────────────
    if uuid_col:
        s        = df[uuid_col].fillna("").astype(str).str.strip()
        not_null = ~_is_null_vec(df[uuid_col])
        dup_mask = not_null & s.duplicated(keep=False)
        if dup_mask.any():
            _flag(dup_mask, uuid_col, "R02_UUID_DUPLICATE")

    # ── RULE 3: CNIC mandatory, numeric, exactly 13 digits ────────────────────
    if cnic_col:
        raw      = df[cnic_col].fillna("").astype(str).str.strip()
        is_null  = _is_null_vec(df[cnic_col])

        # Expand scientific notation
        sci = raw.str.match(r"[+-]?\d+\.?\d*[eE][+-]?\d+", na=False)
        digits = raw.str.replace(_RE_NON_DIGIT.pattern, "", regex=True)
        if sci.any():
            digits = digits.copy()
            digits[sci] = raw[sci].apply(
                lambda v: str(int(float(v))) if v else ""
            )

        wrong_len   = digits.str.len() != 13
        all_same_c  = digits.str.fullmatch(r"(\d)\1{12}", na=False)
        bad_cnic    = is_null | wrong_len | all_same_c | digits.isin(CNIC_FAKE_VALUES)
        if bad_cnic.any():
            _flag(bad_cnic, cnic_col, "R03_CNIC_INVALID")

    # ── RULE 4: UUID ↔ CNIC one-to-one (each UUID → exactly one CNIC) ─────────
    if uuid_col and cnic_col:
        s_uuid = df[uuid_col].fillna("").astype(str).str.strip()
        s_cnic = df[cnic_col].fillna("").astype(str).str.strip()
        both   = ~_is_null_vec(df[uuid_col]) & ~_is_null_vec(df[cnic_col])

        # Count distinct CNICs per UUID
        pair_df    = df.loc[both, [uuid_col, cnic_col]].copy()
        pair_df.columns = ["_uuid", "_cnic"]
        cnic_per_uuid = pair_df.groupby("_uuid")["_cnic"].nunique()
        bad_uuids     = set(cnic_per_uuid[cnic_per_uuid > 1].index.tolist())

        mask = both & s_uuid.isin(bad_uuids)
        if mask.any():
            _flag(mask, uuid_col, "R04_UUID_MULTIPLE_CNICS")

    # ── RULE 5: CNIC not shared across multiple households ─────────────────────
    if uuid_col and cnic_col:
        s_uuid = df[uuid_col].fillna("").astype(str).str.strip()
        s_cnic = df[cnic_col].fillna("").astype(str).str.strip()
        both   = ~_is_null_vec(df[uuid_col]) & ~_is_null_vec(df[cnic_col])

        pair_df    = df.loc[both, [uuid_col, cnic_col]].copy()
        pair_df.columns = ["_uuid", "_cnic"]
        uuid_per_cnic = pair_df.groupby("_cnic")["_uuid"].nunique()
        shared_cnics  = set(uuid_per_cnic[uuid_per_cnic > 1].index.tolist())

        mask = both & s_cnic.isin(shared_cnics)
        if mask.any():
            _flag(mask, cnic_col, "R05_CNIC_SHARED_HOUSEHOLDS")

    # ── RULE 6: Mandatory fields not empty ────────────────────────────────────
    # Build list of mandatory columns present in this dataframe
    mandatory_cols: list[tuple[str, str]] = []   # (col_name, rule_label)
    if uuid_col:     mandatory_cols.append((uuid_col,     "R06_MISSING_UUID"))
    if cnic_col:     mandatory_cols.append((cnic_col,     "R06_MISSING_CNIC"))
    if district_col: mandatory_cols.append((district_col, "R06_MISSING_DISTRICT"))
    if tehsil_col:   mandatory_cols.append((tehsil_col,   "R06_MISSING_TEHSIL"))
    if uc_col:       mandatory_cols.append((uc_col,       "R06_MISSING_UC"))
    if ip_col:       mandatory_cols.append((ip_col,       "R06_MISSING_IP"))
    if bank_col:     mandatory_cols.append((bank_col,     "R06_MISSING_BANK"))
    if stage_col:    mandatory_cols.append((stage_col,    "R06_MISSING_STAGE"))

    for col, label in mandatory_cols:
        null_mask = _is_null_vec(df[col])
        if null_mask.any():
            _flag(null_mask, col, label)

    # ── RULE 7: All dates valid and in DD-MM-YYYY format ──────────────────────
    # Scan every column whose name suggests it is a date.
    date_col_hints = ("date", "dob", "_at", "submitted", "released",
                      "withdraw", "completion", "opening", "chq")
    date_cols_found: list[str] = [
        c for c in df.columns
        if any(h in _norm_col(c) for h in date_col_hints)
        and not any(s in _norm_col(c) for s in ("uuid", "cnic", "account", "number"))
    ]

    parsed_dates: dict[str, pd.Series] = {}   # col → parsed Timestamp Series

    for col in date_cols_found:
        raw      = df[col].fillna("").astype(str).str.strip()
        not_null = ~_is_null_vec(df[col])
        if not not_null.any():
            continue

        parsed = _parse_date_series(df[col])
        parsed_dates[col] = parsed

        # Invalid dates: non-null but unparseable
        bad_parse = not_null & parsed.isna()
        if bad_parse.any():
            _flag(bad_parse, col, "R07_DATE_INVALID")

        # Wrong format: parseable but not already DD-MM-YYYY
        valid = not_null & parsed.notna()
        if valid.any():
            expected_fmt = parsed[valid].dt.strftime(_DD_MM_YYYY)
            actual_str   = raw[valid]
            wrong_fmt    = valid.copy()
            wrong_fmt[valid] = (actual_str.values != expected_fmt.values)
            if wrong_fmt.any():
                _flag(wrong_fmt, col, "R07_DATE_WRONG_FORMAT")

    # ── RULE 8: No future dates ────────────────────────────────────────────────
    future_skip = ("dob", "birth")   # DOB can legitimately be in the past only, skip future check
    for col, parsed in parsed_dates.items():
        if any(h in _norm_col(col) for h in future_skip):
            continue
        not_null    = ~_is_null_vec(df[col])
        valid       = not_null & parsed.notna()
        future_mask = valid & (parsed > today)
        if future_mask.any():
            _flag(future_mask, col, "R08_DATE_FUTURE")

    # ── RULE 9: Stage date ordering ───────────────────────────────────────────
    # Banks: tranche dates must be ascending; stage completion dates must be ascending.
    def _check_date_sequence(seq: list[str]) -> None:
        """Flag rows where date[i] >= date[i+1] for consecutive date columns."""
        present = [c for c in seq if c in df.columns and c in parsed_dates]
        for earlier, later in zip(present, present[1:]):
            p_early = parsed_dates[earlier]
            p_late  = parsed_dates[later]
            both_valid = p_early.notna() & p_late.notna()
            out_of_order = both_valid & (p_early >= p_late)
            if out_of_order.any():
                _flag(out_of_order, later,
                      f"R09_DATE_ORDER_{_norm_col(earlier).upper()}_BEFORE_{_norm_col(later).upper()}")

    for seq in _STAGE_DATE_SEQUENCE:
        _check_date_sequence(list(seq))
    for seq in _TRANCHE_DATE_SEQUENCE:
        _check_date_sequence(list(seq))
    for seq in _CERT_DATE_SEQUENCE:
        _check_date_sequence(list(seq))

    # ── RULE 10: Construction stage dependency ────────────────────────────────
    # e.g. lintel_completion_date must not exist without plinth_completion_date.
    for later_col, required_cols in _STAGE_DEPENDENCIES.items():
        if later_col not in df.columns or later_col not in parsed_dates:
            continue
        later_present = parsed_dates[later_col].notna() & ~_is_null_vec(df[later_col])
        for req_col in required_cols:
            if req_col not in df.columns:
                # Required stage col absent from file entirely → always fail if later exists
                if later_present.any():
                    _flag(later_present, later_col,
                          f"R10_STAGE_DEP_MISSING_{_norm_col(req_col).upper()}")
                continue
            req_present = parsed_dates.get(req_col, pd.Series(pd.NaT, index=df.index)).notna() \
                          & ~_is_null_vec(df[req_col])
            # Later stage present but required earlier stage absent
            violated = later_present & ~req_present
            if violated.any():
                _flag(violated, later_col,
                      f"R10_STAGE_DEP_MISSING_{_norm_col(req_col).upper()}")

    # ── RULE 11: Completed stage must have ITVC verification ──────────────────
    for stage_date_col, verif_col in _ITVC_REQUIREMENTS.items():
        if stage_date_col not in df.columns or stage_date_col not in parsed_dates:
            continue
        stage_present = parsed_dates[stage_date_col].notna() & ~_is_null_vec(df[stage_date_col])
        if not stage_present.any():
            continue
        if verif_col not in df.columns:
            # Verification column entirely absent → flag all rows with stage date
            _flag(stage_present, stage_date_col,
                  f"R11_ITVC_MISSING_{_norm_col(verif_col).upper()}")
            continue
        verif_null = _is_null_vec(df[verif_col])
        missing_verif = stage_present & verif_null
        if missing_verif.any():
            _flag(missing_verif, verif_col,
                  f"R11_ITVC_MISSING_{_norm_col(verif_col).upper()}")

    # ── RULE 12: Stage status ↔ date consistency ──────────────────────────────
    for status_col, date_col, completed_vals, incomplete_vals in _STAGE_STATUS_DATE_PAIRS:
        if status_col not in df.columns:
            continue

        status_s = df[status_col].fillna("").astype(str).str.strip().str.lower()
        status_not_null = ~_is_null_vec(df[status_col])

        is_completed   = status_s.isin(completed_vals)
        is_incomplete  = status_s.isin(incomplete_vals)

        # Case A: status = "completed/disbursed" but date column empty/absent
        if date_col in df.columns:
            date_present = ~_is_null_vec(df[date_col])
            date_col_parsed = parsed_dates.get(date_col)
            if date_col_parsed is not None:
                date_present = date_present & date_col_parsed.notna()

            # Completed status but no date
            completed_no_date = status_not_null & is_completed & ~date_present
            if completed_no_date.any():
                _flag(completed_no_date, status_col,
                      f"R12_STATUS_DATE_MISMATCH_NO_DATE_{_norm_col(status_col).upper()}")

            # Incomplete status but date exists
            incomplete_with_date = status_not_null & is_incomplete & date_present
            if incomplete_with_date.any():
                _flag(incomplete_with_date, date_col,
                      f"R12_STATUS_DATE_MISMATCH_HAS_DATE_{_norm_col(status_col).upper()}")

        else:
            # date col not present at all — flag completed statuses
            completed_no_date = status_not_null & is_completed
            if completed_no_date.any():
                _flag(completed_no_date, status_col,
                      f"R12_STATUS_DATE_COL_ABSENT_{_norm_col(date_col).upper()}")

    return cell_flags, filter_results


def _apply_predefined_validation_to_df(
    df: pd.DataFrame,
    dataset_type: str,
    existing_reviews: dict | None = None,
) -> tuple[pd.DataFrame, dict]:
    """
    Run predefined rules and write results into __validation_status__ column.

    Returns
    -------
    validated_df    : df with __validation_status__ column populated / updated
    validation_summary : dict with total/passed/failed counts + filter_results list
    """
    cell_flags, filter_results = run_predefined_validation(
        df, dataset_type, existing_reviews
    )

    n = len(df)
    today_str = date.today().isoformat()

    # Build per-row status strings, merging with any existing __validation_status__
    existing_status: dict[int, dict] = {}
    if "__validation_status__" in df.columns:
        for pos, raw in enumerate(df["__validation_status__"].tolist()):
            if raw and str(raw).startswith("{"):
                try:
                    existing_status[pos] = json.loads(raw)
                except Exception:
                    pass

    status_col: list[str] = []
    passed = 0

    # PERF: the overwhelming majority of rows have no predefined-rule
    # failures and no pre-existing __validation_status__ entry (this is a
    # fresh pipeline run in the common case) — for those, the resulting
    # JSON string is IDENTICAL every time, so it's computed once here and
    # reused instead of re-building the dict + re-serializing it 340k times.
    _empty_pass_json = _fast_json_dumps(
        {"result": "PASS", "filters": [], "validated_at": today_str}
    )

    # Precompute column value arrays ONCE (vectorised) instead of df.iloc[pos][col]
    # per row, which is an extremely slow pandas scalar lookup at scale.
    # Convert pandas NA → None so the values stay JSON-serialisable.
    col_value_arrays: dict[str, list] = {}
    for _c in {c for flags in cell_flags.values() for c in flags}:
        if _c in df.columns:
            _arr = df[_c].astype("string").tolist()
            col_value_arrays[_c] = [None if v is pd.NA else v for v in _arr]

    for pos in range(n):
        row_flags = cell_flags.get(pos, {})

        if not row_flags and pos not in existing_status:
            status_col.append(_empty_pass_json)
            passed += 1
            continue

        failed_rules = list(row_flags.values())

        # Merge with any previously written validation status
        prev = existing_status.get(pos, {})
        prev_filters = prev.get("filters", [])
        prev_result  = prev.get("result", "PASS")

        new_filters = prev_filters + [
            {
                "label":    rule_id,
                "cond":     "predefined",
                "col":      col,
                "expected": None,
                "actual":   (col_value_arrays.get(col, [None] * n)[pos]
                             if col in col_value_arrays else None),
                "pass":     False,
            }
            for col, rule_id in row_flags.items()
        ]

        overall = "FAIL" if (failed_rules or prev_result == "FAIL") else "PASS"
        if overall == "PASS":
            passed += 1

        status_col.append(_fast_json_dumps(
            {"result": overall, "filters": new_filters, "validated_at": today_str}
        ))

    out = df.copy()
    out["__validation_status__"] = status_col

    failed = n - passed
    summary = {
        "total_rows":     n,
        "passed":         passed,
        "failed":         failed,
        "filter_results": filter_results,
    }
    return out, summary


# ── Shared fast result builder ────────────────────────────────────────────────
FAST_META_KEY = "__fast_meta__"


def _build_uuid_keys(original: pd.DataFrame, uuid_col: str | None) -> list[str]:
    """Vectorised version of the old per-row uuid-key loop (dedupe suffixes)."""
    n = len(original)
    if uuid_col and uuid_col in original.columns:
        s = original[uuid_col].astype("string").str.strip()
        base = s.copy()
        low = s.str.lower()
        bad = s.isna() | low.isin(NULL_TOKENS)
        if bad.any():
            base = base.astype(object)
            pos_bad = np.flatnonzero(bad.to_numpy())
            base.iloc[pos_bad] = [f"ROW_{i + 2}" for i in pos_bad]
        base = base.astype(str)
    else:
        base = pd.Series([f"ROW_{i + 2}" for i in range(n)], index=original.index, dtype=object)
    # occurrence counter per base uuid — first keeps base, repeats get suffix
    occ = base.groupby(base).cumcount().to_numpy()
    base_arr = base.to_numpy(dtype=object)
    out = base_arr.copy()
    rep = np.flatnonzero(occ > 0)
    for i in rep:
        out[i] = f"{base_arr[i]}__duplicate_row_{i + 2}"
    return out.tolist()


def _build_fast_result(
    original: pd.DataFrame,
    changes: "_CleanLog",
    reviews: "_CleanLog",
    duplicate_uuid_mask: pd.Series,
    duplicate_cnic_mask: pd.Series,
    uuid_col: str | None,
    predefined_summary: dict,
    step_timings: dict[str, float] | None = None,
) -> dict:
    """
    Lightweight result contract: no per-row dict materialisation (that used to
    cost seconds + hundreds of MB at 690k rows). routes_clean._summarise and
    output_writer.write_outputs both understand this shape and take
    vectorised paths; anything else still receives a plain dict and falls
    back to legacy behaviour automatically.
    """
    # Capture ORIGINAL values for exactly the touched cells (changes ∪
    # reviews), factorised — so the input frame doesn't have to stay alive
    # through validation + writing just to serve the report's
    # original_values. Lets the caller free the upload cache right after
    # cleaning (~1x dataset size off peak memory).
    originals = _CleanLog("review")
    col_cache: dict[str, pd.Series] = {}
    seen_cells: set[tuple[str, int]] = set()
    for log in (changes, reviews):
        for col, idx_arr, _, _ in log.records:
            if col not in original.columns:
                continue
            fresh = idx_arr
            if seen_cells:
                fresh = np.asarray(
                    [i for i in idx_arr.tolist() if (col, i) not in seen_cells],
                    dtype=np.int64,
                )
                if not len(fresh):
                    continue
            seen_cells.update((col, int(i)) for i in fresh.tolist())
            src_col = col_cache.get(col)
            if src_col is None:
                src_col = original[col]
                col_cache[col] = src_col
            originals.add(fresh, col, src_col.iloc[fresh], None)

    meta = {
        "n_rows":        len(original),
        "uuid_keys":     _build_uuid_keys(original, uuid_col),
        "dup_uuid":      duplicate_uuid_mask.to_numpy(dtype=bool),
        "dup_cnic":      duplicate_cnic_mask.to_numpy(dtype=bool),
        "changes":       changes,
        "reviews":       reviews,
        "originals":     originals,
        "step_timings":  step_timings or {},
    }
    return {
        FAST_META_KEY: meta,
        "__predefined_validation_summary__": predefined_summary,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE — BENEFICIARY
# ═══════════════════════════════════════════════════════════════════════════════

def clean_dataframe_fast(
    df: pd.DataFrame,
    uuid_column: str | None = None,
    global_rules: dict | None = None,
    run_predefined: bool = False,
    case_overrides: dict[str, str] | None = None,
    enabled_rules: dict[str, list] | None = None,
    progress_cb: "Callable[..., None] | None" = None,
    regex_rules: dict | None = None,
    dtype_rules: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, dict]:
    import time as _time
    # `df` is only read from here on (every mutation goes to `cleaned`, a
    # deep copy) — reuse it as `original` instead of paying a second full
    # deep copy of the dataset. Saves ~1x dataset size in peak memory.
    original = df
    cleaned  = df.copy(deep=True)
    changes = _CleanLog("clean")
    reviews = _CleanLog("review")
    cc = _ColCache()
    _co = case_overrides or {}
    _T: dict[str, float] = {}          # real per-step wall-clock seconds

    def _timed(key: str, fn, *a, **k):
        if progress_cb is not None:
            try:
                progress_cb("start", key)
            except Exception:
                pass
        t0 = _time.perf_counter()
        r = fn(*a, **k)
        dt = _time.perf_counter() - t0
        _T[key] = _T.get(key, 0.0) + dt
        if progress_cb is not None:
            try:
                progress_cb("end", key, dt)
            except Exception:
                pass
        return r

    _gr = global_rules or {}
    # Regex/mapping "Regex Clean" rules run FIRST — they operate on raw
    # values, before trim/null/casing/anything else touches them, exactly
    # matching when they used to run as a separate pre-flight phase. Now a
    # real, timed, tracked step in the same hierarchy as everything below,
    # instead of invisible extra requests before the pipeline even started.
    if regex_rules:
        _timed("regex", _step_regex_rules, cleaned, changes, reviews, regex_rules, cc)
    if _gr.get("trim", True):
        _timed("trim", _step_trim, cleaned, changes, cc)
    if _gr.get("null", True):
        _timed("null", _step_null_standardize, cleaned, changes, reviews, cc)
    # "special" global rule — same shape as trim/null above: one pass over
    # every column in the dataset, gated only by this toggle. No longer
    # nested inside the geo/catdate schema steps below (that used to fire
    # it once per geo/catdate column, which both scrambled the popup's step
    # order — it looked like it ran "whenever geo/catdate happened to run"
    # instead of right after trim/null — and made the row flip done→running
    # repeatedly, once per column).
    if _gr.get("special", True):
        _timed("special", _step_special_chars_all, cleaned, changes, cc)

    duplicate_cnic_mask  = pd.Series(False, index=cleaned.index)
    # The user's explicit UUID/ID picker selection wins outright — computed
    # directly here, unconditional on schema matching or Column Rules
    # gating. See _resolve_explicit_uuid's docstring for why this used to
    # be silently dropped.
    uuid_col, duplicate_uuid_mask = _resolve_explicit_uuid(cleaned, uuid_column)
    if duplicate_uuid_mask is None:
        duplicate_uuid_mask = pd.Series(False, index=cleaned.index)

    # Group eligible columns by the REAL step key each will run under (same
    # classification the dispatch below uses — numeric columns split into
    # "unique" vs "dtype" by profile flags, everything else maps 1:1 from
    # col_type), then process one group at a time in the same order the
    # frontend popup lists them. Previously this iterated _SCHEMA in
    # whatever order columns happened to be declared in, so e.g. a CNIC
    # column, then a date column, then a geo column, then another CNIC
    # column would run back-to-back — live progress genuinely interleaved
    # ("geo" done → "catdate" active → "geo" active again) because that's
    # legitimately the order the work happened in, not a display bug. Same
    # total work, same per-column results — grouping first just means the
    # real execution order now matches a clean top-to-bottom checklist.
    _STEP_GROUP_ORDER = ["cnic", "cell", "gender", "geo", "catdate", "dtype", "unique"]
    _groups: dict[str, list[tuple[str, dict]]] = {k: [] for k in _STEP_GROUP_ORDER}
    for col, profile in _SCHEMA.items():
        if col not in cleaned.columns:
            continue
        # PERF/CORRECTNESS: only run a column's schema-driven cleaning step
        # if the user actually configured a Column Rule for it in the
        # Column Rule Preview screen. enabled_rules is the frontend's
        # state.columnRules — {col: [ruleKey, ...]} for EVERY column, empty
        # list for ones with nothing assigned. enabled_rules=None (not
        # passed at all) preserves the old always-on behaviour for any
        # caller that hasn't been updated to send it.
        if enabled_rules is not None and not enabled_rules.get(col):
            continue
        col_type = profile.get("type", "string")
        if col_type == "cnic":
            _groups["cnic"].append((col, profile))
        elif col_type == "numeric":
            key = "unique" if (profile.get("non_null") or profile.get("unique")) else "dtype"
            _groups[key].append((col, profile))
        elif col_type == "float":
            _groups["dtype"].append((col, profile))
        elif col_type in ("bool", "gender"):
            _groups["gender"].append((col, profile))
        elif col_type == "cell_no":
            _groups["cell"].append((col, profile))
        elif col_type == "geo":
            _groups["geo"].append((col, profile))
        elif col_type == "string":
            _groups["catdate"].append((col, profile))

    for step_key in _STEP_GROUP_ORDER:
        for col, profile in _groups[step_key]:
            col_type = profile.get("type", "string")
            if col_type == "cnic":
                cnic_dup_mask       = _timed("cnic", _step_cnic, cleaned, changes, reviews, col, profile, cc)
                duplicate_cnic_mask = duplicate_cnic_mask | cnic_dup_mask
            elif col_type == "numeric":
                if profile.get("non_null") or profile.get("unique"):
                    dup_mask = _timed("unique", _step_uuid, cleaned, changes, reviews, col, profile, cc)
                    if profile.get("unique") and (not uuid_column or col == uuid_column):
                        duplicate_uuid_mask = dup_mask
                        uuid_col = uuid_column or col
                else:
                    _timed("dtype", _step_numeric_type, cleaned, reviews, col, cc)
            elif col_type == "float":
                _timed("dtype", _step_float_coord, cleaned, reviews, col, profile.get("lat_lon", "lat"), cc)
            elif col_type == "bool":
                _timed("gender", _step_bool, cleaned, changes, reviews, col, cc)
            elif col_type == "gender":
                _timed("gender", _step_gender, cleaned, changes, reviews, col, cc)
            elif col_type == "cell_no":
                _timed("cell", _step_cell_no, cleaned, changes, reviews, col, cc)
            elif col_type == "geo":
                _timed("geo", _step_geo, cleaned, changes, reviews, col, profile, cc)
            elif col_type == "string":
                eff_profile = {**profile, "case_style": _co[col]} if col in _co else profile
                _timed("catdate", _step_string_category, cleaned, changes, reviews, col, eff_profile, cc)

    # Columns the user targeted with a case-style rule that aren't part of
    # the static string schema above (e.g. free-text columns) still get the
    # casing applied on their own.
    for col, style in _co.items():
        if col in cleaned.columns and col not in _SCHEMA:
            _timed("case", _step_casing, cleaned, changes, col, {"case_style": style}, cc)

    # DATATYPE_CHECK column rule — user explicitly picked "check this column
    # as <type>" in Column Rule Preview (state.dtypeRules on the frontend).
    # Runs for exactly the columns configured, independent of the static
    # schema/enabled_rules gating above, since this is its own opt-in rule.
    for col, expected_type in (dtype_rules or {}).items():
        if col in cleaned.columns:
            _timed("dtype_check", _step_dtype_check, cleaned, reviews, col, expected_type, cc)

    _timed("date", _step_date, cleaned, changes, reviews, enabled_rules)
    _timed("bank",  _step_bank, cleaned, changes, enabled_rules)
    # _step_duplicate_rows / _step_type_mismatch used to run here
    # unconditionally on every pipeline call — flagging full-duplicate rows
    # and heuristic type mismatches with no way to turn them off and no
    # frontend control for them. Removed: nothing runs here now unless the
    # user configured it (Column Rules / global rules / validation filters).

    # ── PREDEFINED VALIDATION (auto rules) — OFF by default ───────────────────
    # Previously this always ran and auto-flagged rows against rules R01–R12,
    # which (a) polluted user-configured filter results and (b) cost ~15s/200k
    # rows in the per-row materialisation loop. It is now opt-in.
    if run_predefined:
        cleaned, _predefined_summary = _apply_predefined_validation_to_df(
            cleaned, "beneficiary", existing_reviews=reviews
        )
    else:
        _predefined_summary = {
            "total_rows": len(cleaned), "passed": len(cleaned),
            "failed": 0, "filter_results": [],
        }

    # ── Build response (fast contract — no per-row dict materialisation) ──────
    response = _build_fast_result(
        original, changes, reviews,
        duplicate_uuid_mask, duplicate_cnic_mask,
        uuid_col if (uuid_col and uuid_col in cleaned.columns) else None,
        _predefined_summary, step_timings=_T,
    )
    return cleaned, response


# ═══════════════════════════════════════════════════════════════════════════════
# BANKS / FINANCIALS SCHEMA & PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

_TRANCHE_STATUS_CANONICAL = ["Disbursed", "Pending", "Cleared", "Returned", "Processing"]

_BANKS_SCHEMA: dict[str, dict] = {
    "Urban Unit #": {"type": "numeric", "non_null": True, "unique": True},
    "CNIC":         {"type": "cnic",    "non_null": True, "unique": True},
    "Title of Account": {"type": "string", "title": True},
    "Account Number":   {"type": "bank_account"},
    "Bank Name":        {"type": "bank"},
    "Chq date 1st":      {"type": "date"},
    "Chq date 2nd":      {"type": "date"},
    "Chq date 3rd":      {"type": "date"},
    "4th Inst Chq Date": {"type": "date"},
    "Instalment 1 Processed": {"type": "string", "title": True, "canonical": _TRANCHE_STATUS_CANONICAL},
    "Instalment 2 Processed": {"type": "string", "title": True, "canonical": _TRANCHE_STATUS_CANONICAL},
    "Instalment 3 Processed": {"type": "string", "title": True, "canonical": _TRANCHE_STATUS_CANONICAL},
    "Instalment 4 Processed": {"type": "string", "title": True, "canonical": _TRANCHE_STATUS_CANONICAL},
}

_IBAN_PK_RE = re.compile(r"^PK\d{2}[A-Z]{4}\d{16}$")


def _step_bank_account(
    cleaned: pd.DataFrame, changes: dict, reviews: dict,
    col: str, cc: _ColCache,
) -> None:
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    s       = cc.s(col, before).str.strip().str.upper().str.replace(r"\s+", "", regex=True)
    changed = nonnull & (cc.s(col, before).str.strip() != s).fillna(False)
    if changed.any():
        idxs = cleaned.index[changed]
        cleaned.loc[idxs, col] = s.loc[idxs].astype(object)
        _add_clean(changes, idxs, col, s, "BANK_ACCOUNT_NORMALISED")
        cc.invalidate(col)
    invalid = nonnull & ~s.str.match(_IBAN_PK_RE, na=False)
    if invalid.any():
        _add_review(reviews, cleaned.index[invalid], col, before)


def _step_date_col(
    cleaned: pd.DataFrame, changes: dict, reviews: dict,
    col: str, cc: _ColCache,
) -> None:
    before  = cleaned[col].copy()
    nonnull = ~cc.null(col, before)
    if not nonnull.any():
        return
    before_stripped = before.astype("string").str.replace(_TZ_SUFFIX_RE, "", regex=True)
    parsed    = pd.to_datetime(before_stripped, errors="coerce", dayfirst=False, format="mixed")
    formatted = parsed.dt.strftime(AUTO_DATE_FORMAT)
    valid = nonnull & parsed.notna()
    mask  = valid & (before.astype("string") != formatted.astype("string")).fillna(False)
    if mask.any():
        idxs = cleaned.index[mask]
        cleaned.loc[idxs, col] = formatted.loc[idxs].astype(object)
        _add_clean(changes, idxs, col, formatted, "DATE_STANDARDIZED")
        cc.invalidate(col)
    bad = nonnull & parsed.isna()
    if bad.any():
        _add_review(reviews, cleaned.index[bad], col, before)


def _step_bank_name(
    cleaned: pd.DataFrame, changes: dict,
    col: str, cc: _ColCache,
) -> None:
    canonical_banks = list(BANK_NAMES.keys()) if isinstance(BANK_NAMES, dict) else []
    if not canonical_banks:
        return
    alias_norm = {re.sub(r"[^a-z0-9]+", " ", str(k).lower()).strip(): v
                  for k, v in BANK_ALIAS_MAP.items()}
    choices    = tuple(sorted(canonical_banks))
    before     = cleaned[col].copy()
    s          = before.astype("string").str.strip()
    mapping: dict[str, str] = {}
    for val in s.dropna().unique().tolist():
        raw        = str(val)
        low        = raw.lower().strip()
        norm       = re.sub(r"[^a-z0-9]+", " ", low).strip()
        short_norm = re.sub(r"\b(ltd|limited|bank)\b", "", norm).strip()
        fixed      = (BANK_ALIAS_MAP.get(low)
                      or alias_norm.get(norm)
                      or alias_norm.get(short_norm))
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
        _assign_masked(cleaned, col, mask, after)
        _add_clean(changes, idxs, col, after, "BANK_STANDARDIZED")


def clean_dataframe_banks(
    df: pd.DataFrame,
    uuid_column: str | None = None,
    global_rules: dict | None = None,
    run_predefined: bool = False,
    case_overrides: dict[str, str] | None = None,
    enabled_rules: dict[str, list] | None = None,
    progress_cb: "Callable[..., None] | None" = None,
    regex_rules: dict | None = None,
    dtype_rules: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Banks / Financials cleaning pipeline.

    NOTE: this pipeline doesn't have per-step timing instrumentation like
    clean_dataframe_fast does (no _timed() wrapper around individual steps),
    so progress_cb only fires a single "clean" start/end pair around the
    whole function rather than one event per sub-step. Real total time, just
    coarser live granularity than the beneficiary pipeline.
    """
    import time as _time
    if progress_cb is not None:
        try: progress_cb("start", "clean")
        except Exception: pass
    _t_all0 = _time.perf_counter()
    # `df` is only read from here on (every mutation goes to `cleaned`, a
    # deep copy) — reuse it as `original` instead of paying a second full
    # deep copy of the dataset. Saves ~1x dataset size in peak memory.
    original = df
    cleaned  = df.copy(deep=True)
    changes = _CleanLog("clean")
    reviews = _CleanLog("review")
    cc = _ColCache()
    _co = case_overrides or {}

    _gr = global_rules or {}
    if regex_rules:
        _step_regex_rules(cleaned, changes, reviews, regex_rules, cc)
    if _gr.get("trim", True):
        _step_trim(cleaned, changes, cc)
    if _gr.get("null", True):
        _step_null_standardize(cleaned, changes, reviews, cc)
    if _gr.get("special", True):
        _step_special_chars_all(cleaned, changes, cc)

    duplicate_uuid_mask = pd.Series(False, index=cleaned.index)
    duplicate_cnic_mask = pd.Series(False, index=cleaned.index)
    uuid_col: str | None = uuid_column or "Urban Unit #"
    _explicit_uuid, _explicit_dup = _resolve_explicit_uuid(cleaned, uuid_column)
    duplicate_uuid_mask = _explicit_dup if _explicit_dup is not None else pd.Series(False, index=cleaned.index)

    for col, profile in _BANKS_SCHEMA.items():
        if col not in cleaned.columns:
            continue
        # See clean_dataframe_fast for the full rationale — only run a
        # column's schema-driven step if the user configured a Column Rule
        # for it; enabled_rules=None preserves old always-on behaviour.
        if enabled_rules is not None and not enabled_rules.get(col):
            continue
        col_type = profile.get("type")

        if col_type == "numeric":
            dup_mask = _step_uuid(cleaned, changes, reviews, col, profile, cc)
            if profile.get("unique") and (not uuid_column or col == uuid_column):
                duplicate_uuid_mask = dup_mask
                uuid_col = uuid_column or col
        elif col_type == "cnic":
            cnic_dup_mask       = _step_cnic(cleaned, changes, reviews, col, profile, cc)
            duplicate_cnic_mask = duplicate_cnic_mask | cnic_dup_mask
        elif col_type == "bank_account":
            _step_bank_account(cleaned, changes, reviews, col, cc)
        elif col_type == "bank":
            _step_bank_name(cleaned, changes, col, cc)
        elif col_type == "date":
            _step_date_col(cleaned, changes, reviews, col, cc)
        elif col_type == "string":
            eff_profile = {**profile, "case_style": _co[col]} if col in _co else profile
            _step_string_category(cleaned, changes, reviews, col, eff_profile, cc)

    for col, style in _co.items():
        if col in cleaned.columns and col not in _BANKS_SCHEMA:
            _step_casing(cleaned, changes, col, {"case_style": style}, cc)

    for col, expected_type in (dtype_rules or {}).items():
        if col in cleaned.columns:
            _step_dtype_check(cleaned, reviews, col, expected_type, cc)

    # ── PREDEFINED VALIDATION (auto rules) — OFF by default ───────────────────
    if run_predefined:
        cleaned, _predefined_summary = _apply_predefined_validation_to_df(
            cleaned, "banks", existing_reviews=reviews
        )
    else:
        _predefined_summary = {
            "total_rows": len(cleaned), "passed": len(cleaned),
            "failed": 0, "filter_results": [],
        }

    # ── Build response (fast contract — no per-row dict materialisation) ──────
    response = _build_fast_result(
        original, changes, reviews,
        duplicate_uuid_mask, duplicate_cnic_mask,
        uuid_col if (uuid_col and uuid_col in cleaned.columns) else None,
        _predefined_summary,
    )
    _dt_all = _time.perf_counter() - _t_all0
    if progress_cb is not None:
        try: progress_cb("end", "clean", _dt_all)
        except Exception: pass
    response["__fast_meta__"]["step_timings"] = {"clean": round(_dt_all, 3)}
    return cleaned, response


# ═══════════════════════════════════════════════════════════════════════════════
# CERTIFICATES SCHEMA & PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

_MASONRY_CANONICAL   = ["Yes", "No", "Partial", "N/A"]
_ROOF_TYPE_CANONICAL = ["RCC", "Steel", "Timber", "Bamboo", "Thatch", "CGI Sheet"]

_CERT_SCHEMA: dict[str, dict] = {
    "Depth_of_Foundation":          {"type": "numeric"},
    "height_of_plinth_from_ground": {"type": "numeric"},
    "Plinth_Area":                  {"type": "numeric"},
    "no_of_room_size":              {"type": "numeric"},
    "Room Value":                   {"type": "numeric"},
    "type_of_construction":         {"type": "string", "title": True},
    "width_foundation":             {"type": "numeric"},
    "plinth_submitted_at":          {"type": "date"},
    "Use of Acceptable Masonry":          {"type": "string", "title": True, "canonical": _MASONRY_CANONICAL},
    "Use of Acceptable Bonds":            {"type": "string", "title": True, "canonical": _MASONRY_CANONICAL},
    "Use of Appropriate Wall Thickness":  {"type": "string", "title": True, "canonical": _MASONRY_CANONICAL},
    "Use of Specified Mortars":           {"type": "string", "title": True, "canonical": _MASONRY_CANONICAL},
    "Location & Size of Windows":         {"type": "string", "title": True, "canonical": _MASONRY_CANONICAL},
    "Use of Continuous RCC":              {"type": "string", "title": True, "canonical": _MASONRY_CANONICAL},
    "lintel_submitted_at":                {"type": "date"},
    "Roof & Slope Drain":                 {"type": "string", "title": True, "canonical": _MASONRY_CANONICAL},
    "Type of Roof":                       {"type": "string", "title": True, "canonical": _ROOF_TYPE_CANONICAL},
    "Use of Roof Insulation":             {"type": "bool"},
    "Use of Reinforced Concrete":         {"type": "bool"},
    "Safe Use of Any Hanging Load":       {"type": "bool"},
    "roof_submitted_at":                  {"type": "date"},
}

_CERT_FULL_SCHEMA: dict[str, dict] = {**_SCHEMA, **_CERT_SCHEMA}


def clean_dataframe_certificates(
    df: pd.DataFrame,
    uuid_column: str | None = None,
    global_rules: dict | None = None,
    run_predefined: bool = False,
    case_overrides: dict[str, str] | None = None,
    enabled_rules: dict[str, list] | None = None,
    progress_cb: "Callable[..., None] | None" = None,
    regex_rules: dict | None = None,
    dtype_rules: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Certificates cleaning pipeline.

    NOTE: same caveat as clean_dataframe_banks — coarse single start/end
    "clean" event only, no per-sub-step granularity.
    """
    import time as _time
    if progress_cb is not None:
        try: progress_cb("start", "clean")
        except Exception: pass
    _t_all0 = _time.perf_counter()
    # `df` is only read from here on (every mutation goes to `cleaned`, a
    # deep copy) — reuse it as `original` instead of paying a second full
    # deep copy of the dataset. Saves ~1x dataset size in peak memory.
    original = df
    cleaned  = df.copy(deep=True)
    changes = _CleanLog("clean")
    reviews = _CleanLog("review")
    cc = _ColCache()
    _co = case_overrides or {}

    _gr = global_rules or {}
    if regex_rules:
        _step_regex_rules(cleaned, changes, reviews, regex_rules, cc)
    if _gr.get("trim", True):
        _step_trim(cleaned, changes, cc)
    if _gr.get("null", True):
        _step_null_standardize(cleaned, changes, reviews, cc)
    if _gr.get("special", True):
        _step_special_chars_all(cleaned, changes, cc)

    duplicate_cnic_mask = pd.Series(False, index=cleaned.index)
    uuid_col, duplicate_uuid_mask = _resolve_explicit_uuid(cleaned, uuid_column)
    if duplicate_uuid_mask is None:
        duplicate_uuid_mask = pd.Series(False, index=cleaned.index)

    for col, profile in _CERT_FULL_SCHEMA.items():
        if col not in cleaned.columns:
            continue
        # See clean_dataframe_fast for the full rationale.
        if enabled_rules is not None and not enabled_rules.get(col):
            continue
        col_type = profile.get("type", "string")

        if col_type == "cnic":
            cnic_dup_mask       = _step_cnic(cleaned, changes, reviews, col, profile, cc)
            duplicate_cnic_mask = duplicate_cnic_mask | cnic_dup_mask
        elif col_type == "numeric":
            if profile.get("non_null") or profile.get("unique"):
                dup_mask = _step_uuid(cleaned, changes, reviews, col, profile, cc)
                if profile.get("unique") and (not uuid_column or col == uuid_column):
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
            eff_profile = {**profile, "case_style": _co[col]} if col in _co else profile
            _step_string_category(cleaned, changes, reviews, col, eff_profile, cc)
        elif col_type == "date":
            _step_date_col(cleaned, changes, reviews, col, cc)

    for col, style in _co.items():
        if col in cleaned.columns and col not in _CERT_FULL_SCHEMA:
            _step_casing(cleaned, changes, col, {"case_style": style}, cc)

    for col, expected_type in (dtype_rules or {}).items():
        if col in cleaned.columns:
            _step_dtype_check(cleaned, reviews, col, expected_type, cc)

    _step_date(cleaned, changes, reviews, enabled_rules)
    _step_bank(cleaned, changes, enabled_rules)
    # _step_duplicate_rows used to run here unconditionally — see note in
    # clean_dataframe_fast. Removed for the same reason.

    # ── PREDEFINED VALIDATION (auto rules) — OFF by default ───────────────────
    if run_predefined:
        cleaned, _predefined_summary = _apply_predefined_validation_to_df(
            cleaned, "certificates", existing_reviews=reviews
        )
    else:
        _predefined_summary = {
            "total_rows": len(cleaned), "passed": len(cleaned),
            "failed": 0, "filter_results": [],
        }

    # ── Build response (fast contract — no per-row dict materialisation) ──────
    response = _build_fast_result(
        original, changes, reviews,
        duplicate_uuid_mask, duplicate_cnic_mask,
        uuid_col if (uuid_col and uuid_col in cleaned.columns) else None,
        _predefined_summary,
    )
    _dt_all = _time.perf_counter() - _t_all0
    if progress_cb is not None:
        try: progress_cb("end", "clean", _dt_all)
        except Exception: pass
    response["__fast_meta__"]["step_timings"] = {"clean": round(_dt_all, 3)}
    return cleaned, response


# ── Public: auto regex clean for a single column ─────────────────────────────

def auto_regex_clean_column(
    df: pd.DataFrame,
    column: str,
) -> tuple[pd.DataFrame, dict, dict]:
    """Run _step_regex_auto on a single column. Returns (cleaned_df, changes, reviews)."""
    if column not in df.columns:
        raise ValueError(f'Column "{column}" not found.')
    cleaned  = df.copy()
    changes: dict = {}
    reviews: dict = {}
    cc = _ColCache()
    _step_regex_auto(cleaned, changes, reviews, column, cc)
    return cleaned, changes, reviews
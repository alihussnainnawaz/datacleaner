# file_handler.py
#
# Uploads are held ENTIRELY IN MEMORY — never written to disk. This removes
# two disk round trips that used to happen on every upload:
#   old flow: browser -> write bytes to disk -> read bytes back from disk -> parse
#   new flow: browser -> parse directly from the in-memory buffer
# i.e. this is strictly faster than before, not just "not storing a file" —
# one full disk write + one full disk read are gone from the upload path.
#
# Trade-off (same one flagged in PERFORMANCE_NOTES.md, now taken): an upload
# no longer survives a server restart, and regex/mapping tools that used to
# rewrite the raw file on disk now just replace the in-memory DataFrame.
# Cleaned/report OUTPUTS are unaffected — those still go through
# output_writer.py and are written to disk as before (that's the durable,
# resumable project state; this file only ever held the raw upload).
import io
import re
import pandas as pd
from fastapi import UploadFile, HTTPException
from config import ALLOWED_EXTENSIONS, MAX_FILE_SIZE_BYTES


def _slugify(name: str) -> str:
    """
    Turn an original filename stem into a safe, URL/id-friendly slug.
    e.g. "TRDP-Profile - Copy" → "TRDP-Profile-Copy"
         "my file (2).csv"     → "my-file-2"
    Keeps letters, digits, hyphens, underscores. Collapses runs of
    separators into a single hyphen and strips leading/trailing hyphens.
    """
    slug = re.sub(r"[^\w\-]+", "-", name.strip())  # replace non-word chars with -
    slug = re.sub(r"-{2,}", "-", slug)              # collapse multiple hyphens
    slug = slug.strip("-")                           # strip leading/trailing
    return slug or "file"


# ── In-memory upload store ────────────────────────────────────────────────
# file_id -> {"df": DataFrame, "file_name": str, "suffix": str, "version": int}
# This is now the SOLE copy of an uploaded dataset — not a cache in front of
# disk. "version" is bumped on every in-place edit (regex/mapping tools) and
# replaces the old file-mtime check for invalidating the dtype-detection
# cache below, at zero filesystem-stat cost.
#
# Capped at _UPLOAD_MAX_ENTRIES with least-recently-used eviction, same
# bound as the old on-disk cache. Eviction here is real data loss (no disk
# fallback to reparse from) — same as before once the "delete file after
# every run" bug was fixed, uploads already lived for the session; this is
# just now their only home.
_UPLOAD_STORE: dict[str, dict] = {}
_UPLOAD_ORDER: list[str] = []
_UPLOAD_MAX_ENTRIES = 8


def _touch(file_id: str) -> None:
    if file_id in _UPLOAD_ORDER:
        _UPLOAD_ORDER.remove(file_id)
    _UPLOAD_ORDER.append(file_id)
    while len(_UPLOAD_ORDER) > _UPLOAD_MAX_ENTRIES:
        oldest = _UPLOAD_ORDER.pop(0)
        if oldest != file_id:
            _UPLOAD_STORE.pop(oldest, None)
            _DTYPE_CACHE.pop(oldest, None)


def _read_csv_any_encoding(data: bytes) -> pd.DataFrame:
    """
    Read a CSV regardless of encoding, from raw bytes (no disk involved).
    Strategy:
      1. Sniff with chardet (catches utf-8-sig BOM, cp1252, latin-1, etc.)
      2. Try the detected encoding first.
      3. Fall back through a priority list: utf-8-sig, utf-8, cp1252, latin-1.
      4. Last resort: utf-8 with replace for undecodable bytes.
    All variants strip the BOM if present (utf-8-sig handles that automatically).
    """
    _FALLBACKS = ["utf-8-sig", "utf-8", "cp1252", "latin-1"]

    detected = None
    try:
        import chardet
        result = chardet.detect(data[:65536])   # first 64 KB is enough
        detected = result.get("encoding")
    except Exception:
        pass

    candidates = []
    if detected:
        candidates.append(detected)
    for enc in _FALLBACKS:
        if enc.lower().replace("-", "") not in [c.lower().replace("-", "") for c in candidates]:
            candidates.append(enc)

    for enc in candidates:
        try:
            return pd.read_csv(io.BytesIO(data), dtype=str, low_memory=False, encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue

    # Absolute last resort — decode with replacement characters
    return pd.read_csv(io.BytesIO(data), dtype=str, low_memory=False,
                       encoding="utf-8", errors="replace")


_warned_no_calamine = False  # log the missing-dependency fallback only once, not per request


def _read_excel_fast(data: bytes) -> pd.DataFrame:
    """
    Read an .xlsx with the fast Rust-based calamine engine (no openpyxl's
    pure-Python XML parsing — ~8x faster on large files in testing: 52s vs
    6s on a 200k-row file in benchmarking), straight from bytes in memory.
    Falls back to openpyxl if calamine isn't installed, or can't parse a
    particular file's formatting.

    IMPORTANT: if python-calamine isn't installed in this environment, every
    upload silently falls back to the slow path with no visible error —
    uploads just stay slow. This logs that fallback loudly (once) instead of
    swallowing it, since "it's slow" with no error is exactly what makes
    this kind of regression invisible.
    """
    global _warned_no_calamine
    try:
        return pd.read_excel(io.BytesIO(data), engine="calamine", dtype=str)
    except ImportError:
        if not _warned_no_calamine:
            _warned_no_calamine = True
            print(
                "[file_handler] WARNING: python-calamine is not installed — "
                "falling back to openpyxl, which is ~8x slower on large XLSX "
                "files. Run `pip install python-calamine` (already in "
                "requirements.txt) and restart the server to fix this.",
                flush=True,
            )
        return pd.read_excel(io.BytesIO(data), engine="openpyxl", dtype=str)
    except Exception:
        # calamine is installed but choked on this specific file's formatting
        return pd.read_excel(io.BytesIO(data), engine="openpyxl", dtype=str)


def _parse_bytes(data: bytes, suffix: str) -> pd.DataFrame:
    try:
        if suffix == ".xlsx":
            return _read_excel_fast(data)
        elif suffix == ".xls":
            return pd.read_excel(io.BytesIO(data), engine="xlrd", dtype=str)
        elif suffix == ".csv":
            return _read_csv_any_encoding(data)
        else:
            raise HTTPException(400, f"Unsupported format: {suffix}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(422, f"Failed to parse file: {e}")


async def save_upload(file: UploadFile) -> dict:
    """
    Read the incoming upload into memory in 1 MB chunks (still bounded by
    MAX_FILE_SIZE_BYTES, still never buffers past the limit), parse it
    immediately, and keep only the resulting DataFrame — no bytes and no
    file ever touch disk.
    """
    from pathlib import PurePosixPath
    suffix = PurePosixPath(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Invalid file type '{suffix}'. Allowed: {ALLOWED_EXTENSIONS}")

    stem    = PurePosixPath(file.filename).stem
    file_id = _slugify(stem)
    counter = 1
    while file_id in _UPLOAD_STORE:
        file_id = f"{_slugify(stem)}_{counter}"
        counter += 1

    CHUNK = 1024 * 1024
    buf = bytearray()
    while True:
        chunk = await file.read(CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(413, f"File exceeds {MAX_FILE_SIZE_BYTES // (1024*1024)} MB limit.")

    df = _parse_bytes(bytes(buf), suffix)
    _UPLOAD_STORE[file_id] = {
        "df": df, "file_name": file.filename, "suffix": suffix, "version": 0,
    }
    _touch(file_id)
    return {"file_id": file_id, "file_name": file.filename}


def load_dataframe(file_id: str) -> tuple[pd.DataFrame, str]:
    """Return (df, file_id). No parsing happens here anymore — the DataFrame
    was already parsed once, at upload time, and lives in _UPLOAD_STORE for
    the rest of its life (mutated in place by write_dataframe_inplace)."""
    entry = _UPLOAD_STORE.get(file_id)
    if entry is None:
        raise HTTPException(404, f"No file found for file_id '{file_id}'.")
    _touch(file_id)
    return entry["df"], file_id


def get_file_name(file_id: str) -> str:
    entry = _UPLOAD_STORE.get(file_id)
    if entry is None:
        raise HTTPException(404, f"No file found for file_id '{file_id}'.")
    return entry["file_name"]


# Cache of detected column datatypes, keyed by file_id -> (version, result).
# Computed once at upload time and re-used by the "drag Datatype filter onto
# All Columns" flow so that clicking it in the UI is an instant read, not a
# fresh scan of the file. Invalidated by version bump instead of an mtime
# check now that there's no file on disk to stat.
_DTYPE_CACHE: dict[str, tuple[int, dict]] = {}

# Ordered checks: (type_name, regex). First one where >=90% of sampled
# non-null values match wins; falls back to "text".
_DTYPE_PATTERNS = [
    ("integer",  re.compile(r"^-?\d+$")),
    ("decimal",  re.compile(r"^-?\d+\.\d+$")),
    ("boolean",  re.compile(r"^(true|false|yes|no|y|n|0|1)$", re.I)),
    ("email",    re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")),
    ("phone",    re.compile(r"^\+?\d[\d\-\s()]{6,}\d$")),
    ("cnic",     re.compile(r"^\d{5}-?\d{7}-?\d{1}$")),
    ("iban",     re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$", re.I)),
    ("date",     re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}$")),
]

SAMPLE_SIZE  = 500     # rows sampled per column — enough for a confident call, cheap even on huge files
MATCH_THRESH = 0.90    # fraction of sampled non-null values that must match a pattern


def _detect_column_type(series: pd.Series) -> dict:
    non_null = series.dropna().astype(str).str.strip()
    non_null = non_null[non_null != ""]
    total = len(non_null)
    if total == 0:
        return {"type": "unknown", "confidence": 0.0, "sample": []}

    sample = non_null.head(SAMPLE_SIZE)
    best_type, best_ratio = "text", 0.0
    for type_name, pattern in _DTYPE_PATTERNS:
        matches = sample.str.match(pattern).sum()
        ratio = matches / len(sample)
        if ratio >= MATCH_THRESH and ratio > best_ratio:
            best_type, best_ratio = type_name, ratio

    if best_type == "text":
        best_ratio = 1.0  # everything falls into "text" by definition

    return {
        "type":       best_type,
        "confidence": round(float(best_ratio), 3),
        "sample":     sample.head(3).tolist(),
    }


def detect_column_dtypes(file_id: str, df: pd.DataFrame | None = None) -> dict:
    """
    Return {column_name: {type, confidence, sample}} for every column,
    using a cheap regex-based sample scan (see _DTYPE_PATTERNS above).

    Cached per file_id/version so repeat calls (e.g. re-opening the Column
    Rule Preview modal, or dragging the Datatype filter onto "All Columns"
    a second time) are instant reads rather than a re-scan. The cache is
    populated eagerly at upload time in routes_upload.py so the "All
    Columns" auto-detect path never blocks on first use either.
    """
    entry   = _UPLOAD_STORE.get(file_id)
    version = entry["version"] if entry else -1

    cached = _DTYPE_CACHE.get(file_id)
    if cached and cached[0] == version:
        return cached[1]

    if df is None:
        df, _ = load_dataframe(file_id)

    result = {col: _detect_column_type(df[col]) for col in df.columns}
    _DTYPE_CACHE[file_id] = (version, result)
    return result


def write_dataframe_inplace(df: pd.DataFrame, file_id: str) -> None:
    """
    Replace the in-memory DataFrame for file_id (was: rewrite the raw file
    on disk in place). Regex/mapping tools call this after transforming one
    or more columns so subsequent reads (e.g. re-opening Column Rule
    Preview, or the pipeline itself) see the change — same contract as
    before, just without the full-file disk write that used to cost most of
    the wall-clock time here on large files.
    """
    entry = _UPLOAD_STORE.get(file_id)
    if entry is None:
        raise HTTPException(404, f"No file found for file_id '{file_id}'.")
    entry["df"] = df
    entry["version"] += 1
    _DTYPE_CACHE.pop(file_id, None)   # stale after an in-place edit
    _touch(file_id)


def delete_file(file_id: str) -> bool:
    existed = file_id in _UPLOAD_STORE
    _UPLOAD_STORE.pop(file_id, None)
    _DTYPE_CACHE.pop(file_id, None)
    if file_id in _UPLOAD_ORDER:
        _UPLOAD_ORDER.remove(file_id)
    return existed

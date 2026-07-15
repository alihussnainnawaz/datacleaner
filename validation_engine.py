"""
validation_engine.py
────────────────────
Runs a list of validation filter configs against a cleaned pandas DataFrame
and returns a per-row validation status column plus a per-filter summary.

Filter config schema (mirrors the JS frontend — keys sent by filter-collector.js
and validation_engine.js):

  Presence / value
  ─────────────────
  { "cond": "empty"|"dup"|"eq"|"neq"|"gt"|"lt"|"gte"|"lte"|"btwn"|"nbtwn",
    "colIdx":   int,      # 0-based column index
    "colName":  str,      # resolved by name when idx is wrong / absent
    "matchVal": str }     # threshold for value conditions; "lo,hi" for btwn/nbtwn

  Date
  ─────
  { "cond": "date",
    "colIdx":   int,
    "colName":  str,
    "dateCond": str,      # date sub-condition key (see DATE_SUB_CONDS below)
    "dateVal1": str,      # ISO date string or numeric year/month/day/weekday
    "dateVal2": str }     # upper bound for date_btwn / date_nbtwn

  Cross check (single-dataset col-vs-col value lookup)
  ──────────────────────────────────────────────────────
  { "cond": "cross",
    "colIdx":  int,       # Dataset-1 column to check
    "col2Idx": int }      # Dataset-1 reference column whose values form the pool

  Double cross (two-column pair match)
  ──────────────────────────────────────
  { "cond":         "doublecross",
    "colIdx":       int,   # D1 col 1
    "colExtraIdx":  int,   # D1 col 2
    "col2Idx":      int,   # reference col for colIdx    (same df, acts as D2)
    "colExtraIdx2": int }  # reference col for colExtraIdx

  AND / OR
  ─────────
  { "cond": "and"|"or",
    "subfilters": [
      { "colIdx": int, "cond": str, "matchVal": str }, ...
    ] }

  Compare (column-vs-column within same DataFrame)
  ──────────────────────────────────────────────────
  Simple:
    { "cond": "compare", "colIdx": int, "col2Idx": int, "subCond": str }
  Date col-vs-col:
    { "cond": "compare", "colIdx": int, "col2Idx": int,
      "subCond": "date", "dateCond": str }
  AND/OR of col-pairs:
    { "cond": "compare", "subCond": "and"|"or",
      "compareSubfilters": [
        { "colAIdx": int, "colBIdx": int, "cond": str }, ...
      ] }

  Coordinate check
  ─────────────────
  { "cond":          "coords",
    "lngColIdx":     int,
    "lngColName":    str,
    "latColIdx":     int,
    "latColName":    str,
    "coordLevel":    "district"|"tehsil"|"uc",
    "verifyColIdx":  int,
    "verifyColName": str }

  Predefined rule (written by cleaning_engine.py — read-only in this module)
  ───────────────────────────────────────────────────────────────────────────
  { "cond": "predefined", "label": str, "col": str, "pass": bool, ... }
  These are already embedded in __validation_status__ before run_validation()
  is called. run_validation() preserves and merges them rather than replacing.

Returns
───────
  validated_df  – original DataFrame + "__validation_status__" column
                  (JSON string per row: {"result":"PASS"|"FAIL","filters":[...]})
  summary       – dict (see run_validation docstring)
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

# PERF: same rationale as cleaning_engine.py's _fast_json_dumps — the
# per-row __validation_status__ string is built via json.dumps() once per
# row (see the status-column loop below), which is a real cost at 340k+
# rows. orjson benchmarks ~4x faster for this payload shape. Falls back to
# stdlib json if orjson isn't installed.
try:
    import orjson
    def _fast_json_dumps(obj: Any) -> str:
        return orjson.dumps(obj).decode("utf-8")
except ImportError:
    def _fast_json_dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=True)

# ── GeoJSON / spatial index (lazy, module-level singleton) ───────────────────

_GEO_FEATURES:   list | None = None
_SPATIAL_INDEX:  dict | None = None
# PERF: candidate lists only depend on the truncated (gx, gy) grid cell, so
# they're cached once per cell instead of being rebuilt on every single row.
_CANDIDATE_CACHE: dict[tuple, list] = {}

# Grid cell size in degrees for the spatial index. Smaller cells = fewer
# candidate polygons to ray-cast per point, at the cost of slightly more
# cells (still trivial — a few thousand dict entries for all of Pakistan).
# Benchmarked against the real coordinates.json (5,884 UC-level features):
#   1.0°  cells: avg 72 candidates/cell, worst cell 448 candidates
#   0.05° cells: avg  3 candidates/cell, worst cell  40 candidates
# That's roughly a 10x cut in ray-casting work per row on average, and >10x
# on the worst (densest-boundary) cells — which is exactly where real GPS
# data tends to cluster (cities/UCs with many small adjacent polygons).
_GRID_SIZE = 0.05


# ── Precomputed raster (fast path) ────────────────────────────────────────────
#
# Built offline by build_coord_raster.py. If present, resolving a (lng, lat)
# point becomes a single array lookup — no polygon math, no candidate lists,
# no ray-casting, and (see _run_coords) no per-row Python loop at all, since
# the whole column of points can be resolved in one vectorised numpy
# operation. If the raster file doesn't exist yet (build_coord_raster.py
# hasn't been run), everything falls back transparently to the live
# grid-indexed polygon math above — the coords filter still works either
# way, it's just slower without the raster.
_RASTER: dict | None = None
_RASTER_LOAD_ATTEMPTED = False
_RASTER_PATH = Path(__file__).resolve().parent / "coord_raster.npz"


def _load_raster() -> dict | None:
    global _RASTER, _RASTER_LOAD_ATTEMPTED
    if _RASTER_LOAD_ATTEMPTED:
        return _RASTER
    _RASTER_LOAD_ATTEMPTED = True
    if not _RASTER_PATH.exists():
        return None
    try:
        with np.load(_RASTER_PATH, allow_pickle=True) as z:
            raster = z["raster"]
            # MEMORY: the raster stores feature INDICES — with < 32k features
            # int16 is lossless and halves the resident footprint (~765MB →
            # ~380MB for the Sindh grid), which matters a lot on RAM-tight
            # machines where the pipeline otherwise tips into swap.
            n_feats = len(z["feat_ds"])
            if raster.dtype != np.int16 and n_feats < np.iinfo(np.int16).max:
                raster = raster.astype(np.int16)
            _RASTER = {
                "raster": raster,
                "minx": float(z["minx"]), "miny": float(z["miny"]),
                "cell": float(z["cell"]), "nx": int(z["nx"]), "ny": int(z["ny"]),
                "feat_ds": z["feat_ds"], "feat_th": z["feat_th"], "feat_uc": z["feat_uc"],
            }
    except Exception as e:
        print(f"[validation_engine] WARNING: failed to load coord_raster.npz "
              f"({e}) — falling back to live polygon math for coords checks.")
        _RASTER = None
    return _RASTER


def _resolve_admin_batch_raster(lngs: np.ndarray, lats: np.ndarray, level_key: str) -> list[str | None]:
    """
    Vectorised batch resolve using the precomputed raster — the whole
    column of points is resolved in a handful of numpy operations, with NO
    per-row Python loop for the common case. Points that land in a small
    unmapped gap between polygons (raster cell = -1) get a bounded local
    search for the nearest resolved cell, matching the old fallback
    behaviour — deduplicated by grid cell and cached process-wide, so
    repeated GPS pins and repeated pipeline runs don't re-pay the search.
    """
    r = _load_raster()
    minx, miny, cell, nx, ny = r["minx"], r["miny"], r["cell"], r["nx"], r["ny"]
    raster = r["raster"]
    # level_key is already "ds" | "th" | "uc" (see _run_coords's level_key map)
    feat_names = {"ds": r["feat_ds"], "th": r["feat_th"], "uc": r["feat_uc"]}.get(level_key, r["feat_ds"])

    # Pre-stripped name lookup array (built once per raster level, cached) —
    # replaces a per-hit Python str()/strip() loop with one fancy-index op.
    names_cache = r.setdefault("__names_cache__", {})
    names_arr = names_cache.get(level_key)
    if names_arr is None:
        names_arr = np.array(
            [(str(x).strip() or None) if x is not None else None for x in feat_names],
            dtype=object,
        )
        names_cache[level_key] = names_arr

    valid = ~np.isnan(lngs) & ~np.isnan(lats)
    gx = np.floor(np.where(valid, (lngs - minx) / cell, 0)).astype(np.int64)
    gy = np.floor(np.where(valid, (lats - miny) / cell, 0)).astype(np.int64)
    in_bounds = valid & (gx >= 0) & (gx < nx) & (gy >= 0) & (gy < ny)

    safe_gy = np.where(in_bounds, gy, 0)
    safe_gx = np.where(in_bounds, gx, 0)
    idx = np.where(in_bounds, raster[safe_gy, safe_gx], -1)

    out_arr = np.full(len(lngs), None, dtype=object)
    hits = idx >= 0
    if hits.any():
        out_arr[hits] = names_arr[idx[hits]]

    # Bounded nearest-cell fallback for the (usually small) subset that
    # landed in an unmapped gap — same expanding-square-ring search as
    # before, but run ONCE per unique grid cell (repeat GPS pins collapse)
    # with a process-wide cache keyed on (cell, level), so re-runs are free.
    misses = np.flatnonzero(in_bounds & (idx < 0))
    if len(misses):
        gap_cache: dict = r.setdefault("__gap_cache__", {})
        max_radius = max(1, int(round(0.5 / cell)))

        # Run the expanding-window search ONCE per unique grid cell (repeat
        # GPS pins collapse to one search) with a process-wide cache, so
        # repeated values and repeated pipeline runs don't re-pay the cost.
        # Real datasets cluster heavily (villages share pins), so unique
        # miss cells are typically a tiny fraction of miss rows.
        miss_cells = np.stack([safe_gx[misses], safe_gy[misses]], axis=1)
        uniq_cells, inv = np.unique(miss_cells, axis=0, return_inverse=True)

        uniq_results: list = [None] * len(uniq_cells)
        for u in range(len(uniq_cells)):
            cx, cy = int(uniq_cells[u][0]), int(uniq_cells[u][1])
            key = (cx, cy, level_key)
            if key in gap_cache:
                uniq_results[u] = gap_cache[key]
                continue
            found = None
            radius = 4
            while found is None and radius <= max_radius:
                x0, x1 = max(cx - radius, 0), min(cx + radius + 1, nx)
                y0, y1 = max(cy - radius, 0), min(cy + radius + 1, ny)
                window = raster[y0:y1, x0:x1]
                nz = np.argwhere(window >= 0)
                if len(nz):
                    # Nearest by simple squared cell distance within this window
                    dy = nz[:, 0] + y0 - cy
                    dx = nz[:, 1] + x0 - cx
                    best = np.argmin(dx * dx + dy * dy)
                    found = int(window[nz[best][0], nz[best][1]])
                else:
                    radius *= 4
            name = names_arr[found] if found is not None else None
            gap_cache[key] = name
            uniq_results[u] = name

        uniq_arr = np.array(uniq_results, dtype=object)
        out_arr[misses] = uniq_arr[inv]

    return out_arr.tolist()


def _load_geo() -> list:
    global _GEO_FEATURES
    if _GEO_FEATURES is not None:
        return _GEO_FEATURES
    p = Path(__file__).resolve().parent / "coordinates.json"
    if not p.exists():
        _GEO_FEATURES = []
        return _GEO_FEATURES
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    _GEO_FEATURES = data.get("geojson", {}).get("features", [])
    return _GEO_FEATURES


def _build_spatial_index(features: list) -> dict:
    idx: dict[str, list] = {}
    cell = _GRID_SIZE
    for fi, feat in enumerate(features):
        geom = feat.get("geometry")
        if not geom:
            continue
        gtype = geom.get("type", "")
        if gtype == "Polygon":
            rings = [(fi, geom["coordinates"][0])]
        elif gtype == "MultiPolygon":
            rings = [(fi, p[0]) for p in geom["coordinates"]]
        else:
            continue
        for _, ring in rings:
            lngs = [c[0] for c in ring]
            lats = [c[1] for c in ring]
            minx, maxx = min(lngs), max(lngs)
            miny, maxy = min(lats), max(lats)
            bbox  = (minx, maxx, miny, maxy)
            entry = (fi, ring, bbox)
            gx0, gx1 = math.floor(minx / cell), math.floor(maxx / cell)
            gy0, gy1 = math.floor(miny / cell), math.floor(maxy / cell)
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    key = f"{gx},{gy}"
                    if key not in idx:
                        idx[key] = []
                    idx[key].append(entry)
    return idx


def _get_spatial_index() -> dict:
    global _SPATIAL_INDEX
    if _SPATIAL_INDEX is None:
        _SPATIAL_INDEX = _build_spatial_index(_load_geo())
    return _SPATIAL_INDEX


def _get_candidates(gx: int, gy: int, ring_radius: int = 1) -> list:
    """
    Candidates within `ring_radius` grid cells of (gx, gy), cached per
    (cell, radius). ring_radius=1 (the default / fast path) covers the
    immediate 3x3 neighborhood — plenty for the common case where the point
    lands inside a polygon. The 0.5°-fallback path in _resolve_admin_cached
    asks for a wider radius explicitly when the fast path finds nothing,
    since with the finer _GRID_SIZE a single ring no longer spans 0.5° the
    way it did back when cells were 1° wide.
    """
    key = (gx, gy, ring_radius)
    cands = _CANDIDATE_CACHE.get(key)
    if cands is not None:
        return cands
    spatial = _get_spatial_index()
    seen: set = set()
    cands = []
    for dx in range(-ring_radius, ring_radius + 1):
        for dy in range(-ring_radius, ring_radius + 1):
            bucket = spatial.get(f"{gx+dx},{gy+dy}", [])
            for item in bucket:
                if id(item) not in seen:
                    seen.add(id(item))
                    cands.append(item)
    _CANDIDATE_CACHE[key] = cands
    return cands


def _pip(lng: float, lat: float, ring: list) -> bool:
    """Ray-casting point-in-polygon."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lng < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def _bbox_dist2(lng: float, lat: float, bbox: tuple) -> float:
    """Squared distance from a point to a bounding box (0.0 if inside it)."""
    minx, maxx, miny, maxy = bbox
    dx = minx - lng if lng < minx else (lng - maxx if lng > maxx else 0.0)
    dy = miny - lat if lat < miny else (lat - maxy if lat > maxy else 0.0)
    return dx * dx + dy * dy


@lru_cache(maxsize=200_000)
def _resolve_admin_cached(lng_r: float, lat_r: float, level_key: str) -> tuple:
    """Cached core resolver, keyed on rounded coordinates (~11cm precision).
    Repeat GPS pins (same village entered multiple times) become O(1) cache hits."""
    features = _load_geo()
    gx, gy = math.floor(lng_r / _GRID_SIZE), math.floor(lat_r / _GRID_SIZE)
    cands  = _get_candidates(gx, gy)  # fast path: immediate 3x3 neighborhood

    # Exact point-in-polygon — bbox pre-check skips expensive ray-cast
    for fi, ring, bbox in cands:
        if _bbox_dist2(lng_r, lat_r, bbox) > 0.0:
            continue
        if _pip(lng_r, lat_r, ring):
            name = str(features[fi]["properties"].get(level_key, "") or "").strip()
            return name, True

    # Nearest-polygon fallback ≤ 0.5° — this is the rare path (point isn't
    # inside any polygon at all, e.g. bad GPS data, a swapped lat/lng, or a
    # point just outside a boundary). Expand the search ring-by-ring instead
    # of always jumping straight to the full 0.5° neighborhood: most
    # near-miss points (slightly outside a real boundary) are found within
    # a ring or two, and this avoids paying for a huge candidate scan on
    # every row for genuinely invalid/out-of-country coordinates, which
    # would otherwise never find anything nearby no matter how wide the
    # search — better to fail that case fast than scan ~450 cells for it.
    max_radius  = max(1, math.ceil(0.5 / _GRID_SIZE))
    wide_cands  = cands
    radius      = 1
    while not wide_cands and radius < max_radius:
        radius     = min(radius * 4, max_radius)
        wide_cands = _get_candidates(gx, gy, ring_radius=radius)

    best_d = 0.25
    best_name = None
    for fi, ring, bbox in wide_cands:
        if _bbox_dist2(lng_r, lat_r, bbox) >= best_d:
            continue
        d = min((c[0] - lng_r) ** 2 + (c[1] - lat_r) ** 2 for c in ring)
        if d < best_d:
            best_d = d
            best_name = str(features[fi]["properties"].get(level_key, "") or "").strip()

    return best_name, False


def _resolve_admin(lng: float, lat: float, level_key: str) -> tuple[str | None, bool]:
    """Returns (admin_name, is_exact). Falls back to nearest polygon within 0.5°."""
    if not isinstance(lng, (int, float)) or not isinstance(lat, (int, float)):
        return None, False
    try:
        if np.isnan(lng) or np.isnan(lat):
            return None, False
    except (TypeError, ValueError):
        return None, False
    # Round to 6 decimals (~11cm) — collapses float noise so identical GPS
    # pins actually hit the lru_cache.
    return _resolve_admin_cached(round(lng, 6), round(lat, 6), level_key)


# ── shared normalisation helpers ─────────────────────────────────────────────

NULL_TOKENS = {"", "nan", "none", "null", "n/a", "na", "nil", "-", "--", "#n/a"}

DATE_SUB_CONDS = {
    "date_eq", "date_neq",
    "date_before", "date_after", "date_before_eq", "date_after_eq",
    "date_btwn",   "date_nbtwn",
    "date_empty",  "date_invalid",
    "date_year_eq","date_year_gt","date_year_lt",
    "date_month_eq","date_day_eq","date_weekday",
    "date_future", "date_past",
    # legacy aliases accepted from older filter configs
    "invalid", "before", "after", "between", "future", "past",
}

# Human-readable labels for every predefined rule ID emitted by cleaning_engine.py
PREDEFINED_RULE_LABELS: dict[str, str] = {
    "R01_UUID_INVALID_FORMAT":                "UUID — Invalid Format",
    "R02_UUID_DUPLICATE":                     "UUID — Duplicate",
    "R03_CNIC_INVALID":                       "CNIC — Invalid (not 13 digits)",
    "R04_UUID_MULTIPLE_CNICS":                "UUID — Linked to Multiple CNICs",
    "R05_CNIC_SHARED_HOUSEHOLDS":             "CNIC — Shared Across Households",
    "R06_MISSING_UUID":                       "Mandatory Field — UUID Empty",
    "R06_MISSING_CNIC":                       "Mandatory Field — CNIC Empty",
    "R06_MISSING_DISTRICT":                   "Mandatory Field — District Empty",
    "R06_MISSING_TEHSIL":                     "Mandatory Field — Tehsil Empty",
    "R06_MISSING_UC":                         "Mandatory Field — UC Empty",
    "R06_MISSING_IP":                         "Mandatory Field — IP Empty",
    "R06_MISSING_BANK":                       "Mandatory Field — Bank Empty",
    "R06_MISSING_STAGE":                      "Mandatory Field — Stage Empty",
    "R07_DATE_INVALID":                       "Date — Invalid Value",
    "R07_DATE_WRONG_FORMAT":                  "Date — Wrong Format (expected DD-MM-YYYY)",
    "R08_DATE_FUTURE":                        "Date — Future Date Not Allowed",
    "R09_DATE_ORDER":                         "Date — Stage Order Violated",
    "R10_STAGE_DEP":                          "Construction — Stage Dependency Violated",
    "R11_ITVC":                               "ITVC — Verification Missing for Completed Stage",
    "R12_STATUS_DATE_MISMATCH_NO_DATE":       "Stage Status/Date Mismatch — Completed but No Date",
    "R12_STATUS_DATE_MISMATCH_HAS_DATE":      "Stage Status/Date Mismatch — Incomplete but Has Date",
    "R12_STATUS_DATE_COL_ABSENT":             "Stage Status/Date Mismatch — Date Column Missing",
}

# Colour mapping for predefined rules — used by the frontend to pick highlight colour
# Keys are rule prefixes; the frontend maps these to CSS classes
PREDEFINED_RULE_COLORS: dict[str, str] = {
    "R01": "red",      # UUID format
    "R02": "red",      # UUID duplicate
    "R03": "red",      # CNIC invalid
    "R04": "orange",   # UUID-CNIC linkage
    "R05": "orange",   # CNIC shared
    "R06": "red",      # mandatory missing  → bright red
    "R07": "purple",   # date format/invalid
    "R08": "purple",   # future date
    "R09": "yellow",   # date ordering
    "R10": "yellow",   # stage dependency
    "R11": "orange",   # ITVC missing
    "R12": "yellow",   # status/date mismatch
}


def _predefined_rule_label(rule_id: str) -> str:
    """Resolve human-readable label for a predefined rule ID."""
    # Exact match first
    if rule_id in PREDEFINED_RULE_LABELS:
        return PREDEFINED_RULE_LABELS[rule_id]
    # Prefix match for dynamic rule IDs (e.g. R09_DATE_ORDER_PLINTH_BEFORE_LINTEL)
    for prefix, label in PREDEFINED_RULE_LABELS.items():
        if rule_id.startswith(prefix.split("_")[0] + "_"):
            # Build readable label from the rule_id itself
            parts = rule_id.split("_")[1:]  # drop R09
            return " — ".join(p.replace("_", " ").title() for p in parts if p)
    return rule_id


def _predefined_rule_color(rule_id: str) -> str:
    """Return the colour token for a predefined rule ID."""
    prefix = rule_id[:3]  # e.g. "R06"
    return PREDEFINED_RULE_COLORS.get(prefix, "red")


def _parse_bad_patterns(cfg: dict) -> list[str]:
    """
    Normalise the user-supplied pattern list for the "bad_pattern" filter into
    a list of regex-ready strings.

    Accepts either:
      cfg["values"]: ["11111", "0000", "111111111"]   (preferred — explicit list)
      cfg["value"]:  "11111, 0000, 111111111"          (comma/newline-separated string)

    Each entry is treated as LITERAL text by default (re.escape'd) — the user
    types values like "11111" or "0000" meaning "this exact digit run", not
    regex syntax. If an entry already looks like it's using regex
    metacharacters (e.g. they wrote "0{4,}" or "^00"), it's used as-is so
    power users aren't blocked from writing real patterns.
    """
    raw_list: list[str] = []
    if isinstance(cfg.get("values"), list):
        raw_list = [str(v) for v in cfg["values"]]
    elif cfg.get("value"):
        raw_list = re.split(r"[,\n]+", str(cfg["value"]))

    patterns: list[str] = []
    _REGEX_HINT = re.compile(r"[\\^$.|?*+()\[\]{}]")
    for raw in raw_list:
        v = raw.strip()
        if not v:
            continue
        patterns.append(v if _REGEX_HINT.search(v) else re.escape(v))
    return patterns


def _col(
    df: pd.DataFrame,
    cfg: dict,
    idx_key:  str = "colIdx",
    name_key: str = "colName",
) -> pd.Series | None:
    """Resolve a DataFrame column by name first, then by 0-based index."""
    name = cfg.get(name_key)
    if name and name in df.columns:
        return df[name]
    idx = cfg.get(idx_key)
    if idx is not None and idx != "" and 0 <= int(idx) < len(df.columns):
        return df.iloc[:, int(idx)]
    return None


def _norm(s: pd.Series) -> pd.Series:
    """Lowercase, strip, treat nulls as ''.

    PERF: astype("string") routes strip/lower/contains/isin through the
    pyarrow C++ kernels instead of per-element Python calls — same values
    as the old fillna("").astype(str) path (str() and Arrow's cast format
    scalars identically), several times faster at 690k rows."""
    return s.astype("string").fillna("").str.strip().str.lower()


def _is_null(s: pd.Series) -> pd.Series:
    return _norm(s).isin(NULL_TOKENS)


def _parse_dates(s: pd.Series) -> pd.Series:
    """Parse a string Series to datetime64, coercing errors to NaT.

    PERF: parses UNIQUE values then gathers back by factorize code — date
    columns repeat heavily and format="mixed" infers per element anyway, so
    results are identical to parsing the full column at a fraction of the
    cost."""
    cleaned = s.astype("string").fillna("").str.strip()
    codes, uniq = pd.factorize(cleaned, use_na_sentinel=True)
    parsed_u = pd.to_datetime(
        pd.Series(np.asarray(uniq, dtype=object)),
        errors="coerce",
        format="mixed",
        dayfirst=False,
    )
    vals = parsed_u.to_numpy()                       # datetime64[ns] with NaT
    out = np.full(len(codes), np.datetime64("NaT"), dtype=vals.dtype if len(vals) else "datetime64[ns]")
    hit = codes >= 0
    if hit.any() and len(vals):
        out[hit] = vals[codes[hit]]
    return pd.Series(out, index=s.index)


def _matchval_to_lo_hi(cfg: dict) -> tuple[float, float] | None:
    """Extract lo, hi from matchVal 'lo,hi' or from value / value2 keys."""
    mv = str(cfg.get("matchVal") or cfg.get("value") or "")
    if "," in mv:
        parts = mv.split(",", 1)
        try:
            return float(parts[0]), float(parts[1])
        except (ValueError, TypeError):
            pass
    try:
        lo = float(cfg.get("value",  cfg.get("dateVal1", 0)))
        hi = float(cfg.get("value2", cfg.get("dateVal2", 0)))
        return lo, hi
    except (ValueError, TypeError):
        return None


# ── per-filter runner ─────────────────────────────────────────────────────────

def _run_filter(df: pd.DataFrame, cfg: dict, df2: pd.DataFrame | None = None) -> pd.Series:
    """
    Return a boolean mask aligned to df.index.
    True  = row FAILS this filter (flagged).
    False = row passes.

    df2 : optional real second dataset, used only by "cross"/"doublecross".
    """
    if "matchVal" in cfg and "value" not in cfg:
        cfg = {**cfg, "value": cfg["matchVal"]}

    cond = (cfg.get("cond") or "").strip().lower()
    fail = pd.Series(False, index=df.index)

    # ── MISSING / NULL ───────────────────────────────────────────────────────
    if cond == "empty":
        col = _col(df, cfg)
        return _is_null(col) if col is not None else fail

    if cond == "notempty":
        col = _col(df, cfg)
        return ~_is_null(col) if col is not None else fail

    # ── DUPLICATE ────────────────────────────────────────────────────────────
    if cond == "dup":
        col = _col(df, cfg)
        if col is None:
            return fail
        not_null = ~_is_null(col)
        return col.duplicated(keep=False) & not_null

    # ── EQUAL / NOT EQUAL ────────────────────────────────────────────────────
    if cond == "eq":
        col = _col(df, cfg)
        if col is None:
            return fail
        v = str(cfg.get("value", "")).strip().lower()
        return _norm(col) != v

    if cond == "neq":
        col = _col(df, cfg)
        if col is None:
            return fail
        v = str(cfg.get("value", "")).strip().lower()
        return (_norm(col) == v) & ~_is_null(col)

    # ── NUMERIC COMPARISONS ──────────────────────────────────────────────────
    if cond in ("gt", "lt", "gte", "lte"):
        col = _col(df, cfg)
        if col is None:
            return fail
        nums = pd.to_numeric(col, errors="coerce")
        try:
            thr = float(cfg.get("value", 0))
        except (TypeError, ValueError):
            return fail
        ops = {
            "gt":  ~(nums >  thr),
            "lt":  ~(nums <  thr),
            "gte": ~(nums >= thr),
            "lte": ~(nums <= thr),
        }
        return ops[cond].fillna(True)

    # ── BETWEEN / NOT BETWEEN ────────────────────────────────────────────────
    if cond == "btwn":
        col = _col(df, cfg)
        if col is None:
            return fail
        bounds = _matchval_to_lo_hi(cfg)
        if bounds is None:
            return fail
        lo, hi = bounds
        nums   = pd.to_numeric(col, errors="coerce")
        return (~((nums >= lo) & (nums <= hi))).fillna(True)

    if cond == "nbtwn":
        col = _col(df, cfg)
        if col is None:
            return fail
        bounds = _matchval_to_lo_hi(cfg)
        if bounds is None:
            return fail
        lo, hi = bounds
        nums   = pd.to_numeric(col, errors="coerce")
        return ((nums >= lo) & (nums <= hi)).fillna(False)

    # ── CONTAINS / NOT CONTAINS ──────────────────────────────────────────────
    if cond == "contains":
        col = _col(df, cfg)
        if col is None:
            return fail
        v = str(cfg.get("value", "")).lower()
        return ~_norm(col).str.contains(v, regex=False, na=False)

    if cond == "ncontains":
        col = _col(df, cfg)
        if col is None:
            return fail
        v = str(cfg.get("value", "")).lower()
        return _norm(col).str.contains(v, regex=False, na=False)

    # ── REGEX ────────────────────────────────────────────────────────────────
    if cond == "regex":
        col = _col(df, cfg)
        pat = cfg.get("value", "")
        if col is None or not pat:
            return fail
        try:
            return ~col.astype(str).str.contains(pat, regex=True, na=False)
        except re.error:
            return fail

    # ── BAD PATTERN (flag rows that DO match any of one or more user-supplied
    # patterns — opposite intent from "regex" above, which flags rows that
    # DON'T match a single required format). Built for placeholder/junk-value
    # detection, e.g. CNIC or UUID columns containing "11111", "0000",
    # "111111111" — values someone typed to get past a required field rather
    # than a real ID. Each pattern is matched as a substring regex (so "0000"
    # also catches "90400000000"); a row is flagged if ANY pattern matches. ──
    if cond == "bad_pattern":
        col = _col(df, cfg)
        if col is None:
            return fail
        patterns = _parse_bad_patterns(cfg)
        if not patterns:
            return fail
        s = col.astype(str)
        combined_mask = pd.Series(False, index=df.index)
        for pat in patterns:
            try:
                combined_mask = combined_mask | s.str.contains(pat, regex=True, na=False)
            except re.error:
                continue  # skip an invalid individual pattern rather than failing the whole filter
        return combined_mask

    # ── DATE FILTER ──────────────────────────────────────────────────────────
    if cond == "date":
        return _run_date_filter(df, cfg)

    # ── CROSS CHECK ──────────────────────────────────────────────────────────
    if cond == "cross":
        if df2 is None:
            return fail   # no Dataset 2 uploaded — fail safe, flag nothing
        col1 = _col(df,  cfg, "colIdx",  "colName")
        col2 = _col(df2, cfg, "col2Idx", "col2Name")
        if col1 is None or col2 is None:
            return fail
        pool = set(_norm(col2[~_is_null(col2)]))
        return ~_is_null(col1) & ~_norm(col1).isin(pool)

    # ── DOUBLE CROSS ─────────────────────────────────────────────────────────
    if cond == "doublecross":
        return _run_doublecross(df, cfg, df2)

    # ── AND / OR ─────────────────────────────────────────────────────────────
    if cond in ("and", "or"):
        return _run_logic(df, cfg)

    # ── COMPARE ──────────────────────────────────────────────────────────────
    if cond in ("compare", "compare2"):
        return _run_compare(df, cfg, df2)

    # ── COORDINATE CHECK ─────────────────────────────────────────────────────
    if cond == "coords":
        return _run_coords(df, cfg)

    # ── PREDEFINED (already embedded — not re-run, just preserved) ───────────
    if cond == "predefined":
        return fail   # handled by merge logic in run_validation

    return fail


# ── date filter ───────────────────────────────────────────────────────────────

def _run_date_filter(df: pd.DataFrame, cfg: dict) -> pd.Series:
    fail   = pd.Series(False, index=df.index)
    col    = _col(df, cfg)
    if col is None:
        return fail

    date_cond = (
        cfg.get("dateCond")
        or cfg.get("dateSubCond")
        or cfg.get("date_cond")
        or ""
    ).strip()

    parsed   = _parse_dates(col)
    nonnull  = ~_is_null(col)
    is_valid = nonnull & parsed.notna()

    if date_cond in ("date_empty", "empty"):
        return _is_null(col)

    if date_cond in ("date_invalid", "invalid"):
        return nonnull & parsed.isna()

    if date_cond in ("date_future", "future"):
        now = pd.Timestamp.now().normalize()
        return is_valid & (parsed > now)

    if date_cond in ("date_past", "past"):
        now = pd.Timestamp.now().normalize()
        return is_valid & (parsed < now)

    val1 = str(cfg.get("dateVal1") or cfg.get("value") or "").strip()

    if date_cond in ("date_eq", "date_neq",
                     "date_before", "date_after",
                     "date_before_eq", "date_after_eq",
                     "before", "after"):
        try:
            ref = pd.Timestamp(val1).normalize()
        except Exception:
            return fail
        if date_cond == "date_eq":         return is_valid & (parsed.dt.normalize() != ref)
        if date_cond == "date_neq":        return is_valid & (parsed.dt.normalize() == ref)
        if date_cond in ("date_before", "before"): return is_valid & (parsed.dt.normalize() >= ref)
        if date_cond in ("date_after",  "after"):  return is_valid & (parsed.dt.normalize() <= ref)
        if date_cond == "date_before_eq":  return is_valid & (parsed.dt.normalize() > ref)
        if date_cond == "date_after_eq":   return is_valid & (parsed.dt.normalize() < ref)

    try:
        ref_num = int(val1)
    except (ValueError, TypeError):
        ref_num = None

    if date_cond == "date_year_eq":   return fail if ref_num is None else is_valid & (parsed.dt.year  != ref_num)
    if date_cond == "date_year_gt":   return fail if ref_num is None else is_valid & ~(parsed.dt.year  > ref_num)
    if date_cond == "date_year_lt":   return fail if ref_num is None else is_valid & ~(parsed.dt.year  < ref_num)
    if date_cond == "date_month_eq":  return fail if ref_num is None else is_valid & (parsed.dt.month != ref_num)
    if date_cond == "date_day_eq":    return fail if ref_num is None else is_valid & (parsed.dt.day   != ref_num)
    if date_cond == "date_weekday":
        return fail if ref_num is None else is_valid & (parsed.dt.isocalendar().day.astype(int) != ref_num)

    val2 = str(cfg.get("dateVal2") or cfg.get("value2") or "").strip()

    if date_cond in ("date_btwn", "between"):
        try:
            lo = pd.Timestamp(val1).normalize()
            hi = pd.Timestamp(val2).normalize()
        except Exception:
            return fail
        in_range = is_valid & (parsed.dt.normalize() >= lo) & (parsed.dt.normalize() <= hi)
        return ~in_range & nonnull

    if date_cond == "date_nbtwn":
        try:
            lo = pd.Timestamp(val1).normalize()
            hi = pd.Timestamp(val2).normalize()
        except Exception:
            return fail
        in_range = is_valid & (parsed.dt.normalize() >= lo) & (parsed.dt.normalize() <= hi)
        return in_range

    return fail


# ── double cross ──────────────────────────────────────────────────────────────

def _run_doublecross(df: pd.DataFrame, cfg: dict, df2: pd.DataFrame | None = None) -> pd.Series:
    fail = pd.Series(False, index=df.index)

    if df2 is None:
        return fail   # no Dataset 2 uploaded — fail safe, flag nothing

    col1  = _col(df,  cfg, "colIdx",       "colName")
    col2  = _col(df,  cfg, "colExtraIdx",  "colExtraName")
    ref1  = _col(df2, cfg, "col2Idx",      "col2Name")
    ref2  = _col(df2, cfg, "colExtraIdx2", "colExtraName2")

    if any(c is None for c in (col1, col2, ref1, ref2)):
        return fail

    pair_set: set[tuple] = set()
    for v1, v2 in zip(_norm(ref1), _norm(ref2)):
        if v1 not in NULL_TOKENS and v2 not in NULL_TOKENS:
            pair_set.add((v1, v2))

    n1 = _norm(col1)
    n2 = _norm(col2)
    # PERF: null_mask.iloc[i] inside the loop below used to pay pandas'
    # per-call accessor overhead on every row (~23x slower than a plain
    # array lookup, benchmarked at 200k rows: 0.70s vs 0.03s — several
    # seconds at this app's 690k-row target scale). Converting to a numpy
    # array once, outside the loop, removes that cost entirely.
    null_mask = (n1.isin(NULL_TOKENS) | n2.isin(NULL_TOKENS)).to_numpy()
    pairs = list(zip(n1.tolist(), n2.tolist()))
    flagged = pd.array([not null_mask[i] and pairs[i] not in pair_set
                        for i in range(len(pairs))], dtype=bool)
    return pd.Series(flagged, index=df.index)


# ── AND / OR logic ────────────────────────────────────────────────────────────

def _run_logic(df: pd.DataFrame, cfg: dict) -> pd.Series:
    cond     = cfg.get("cond", "and")
    subs     = cfg.get("subfilters") or []
    fail     = pd.Series(False, index=df.index)
    if not subs:
        return fail

    masks = []
    for s in subs:
        if "matchVal" in s and "value" not in s:
            s = {**s, "value": s["matchVal"]}
        try:
            masks.append(_run_filter(df, s))
        except Exception:
            masks.append(fail.copy())

    if cond == "and":
        result = masks[0].copy()
        for m in masks[1:]:
            result = result & m
    else:
        result = masks[0].copy()
        for m in masks[1:]:
            result = result | m

    return result


# ── compare (column-vs-column) ────────────────────────────────────────────────

def _join_d2_col_by_uuid(
    df: pd.DataFrame, df2: pd.DataFrame, cfg: dict, d2_col: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    """
    Look up each Dataset-1 row's matching Dataset-2 value by UUID, instead
    of assuming the two files are in the same row order. This is the actual
    "Compare with Dataset 2" join: Dataset 1's UUID column is the one
    already configured for the whole project (Settings → UUID/ID column —
    reused automatically, no extra picker needed); Dataset 2's UUID column
    is chosen once per compare2 filter via its own dropdown.

    Vectorized as a single hash-join lookup (pandas Index.get_indexer) —
    O(n+m), no Python-level loop, same cost class as the positional version
    it replaced.

    Returns (joined_values, matched_mask). matched_mask is True only where
    the Dataset 1 row's UUID was actually found in Dataset 2 — callers
    should AND their flag result with this, because "no UUID match" and
    "matched, but the value is genuinely blank" are different situations
    that some comparisons (date comparisons especially) treat very
    differently: an unmatched row has NOTHING to compare against and must
    never be flagged, while a matched-but-blank row is still a real data
    issue worth flagging.
    """
    d1_uuid = _col(df,  cfg, "uuidColIdx",   "uuidColName")
    d2_uuid = _col(df2, cfg, "d2UuidColIdx", "d2UuidColName")
    if d1_uuid is None or d2_uuid is None:
        empty = pd.Series([None] * len(df), index=df.index, dtype=object)
        return empty, pd.Series(False, index=df.index)

    # Build D2: normalized-uuid -> value. Duplicate UUIDs in Dataset 2 keep
    # the FIRST occurrence (matches "cross check" pool semantics elsewhere
    # in this file) rather than raising or silently picking an arbitrary one.
    d2_key = _norm(d2_uuid)
    dedup  = ~d2_key.duplicated(keep="first")
    d2_key_arr  = d2_key.to_numpy()[dedup.to_numpy()]
    d2_vals_arr = d2_col.to_numpy()[dedup.to_numpy()]

    d1_key = _norm(d1_uuid)
    d1_arr = d1_key.to_numpy()

    # PERF: one get_indexer() call instead of two separate reindex() calls
    # (one for the value join, one for match detection) — those were
    # computing the identical hash lookup twice. get_indexer is the same
    # underlying primitive reindex uses internally; calling it once and
    # deriving both the joined values AND the match mask from its result
    # cuts this step to roughly 60% of the two-reindex version (benchmarked
    # ~0.86s vs ~1.43s at 700k x 500k rows). Earlier version used
    # pd.Index(...).isin() for match detection, which alone cost 3.86s at
    # that scale — the single dominant cost in this whole function before
    # this fix.
    positions = pd.Index(d2_key_arr).get_indexer(d1_arr)   # -1 = no match
    found     = positions != -1
    joined_arr = np.where(found, d2_vals_arr[np.clip(positions, 0, None)], None)

    joined  = pd.Series(joined_arr, index=df.index)
    matched = pd.Series(found, index=df.index) & ~_is_null(d1_uuid)

    return joined, matched


def _run_compare(df: pd.DataFrame, cfg: dict, df2: pd.DataFrame | None = None) -> pd.Series:
    fail  = pd.Series(False, index=df.index)
    sub   = (cfg.get("subCond") or "").strip().lower()
    is_d2 = (cfg.get("cond") or "").strip().lower() == "compare2"
    if is_d2 and df2 is None:
        return fail   # Dataset 2 not uploaded — fail safe, flag nothing
    col2_src = df2 if is_d2 else df

    if sub in ("and", "or"):
        subs = cfg.get("compareSubfilters") or []
        if not subs:
            return fail
        sub_masks = []
        d2_matched = None
        for sf in subs:
            colA = _col(df, sf, "colAIdx", "colAName")
            colB = _col(col2_src, sf, "colBIdx", "colBName")
            if colA is None or colB is None:
                continue
            if is_d2:
                colB, matched = _join_d2_col_by_uuid(df, df2, cfg, colB)
                d2_matched = matched if d2_matched is None else (d2_matched & matched)
            sc = (sf.get("cond") or "").strip().lower()
            sub_masks.append(_compare_two_cols(colA, colB, sc, fail))
        if not sub_masks:
            return fail
        if sub == "and":
            passing = sub_masks[0].copy()
            for m in sub_masks[1:]:
                passing = passing & m
        else:
            passing = sub_masks[0].copy()
            for m in sub_masks[1:]:
                passing = passing | m
        result = ~passing
        if is_d2 and d2_matched is not None:
            result = result & d2_matched   # no UUID match in Dataset 2 → never flag
        return result

    if sub == "date":
        return _run_compare_date(df, cfg, df2 if is_d2 else None)

    col1 = _col(df, cfg, "colIdx",  "colName")
    col2 = _col(col2_src, cfg, "col2Idx", "col2Name")
    if col1 is None or col2 is None:
        return fail
    d2_matched = None
    if is_d2:
        col2, d2_matched = _join_d2_col_by_uuid(df, df2, cfg, col2)

    result = ~_compare_two_cols(col1, col2, sub, fail)
    if d2_matched is not None:
        result = result & d2_matched   # no UUID match in Dataset 2 → never flag
    return result


def _compare_two_cols(
    col1: pd.Series,
    col2: pd.Series,
    sub: str,
    fail: pd.Series,
) -> pd.Series:
    s1 = _norm(col1)
    s2 = _norm(col2)
    n1 = pd.to_numeric(col1, errors="coerce")
    n2 = pd.to_numeric(col2, errors="coerce")
    both_notnull = ~_is_null(col1) & ~_is_null(col2)

    if sub == "eq":    return both_notnull & (s1 == s2)
    if sub == "neq":   return both_notnull & (s1 != s2)
    if sub == "gt":    return both_notnull & (n1 >  n2).fillna(False)
    if sub == "lt":    return both_notnull & (n1 <  n2).fillna(False)
    if sub == "gte":   return both_notnull & (n1 >= n2).fillna(False)
    if sub == "lte":   return both_notnull & (n1 <= n2).fillna(False)
    if sub == "empty":
        e1, e2 = _is_null(col1), _is_null(col2)
        return e1 == e2
    if sub == "dup":
        return both_notnull & (s1 == s2)
    if sub == "btwn":
        return both_notnull & (n1 >= n2).fillna(False)
    if sub == "nbtwn":
        return both_notnull & (n1 < n2).fillna(False)

    return fail.copy()


def _run_compare_date(df: pd.DataFrame, cfg: dict, df2: pd.DataFrame | None = None) -> pd.Series:
    fail = pd.Series(False, index=df.index)

    col1 = _col(df, cfg, "colIdx",  "colName")
    col2 = _col(df2 if df2 is not None else df, cfg, "col2Idx", "col2Name")
    if col1 is None or col2 is None:
        return fail
    d2_matched = None
    if df2 is not None:
        col2, d2_matched = _join_d2_col_by_uuid(df, df2, cfg, col2)

    dc   = (cfg.get("dateCond") or "").strip()
    dA   = _parse_dates(col1)
    dB   = _parse_dates(col2)
    eA   = _is_null(col1)
    eB   = _is_null(col2)
    invA = ~eA & dA.isna()
    invB = ~eB & dB.isna()

    def _apply_match_mask(result: pd.Series) -> pd.Series:
        # A row with no UUID match in Dataset 2 has nothing to compare
        # against — that's categorically different from "matched, but the
        # value is blank" (which the branches below correctly still flag
        # as a broken pair). Never flag an unmatched row, regardless of
        # which date sub-condition ran.
        if d2_matched is not None:
            return result & d2_matched
        return result

    if dc == "date_empty":   return _apply_match_mask(eA != eB)
    if dc == "date_invalid": return _apply_match_mask(invA != invB)

    both_valid = ~eA & ~eB & dA.notna() & dB.notna()

    # A row where exactly one side is missing/unparseable and the other side
    # has a real date is NOT "not applicable" — it's a broken pair (e.g. an
    # installment date left blank while the neighboring date is filled in).
    # Only treat a row as out-of-scope when BOTH sides are missing (neither
    # date exists yet, e.g. a future installment that hasn't happened).
    missingA = eA | dA.isna()
    missingB = eB | dB.isna()
    partial_missing = missingA ^ missingB  # exactly one side missing, not both

    tA = dA.dt.normalize()
    tB = dB.dt.normalize()

    if dc == "date_eq":        return _apply_match_mask((both_valid & (tA != tB)) | partial_missing)
    if dc == "date_neq":       return _apply_match_mask((both_valid & (tA == tB)) | partial_missing)
    if dc == "date_before":    return _apply_match_mask((both_valid & ~(tA <  tB)) | partial_missing)
    if dc == "date_after":     return _apply_match_mask((both_valid & ~(tA >  tB)) | partial_missing)
    if dc == "date_before_eq": return _apply_match_mask((both_valid & ~(tA <= tB)) | partial_missing)
    if dc == "date_after_eq":  return _apply_match_mask((both_valid & ~(tA >= tB)) | partial_missing)
    if dc == "date_year_eq":   return _apply_match_mask((both_valid & (dA.dt.year  != dB.dt.year)) | partial_missing)
    if dc == "date_month_eq":  return _apply_match_mask((both_valid & (dA.dt.month != dB.dt.month)) | partial_missing)
    if dc == "date_day_eq":    return _apply_match_mask((both_valid & (dA.dt.day   != dB.dt.day)) | partial_missing)
    if dc == "date_weekday":
        wA = dA.dt.isocalendar().day.astype(int)
        wB = dB.dt.isocalendar().day.astype(int)
        return _apply_match_mask((both_valid & (wA != wB)) | partial_missing)

    return _apply_match_mask(~both_valid & (~eA | ~eB))


# ── coordinate check ──────────────────────────────────────────────────────────

def _run_coords(
    df: pd.DataFrame,
    cfg: dict,
    *,
    return_detail: bool = False,
):
    """
    Flag rows where the district/tehsil/UC resolved from (lng, lat) doesn't
    match the value already recorded in the verify column.

    With return_detail=True, also returns two lists aligned to df.index:
      resolved_vals : the admin name resolved from coordinates ("actual"
                       geography) — None if coords are missing/invalid or no
                       polygon matched.
      verify_vals   : the value already in the verify column ("expected" /
                       claimed value) — None if missing.
    These are what should be surfaced as a row's actual/expected for the
    coords filter, since there is no single "column value" to read the way
    there is for simple single-column filters.
    """
    fail    = pd.Series(False, index=df.index)
    lng_col = _col(df, cfg, "lngColIdx", "lngColName")
    lat_col = _col(df, cfg, "latColIdx", "latColName")
    ver_col = _col(df, cfg, "verifyColIdx", "verifyColName")
    if lng_col is None or lat_col is None or ver_col is None:
        if return_detail:
            n = len(df)
            return fail, [None] * n, [None] * n
        return fail

    level_key = {
        "district": "ds",
        "tehsil":   "th",
        "uc":       "uc",
    }.get(str(cfg.get("coordLevel", "district")).lower(), "ds")

    # PERF: pull to plain numpy/list ONCE — pandas .at[i] scalar access inside
    # a tight per-row loop is 10-20x slower than plain array indexing for
    # large datasets. This is the dominant cost of the coords filter.
    lngs_np = pd.to_numeric(lng_col, errors="coerce").to_numpy()
    lats_np = pd.to_numeric(lat_col, errors="coerce").to_numpy()
    vers_raw = ver_col.tolist()
    vers_np  = ver_col.astype("string").fillna("").str.strip().str.lower().to_numpy()

    n = len(df)

    # ── Fast path: precomputed raster (see build_coord_raster.py) ──────────
    # Resolves the ENTIRE column in a handful of vectorised numpy ops, no
    # per-row Python loop at all. Falls back to the live grid+ray-cast path
    # below only if coord_raster.npz hasn't been built yet.
    if _load_raster() is not None:
        resolved_list = _resolve_admin_batch_raster(lngs_np, lats_np, level_key)
        valid_mask = ~np.isnan(lngs_np) & ~np.isnan(lats_np)

        verify_list: list[str | None] = [
            (str(v).strip() if v not in (None, "") and str(v).strip().lower() not in NULL_TOKENS else None)
            for v in vers_raw
        ]

        # Vectorised comparison (was a per-row Python loop): fail where coords
        # are missing/invalid, resolution found nothing, or the resolved admin
        # name doesn't match the recorded value (case/whitespace-insensitive).
        resolved_norm = (
            pd.Series(resolved_list, dtype="object")
            .astype("string").str.strip().str.lower()
            .to_numpy(dtype=object, na_value=None)
        )
        resolved_missing = np.array([x is None for x in resolved_list], dtype=bool)
        mismatch = resolved_norm != vers_np
        out_np = ~valid_mask | resolved_missing | mismatch
        out = out_np.tolist()

        result = pd.Series(out, index=df.index)
        if return_detail:
            return result, resolved_list, verify_list
        return result

    # ── Fallback path: live polygon math (no raster file present yet) ──────
    out = [False] * n
    resolved_list = []
    verify_list   = []

    for pos in range(n):
        lng_v = lngs_np[pos]
        lat_v = lats_np[pos]
        ver_raw = vers_raw[pos]
        ver_display = str(ver_raw).strip() if ver_raw not in (None, "") and str(ver_raw).strip().lower() not in NULL_TOKENS else None

        if np.isnan(lng_v) or np.isnan(lat_v):
            out[pos] = True
            resolved_list.append(None)
            verify_list.append(ver_display)
            continue

        resolved, _ = _resolve_admin(float(lng_v), float(lat_v), level_key)

        if resolved is None:
            out[pos] = True
        elif resolved.strip().lower() != vers_np[pos]:
            out[pos] = True

        resolved_list.append(resolved.strip() if resolved else None)
        verify_list.append(ver_display)

    result = pd.Series(out, index=df.index)
    if return_detail:
        return result, resolved_list, verify_list
    return result


# ── filter label ─────────────────────────────────────────────────────────────

COND_LABELS = {
    "empty":       "Missing / Null",
    "notempty":    "Not Empty",
    "dup":         "Duplicate",
    "eq":          "Equal To",
    "neq":         "Not Equal To",
    "gt":          "Greater Than",
    "lt":          "Less Than",
    "gte":         "Greater Than or Equal To",
    "lte":         "Less Than or Equal To",
    "btwn":        "Between",
    "nbtwn":       "Not Between",
    "contains":    "Contains",
    "ncontains":   "Does Not Contain",
    "regex":       "Regex",
    "bad_pattern": "Bad Pattern",
    "date":        "Date Filter",
    "cross":       "Cross Check",
    "doublecross": "Double Cross Check",
    "compare":     "Compare",
    "compare2":    "Compare (Dataset 2)",
    "and":         "AND",
    "or":          "OR",
    "coords":      "Coordinate Check",
    "predefined":  "Predefined Rule",
}

DATE_COND_LABELS = {
    "date_eq":         "Date Equal To",
    "date_neq":        "Date Not Equal To",
    "date_before":     "Date Before",
    "date_after":      "Date After",
    "date_before_eq":  "Date Before or Equal To",
    "date_after_eq":   "Date After or Equal To",
    "date_btwn":       "Date Between",
    "date_nbtwn":      "Date Not Between",
    "date_empty":      "Missing / Null Date",
    "date_invalid":    "Invalid Date Format",
    "date_year_eq":    "Year Equals",
    "date_year_gt":    "Year Greater Than",
    "date_year_lt":    "Year Less Than",
    "date_month_eq":   "Month Equals",
    "date_day_eq":     "Day Equals",
    "date_weekday":    "Weekday Equals",
    "date_future":     "Date in the Future",
    "date_past":       "Date in the Past",
    # legacy
    "invalid":  "Invalid Date",
    "before":   "Date Before",
    "after":    "Date After",
    "between":  "Date Between",
    "future":   "Date in the Future",
    "past":     "Date in the Past",
}


def _filter_label(df: pd.DataFrame, cfg: dict, df2: pd.DataFrame | None = None) -> str:
    cond     = (cfg.get("cond") or "").strip().lower()
    base     = COND_LABELS.get(cond, cond)
    col      = _col(df, cfg)
    col_name = (
        cfg.get("colName")
        or cfg.get("lngColName")
        or (col.name if col is not None else "")
        or ""
    )

    if cond == "predefined":
        return cfg.get("label") or _predefined_rule_label(cfg.get("rule_id", ""))

    if cond == "date":
        dc     = cfg.get("dateCond", "")
        dc_lbl = DATE_COND_LABELS.get(dc, dc)
        v1     = cfg.get("dateVal1", "")
        v2     = cfg.get("dateVal2", "")
        rng    = f" {v1} – {v2}" if v2 else (f" {v1}" if v1 else "")
        return f"{col_name}: {dc_lbl}{rng}" if col_name else f"{dc_lbl}{rng}"

    if cond in ("compare", "compare2"):
        sub     = cfg.get("subCond", "")
        sub_lbl = DATE_COND_LABELS.get(sub, COND_LABELS.get(sub, sub))
        col2_src = df2 if (cond == "compare2" and df2 is not None) else df
        col2    = _col(col2_src, cfg, "col2Idx", "col2Name")
        c2n     = cfg.get("col2Name") or (col2.name if col2 is not None else "")
        prefix  = "Compare (D2)" if cond == "compare2" else "Compare"
        return f"{prefix}: {col_name} {sub_lbl} {c2n}" if col_name else f"{prefix} {sub_lbl}"

    if cond in ("and", "or"):
        subs = cfg.get("subfilters") or []
        parts = []
        for s in subs:
            c = _col(df, s)
            parts.append(cfg.get("colName") or (c.name if c is not None else "?"))
        return f"{base}: {', '.join(parts)}" if parts else base

    if cond == "coords":
        level = cfg.get("coordLevel", "")
        lng_n = cfg.get("lngColName", "Lng")
        lat_n = cfg.get("latColName", "Lat")
        return f"Coord Check ({lng_n}/{lat_n} → {level})"

    if cond == "cross":
        col2 = _col(df2, cfg, "col2Idx", "col2Name") if df2 is not None else None
        c2n  = cfg.get("col2Name") or (col2.name if col2 is not None else "Dataset 2")
        return f"Cross Check: {col_name} vs {c2n}" if col_name else "Cross Check"

    if cond == "doublecross":
        return f"Double Cross: {col_name}" if col_name else "Double Cross Check"

    if cond == "bad_pattern":
        patterns = cfg.get("values") if isinstance(cfg.get("values"), list) else None
        if patterns is None and cfg.get("value"):
            patterns = [p.strip() for p in re.split(r"[,\n]+", str(cfg["value"])) if p.strip()]
        plist = ", ".join(str(p) for p in (patterns or []))
        suffix = f" ({plist})" if plist else ""
        return f"{col_name}: {base}{suffix}" if col_name else f"{base}{suffix}"

    val = cfg.get("value") or cfg.get("matchVal") or ""
    if val:
        return f"{col_name}: {base} {val}" if col_name else f"{base} {val}"

    return f"{col_name}: {base}" if col_name else base


# ── helpers for reading predefined results already in __validation_status__ ───

def _load_existing_status(df: pd.DataFrame) -> dict[int, dict]:
    """
    Parse any existing __validation_status__ column written by cleaning_engine.py.
    Returns {positional_row_index: parsed_status_dict}.
    """
    existing: dict[int, dict] = {}
    if "__validation_status__" not in df.columns:
        return existing
    for pos, raw in enumerate(df["__validation_status__"].tolist()):
        if raw and str(raw).strip().startswith("{"):
            try:
                existing[pos] = json.loads(raw)
            except Exception:
                pass
    return existing


def _collect_predefined_filter_results(
    existing_status: dict[int, dict],
    n_rows: int,
) -> list[dict]:
    """
    Aggregate per-row predefined filter entries into a filter_results summary.

    Returns
    -------
    predefined_filter_results : list of {label, cond, flagged_count, color}
    """
    rule_rows: dict[str, int] = {}         # label → count
    rule_meta: dict[str, dict] = {}        # label → {col, color}

    for pos, status in existing_status.items():
        for f in status.get("filters", []):
            if f.get("cond") != "predefined":
                continue
            label = f.get("label", "")
            if not label:
                continue
            if label not in rule_rows:
                rule_rows[label] = 0
                rule_meta[label] = {
                    "col":   f.get("col", ""),
                    "color": f.get("color", _predefined_rule_color(label)),
                }
            rule_rows[label] += 1

    filter_results = [
        {
            "label":         label,
            "cond":          "predefined",
            "flagged_count": count,
            "col":           rule_meta[label]["col"],
            "color":         rule_meta[label]["color"],
        }
        for label, count in rule_rows.items()
    ]

    return filter_results


# ── main entry point ──────────────────────────────────────────────────────────

def run_validation(
    df: pd.DataFrame,
    filters: list[dict],
    progress_cb: "Callable[..., None] | None" = None,
    df2: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Run all user-configured filter configs against *df*, then merge results
    with any predefined rule failures already written by cleaning_engine.py
    into __validation_status__.

    Parameters
    ----------
    df      : cleaned DataFrame — may already have __validation_status__ from
              cleaning_engine._apply_predefined_validation_to_df()
    filters : list of filter config dicts sent from the JS frontend
    df2     : optional second DataFrame — the real "Dataset 2" reference pool
              for "cross"/"doublecross" filters (uploaded independently via
              the Cross Check / Double Cross Check pill in the frontend, see
              routes_upload.py's generic upload endpoint + routes_clean.py's
              dataset2_file_id body field). A cross/doublecross filter run
              without df2 provided fails safe — flags nothing — rather than
              reading a column out of df itself; a column from Dataset 1 is
              not Dataset 2, even when an index happens to be in range.

    Returns
    -------
    validated_df : pd.DataFrame
        Copy of df with __validation_status__ updated.
        Each cell is a JSON string:
          {
            "result":  "PASS" | "FAIL",
            "filters": [
              {label, cond, col, expected, actual, pass, color?}, ...
            ]
          }
        Predefined rule entries (cond="predefined") are preserved from the
        cleaning stage and appear first in each row's filter list.

    summary : dict
        {
          "total_rows":    int,
          "passed":        int,
          "failed":        int,
          "filter_results": [
            {
              "label":         str,
              "cond":          str,        # "predefined" | actual cond key
              "flagged_count": int,
              "color":         str,        # colour token for frontend highlighting
            }, ...
          ]
        }
        Predefined rule summaries appear first, user filter summaries append after.
        Rows are identified by UUID (already unique) in the cleaned/report
        output — this summary carries counts only, not Excel row-index lists,
        since nothing downstream keys off row index anymore.
    """
    # Shallow copy: run_validation only ADDS/REPLACES the status column —
    # no shared arrays are mutated — so a deep copy of the whole cleaned
    # frame here was pure memory waste (~1x dataset size at peak).
    out = df.copy(deep=False)
    n   = len(df)
    import time as _time

    # ── Step 1: Load predefined results already in the df ────────────────────
    existing_status = _load_existing_status(df)
    predefined_filter_results = _collect_predefined_filter_results(existing_status, n)

    # ── Step 2: Run user-configured filters (vectorised, failure-sparse) ──────
    # OLD behaviour built one entry dict per filter PER ROW (16 filters x 690k
    # rows = 11M dicts) and JSON-dumped a ~2KB blob for every row (~1.4GB of
    # strings) — that alone dominated pipeline wall-clock and memory. NEW
    # behaviour: masks stay fully vectorised; per-row detail entries are built
    # ONLY for rows that FAIL a filter; rows failing nothing get the literal
    # string "PASS". The report UI iterates whatever entries exist per row, so
    # storing only failures renders identically (failed filters highlighted).
    user_filter_results: list[dict] = []
    filter_timings:      list[dict] = []
    # fail_lists[pos] = list of prebuilt JSON entry strings for filters this
    # row FAILED (None = row failed nothing). Entries are composed from a
    # constant per-filter prefix + the row's escaped "actual" value — no
    # per-row dict allocation, no per-row nested json.dumps.
    fail_lists: list = [None] * n
    fail_labels: list = [None] * n   # parallel: failed filter labels per row

    def _esc(v) -> str:
        """JSON-encode a scalar (cached — real data repeats values heavily)."""
        if v is None:
            return "null"
        s = str(v)
        cached = _esc_cache.get(s)
        if cached is None:
            cached = _fast_json_dumps(s)
            if len(_esc_cache) < 500_000:
                _esc_cache[s] = cached
        return cached
    _esc_cache: dict[str, str] = {}

    def _add_failures(fail_pos, prefix: str, actuals=None):
        """Append one prebuilt entry string per failing row. actuals: list
        aligned to fail_pos, or None when the entry has no varying part."""
        if actuals is None:
            entry = prefix + "null}"
            for p in fail_pos.tolist():
                lst = fail_lists[p]
                if lst is None:
                    fail_lists[p] = [entry]
                    fail_labels[p] = [label]
                else:
                    lst.append(entry)
                    fail_labels[p].append(label)
        else:
            fp = fail_pos.tolist()
            for k in range(len(fp)):
                p = fp[k]
                entry = prefix + _esc(actuals[k]) + "}"
                lst = fail_lists[p]
                if lst is None:
                    fail_lists[p] = [entry]
                    fail_labels[p] = [label]
                else:
                    lst.append(entry)
                    fail_labels[p].append(label)

    def _entry_prefix(label, cond, col, expected) -> str:
        head = _fast_json_dumps({
            "label": label, "cond": cond, "col": col,
            "expected": expected, "pass": False,
        })
        # '{"label":...,"pass":false}'  ->  '{"label":...,"pass":false,"actual":'
        return head[:-1] + ',"actual":'

    # Group filters by cond BEFORE processing — the frontend already
    # collapses same-cond filters (e.g. fourteen separate "not-empty"
    # rules on different columns) into one popup row with a "(14)" count.
    # Firing progress_cb's start/end once per INDIVIDUAL filter instead of
    # once per distinct cond used to flip that single row done→running
    # repeatedly, once per filter sharing the cond — the exact same bug
    # special-char cleaning had (see cleaning_engine.py's
    # _step_special_chars_all). Grouping here brackets one start/end pair
    # around all filters of a given cond; per-filter timings for
    # filter_timings are still recorded individually inside the group, so
    # the "seconds per filter" breakdown doesn't lose any precision.
    _cond_groups: dict[str, list[dict]] = {}
    for cfg in filters:
        c = (cfg.get("cond") or "").strip().lower()
        if c == "predefined":
            continue
        _cond_groups.setdefault(c, []).append(cfg)

    for cond, cfgs in _cond_groups.items():
        if progress_cb is not None:
            try: progress_cb("start", cond)
            except Exception: pass
        _group_t0 = _time.perf_counter()

        for cfg in cfgs:
            if "matchVal" in cfg and "value" not in cfg:
                cfg = {**cfg, "value": cfg["matchVal"]}

            label = _filter_label(df, cfg, df2)
            _t0 = _time.perf_counter()

            if cond == "coords":
                try:
                    mask, resolved_arr, verify_arr = _run_coords(df, cfg, return_detail=True)
                except Exception:
                    mask = pd.Series(False, index=df.index)
                    resolved_arr = [None] * n
                    verify_arr   = [None] * n
                mask = mask.fillna(False).astype(bool)

                ver_col_obj  = _col(df, cfg, "verifyColIdx", "verifyColName")
                ver_col_name = cfg.get("verifyColName") or (ver_col_obj.name if ver_col_obj is not None else None)
                mask_arr = mask.to_numpy()
                fail_pos = np.flatnonzero(mask_arr)

                # coords entries vary in BOTH expected (verify col) and actual
                # (resolved geography) — compose with two varying slots.
                base = _fast_json_dumps({"label": label, "cond": cond, "col": ver_col_name, "pass": False})
                head = base[:-1] + ',"expected":'
                fp = fail_pos.tolist()
                for p in fp:
                    entry = head + _esc(verify_arr[p]) + ',"actual":' + _esc(resolved_arr[p]) + "}"
                    lst = fail_lists[p]
                    if lst is None:
                        fail_lists[p] = [entry]
                        fail_labels[p] = [label]
                    else:
                        lst.append(entry)
                        fail_labels[p].append(label)
            else:
                try:
                    mask = _run_filter(df, cfg, df2)
                except Exception:
                    mask = pd.Series(False, index=df.index)
                mask = mask.fillna(False).astype(bool)
                mask_arr = mask.to_numpy()
                fail_pos = np.flatnonzero(mask_arr)

                col_obj  = _col(df, cfg)
                col_name = cfg.get("colName") or (col_obj.name if col_obj is not None else None)
                if cond == "bad_pattern" and isinstance(cfg.get("values"), list):
                    expected = ", ".join(str(v) for v in cfg["values"]) or None
                else:
                    expected = str(
                        cfg.get("value") or cfg.get("matchVal") or
                        cfg.get("dateVal1") or ""
                    ) or None

                prefix = _entry_prefix(label, cond, col_name, expected)

                # "actual" values fetched ONLY for failing rows — not the whole column.
                if col_obj is not None and len(fail_pos):
                    raw_vals = col_obj.iloc[fail_pos].astype("string").tolist()
                    actuals = [
                        None if (a is None or a is pd.NA or str(a) in ("", "nan", "None", "NaT", "<NA>"))
                        else str(a)
                        for a in raw_vals
                    ]
                    _add_failures(fail_pos, prefix, actuals)
                else:
                    _add_failures(fail_pos, prefix, None)

            flagged_count = int(len(fail_pos))
            user_filter_results.append({
                "label":         label,
                "cond":          cond,
                "flagged_count": flagged_count,
                "color":         "red",
            })
            _dt = round(_time.perf_counter() - _t0, 3)
            filter_timings.append({
                "label":   label,
                "cond":    cond,
                "seconds": _dt,
            })

        _group_dt = round(_time.perf_counter() - _group_t0, 3)
        if progress_cb is not None:
            try: progress_cb("end", cond, seconds=_group_dt)
            except Exception: pass

    # ── Step 3: Merge predefined + user results into __validation_status__ ─────
    # All-pass rows cost a single constant write; JSON strings are composed by
    # joining the prebuilt per-filter fragments — no dict/dump per row.
    status_arr   = np.full(n, "PASS", dtype=object)
    readable_arr = np.full(n, "PASS", dtype=object)
    passed = n

    _FAIL_HEAD = '{"result":"FAIL","filters":['
    _PASS_HEAD = '{"result":"PASS","filters":['

    if existing_status:
        # Rows carrying predefined results merge their previous entries first.
        for pos, prev in existing_status.items():
            prev_filters = prev.get("filters", [])
            prev_result  = prev.get("result", "PASS")
            user_lst     = fail_lists[pos]
            prev_frag    = ",".join(_fast_json_dumps(e) for e in prev_filters)
            user_frag    = ",".join(user_lst) if user_lst else ""
            joined       = ",".join(x for x in (prev_frag, user_frag) if x)
            overall_fail = (prev_result == "FAIL") or bool(user_lst)
            if overall_fail:
                passed -= 1
                prev_labels = [f.get("label", "") for f in prev_filters if not f.get("pass", True)]
                labels = [l for l in prev_labels if l] + (fail_labels[pos] or [])
                readable_arr[pos] = ("FAIL: " + " | ".join(labels)) if labels else "FAIL"
            status_arr[pos] = (_FAIL_HEAD if overall_fail else _PASS_HEAD) + joined + "]}"
            fail_lists[pos] = False   # sentinel: already handled

    for pos in range(n):
        lst = fail_lists[pos]
        if not lst:            # None (all-pass) or False (merged above)
            continue
        passed -= 1
        status_arr[pos] = _FAIL_HEAD + ",".join(lst) + "]}"
        readable_arr[pos] = "FAIL: " + " | ".join(fail_labels[pos])

    out["__validation_status__"] = status_arr
    # Human-readable per-row status ("PASS" / "FAIL: <filter labels>") —
    # composed here where the labels are already in hand, so the output
    # writer doesn't have to re-parse every row's JSON to build the column
    # that ships in the cleaned file.
    out["Validation Status"] = readable_arr

    # ── Step 4: Build combined summary ────────────────────────────────────────
    failed = n - passed

    # Predefined summaries first, then user summaries
    all_filter_results = predefined_filter_results + user_filter_results

    summary: dict[str, Any] = {
        "total_rows":     n,
        "passed":         passed,
        "failed":         failed,
        "filter_results": all_filter_results,
        # Real measured wall-clock seconds per user-configured filter —
        # surfaced by the frontend pipeline popup instead of fabricated splits.
        "filter_timings": filter_timings,
    }

    return out, summary
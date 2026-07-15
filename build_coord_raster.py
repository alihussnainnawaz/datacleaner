"""
build_coord_raster.py
──────────────────────
Run this ONCE (and again only if coordinates.json ever changes) to
precompute a coordinate → admin-boundary lookup raster from coordinates.json.

WHY THIS EXISTS
────────────────
The coords validation filter used to do live point-in-polygon ray-casting
against coordinates.json for every single row, every single pipeline run.
This script moves that work to a one-time offline step instead: it
rasterizes every polygon in coordinates.json onto a fine grid (~111m cells
by default) and saves "which admin unit is this cell inside" as a plain
numpy array. At runtime, checking a GPS point becomes a single array
lookup — no polygon math, no ray-casting, nothing per-row at all.

USAGE
──────
    python build_coord_raster.py                  # default ~111m cells
    python build_coord_raster.py --resolution 0.0005   # ~55m cells (finer, bigger file)
    python build_coord_raster.py --resolution 0.002    # ~220m cells (coarser, smaller/faster)

Writes coord_raster.npz next to coordinates.json. validation_engine.py
picks it up automatically the next time the server starts (or the next
coords check, since it's lazy-loaded) — no other code changes needed to
use it once it exists. If this file is missing, the coords filter still
works correctly; it just falls back to the slower live polygon-math path.

OUTPUT FORMAT
──────────────
coord_raster.npz contains:
  raster : int32 array, shape (ny, nx). raster[gy, gx] = index into the
           feature metadata arrays below, or -1 if that cell isn't inside
           any polygon.
  minx, miny, cell, nx, ny : grid geometry (floats/ints) needed to convert
           a (lng, lat) into a (gx, gy) cell index.
  feat_ds, feat_th, feat_uc : parallel string arrays (one entry per
           feature index) — the district/tehsil/UC name for that polygon,
           pulled straight from coordinates.json's properties ("ds"/"th"/"uc").
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

try:
    from matplotlib.path import Path as MplPath
except ImportError:
    raise SystemExit(
        "This script needs matplotlib for fast vectorised point-in-polygon "
        "rasterization. Install it with: pip install matplotlib"
    )

BASE_DIR   = Path(__file__).resolve().parent
GEOJSON    = BASE_DIR / "coordinates.json"
OUT_FILE   = BASE_DIR / "coord_raster.npz"

DEFAULT_RESOLUTION = 0.001  # degrees, ~111m at Pakistan's latitude


def load_features() -> list:
    if not GEOJSON.exists():
        raise SystemExit(f"coordinates.json not found at {GEOJSON}")
    with open(GEOJSON, encoding="utf-8") as f:
        data = json.load(f)
    feats = data.get("geojson", {}).get("features", [])
    if not feats:
        raise SystemExit("coordinates.json has no features under geojson.features")
    return feats


def compute_bbox(features: list) -> tuple[float, float, float, float]:
    minx = miny = math.inf
    maxx = maxy = -math.inf
    for feat in features:
        geom = feat.get("geometry")
        if not geom:
            continue
        gtype = geom.get("type", "")
        if gtype == "Polygon":
            rings = [geom["coordinates"][0]]
        elif gtype == "MultiPolygon":
            rings = [p[0] for p in geom["coordinates"]]
        else:
            continue
        for ring in rings:
            for x, y in ring:
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y
    return minx, maxx, miny, maxy


def build_raster(features: list, cell: float) -> dict:
    minx, maxx, miny, maxy = compute_bbox(features)
    nx = int(math.ceil((maxx - minx) / cell)) + 1
    ny = int(math.ceil((maxy - miny) / cell)) + 1
    print(f"Grid: {nx} x {ny} = {nx * ny:,} cells at {cell}\u00b0 resolution "
          f"(bbox lng [{minx:.4f}, {maxx:.4f}]  lat [{miny:.4f}, {maxy:.4f}])")

    raster = np.full((ny, nx), -1, dtype=np.int32)
    feat_ds, feat_th, feat_uc = [], [], []

    t0 = time.time()
    for fi, feat in enumerate(features):
        props = feat.get("properties", {}) or {}
        feat_ds.append(str(props.get("ds", "") or ""))
        feat_th.append(str(props.get("th", "") or ""))
        feat_uc.append(str(props.get("uc", "") or ""))

        geom = feat.get("geometry")
        if not geom:
            continue
        gtype = geom.get("type", "")
        if gtype == "Polygon":
            rings = [geom["coordinates"][0]]
        elif gtype == "MultiPolygon":
            rings = [p[0] for p in geom["coordinates"]]
        else:
            continue

        for ring in rings:
            lngs = [c[0] for c in ring]
            lats = [c[1] for c in ring]
            bminx, bmaxx = min(lngs), max(lngs)
            bminy, bmaxy = min(lats), max(lats)

            gx0 = max(int((bminx - minx) / cell), 0)
            gx1 = min(int((bmaxx - minx) / cell) + 1, nx)
            gy0 = max(int((bminy - miny) / cell), 0)
            gy1 = min(int((bmaxy - miny) / cell) + 1, ny)
            if gx1 <= gx0 or gy1 <= gy0:
                continue

            # Vectorised point-in-polygon over every cell CENTER within this
            # polygon's own (small) bounding box — this is the whole trick:
            # each polygon only tests the cells it could plausibly contain,
            # not the whole country, and matplotlib's Path.contains_points
            # is a compiled C loop rather than our Python ray-caster.
            xs = minx + (np.arange(gx0, gx1) + 0.5) * cell
            ys = miny + (np.arange(gy0, gy1) + 0.5) * cell
            xx, yy = np.meshgrid(xs, ys)
            pts = np.column_stack([xx.ravel(), yy.ravel()])
            mask = MplPath(ring).contains_points(pts).reshape(yy.shape)

            sub = raster[gy0:gy1, gx0:gx1]
            sub[mask] = fi
            raster[gy0:gy1, gx0:gx1] = sub

        if (fi + 1) % 1000 == 0:
            print(f"  ...{fi + 1}/{len(features)} polygons rasterized "
                  f"({time.time() - t0:.1f}s elapsed)")

    dt = time.time() - t0
    filled = int((raster >= 0).sum())
    print(f"Done: {dt:.1f}s. {filled:,}/{raster.size:,} cells resolved to a polygon "
          f"({filled / raster.size * 100:.1f}%).")

    return {
        "raster": raster,
        "minx": minx, "miny": miny, "cell": cell, "nx": nx, "ny": ny,
        "feat_ds": np.array(feat_ds, dtype=object),
        "feat_th": np.array(feat_th, dtype=object),
        "feat_uc": np.array(feat_uc, dtype=object),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resolution", type=float, default=DEFAULT_RESOLUTION,
                     help=f"Grid cell size in degrees (default {DEFAULT_RESOLUTION} \u2248 111m). "
                          f"Smaller = more accurate at boundaries, larger file, slower build.")
    ap.add_argument("--out", type=Path, default=OUT_FILE,
                     help=f"Output path (default {OUT_FILE})")
    args = ap.parse_args()

    print(f"Loading {GEOJSON} ...")
    features = load_features()
    print(f"{len(features):,} polygon features loaded.")

    result = build_raster(features, args.resolution)

    np.savez_compressed(args.out, **result)
    size_mb = args.out.stat().st_size / 1e6
    print(f"Saved {args.out} ({size_mb:.2f} MB).")
    print("The coords validation filter will pick this up automatically "
          "(restart the backend if it's currently running).")


if __name__ == "__main__":
    main()

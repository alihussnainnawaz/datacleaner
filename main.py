# main.py
from contextlib import asynccontextmanager
import json
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import API_TITLE, API_VERSION, DEBUG
from output_writer import ensure_all_directories
import routes_upload
import routes_clean
from report import routes_report
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_all_directories()  # beneficiary/ banks/ certificates/ financials/
    yield


app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description="Upload CSV/XLSX → run cleaning pipeline → download + paginate results.",
    lifespan=lifespan,
    debug=DEBUG,
)

app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.get("/app", include_in_schema=False)
async def serve_ui():
    return FileResponse("frontend/pages/index.html")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

app.include_router(routes_upload.router, prefix="/api/upload", tags=["Upload"])
app.include_router(routes_clean.router,  prefix="/api/clean",  tags=["Cleaning"])
app.include_router(routes_report.router, prefix="/api/report", tags=["Report"])


@app.get("/api/coordinates", tags=["Geo"])
async def get_coordinates():
    """Serve the GeoJSON boundary data for the Coordinate Check validation filter."""
    p = Path(__file__).resolve().parent / "coordinates.json"
    if not p.exists():
        return JSONResponse({"success": False, "geojson": None, "type": "unknown"})
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return JSONResponse(data)


@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "version": API_VERSION}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "version": API_VERSION}

# ── Project / filter profile storage ──────────────────────────────────────────

from fastapi import Request

BASE_DIR = Path(__file__).resolve().parent
PROJECTS_FILE = BASE_DIR / "projects.json"
if not PROJECTS_FILE.exists():
    PROJECTS_FILE.write_text("[]")


def _load_projects() -> list:
    try:
        return json.loads(PROJECTS_FILE.read_text())
    except Exception:
        return []


def _save_projects(projects: list) -> None:
    PROJECTS_FILE.write_text(json.dumps(projects, indent=2))


@app.get("/api/projects", tags=["Projects"])
async def get_projects():
    return JSONResponse(_load_projects())


@app.post("/api/projects", tags=["Projects"])
async def create_project(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    desc = body.get("description", "").strip()
    if not name:
        return JSONResponse({"success": False, "error": "Name is required"}, status_code=400)
    projects = _load_projects()
    new_id = max((p["id"] for p in projects), default=0) + 1
    proj = {"id": new_id, "name": name, "description": desc,
            "filters": [], "dataType": None, "ipName": None,
            # Cleaner "Column Rule Preview" state — saved so re-selecting this
            # project reapplies exactly what was configured, instead of
            # re-guessing rules from column names on every fresh upload.
            "columnRules": {}, "caseStyles": {}, "globalRules": {},
            "regexRules": {}, "dtypeRules": {}}
    projects.append(proj)
    _save_projects(projects)
    return JSONResponse({"success": True, "project": proj})


@app.put("/api/projects/{project_id}", tags=["Projects"])
async def update_project(project_id: int, request: Request):
    body    = await request.json()
    projects = _load_projects()
    for p in projects:
        if p["id"] == project_id:
            if "filters"  in body: p["filters"]  = body["filters"]
            if "dataType" in body: p["dataType"] = body["dataType"]
            if "ipName"   in body: p["ipName"]   = body["ipName"]
            if "description" in body: p["description"] = body["description"]
            # Cleaner "Column Rule Preview" state (see restoreColumnRulesFromProject
            # in main.js) — persisted so a saved workflow reapplies exactly what
            # was configured rather than re-guessing from column names.
            if "columnRules" in body: p["columnRules"] = body["columnRules"]
            if "caseStyles"  in body: p["caseStyles"]  = body["caseStyles"]
            if "globalRules" in body: p["globalRules"] = body["globalRules"]
            if "regexRules"  in body: p["regexRules"]  = body["regexRules"]
            if "dtypeRules"  in body: p["dtypeRules"]  = body["dtypeRules"]
            _save_projects(projects)
            return JSONResponse({"success": True, "project": p})
    return JSONResponse({"success": False, "error": "Not found"}, status_code=404)


@app.delete("/api/projects/{project_id}", tags=["Projects"])
async def delete_project(project_id: int):
    projects = [p for p in _load_projects() if p["id"] != project_id]
    _save_projects(projects)
    return JSONResponse({"success": True})


# ── Dataset viewer endpoints ───────────────────────────────────────────────────

import glob as _glob
import pandas as pd

def _find_latest_output(data_type: str, ip_name: str | None, suffix: str) -> Path | None:
    """Find the most recently modified *_cleaned or *_report parquet for a project."""
    try:
        from output_writer import resolve_dir
        folder = resolve_dir(data_type, ip_name, create=False)
    except Exception:
        return None
    pattern = str(folder / f"*_{suffix}.parquet")
    files = _glob.glob(pattern)
    if not files:
        return None
    return Path(max(files, key=lambda p: Path(p).stat().st_mtime))


# ── Cached parquet reads ─────────────────────────────────────────────────────
#
# /api/dataset was re-reading the ENTIRE cleaned parquet from disk on every
# single page request (100 rows at a time out of a file that can be 700k+
# rows), and separately re-reading and Python-looping over every row of the
# report parquet just to build cell flags for the one page actually being
# displayed — both from scratch, every page click, with zero caching. That's
# the direct cause of "takes so much to load into pages": clicking to page 2
# repeated the SAME full-file read + full-file loop as page 1, and repeated
# again for page 3, etc. Fixed here with an mtime-keyed cache (same pattern
# as file_handler.py's upload cache) so navigating between pages of the same
# file only pays the read cost once, and by slicing to just the requested
# page's rows before doing any per-row work instead of processing the whole
# dataset on every request.
_PARQUET_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_PARQUET_CACHE_ORDER: list[str] = []
_PARQUET_CACHE_MAX_ENTRIES = 6


def _read_parquet_cached(path: Path) -> pd.DataFrame:
    key = str(path)
    mtime = path.stat().st_mtime
    cached = _PARQUET_CACHE.get(key)
    if cached and cached[0] == mtime:
        if key in _PARQUET_CACHE_ORDER:
            _PARQUET_CACHE_ORDER.remove(key)
        _PARQUET_CACHE_ORDER.append(key)
        return cached[1]
    df = pd.read_parquet(path, engine="fastparquet")
    _PARQUET_CACHE[key] = (mtime, df)
    _PARQUET_CACHE_ORDER.append(key)
    while len(_PARQUET_CACHE_ORDER) > _PARQUET_CACHE_MAX_ENTRIES:
        oldest = _PARQUET_CACHE_ORDER.pop(0)
        if oldest != key:
            _PARQUET_CACHE.pop(oldest, None)
    return df


@app.get("/api/dataset/{data_type}", tags=["Dataset Viewer"])
@app.get("/api/dataset/{data_type}/{ip_name}", tags=["Dataset Viewer"])
async def get_cleaned_dataset(
    data_type: str,
    ip_name: str | None = None,
    page: int = 1,
    page_size: int = 100,
):
    """Return a page of rows from the latest cleaned parquet, with per-cell flags."""
    p = _find_latest_output(data_type, ip_name, "cleaned")
    if not p:
        return JSONResponse({"columns": [], "rows": [], "total_rows": 0,
                             "page": page, "page_size": page_size,
                             "error": "No cleaned file found. Run the pipeline first."})
    try:
        df = _read_parquet_cached(p)
    except Exception as e:
        return JSONResponse({"columns": [], "rows": [], "total_rows": 0,
                             "page": page, "page_size": page_size, "error": str(e)})

    total = len(df)
    start = (page - 1) * page_size
    page_df = df.iloc[start:start + page_size]
    columns = list(df.columns)

    def _base_flags(col_name: str, val: str) -> list[str]:
        flags = []
        v = str(val)
        if v in ("", "nan", "None", "none", "null", "N/A"):
            flags.append("null_value")
        for ch, name in [("@","at"),("!","bang"),("?","question"),("<","angle"),
                         ("{","curly"),("#","hash"),("$","dollar"),("%","percent"),
                         ("^","caret"),("*","star"),("|","pipe"),("~","tilde")]:
            if ch in v:
                flags.append(f"special_{name}")
                break
        return flags

    # ── Load per-cell clean/review flags from report parquet + tool sidecar ──
    # PERF: only the CURRENT PAGE's rows are ever parsed here — the report
    # parquet is row-order-aligned with the cleaned parquet (both written
    # from the same DataFrame), so slicing to the same [start:start+page_size]
    # range before touching any row is exactly equivalent to the old
    # "loop every row, keep only this page's flags" behaviour, just without
    # doing (total_rows / page_size) times more work than necessary.
    import json as _json
    cell_flags: dict[int, dict[str, str]] = {}

    report_path = _find_latest_output(data_type, ip_name, "report")
    if report_path:
        try:
            rdf_full = _read_parquet_cached(report_path)
            rdf_full = rdf_full[rdf_full["uuid"] != "__validation_summary__"]
            rdf = rdf_full.iloc[start:start + page_size]
            for row_i, rrow in zip(range(start, start + len(rdf)), rdf.itertuples(index=False)):
                per_col: dict[str, str] = {}
                try:
                    cv = _json.loads(rrow.cleaned_values) if rrow.cleaned_values else {}
                    for col in cv:
                        per_col[col] = "cleaned"
                except Exception:
                    pass
                try:
                    rv = _json.loads(rrow.manual_reviews) if rrow.manual_reviews else {}
                    for col, v in rv.items():
                        if v is not None and col not in per_col:
                            per_col[col] = "review"
                except Exception:
                    pass
                if per_col:
                    cell_flags[int(row_i)] = per_col
        except Exception:
            pass

    # Source 2: tool-flags sidecar JSON (written by toolbar tools after apply)
    cleaned_path = _find_latest_output(data_type, ip_name, "cleaned")
    if cleaned_path:
        sidecar = Path(str(cleaned_path).replace(".parquet", "__flags__.json"))
        if sidecar.exists():
            try:
                sidecar_flags = _json.loads(sidecar.read_text())
                for k, cols in sidecar_flags.items():
                    row_i = int(k)
                    if row_i < start or row_i >= start + page_size:
                        continue   # only this page's rows matter here
                    if row_i not in cell_flags:
                        cell_flags[row_i] = {}
                    # Tool flags override — most recent action wins
                    cell_flags[row_i].update(cols)
            except Exception:
                pass

    # "__validation_status__" is no longer written into the cleaned parquet
    # (see output_writer.py), and — per explicit request — the dataset
    # viewer grid doesn't show a "Validation Status" column either. PASS/
    # FAIL info still exists (in the report file, and via
    # /api/validation_results for the summary panel); it's just not
    # rendered as an extra column here.
    rows_out = []
    for page_i, (_, row) in enumerate(page_df.iterrows()):
        # Absolute row index in the full df
        abs_i   = start + page_i
        cf      = cell_flags.get(abs_i, {})
        cells   = []
        for col in columns:
            raw = str(row[col]) if row[col] is not None else ""
            flags = _base_flags(col, raw)
            # Overlay pipeline clean/review flags
            cell_flag = cf.get(col)
            if cell_flag == "cleaned":
                flags.append("cell-auto-cleaned")
            elif cell_flag == "review":
                flags.append("cell-needs-review")
            cells.append({"value": raw, "flags": flags})
        rows_out.append(cells)

    return JSONResponse({
        "columns": columns,
        "rows":    rows_out,
        "total_rows":  total,
        "page":        page,
        "page_size":   page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "file":        p.name,
    })


@app.get("/api/validation_results/{data_type}", tags=["Dataset Viewer"])
@app.get("/api/validation_results/{data_type}/{ip_name}", tags=["Dataset Viewer"])
async def get_validation_results(data_type: str, ip_name: str | None = None):
    """Return per-row validation status from the latest report parquet.

    Was reading __validation_status__ off the cleaned parquet — that column
    no longer exists there (moved out of the downloadable cleaned file per
    explicit request); the same content lives in the report file's
    validation_status column instead, keyed in the same row order.
    """
    p = _find_latest_output(data_type, ip_name, "cleaned")
    if not p:
        return JSONResponse({"total_rows": 0, "passed": 0, "failed": 0,
                             "filter_results": [], "failures": [],
                             "error": "No cleaned file found."})
    report_p = _find_latest_output(data_type, ip_name, "report")
    if not report_p:
        try:
            total = len(_read_parquet_cached(p))
        except Exception:
            total = 0
        return JSONResponse({"total_rows": total, "passed": total, "failed": 0,
                             "filter_results": [], "failures": [],
                             "note": "No validation filters were run."})
    try:
        rdf = _read_parquet_cached(report_p)
        rdf = rdf[rdf["uuid"] != "__validation_summary__"]
    except Exception as e:
        return JSONResponse({"total_rows": 0, "passed": 0, "failed": 0,
                             "filter_results": [], "failures": [], "error": str(e)})

    val_col = "validation_status"
    if val_col not in rdf.columns:
        total = len(rdf)
        return JSONResponse({"total_rows": total, "passed": total, "failed": 0,
                             "filter_results": [], "failures": [],
                             "note": "No validation filters were run."})

    total  = len(rdf)
    status = rdf[val_col].reset_index(drop=True)
    is_pass = (status == "PASS")
    passed  = int(is_pass.sum())
    failed  = total - passed

    # PERF: this used to Python-loop and JSON-parse EVERY row just to find
    # the (usually much smaller) subset that failed. Filter to failing rows
    # first with a vectorised comparison, then only loop and parse those —
    # on a mostly-passing dataset this can be an order of magnitude less
    # work, and it's never worse than the old approach.
    failures = []
    import json as _json2
    fail_positions = (~is_pass).to_numpy().nonzero()[0]
    status_arr = status.to_numpy()
    for i in fail_positions.tolist():
        raw_status = str(status_arr[i]) if status_arr[i] else "PASS"
        try:
            vs_obj = _json2.loads(raw_status) if raw_status.startswith("{") else None
        except Exception:
            vs_obj = None

        if vs_obj:
            result = vs_obj.get("result", "PASS")
            if result != "PASS":
                filters_failed = [d["label"] for d in vs_obj.get("filters", []) if not d.get("pass", True)]
                failures.append({
                    "row":            i + 2,
                    "filters_failed": filters_failed,
                    "filter_details": vs_obj.get("filters", []),
                    "status":         result,
                })
        else:
            # Legacy plain-text format
            if raw_status not in ("PASS", ""):
                filters_failed = raw_status.replace("FAIL: ", "").split(" | ")
                failures.append({
                    "row":            i + 2,
                    "filters_failed": filters_failed,
                    "filter_details": [],
                    "status":         "FAIL",
                })

    # Read full filter_results (cond, color, etc.) from sidecar JSON
    import json as _json
    filter_results = []
    try:
        from output_writer import resolve_dir, output_stem
        out_dir = resolve_dir(data_type, ip_name, create=False)
        stem    = output_stem(data_type, p.stem.split("_cleaned")[0], ip_name)
        sidecar = out_dir / f"{stem}_validation_summary.json"
        if sidecar.exists():
            vs = _json.loads(sidecar.read_text())
            filter_results = vs.get("filter_results", [])
    except Exception:
        pass

    # Fallback: build filter_results from failures if sidecar not found
    if not filter_results:
        filter_counts: dict[str, int] = {}
        for f in failures:
            for lbl in f["filters_failed"]:
                filter_counts[lbl] = filter_counts.get(lbl, 0) + 1
        filter_results = [{"label": k, "cond": "", "flagged_count": v}
                          for k, v in filter_counts.items()]

    return JSONResponse({
        "total_rows":     total,
        "passed":         passed,
        "failed":         failed,
        "filter_results": filter_results,
        "failures":       failures[:2000],
    })

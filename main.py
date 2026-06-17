# main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import API_TITLE, API_VERSION, UPLOAD_DIR, LOGS_DIR, DEBUG
from output_writer import ensure_all_directories
import routes_upload
import routes_clean
from report import routes_report


@asynccontextmanager
async def lifespan(app: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ensure_all_directories()  # beneficiary/ banks/ certificates/ financials/
    yield


app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description="Upload CSV/XLSX → run cleaning pipeline → download + paginate results.",
    lifespan=lifespan,
    debug=DEBUG,
)

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


@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "version": API_VERSION}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "version": API_VERSION}
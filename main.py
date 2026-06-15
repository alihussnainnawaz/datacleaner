# main.py

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import API_TITLE, API_VERSION, UPLOAD_DIR, DEBUG
import upload as upload_module
import transform as transform_module


@asynccontextmanager
async def lifespan(app: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"✓ Upload directory ready: {UPLOAD_DIR}")
    print(f"✓ Debug mode: {DEBUG}")
    print(f"✓ {API_TITLE} v{API_VERSION} is running")
    print("✓ Backend API: http://127.0.0.1:8000")
    yield
    print("✓ Shutting down cleanly.")


app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description="""
Data Transformer API — Upload, clean, validate, and export Excel data.
""",
    lifespan=lifespan,
    debug=DEBUG,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


app.include_router(upload_module.router, prefix="/api/upload", tags=["Upload"])
app.include_router(transform_module.router, prefix="/api/transform", tags=["Transform"])


@app.get("/", tags=["Root"])
async def root():
    return {
        "message": "Data Transformer Backend API is running",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["Health"])
async def health():
    upload_dir_exists = UPLOAD_DIR.exists()
    files_count = len(list(UPLOAD_DIR.glob("*"))) if upload_dir_exists else 0

    return JSONResponse(content={
        "status": "healthy",
        "upload_dir": str(UPLOAD_DIR),
        "upload_dir_ok": upload_dir_exists,
        "files_on_disk": files_count,
        "debug": DEBUG,
    })


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error",
            "detail": str(exc) if DEBUG else "Enable DEBUG mode for details.",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=DEBUG,
        workers=1,
    )
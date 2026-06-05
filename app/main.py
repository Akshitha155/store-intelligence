"""
main.py — FastAPI entrypoint for the Purplle Store Intelligence API.
"""

import uuid
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import init_db
from ingestion import router as ingest_router
from metrics   import router as metrics_router
from funnel    import router as funnel_router
from heatmap   import router as heatmap_router
from anomalies import router as anomalies_router
from health    import router as health_router

# ─────────────────────────────────────────────
# STRUCTURED LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("store_intelligence")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info('"Store Intelligence API starting up"')
    init_db()
    yield
    logger.info('"Store Intelligence API shutting down"')


app = FastAPI(
    title="Purplle Store Intelligence API",
    version="1.0.0",
    description="Real-time store analytics from CCTV event streams.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# REQUEST LOGGING MIDDLEWARE
# ─────────────────────────────────────────────
@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    trace_id  = request.headers.get("X-Trace-Id", str(uuid.uuid4())[:8])
    store_id  = request.path_params.get("store_id", "-")
    start     = time.perf_counter()

    response  = await call_next(request)

    latency   = round((time.perf_counter() - start) * 1000, 2)
    logger.info(
        f'{{"trace_id":"{trace_id}","store_id":"{store_id}",'
        f'"endpoint":"{request.url.path}","method":"{request.method}",'
        f'"latency_ms":{latency},"status_code":{response.status_code}}}'
    )
    response.headers["X-Trace-Id"]   = trace_id
    response.headers["X-Latency-Ms"] = str(latency)
    return response


# ─────────────────────────────────────────────
# GLOBAL EXCEPTION HANDLER
# ─────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f'"Unhandled exception: {str(exc)}"')
    return JSONResponse(
        status_code=500,
        content={
            "error":   "internal_server_error",
            "message": "An unexpected error occurred.",
            "path":    str(request.url.path),
        },
    )


# ─────────────────────────────────────────────
# ROUTERS
# ─────────────────────────────────────────────
app.include_router(ingest_router)
app.include_router(metrics_router)
app.include_router(funnel_router)
app.include_router(heatmap_router)
app.include_router(anomalies_router)
app.include_router(health_router)


# ─────────────────────────────────────────────
# DASHBOARD (static HTML)
# ─────────────────────────────────────────────
import os
static_dir = os.path.join(os.path.dirname(__file__), "..", "dashboard")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

templates_dir = os.path.join(os.path.dirname(__file__), "..", "dashboard")

@app.get("/dashboard", include_in_schema=False)
async def dashboard(request: Request):
    from fastapi.responses import FileResponse
    index = os.path.join(templates_dir, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return JSONResponse({"message": "Dashboard not found. Place index.html in /dashboard/"})


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "Purplle Store Intelligence API",
        "version": "1.0.0",
        "docs":    "/docs",
        "health":  "/health",
        "dashboard": "/dashboard",
    }

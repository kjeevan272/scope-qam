from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.logging import get_logger
from app.db import Base, engine
from app.pipeline import run_pipeline
from app.routers import companies, snapshots, uploads
from app.routers import rules, observability, admin
from app.routers import analytics, workflow, exports, fx as fx_router
from app.fx import seed_default_rates
from app.schema_registry import seed_v1

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Creating database schema...")
    Base.metadata.create_all(engine)

    from app.db import SessionLocal
    db = SessionLocal()
    try:
        seed_v1(db)              # bootstrap schema registry with v1 manifest
        seed_default_rates(db)   # bootstrap FX rates for peer benchmarking
    finally:
        db.close()

    log.info("Running ETL pipeline on startup...")
    summary = run_pipeline()
    log.info("Pipeline complete: %s", summary)
    yield


app = FastAPI(
    title="Corporate Credit Rating API",
    description=(
        "Production-grade data platform for corporate credit rating submissions. "
        "Features: multi-tenancy, SCD Type 2, delta detection, cell-level provenance, "
        "schema registry + drift detection, Great Expectations validation, "
        "CDC outbox, queue-based ingestion, OpenLineage events, "
        "RBAC/API keys, retention policies, Prometheus observability."
    ),
    version="3.0.0",
    lifespan=lifespan,
)

app.include_router(companies.router)
app.include_router(snapshots.router)
app.include_router(uploads.router)
app.include_router(rules.router)
app.include_router(observability.router)
app.include_router(admin.router)
app.include_router(analytics.router)
app.include_router(workflow.router)
app.include_router(exports.router)
app.include_router(fx_router.router)


@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "version": "3.0.0"}


@app.get("/", tags=["System"])
def root():
    return {
        "service": "Corporate Credit Rating API",
        "version": "3.0.0",
        "docs": "/docs",
        "metrics": "/metrics",
        "admin": "/admin",
    }

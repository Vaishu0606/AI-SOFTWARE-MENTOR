"""AI Software Mentor — FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.analysis import router as analysis_router
from app.api.arch import router as arch_router
from app.api.health import router as health_router
from app.api.mentor import router as mentor_router
from app.api.repositories import router as repositories_router
from app.api.stream import router as stream_router
from app.core.config import settings
from app.core.logging import configure_logging

configure_logging()

app = FastAPI(
    title=settings.app_name,
    description=(
        "Agentic AI developer assistant for understanding, debugging, "
        "testing, and maintaining unfamiliar codebases."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(health_router)
app.include_router(repositories_router)  # Phase 3: repository ingestion
app.include_router(analysis_router)      # Phase 4: AST analysis
app.include_router(arch_router)          # Phase 5: architecture analysis
app.include_router(mentor_router)        # Phase 6: mentor workflow runs
app.include_router(stream_router)        # Phase 6: SSE workflow event stream

# Future routers (added in later phases):
# from app.api.sessions import router as sessions_router

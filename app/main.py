"""
main.py — Nutriblend API entrypoint.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import database
from app.config import settings
from app.errors import register_error_handlers
from app.routers import auth, users
from app.utils.sms import current_provider_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("nutriblend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify the MongoDB connection and build indexes before serving traffic.
    # A bad MONGO_URI now fails here with a readable message instead of
    # surfacing as a mystery 500 on the first request.
    await database.connect()
    logger.info("SMS provider: %s", current_provider_name())
    yield
    await database.close()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "Mobile-number + OTP authentication for the Nutriblend app. "
        "All endpoints are versioned under `/api/v1`; `/api/health` is unversioned "
        "because it is infrastructure, not API surface."
    ),
)

# Every error leaves the API as {"error": {"code", "message"}} — see app/errors.py.
register_error_handlers(app)

# CORS: allow browsers on any origin to call this API during development.
# Before production, replace "*" with your real frontend domain(s).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router)
app.include_router(auth.router)


@app.get("/api/health", tags=["health"])
async def health():
    """Liveness + dependency check. `database` is "ok" only if Mongo answers."""
    db_status = "ok"
    try:
        await database.ping()
    except Exception as exc:  # noqa: BLE001 — health must never raise
        db_status = f"unavailable: {type(exc).__name__}"

    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "app": settings.APP_NAME,
        "env": settings.ENV,
        "database": db_status,
        "sms_provider": current_provider_name(),
    }

"""
main.py — Nutriblend API entrypoint.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core import database
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.providers.sms import current_provider_name
from app.providers.storage import get_storage
from app.routers import admin, admin_accounts, admin_auth, admin_videos, auth, legal, media, users, videos
from app.schemas.common import ok
from app.services import video_service
from app.utils.network import lan_ip

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
    storage = get_storage()
    logger.info("Media storage: %s (%s)", storage.name, storage.health())
    await video_service.cleanup_expired_uploads()
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

# Every response uses one envelope: {"success", "message", "data" | "error"} —
# see app/schemas/common.py and app/core/errors.py.
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
app.include_router(admin.router)
app.include_router(admin_accounts.router)
app.include_router(admin_auth.router)
app.include_router(admin_videos.router)
app.include_router(videos.router)
app.include_router(media.router)
app.include_router(legal.router)


@app.get("/api/health", tags=["health"])
async def health(request: Request):
    """
    Liveness + dependency check. `database` is "ok" only if Mongo answers.

    Also reports the URLs this server was reached on:

    - `base_url` / `api_base_url` — as seen by this request.
    - `lan_base_url` (development only) — this machine's address on the local
      network. Put this in the mobile app's BASE_URL when testing on a real
      phone connected to the same Wi-Fi. Hidden in production.
    """
    db_status = "ok"
    try:
        await database.ping()
    except Exception as exc:  # noqa: BLE001 — health must never raise
        db_status = f"unavailable: {type(exc).__name__}"

    base_url = str(request.base_url)
    data = {
        "status": "ok" if db_status == "ok" else "degraded",
        "app": settings.APP_NAME,
        "env": settings.ENV,
        "database": db_status,
        "sms_provider": current_provider_name(),
        "storage": f"{get_storage().name} ({get_storage().health()})",
        "base_url": base_url,
        "api_base_url": f"{base_url}{settings.API_PREFIX.strip('/')}/",
    }
    if not settings.is_production:
        ip = lan_ip()
        port = request.url.port
        data["lan_base_url"] = f"http://{ip}{f':{port}' if port else ''}/" if ip else None

    healthy = db_status == "ok"
    return ok(data, "Service is healthy" if healthy else "Service is degraded")

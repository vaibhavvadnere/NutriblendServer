"""
database.py — MongoDB connection (Motor async client).

Works unchanged against a local mongod, a Docker container, or MongoDB Atlas —
the only thing that differs is MONGO_URI in .env:

    local   MONGO_URI=mongodb://localhost:27017
    Atlas   MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority

For Atlas (mongodb+srv://) we hand the driver certifi's CA bundle, because the
Python install on macOS often has no system CA store and the TLS handshake
fails with a confusing "certificate verify failed" otherwise.
"""

import logging

import certifi
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from app.config import settings

logger = logging.getLogger("nutriblend.db")


def _client_kwargs() -> dict:
    kwargs: dict = {
        "serverSelectionTimeoutMS": settings.MONGO_SERVER_SELECTION_TIMEOUT_MS,
        "uuidRepresentation": "standard",
        "appname": settings.APP_NAME,
    }
    # Atlas / any TLS-enabled deployment: use certifi's CA bundle.
    if settings.MONGO_URI.startswith("mongodb+srv://") or "tls=true" in settings.MONGO_URI.lower():
        kwargs["tlsCAFile"] = certifi.where()
    return kwargs


client = AsyncIOMotorClient(settings.MONGO_URI, **_client_kwargs())
db = client[settings.MONGO_DB_NAME]

# Collections
users_collection = db["users"]
otps_collection = db["otps"]
refresh_tokens_collection = db["refresh_tokens"]
rate_limits_collection = db["rate_limits"]


def describe_connection() -> str:
    """The MONGO_URI with any password redacted — safe to log or show in errors."""
    uri = settings.MONGO_URI
    if "@" in uri and "://" in uri:
        scheme, rest = uri.split("://", 1)
        creds, host = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return uri


async def ping() -> None:
    """Raise if the database is unreachable. Used at startup and by check_db.py."""
    await client.admin.command("ping")


async def init_indexes() -> None:
    """Create indexes needed for correctness/performance. Called once on startup."""
    # One account per mobile number. This is the real guard against duplicate
    # signups racing each other — application checks alone cannot guarantee it.
    await users_collection.create_index("mobile_number", unique=True)
    await users_collection.create_index("status")

    if settings.ENFORCE_UNIQUE_EMAIL:
        # Sparse + unique: at most one account per email address, but any number
        # of accounts may have no email at all.
        await users_collection.create_index(
            "email", unique=True, sparse=True, name="email_unique_sparse"
        )

    # TTL on a field that only pending users carry. Once a user is activated the
    # field is unset, and a document with no value for a TTL field never expires
    # — so activated accounts are never touched by this.
    await users_collection.create_index("pending_expires_at", expireAfterSeconds=0)

    await otps_collection.create_index("mobile_number")
    # TTL index: MongoDB auto-deletes OTP docs once expires_at passes.
    await otps_collection.create_index("expires_at", expireAfterSeconds=0)

    # Refresh tokens: looked up by hash, revoked by family or by user.
    await refresh_tokens_collection.create_index("token_hash", unique=True)
    await refresh_tokens_collection.create_index("family_id")
    await refresh_tokens_collection.create_index("mobile_number")
    # TTL index: expired refresh tokens clean themselves up.
    await refresh_tokens_collection.create_index("expires_at", expireAfterSeconds=0)

    # Rate-limit counters expire themselves, so the collection stays small
    # without any cleanup job.
    await rate_limits_collection.create_index("expires_at", expireAfterSeconds=0)


async def connect() -> None:
    """Verify the connection, then build indexes. Fails with a readable message."""
    try:
        await ping()
    except PyMongoError as exc:
        raise RuntimeError(
            f"Could not connect to MongoDB at {describe_connection()}.\n"
            f"  {type(exc).__name__}: {exc}\n"
            "  - Local: is mongod running? (`brew services start mongodb-community`, "
            "or `docker run -d -p 27017:27017 --name mongo mongo`)\n"
            "  - Atlas: check the username/password in MONGO_URI and make sure your "
            "current IP is allowed under Network Access."
        ) from exc

    logger.info("Connected to MongoDB at %s (db=%s)", describe_connection(), settings.MONGO_DB_NAME)
    await init_indexes()


async def close() -> None:
    client.close()

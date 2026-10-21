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

from app.core.config import settings

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
videos_collection = db["videos"]
# Multipart uploads in progress (R2 storage only): upload id + first bytes.
storage_uploads_collection = db["storage_uploads"]
# Payments: plans on sale, Razorpay orders (one per checkout attempt), and each
# user's subscription (one document per user, extended on every purchase).
plans_collection = db["plans"]
payment_orders_collection = db["payment_orders"]
subscriptions_collection = db["subscriptions"]
# Razorpay webhook deliveries already processed (de-duplication + audit trail).
payment_events_collection = db["payment_events"]
# Videos a user marked as favourite: one document per (user, video).
favourites_collection = db["favourites"]
watch_history_collection = db["watch_history"]


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
    # Admin dashboard: newest-first user list and signup / login statistics.
    await users_collection.create_index("created_at")
    await users_collection.create_index("last_login_at")
    await users_collection.create_index("role")

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

    # Videos: app list (published, ordered) and admin list/filters.
    await videos_collection.create_index([("status", 1), ("sort_order", 1), ("published_at", -1)])
    await videos_collection.create_index([("category", 1), ("status", 1)])
    await videos_collection.create_index("created_at")
    # Duplicate detection: same file = same SHA-256.
    await videos_collection.create_index("file.sha256", sparse=True)
    # Abandoned uploads are found (and cleaned up, files included) by this.
    await videos_collection.create_index("upload.expires_at")

    # R2 upload bookkeeping cleans itself up too.
    await storage_uploads_collection.create_index("expires_at", expireAfterSeconds=0)
    # Documents left half-prepared by a restart are found by this.
    await videos_collection.create_index("document.status", sparse=True)

    # Payments. Unique Razorpay ids are the real guard against counting one
    # payment twice (verify + webhook racing, or a webhook delivered twice).
    await plans_collection.create_index([("active", 1), ("sort_order", 1)])
    await payment_orders_collection.create_index("razorpay_order_id", unique=True)
    await payment_orders_collection.create_index("razorpay_payment_id", unique=True, sparse=True)
    await payment_orders_collection.create_index([("user_id", 1), ("created_at", -1)])
    await payment_orders_collection.create_index([("status", 1), ("created_at", -1)])
    # Keyed by the user's _id, not the mobile number: account deletion renames the number.
    await subscriptions_collection.create_index("user_id", unique=True)
    await subscriptions_collection.create_index("expires_at")
    # Razorpay retries a webhook until it gets a 2xx, and may deliver one twice.
    await payment_events_collection.create_index("event_id", unique=True)
    await payment_events_collection.create_index([("razorpay_order_id", 1), ("received_at", -1)])
    # Keep the audit trail for a year, then let Mongo clean it up.
    await payment_events_collection.create_index("received_at", expireAfterSeconds=365 * 24 * 3600)

    # Favourites: the unique pair is the real guard against duplicates (two taps at once).
    await favourites_collection.create_index([("user_id", 1), ("video_id", 1)], unique=True)
    await favourites_collection.create_index([("user_id", 1), ("created_at", -1)])
    await favourites_collection.create_index("video_id")

    # Watch history: one row per (user, video). Unique pair is the real guard
    # against two simultaneous "play" posts creating duplicates.
    await watch_history_collection.create_index(
        [("user_id", 1), ("video_id", 1)], unique=True, name="watch_user_video_unique"
    )
    # The user's history, newest first.
    await watch_history_collection.create_index([("user_id", 1), ("last_watched_at", -1)])
    # Per-video ranking, and clean-up when a video is deleted.
    await watch_history_collection.create_index("video_id")

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

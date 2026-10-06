#!/usr/bin/env python3
"""
verify_atlas.py — Run the real signup/signin flow against the live database.

    ./.venv/bin/python3 scripts/verify_atlas.py

Why this exists: the automated smoke tests run against an in-memory mock Mongo,
which does NOT enforce unique indexes, TTL indexes, or real driver errors. This
script exercises the same flow against whatever MONGO_URI points at, so those
guarantees are actually proven.

Safety:
  * Uses a reserved test number (9999900001) that no real user can have — the
    mobile validator only accepts numbers starting 6-9, and this one is parked
    for testing.
  * Deletes everything it created before exiting, including on failure.
  * Refuses to run when ENV=production.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.core import database  # noqa: E402
from app.core.config import settings  # noqa: E402

TEST_MOBILE = "9999900001"
# NB: not .invalid/.test/.localhost/example.com — email-validator rejects
# special-use domains outright, whatever the DNS settings.
TEST_EMAIL = "atlas.verify@nutriblend-verify.co"

PASSED, FAILED = [], []


class _AbortVerification(Exception):
    """Raised to stop early when a prerequisite step failed, so the output shows
    the real problem instead of a cascade of unrelated errors."""


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    mark = "PASS" if condition else "FAIL"
    print(f"  {mark}  {label}" + (f"\n        {detail}" if detail and not condition else ""))


async def cleanup():
    await database.users_collection.delete_many(
        {"$or": [{"mobile_number": TEST_MOBILE}, {"email": TEST_EMAIL}]}
    )
    await database.otps_collection.delete_many({"mobile_number": TEST_MOBILE})
    await database.refresh_tokens_collection.delete_many({"mobile_number": TEST_MOBILE})
    await database.rate_limits_collection.delete_many({"identifier": TEST_MOBILE})


async def main() -> int:
    if settings.is_production:
        print("Refusing to run against a production environment (ENV=production).")
        return 2

    print(f"Target   : {database.describe_connection()}")
    print(f"Database : {settings.MONGO_DB_NAME}")
    print(f"Test user: {TEST_MOBILE} (created and deleted by this script)\n")

    from app.main import app  # imported late, after settings are read

    try:
        await database.connect()
    except RuntimeError as exc:
        print(f"FAILED to connect:\n{exc}")
        return 1

    await cleanup()
    V1 = settings.API_PREFIX

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://verify") as c:

            print("1. Health")
            r = await c.get("/api/health")
            check("health reports database ok", r.json().get("data", {}).get("database") == "ok", r.text)

            print("\n2. Signup against the real database")
            r = await c.post(
                f"{V1}/auth/signup",
                json={"mobile_number": TEST_MOBILE, "name": "Atlas Verify", "email": TEST_EMAIL},
            )
            check("signup -> 201", r.status_code == 201, r.text)
            otp = r.json().get("data", {}).get("dev_otp")
            check("dev_otp present (ENV != production)", bool(otp), r.text)

            doc = await database.users_collection.find_one({"mobile_number": TEST_MOBILE})
            check("user persisted to Atlas", doc is not None)
            check("status is pending", doc and doc.get("status") == "pending", doc and doc.get("status"))
            check("pending TTL field set", doc and doc.get("pending_expires_at") is not None)

            if doc is None or not otp:
                print("\n  Signup did not succeed — skipping the rest, since every later "
                      "check depends on it.")
                raise _AbortVerification()

            print("\n3. Real unique index enforcement (mock Mongo cannot prove this)")
            from pymongo.errors import DuplicateKeyError

            duplicate_rejected = False
            try:
                await database.users_collection.insert_one(
                    {"mobile_number": TEST_MOBILE, "name": "Dupe", "created_at": doc["created_at"]}
                )
            except DuplicateKeyError:
                duplicate_rejected = True
            check("duplicate mobile_number rejected by the database", duplicate_rejected)

            email_rejected = False
            if settings.ENFORCE_UNIQUE_EMAIL:
                try:
                    await database.users_collection.insert_one(
                        {
                            "mobile_number": "9999900002",
                            "email": TEST_EMAIL,
                            "name": "Dupe Email",
                            "created_at": doc["created_at"],
                        }
                    )
                except DuplicateKeyError:
                    email_rejected = True
                finally:
                    await database.users_collection.delete_many({"mobile_number": "9999900002"})
                check("duplicate email rejected by the sparse unique index", email_rejected)

                # Two users with NO email must both be allowed — this is what
                # "sparse" buys us, and getting it wrong breaks every signup
                # that omits the optional email.
                await database.users_collection.delete_many({"mobile_number": {"$in": ["9999900003", "9999900004"]}})
                both_allowed = True
                try:
                    await database.users_collection.insert_one({"mobile_number": "9999900003", "name": "No Email 1", "created_at": doc["created_at"]})
                    await database.users_collection.insert_one({"mobile_number": "9999900004", "name": "No Email 2", "created_at": doc["created_at"]})
                except DuplicateKeyError:
                    both_allowed = False
                finally:
                    await database.users_collection.delete_many({"mobile_number": {"$in": ["9999900003", "9999900004"]}})
                check("two accounts without an email are both allowed", both_allowed)

            print("\n4. Verify OTP -> tokens")
            r = await c.post(f"{V1}/auth/verify-otp", json={"mobile_number": TEST_MOBILE, "otp": otp})
            check("verify -> 200", r.status_code == 200, r.text)
            body = r.json().get("data", {}) if r.status_code == 200 else {}
            access = body.get("access_token")
            refresh = body.get("refresh_token")
            check("account activated", body.get("user", {}).get("status") == "active", str(body.get("user")))

            doc = await database.users_collection.find_one({"mobile_number": TEST_MOBILE})
            check("pending_expires_at removed on activation", doc and "pending_expires_at" not in doc)
            check("verified_at persisted", doc and doc.get("verified_at") is not None)

            print("\n5. Protected route + token rotation")
            r = await c.get(f"{V1}/users/me", headers={"Authorization": f"Bearer {access}"})
            check("/me -> 200", r.status_code == 200, r.text)
            r = await c.post(f"{V1}/auth/refresh", json={"refresh_token": refresh})
            check("refresh -> 200", r.status_code == 200, r.text)
            rotated = r.json().get("data", {}).get("refresh_token") if r.status_code == 200 else None
            check("refresh token rotated", bool(rotated) and rotated != refresh)
            r = await c.post(f"{V1}/auth/refresh", json={"refresh_token": refresh})
            check("replaying the old token -> 401", r.status_code == 401, r.text)

            print("\n6. Rate limiting against the real database")
            await database.rate_limits_collection.delete_many({"identifier": TEST_MOBILE})
            r = await c.post(f"{V1}/auth/signin", json={"mobile_number": TEST_MOBILE})
            check("first OTP allowed", r.status_code == 200, r.text)
            r = await c.post(f"{V1}/auth/signin", json={"mobile_number": TEST_MOBILE})
            check("second within cooldown -> 429", r.status_code == 429, r.text)
            check("Retry-After header sent", "retry-after" in {k.lower() for k in r.headers})

            print("\n7. Index inventory")
            for coll in (
                database.users_collection,
                database.otps_collection,
                database.refresh_tokens_collection,
                database.rate_limits_collection,
            ):
                info = await coll.index_information()
                ttl = [n for n, v in info.items() if "expireAfterSeconds" in v]
                print(f"  {coll.name:16} {len(info)} indexes"
                      + (f"  (TTL: {', '.join(ttl)})" if ttl else ""))

    except _AbortVerification:
        pass
    finally:
        await cleanup()
        print("\nTest data cleaned up.")

    print(f"\n{'=' * 56}\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("FAILED: " + "; ".join(FAILED))
    await database.close()
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

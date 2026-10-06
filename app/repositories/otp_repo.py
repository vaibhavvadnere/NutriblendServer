"""
repositories/otp_repo.py — Data access for the `otps` collection.

One document per mobile number; requesting a new OTP overwrites the old one.
Only the OTP's hash is stored. The `expires_at` TTL index deletes stale records.
"""

from datetime import datetime
from typing import Optional

from pymongo import ReturnDocument

from app.core.database import otps_collection


async def save(mobile_number: str, otp_hash: str, expires_at: datetime, created_at: datetime) -> None:
    """Store a fresh OTP for this number, replacing any previous one."""
    await otps_collection.update_one(
        {"mobile_number": mobile_number},
        {
            "$set": {
                "otp_hash": otp_hash,
                "expires_at": expires_at,
                "attempts": 0,
                "created_at": created_at,
            }
        },
        upsert=True,
    )


async def find_by_mobile(mobile_number: str) -> Optional[dict]:
    return await otps_collection.find_one({"mobile_number": mobile_number})


async def increment_attempts(otp_id) -> Optional[dict]:
    """Count one failed attempt and return the updated record."""
    return await otps_collection.find_one_and_update(
        {"_id": otp_id}, {"$inc": {"attempts": 1}}, return_document=ReturnDocument.AFTER
    )


async def delete(otp_id) -> None:
    await otps_collection.delete_one({"_id": otp_id})


async def delete_by_mobile(mobile_number: str) -> None:
    await otps_collection.delete_one({"mobile_number": mobile_number})

#!/usr/bin/env python3
"""
check_db.py — Verify the MongoDB connection in .env before starting the server.

    .venv/bin/python3 scripts/check_db.py

Prints the server version, the database name, the collections it can see, and
the indexes it created. Use this right after pasting an Atlas connection string
into .env — it tells you whether the URI, the password and the IP allowlist are
all correct, without having to start uvicorn and read a stack trace.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import database  # noqa: E402
from app.core.config import settings  # noqa: E402


async def main() -> int:
    print(f"URI      : {database.describe_connection()}")
    print(f"Database : {settings.MONGO_DB_NAME}")
    print("Connecting...", flush=True)

    try:
        await database.connect()
    except RuntimeError as exc:
        print(f"\nFAILED\n{exc}")
        return 1

    info = await database.client.server_info()
    print(f"\nConnected. MongoDB version {info.get('version')}")

    names = await database.db.list_collection_names()
    print(f"Collections: {', '.join(sorted(names)) if names else '(none yet)'}")

    for coll in (database.users_collection, database.otps_collection, database.refresh_tokens_collection):
        indexes = await coll.index_information()
        print(f"  {coll.name}: indexes {', '.join(sorted(indexes))}")

    await database.close()
    print("\nAll good — you can start the server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

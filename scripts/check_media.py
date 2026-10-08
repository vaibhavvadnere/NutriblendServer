#!/usr/bin/env python3
"""
check_media.py — Find (and optionally delete) videos whose files are missing
from the current storage (STORAGE_PROVIDER).

    .venv/bin/python3 scripts/check_media.py            # report only
    .venv/bin/python3 scripts/check_media.py --delete   # delete those video records (asks first)

Typical cause: videos uploaded while STORAGE_PROVIDER=local, after switching
to r2 — their files were on the old server disk, not in the R2 bucket.
Deleting a record also removes whatever files of it do still exist.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import database  # noqa: E402
from app.providers.storage import get_storage  # noqa: E402
from app.services import video_service  # noqa: E402


async def main(delete: bool, assume_yes: bool) -> int:
    await database.connect()
    storage = get_storage()
    print(f"Storage: {storage.name}")
    result = await video_service.storage_check()
    print(f"Checked {result.checked} video(s): {len(result.broken)} with missing files"
          + (f", {result.errors} could not be checked (storage unreachable)" if result.errors else ""))
    for b in result.broken:
        print(f"  - {b.id}  [{b.status}]  {b.created_at:%d %b %Y %H:%M}  \"{b.title}\"  missing: {', '.join(b.missing_files)}")
    if not result.broken or not delete:
        if result.broken:
            print("\nRun again with --delete to remove these records.")
        return 0
    if not assume_yes:
        answer = input(f"\nDelete these {len(result.broken)} video record(s)? This cannot be undone. [y/N] ")
        if answer.strip().lower() != "y":
            print("Nothing deleted.")
            return 0
    for b in result.broken:
        await video_service.delete_video(b.id)
        print(f"  deleted {b.id}  \"{b.title}\"")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--delete", action="store_true", help="delete the video records with missing files")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.delete, args.yes)))

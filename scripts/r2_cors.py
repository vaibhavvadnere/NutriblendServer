#!/usr/bin/env python3
"""
r2_cors.py — Let the dashboard's upload box (running in an admin's browser)
send video pieces straight into the R2 bucket.

Browsers only PUT to another site when that site allows it (CORS). This sets
the bucket's CORS rules to allow PUT from DASHBOARD_ORIGINS (in .env), with
the Content-MD5 header, and lets the browser read the ETag. Nothing else is
allowed: no listing, no reading, no deleting from a browser.

    .venv/bin/python3 scripts/r2_cors.py          # show the rules to paste + current rules
    .venv/bin/python3 scripts/r2_cors.py --apply  # set them from DASHBOARD_ORIGINS

The app's R2 key normally only reads/writes files (Object Read & Write) and is
NOT allowed to change bucket settings — then --apply prints the JSON to paste
in Cloudflare: R2 → bucket → Settings → CORS Policy → Add CORS policy.

Run --apply again whenever DASHBOARD_ORIGINS changes (e.g. when the dashboard
gets its own web address).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402


def rules(origins: list[str]) -> dict:
    return {
        "CORSRules": [
            {
                "AllowedOrigins": origins,
                "AllowedMethods": ["PUT"],
                "AllowedHeaders": ["content-md5", "content-type"],
                "ExposeHeaders": ["ETag"],
                "MaxAgeSeconds": 3600,
            }
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="set the rules from DASHBOARD_ORIGINS")
    args = parser.parse_args()

    if settings.STORAGE_PROVIDER != "r2":
        print("STORAGE_PROVIDER is not r2 — nothing to do (local uploads go through the server).")
        return 0
    from botocore.exceptions import ClientError

    from app.providers.storage.r2 import R2Storage

    r2 = R2Storage()
    origins = [o.rstrip("/") for o in settings.DASHBOARD_ORIGINS if o.strip()]
    if not origins or "*" in origins:
        print("Set DASHBOARD_ORIGINS in .env to the exact dashboard address(es) first (no '*').")
        return 1
    paste = json.dumps(rules(origins)["CORSRules"], indent=2)

    def manual(reason: str) -> None:
        print(f"{reason}\n\nSet it in Cloudflare instead: R2 → bucket {r2.bucket} → Settings → "
              f"CORS Policy → Add CORS policy (or Edit), paste this and save:\n\n{paste}\n")

    if args.apply:
        try:
            r2.client.put_bucket_cors(Bucket=r2.bucket, CORSConfiguration=rules(origins))
            print(f"Bucket {r2.bucket}: browser uploads allowed from {', '.join(origins)}")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "AccessDenied":
                raise
            manual("This R2 key can't change bucket settings (it only reads/writes files — that's good).")
            return 0
    try:
        current = r2.client.get_bucket_cors(Bucket=r2.bucket)["CORSRules"]
        print("Current CORS rules:\n" + json.dumps(current, indent=2))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("NoSuchCORSConfiguration", "NoSuchCORSConfig"):
            manual("No CORS rules yet — browser uploads straight to R2 are blocked.")
        elif code == "AccessDenied":
            manual("This R2 key can't read bucket settings, so the current rules can't be shown.")
        else:
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
services/legal_service.py — The privacy policy and account-deletion pages.

Single source of truth: the Markdown templates in app/legal/*.md, filled with
LEGAL_* values from .env. The same text is served

    publicly as HTML   GET /privacy-policy, GET /account-deletion  (Play Store link, in-app link)
    as JSON            GET /api/v1/legal/privacy-policy            (dashboard + app)

and the JSON includes a Google Play readiness checklist, so the dashboard can
show what is still missing before the policy is submitted.
"""

import hashlib
import html
import re
from functools import lru_cache
from pathlib import Path

import markdown as md

from app.core.config import settings
from app.providers.sms import current_provider_name

LEGAL_DIR = Path(__file__).resolve().parents[1] / "legal"
POLICY_PATH = "/privacy-policy"
DELETION_PATH = "/account-deletion"
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")

_SMS_NAMES = {"msg91": "MSG91", "twilio": "Twilio", "fast2sms": "Fast2SMS", "mock": "SMS gateway (to be configured)"}

# field -> (label shown in the checklist, value)
def _fields(base_url: str) -> dict[str, tuple[str, str]]:
    return {
        "app_name": ("App name", settings.LEGAL_APP_NAME),
        "company_name": ("Company / developer name", settings.LEGAL_COMPANY_NAME),
        "company_address": ("Company address", settings.LEGAL_COMPANY_ADDRESS),
        "contact_email": ("Privacy contact email", settings.LEGAL_CONTACT_EMAIL),
        "grievance_officer": ("Grievance Officer name", settings.LEGAL_GRIEVANCE_OFFICER),
        "grievance_email": ("Grievance Officer email", settings.LEGAL_GRIEVANCE_EMAIL or settings.LEGAL_CONTACT_EMAIL),
        "hosting_provider": ("Server hosting provider", settings.LEGAL_HOSTING_PROVIDER),
        "effective_date": ("Effective date", settings.PRIVACY_POLICY_EFFECTIVE_DATE),
        "sms_provider": ("SMS provider", _SMS_NAMES.get(current_provider_name(), current_provider_name())),
        "deletion_days": ("Deletion time (days)", str(settings.LEGAL_DELETION_DAYS)),
        "log_retention_days": ("Log retention (days)", str(settings.LEGAL_LOG_RETENTION_DAYS)),
        "policy_url": ("Policy URL", f"{base_url}{POLICY_PATH}"),
        "deletion_url": ("Account deletion URL", f"{base_url}{DELETION_PATH}"),
    }


@lru_cache(maxsize=4)
def _template(name: str) -> str:
    return (LEGAL_DIR / f"{name}.md").read_text(encoding="utf-8")


def render_markdown(name: str, base_url: str) -> tuple[str, list[str]]:
    """Filled Markdown and the labels of fields that are still empty."""
    fields = _fields(base_url)
    missing: list[str] = []

    def fill(match: re.Match) -> str:
        label, value = fields.get(match.group(1), (match.group(1), ""))
        if value.strip():
            return value.strip()
        if label not in missing:
            missing.append(label)
        return f"[{label} — to be filled]"

    return _PLACEHOLDER.sub(fill, _template(name)), missing


def version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:10]


def checklist(base_url: str) -> list[dict]:
    """Google Play privacy requirements, each with ok / warn and a hint."""
    _, missing = render_markdown("privacy_policy", base_url)
    public = settings.PUBLIC_BASE_URL.strip()
    https_public = public.startswith("https://") and not re.search(r"localhost|127\.0\.0\.1|0\.0\.0\.0|192\.168\.", public)
    return [
        {"item": "Privacy policy page exists (HTML, not a PDF)", "ok": True,
         "hint": f"Served at {base_url}{POLICY_PATH}."},
        {"item": "Publicly reachable HTTPS address (for Play Console + in-app link)", "ok": https_public,
         "hint": "Deploy the server on a public domain and set PUBLIC_BASE_URL=https://… in .env."
                 if not https_public else f"Use {public}{POLICY_PATH} in Play Console."},
        {"item": "Developer details, privacy contact and Grievance Officer filled in", "ok": not missing,
         "hint": "Fill in .env: " + ", ".join(missing) if missing else "All details present."},
        {"item": "Names the app and the developer shown on the Play listing", "ok": bool(settings.LEGAL_COMPANY_NAME.strip()),
         "hint": "LEGAL_COMPANY_NAME must match the developer name on your Play Store listing."},
        {"item": "Describes data collected, use, sharing, security, retention and deletion", "ok": True,
         "hint": "Covered in sections 2–7 of the policy."},
        {"item": "Account deletion request page on the web", "ok": True,
         "hint": f"Served at {base_url}{DELETION_PATH} — enter this in Play Console's Data safety → Data deletion."},
        {"item": "Account deletion inside the app", "ok": False,
         "hint": "Not built yet: needs a server endpoint (DELETE /api/v1/users/me) and a 'Delete account' button in the Android app."},
        {"item": "Real SMS provider configured (named in the policy)", "ok": current_provider_name() != "mock",
         "hint": "Set SMS_PROVIDER to msg91 / twilio / fast2sms before going live."},
        {"item": "Production mode (no development OTPs in responses)", "ok": settings.is_production,
         "hint": "Set ENV=production before going live."},
    ]


_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#fcfcfb; --fg:#1b1b1a; --muted:#5f5e5a; --line:#e6e5e1; --accent:#2e7d32; --card:#ffffff; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161615; --fg:#f1f0ec; --muted:#b5b3ab; --line:#33322f; --accent:#6cc070; --card:#1e1e1c; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:820px; margin:0 auto; padding:32px 16px 64px; }}
h1 {{ font-size:1.9rem; line-height:1.25; margin:0 0 8px; }}
h2 {{ font-size:1.25rem; margin:36px 0 8px; padding-top:12px; border-top:1px solid var(--line); }}
a {{ color:var(--accent); }}
table {{ width:100%; border-collapse:collapse; margin:12px 0; font-size:.95rem; display:block; overflow-x:auto; }}
th,td {{ text-align:left; vertical-align:top; padding:8px 10px; border-bottom:1px solid var(--line); }}
th {{ color:var(--muted); font-weight:600; }}
hr {{ border:0; border-top:1px solid var(--line); margin:32px 0; }}
footer {{ color:var(--muted); font-size:.85rem; margin-top:40px; }}
</style></head>
<body><main>
{body}
<footer>Version {version}</footer>
</main></body></html>"""


def html_page(name: str, base_url: str) -> str:
    text, _ = render_markdown(name, base_url)
    body = md.markdown(text, extensions=["tables"], output_format="html")
    title = text.splitlines()[0].lstrip("# ").strip()
    return _PAGE.format(title=html.escape(title), body=body, version=version(text))

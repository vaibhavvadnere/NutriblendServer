"""
routers/legal.py — Public legal pages (no login).

    GET /privacy-policy                 HTML — the URL for Play Console and the in-app link
    GET /account-deletion               HTML — Play Console "Data deletion" web link
    GET /api/v1/legal/privacy-policy    JSON — text + Google Play checklist (dashboard / app)
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.config import settings
from app.core.deps import public_base_url
from app.schemas.common import ok
from app.services import legal_service

router = APIRouter(tags=["legal"])


@router.get(legal_service.POLICY_PATH, response_class=HTMLResponse, summary="Privacy policy (public HTML)")
async def privacy_policy_page(request: Request):
    return HTMLResponse(legal_service.html_page("privacy_policy", public_base_url(request)),
                        headers={"Cache-Control": "public, max-age=300"})


@router.get(legal_service.DELETION_PATH, response_class=HTMLResponse, summary="Account deletion (public HTML)")
async def account_deletion_page(request: Request):
    return HTMLResponse(legal_service.html_page("account_deletion", public_base_url(request)),
                        headers={"Cache-Control": "public, max-age=300"})


@router.get(f"{settings.API_PREFIX}/legal/privacy-policy", summary="Privacy policy text + Play Store checklist")
async def privacy_policy_json(request: Request):
    base = public_base_url(request)
    text, missing = legal_service.render_markdown("privacy_policy", base)
    return ok(
        {
            "markdown": text,
            "version": legal_service.version(text),
            "effective_date": settings.PRIVACY_POLICY_EFFECTIVE_DATE or None,
            "policy_url": f"{base}{legal_service.POLICY_PATH}",
            "deletion_url": f"{base}{legal_service.DELETION_PATH}",
            "missing_fields": missing,
            "play_store_checklist": legal_service.checklist(base),
        },
        "Privacy policy",
    )

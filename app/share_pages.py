"""Branded, cache-safe responses for public workspace shares."""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.static_assets import STATIC_ASSET_VERSION


templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.globals["app_version"] = settings.APP_VERSION
templates.env.globals["static_version"] = STATIC_ASSET_VERSION

SHARE_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
}

ERROR_CONTENT = {
    "invalid": (
        "Paylaşım bağlantısı geçersiz",
        "Bu bağlantı doğrulanamadı. Bağlantının eksiksiz ve güncel olduğundan emin olun.",
        "!",
    ),
    "expired": (
        "Paylaşımın süresi doldu",
        "Bu paylaşım için belirlenen erişim süresi sona ermiş.",
        "⌛",
    ),
    "deleted": (
        "Paylaşılan çalışma alanı silinmiş",
        "Bu bağlantının ait olduğu çalışma alanı artık mevcut değil.",
        "×",
    ),
    "revoked": (
        "Paylaşım bağlantısı artık kullanılamıyor",
        "Bağlantı, paylaşımı oluşturan kullanıcı tarafından devre dışı bırakılmış.",
        "×",
    ),
    "unavailable": (
        "Paylaşılan uygulamaya şu anda erişilemiyor",
        "Uygulama henüz hazır olmayabilir veya geçici olarak çevrim dışı olabilir.",
        "…",
    ),
}


def render_share_page(
    request: Request,
    *,
    state: str,
    title: str,
    message: str,
    icon: str,
    status_code: int = 200,
    sharer_label: str | None = None,
    remaining_text: str | None = None,
    password_required: bool = False,
    error_message: str | None = None,
    retryable: bool = False,
    extra_headers: dict[str, str] | None = None,
):
    headers = dict(SHARE_PAGE_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    return templates.TemplateResponse(
        request=request,
        name="share_page.html",
        context={
            "state": state,
            "title": title,
            "message": message,
            "icon": icon,
            "sharer_label": sharer_label,
            "remaining_text": remaining_text,
            "password_required": password_required,
            "error_message": error_message,
            "retryable": retryable,
        },
        status_code=status_code,
        headers=headers,
    )


def render_share_error(request: Request, error):
    state = getattr(error, "share_state", "invalid")
    title, message, icon = ERROR_CONTENT.get(state, ERROR_CONTENT["invalid"])
    status_code = getattr(error, "status_code", 404)
    retryable = state == "unavailable" and status_code >= 500
    extra_headers = getattr(error, "headers", None)
    return render_share_page(
        request,
        state=state,
        title=title,
        message=message,
        icon=icon,
        status_code=status_code,
        retryable=retryable,
        extra_headers=extra_headers,
    )

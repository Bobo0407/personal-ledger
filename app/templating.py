"""Jinja2 模板與頁面輸出。所有整頁回應都經過 render()，確保表單一定拿得到 CSRF 權杖。"""

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from app import csrf
from app.config import Settings

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    settings: Settings = request.app.state.settings
    cookie_value, is_new = csrf.ensure_cookie_value(request)

    ctx = dict(context or {})
    ctx["csrf_token"] = csrf.sign(settings.secret_key, cookie_value)

    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    if is_new:
        csrf.set_cookie(response, cookie_value, settings)
    return response

"""FastAPI 應用程式工廠。

啟動方式：uvicorn app.main:create_app --factory
用工廠而不是模組層級的 app 物件：import 這個模組不會讀設定，
而真正啟動時會立刻呼叫 get_settings()，缺 SECRET_KEY 就直接啟動失敗。
"""

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app.config import Settings, get_settings
from app.csrf import csrf_protect
from app.deps import NotAuthenticated, login_url
from app.logging_config import setup_logging
from app.routers import auth as auth_router
from app.routers import budgets as budgets_router
from app.routers import home as home_router
from app.routers import merchants as merchants_router
from app.routers import settings as settings_router
from app.routers import txn as txn_router
from app.templating import templates

STATIC_DIR = Path(__file__).resolve().parent / "static"

logger = logging.getLogger(__name__)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


async def _redirect_to_login(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, NotAuthenticated)
    if _is_htmx(request):
        # HTMX 會在背景跟隨 303，把登入頁塞進局部區塊；改用 HX-Redirect 讓整頁導向
        return Response(status_code=200, headers={"HX-Redirect": login_url(exc.next_path)})
    return RedirectResponse(login_url(exc.next_path), status_code=303)


async def _server_error(request: Request, exc: Exception) -> Response:
    """規格 9.3：畫面只顯示「發生錯誤，資料未變更」，詳情寫進日誌。

    資料庫交易不需要在這裡處理：服務層只在成功時 commit，
    例外發生時 get_db 會關閉 Session，未提交的變更隨之捨棄。
    """
    # 只記方法與路徑，不記查詢字串與表單內容
    logger.error("unhandled error %s %s", request.method, request.url.path, exc_info=exc)
    if _is_htmx(request):
        return templates.TemplateResponse(request, "partials/error.html", {}, status_code=500)
    return templates.TemplateResponse(request, "pages/error.html", {}, status_code=500)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings)

    app = FastAPI(
        title="ledger",
        # 個人系統不需要公開 API 文件
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(csrf_protect)],
    )
    app.state.settings = settings

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth_router.router)
    app.include_router(home_router.router)
    app.include_router(budgets_router.router)
    app.include_router(merchants_router.router)
    app.include_router(settings_router.router)
    app.include_router(txn_router.router)
    app.add_exception_handler(NotAuthenticated, _redirect_to_login)
    app.add_exception_handler(Exception, _server_error)
    return app

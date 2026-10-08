"""/ 佔位首頁（P0）。P3 會換成真正的首頁（規格 8.1）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from app.deps import require_user
from app.models import User
from app.templating import render

router = APIRouter()


@router.get("/")
def home(request: Request, user: Annotated[User, Depends(require_user)]) -> Response:
    return render(request, "pages/home.html", {"user": user})

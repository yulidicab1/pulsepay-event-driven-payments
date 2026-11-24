from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import get_auth_service, require_origin
from app.core.config import settings
from app.schemas.auth import LoginRequest, SessionInfo
from app.services.auth_service import AuthService

router = APIRouter()


def set_session_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


def info(record) -> SessionInfo:
    return SessionInfo(email=record.user.email, merchant_id=record.user.merchant_id, csrf_token=record.csrf_token)


@router.get("/csrf")
def csrf(request: Request, response: Response, service: AuthService = Depends(get_auth_service)):
    token, record = service.csrf(
        request.cookies.get(settings.session_cookie_name),
        request.client.host if request.client else "unknown",
    )
    if token is not None:
        set_session_cookie(response, token, 600)
    return {"csrf_token": record.csrf_token}


@router.post("/login", response_model=SessionInfo, dependencies=[Depends(require_origin)])
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
):
    token, record = service.login(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get("X-CSRF-Token"),
        str(payload.email),
        payload.password.get_secret_value(),
        request.client.host if request.client else "unknown",
    )
    set_session_cookie(response, token, settings.session_absolute_seconds)
    return info(record)


@router.get("/session", response_model=SessionInfo)
def session(request: Request, service: AuthService = Depends(get_auth_service)):
    return info(service.session_info(request.cookies.get(settings.session_cookie_name)))


@router.post("/logout", status_code=204, dependencies=[Depends(require_origin)])
def logout(request: Request, response: Response, service: AuthService = Depends(get_auth_service)):
    service.logout(request.cookies.get(settings.session_cookie_name), request.headers.get("X-CSRF-Token"))
    response.delete_cookie(settings.session_cookie_name, path="/", secure=settings.secure_cookies, httponly=True, samesite="lax")

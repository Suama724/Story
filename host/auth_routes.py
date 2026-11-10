"""供 Qt 客户端使用的登录与改密接口。"""

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool

from .auth import AuthError, AuthService

router = APIRouter(prefix="/auth")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_name: str = Field(min_length=1)
    actor_name: str = Field(min_length=1, max_length=80)
    password: SecretStr


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: SecretStr
    new_password: SecretStr


def bearer_token(authorization: str | None) -> str:
    parts = (authorization or "").split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) != 43:
        raise AuthError("invalid_session", "请使用有效会话登录。")
    return parts[1]


def auth_service(request: Request) -> AuthService:
    service = request.app.state.auth
    if service is None:
        raise AuthError("campaign_not_configured", "请先配置主机的团目录。", 503)
    return service


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    service = auth_service(request)
    async with request.app.state.lobby.lock:
        result = await run_in_threadpool(
            service.login, body.campaign_name, body.actor_name, body.password.get_secret_value(),
        )
        await request.app.state.lobby.revoke_member(result["member_id"], result["token"])
    return result


@router.post("/password")
async def change_password(body: PasswordRequest, request: Request, response: Response) -> dict[str, bool]:
    response.headers["Cache-Control"] = "no-store"
    service = auth_service(request)
    token = bearer_token(request.headers.get("authorization"))
    async with request.app.state.lobby.lock:
        await run_in_threadpool(
            service.change_password, token,
            body.current_password.get_secret_value(), body.new_password.get_secret_value(),
        )
        if request.app.state.on_password_changed is not None:
            request.app.state.on_password_changed()
    return {"changed": True}


@router.get("/session")
async def session_info(request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    service = auth_service(request)
    token = bearer_token(request.headers.get("authorization"))
    session = await run_in_threadpool(service.require_session, token)
    return session.public_info()


@router.post("/logout")
async def logout(request: Request) -> dict[str, bool]:
    service = auth_service(request)
    token = bearer_token(request.headers.get("authorization"))
    async with request.app.state.lobby.lock:
        member_id = await run_in_threadpool(service.logout, token)
        await request.app.state.lobby.revoke_member(member_id)
    return {"logged_out": True}

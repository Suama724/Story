"""供 Qt 客户端使用的登录与改密接口。"""

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool

from coc7_attributes import MAX_IMPORT_LENGTH
from .attributes import player_attributes
from .auth import AuthError, AuthService
from .storage import campaign_info, open_campaign

router = APIRouter(prefix="/auth")


@router.get("/status")
async def host_status(request: Request, response: Response) -> dict[str, object]:
    """仅标识当前监听的团，不公开人物与凭据；供断开的客户端独立探测。"""
    response.headers["Cache-Control"] = "no-store"
    service = request.app.state.auth
    if service is None:
        return {"running": False, "campaign_id": None}

    def read():
        with open_campaign(service.directory) as connection:
            return {"running": True, "campaign_id": campaign_info(connection)["campaign_id"]}

    return await run_in_threadpool(read)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_name: str = Field(min_length=1)
    actor_name: str = Field(min_length=1, max_length=80)
    password: SecretStr
    role: Literal["pl", "ob"] = "pl"


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: SecretStr
    new_password: SecretStr


class NicknameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nickname: str = Field(min_length=1, max_length=80)


class AttributesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: SecretStr


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
            service.login, body.campaign_name, body.actor_name, body.password.get_secret_value(), body.role,
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


@router.get("/attributes")
async def read_attributes(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    service = auth_service(request)
    token = bearer_token(request.headers.get("authorization"))
    return await run_in_threadpool(player_attributes, service.directory, token)


@router.post("/attributes")
async def import_attributes(body: AttributesRequest, request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    service = auth_service(request)
    token = bearer_token(request.headers.get("authorization"))
    text = body.text.get_secret_value()
    if len(text) > MAX_IMPORT_LENGTH:
        raise AuthError("invalid_attributes", "属性指令太长，请只复制人物卡的 .st 属性行。", 400)
    async with request.app.state.lobby.lock:
        session = await run_in_threadpool(service.require_session, token)
        result = await run_in_threadpool(player_attributes, service.directory, token, text=text)
        await request.app.state.lobby.channels_changed(member_id=session.member_id)
        if request.app.state.on_attributes_changed is not None:
            request.app.state.on_attributes_changed()
    return result


@router.post("/nickname")
async def change_nickname(body: NicknameRequest, request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    token = bearer_token(request.headers.get("authorization"))
    async with request.app.state.lobby.lock:
        result = await run_in_threadpool(auth_service(request).change_nickname, token, body.nickname)
        if request.app.state.on_password_changed is not None:
            request.app.state.on_password_changed()
    return result


@router.post("/logout")
async def logout(request: Request) -> dict[str, bool]:
    service = auth_service(request)
    token = bearer_token(request.headers.get("authorization"))
    async with request.app.state.lobby.lock:
        member_id = await run_in_threadpool(service.logout, token)
        await request.app.state.lobby.revoke_member(member_id)
    return {"logged_out": True}

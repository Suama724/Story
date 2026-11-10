import asyncio
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from collections.abc import Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from starlette.concurrency import run_in_threadpool

from chat_protocol import (
    CHAT_ERROR,
    CHAT_MESSAGE,
    CHAT_SEND,
    LOBBY_PATH,
    chat_text_error,
    decode_event,
)

from .auth import AuthError, AuthService, MemberSession
from .auth_routes import bearer_token

router = APIRouter()


@dataclass(frozen=True)
class AuthenticatedConnection:
    token: str
    session: MemberSession


class Lobby:
    def __init__(
        self, auth: AuthService | None,
        on_message: Callable[[dict[str, object]], None] | None = None,
        on_player_log: Callable[[str], None] | None = None,
    ) -> None:
        self.auth = auth
        self.on_message = on_message
        self.on_player_log = on_player_log
        self.connections: dict[WebSocket, AuthenticatedConnection | None] = {}
        # 登录顶替、改密、订阅和广播共用此锁，避免撤销期间漏发消息。
        self.lock = asyncio.Lock()

    def _remove_connection(self, websocket: WebSocket) -> None:
        client = self.connections.pop(websocket, None)
        if client is not None and self.on_player_log is not None:
            self.on_player_log(f"{client.session.actor_name}离开了")

    async def close_connection(self, websocket: WebSocket) -> None:
        self._remove_connection(websocket)
        with suppress(RuntimeError, WebSocketDisconnect, OSError):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)

    async def revoke_member(self, member_id: str, active_token: str | None = None) -> None:
        """调用方持有大厅锁，在新会话已提交后关闭旧连接。"""
        for websocket, client in tuple(self.connections.items()):
            if client is not None and client.session.member_id == member_id and client.token != active_token:
                await self.close_connection(websocket)

    async def join(self, websocket: WebSocket) -> None:
        client = None
        async with self.lock:
            try:
                if self.auth is not None:
                    token = bearer_token(websocket.headers.get("authorization"))
                    session = await run_in_threadpool(self.auth.require_session, token)
                    client = AuthenticatedConnection(token, session)
                    nickname = session.actor_name
                else:
                    nickname = websocket.query_params.get("nickname", "").strip()
                    if not nickname or len(nickname.splitlines()) != 1:
                        raise AuthError("invalid_nickname", "请输入单行昵称。")
            except AuthError:
                await self.close_connection(websocket)
                return
            await websocket.accept()
            self.connections[websocket] = client
            if client is not None and self.on_player_log is not None:
                self.on_player_log(f"{nickname}加入了")

        try:
            while True:
                if client is None:
                    frame = await websocket.receive()
                else:
                    remaining = max(0, client.session.expires_at - time.time())
                    frame = await asyncio.wait_for(websocket.receive(), timeout=remaining)
                if frame["type"] == "websocket.disconnect":
                    return
                payload = frame.get("text")
                if payload is None:
                    continue
                event = decode_event(payload)
                if event is None or event.get("type") != CHAT_SEND:
                    continue

                async with self.lock:
                    if websocket not in self.connections:
                        return
                    if client is not None:
                        session = await run_in_threadpool(self.auth.require_session, client.token)
                        nickname = session.actor_name
                    text = event.get("text")
                    error = chat_text_error(text)
                    if isinstance(text, str) and error is None:
                        await self.broadcast_message(websocket, nickname, text)
                    else:
                        await websocket.send_text(json.dumps({"type": CHAT_ERROR, "code": error}))
        except (AuthError, TimeoutError):
            async with self.lock:
                await self.close_connection(websocket)
        except WebSocketDisconnect:
            return
        finally:
            async with self.lock:
                self._remove_connection(websocket)

    async def broadcast_message(self, sender: WebSocket | None, nickname: str, text: str) -> None:
        event = {"type": CHAT_MESSAGE, "nickname": nickname, "text": text}
        payload = json.dumps(event)
        sender_payload = json.dumps({**event, "own": True})
        for websocket, client in tuple(self.connections.items()):
            try:
                if client is not None:
                    await run_in_threadpool(self.auth.require_session, client.token)
                await websocket.send_text(sender_payload if websocket is sender else payload)
            except AuthError:
                await self.close_connection(websocket)
            except (WebSocketDisconnect, RuntimeError, OSError):
                self._remove_connection(websocket)
        if self.on_message is not None:
            self.on_message(event)


@router.websocket(LOBBY_PATH)
async def join_lobby(websocket: WebSocket) -> None:
    await websocket.app.state.lobby.join(websocket)

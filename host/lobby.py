import asyncio
import json
import sqlite3
import time
from contextlib import suppress
from dataclasses import dataclass
from collections.abc import Callable
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from starlette.concurrency import run_in_threadpool

from chat_protocol import (
    CHAT_ACK,
    CHAT_ERROR,
    CHAT_HISTORY,
    CHAT_HISTORY_GET,
    CHAT_MESSAGE,
    CHAT_SEND,
    CHANNEL_CHANGED,
    CHANNEL_STATE_GET,
    LOBBY_PATH,
    chat_text_error,
    decode_event,
    valid_request_id,
)

from .auth import AuthError, AuthService, MemberSession
from .auth_routes import bearer_token
from .channels import get_channel_access, player_channel_state
from .messages import ChatSendError, lobby_history, save_lobby_message
from .storage import open_campaign

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
        self.channel_clients: set[WebSocket] = set()
        # 登录顶替、改密、订阅和广播共用此锁，避免撤销期间漏发消息。
        self.lock = asyncio.Lock()

    def _remove_connection(self, websocket: WebSocket) -> None:
        self.channel_clients.discard(websocket)
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
                if event is None or event.get("type") not in (CHAT_SEND, CHAT_HISTORY_GET, CHANNEL_STATE_GET):
                    continue

                async with self.lock:
                    if websocket not in self.connections:
                        return
                    if event["type"] == CHANNEL_STATE_GET:
                        if client is not None:
                            self.channel_clients.add(websocket)
                            await self.send_channel_state(websocket, client)
                        continue
                    if event["type"] == CHAT_HISTORY_GET:
                        if client is not None:
                            await self.send_history(websocket, client, event)
                        continue
                    text = event.get("text")
                    request_id = event.get("request_id")
                    try:
                        if client is None:
                            error = chat_text_error(text)
                            if error is not None:
                                raise ChatSendError(error)
                            await self.broadcast_message(
                                {"type": CHAT_MESSAGE, "nickname": nickname, "text": text}, own_for=websocket,
                            )
                        else:
                            legacy_send = "request_id" not in event
                            await self.send_campaign_message(
                                text, str(uuid4()) if legacy_send else request_id,
                                sender=websocket, token=client.token, legacy_send=legacy_send,
                                channel_id=event.get("channel_id"),
                            )
                    except ChatSendError as error:
                        response = {"type": CHAT_ERROR, "code": error.code}
                        if valid_request_id(request_id):
                            response["request_id"] = request_id
                        await websocket.send_json(response)
        except (AuthError, TimeoutError):
            async with self.lock:
                await self.close_connection(websocket)
        except WebSocketDisconnect:
            return
        finally:
            async with self.lock:
                self._remove_connection(websocket)

    async def send_history(
        self, websocket: WebSocket, client: AuthenticatedConnection, event: dict[str, object],
    ) -> None:
        mode, cursor = event.get("mode"), event.get("cursor", 0)
        channel_id = event.get("channel_id")
        response = {"type": CHAT_HISTORY, "mode": mode, "cursor": cursor,
                    "channel_id": channel_id, "request_id": event.get("request_id")}
        if (mode not in ("latest", "before", "after") or type(cursor) is not int or not 0 <= cursor < 2**63
                or (channel_id is not None and not isinstance(channel_id, str))):
            await websocket.send_json({"type": CHAT_HISTORY, "error": "invalid_cursor"})
            return
        try:
            page = await run_in_threadpool(lobby_history, self.auth.directory, client.token, mode, cursor, channel_id)
        except ChatSendError as error:
            await websocket.send_json({**response, "error": error.code})
            return
        except sqlite3.Error:
            await websocket.send_json({**response, "error": "storage_error"})
            return
        await websocket.send_json({**response, **page})

    async def send_channel_state(self, websocket: WebSocket, client: AuthenticatedConnection) -> None:
        state = await run_in_threadpool(player_channel_state, self.auth.directory, client.token)
        await websocket.send_json({"type": CHANNEL_CHANGED, **state})

    async def channels_changed(self) -> None:
        """调用方持有大厅锁，先提交分组，再发布当前权限。"""
        for websocket, client in tuple(self.connections.items()):
            if client is None or websocket not in self.channel_clients:
                continue
            try:
                await self.send_channel_state(websocket, client)
            except AuthError:
                await self.close_connection(websocket)
            except (WebSocketDisconnect, RuntimeError, OSError):
                self._remove_connection(websocket)

    async def send_campaign_message(
        self, text: object, request_id: object, *, sender: WebSocket | None = None,
        token: str | None = None, legacy_send: bool = False, channel_id: str | None = None,
    ) -> dict[str, object]:
        """调用方持有大厅锁。保存后确认，新消息按提交顺序广播。"""
        assert self.auth is not None
        if channel_id is not None and not isinstance(channel_id, str):
            raise ChatSendError("channel_forbidden")
        record, created = await run_in_threadpool(
            save_lobby_message, self.auth.directory, request_id, text, token=token, channel_id=channel_id,
        )
        event = {"type": CHAT_MESSAGE, **record}
        del event["request_id"]
        if sender is not None and not legacy_send:
            try:
                await sender.send_json({"type": CHAT_ACK, "request_id": request_id, "message": event})
            except (WebSocketDisconnect, RuntimeError, OSError):
                self._remove_connection(sender)
        if created:
            await self.broadcast_message(event, own_for=sender if legacy_send else None)
        return event

    async def broadcast_message(self, event: dict[str, object], *, own_for: WebSocket | None = None) -> None:
        payload = json.dumps(event)
        sender_payload = json.dumps({**event, "own": True})
        for websocket, client in tuple(self.connections.items()):
            try:
                if client is not None:
                    await run_in_threadpool(self.auth.require_session, client.token)
                    if "channel_id" in event:
                        visible = await run_in_threadpool(self._message_visible, client, event)
                        if not visible:
                            continue
                await websocket.send_text(sender_payload if websocket is own_for else payload)
            except AuthError:
                await self.close_connection(websocket)
            except (WebSocketDisconnect, RuntimeError, OSError):
                self._remove_connection(websocket)
        if self.on_message is not None:
            self.on_message(event)

    def _message_visible(self, client: AuthenticatedConnection, event: dict) -> bool:
        with open_campaign(self.auth.directory) as connection:
            return get_channel_access(connection, client.session.member_id, event["channel_id"]).includes(event["sequence"])


@router.websocket(LOBBY_PATH)
async def join_lobby(websocket: WebSocket) -> None:
    await websocket.app.state.lobby.join(websocket)

import json
from asyncio import Lock

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from chat_protocol import (
    CHAT_ERROR,
    CHAT_MESSAGE,
    CHAT_SEND,
    LOBBY_PATH,
    chat_text_error,
    decode_event,
)

router = APIRouter()
_connections: set[WebSocket] = set()
_broadcast_lock = Lock()


@router.websocket(LOBBY_PATH)
async def join_lobby(websocket: WebSocket) -> None:
    nickname = websocket.query_params.get("nickname", "").strip()
    if not nickname or len(nickname.splitlines()) != 1:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    _connections.add(websocket)

    try:
        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                return

            payload = frame.get("text")
            if payload is None:
                continue

            event = decode_event(payload)
            if event is None or event.get("type") != CHAT_SEND:
                continue

            text = event.get("text")
            error = chat_text_error(text)
            if isinstance(text, str) and error is None:
                await _broadcast_message(websocket, nickname, text)
            else:
                await websocket.send_text(json.dumps({"type": CHAT_ERROR, "code": error}))
    except WebSocketDisconnect:
        return
    finally:
        _connections.discard(websocket)


async def _broadcast_message(sender: WebSocket, nickname: str, text: str) -> None:
    event = {"type": CHAT_MESSAGE, "nickname": nickname, "text": text}
    payload = json.dumps(event)
    sender_payload = json.dumps({**event, "own": True})

    # 按整条消息广播，让各窗口看到一致的顺序。
    async with _broadcast_lock:
        for connection in tuple(_connections):
            if connection not in _connections:
                continue

            try:
                await connection.send_text(sender_payload if connection is sender else payload)
            except WebSocketDisconnect:
                _connections.discard(connection)

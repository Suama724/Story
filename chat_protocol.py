"""单大厅客户端和主机共用的 WebSocket 事件。"""

import json

LOBBY_PATH = "/ws/lobby"
CHAT_SEND = "chat.send"
CHAT_MESSAGE = "chat.message"


def decode_event(payload: str) -> dict[str, object] | None:
    try:
        event = json.loads(payload)
    except (ValueError, RecursionError):
        return None

    if isinstance(event, dict):
        return event

    return None

"""单大厅客户端和主机共用的 WebSocket 事件。"""

import json

LOBBY_PATH = "/ws/lobby"
CHAT_SEND = "chat.send"
CHAT_MESSAGE = "chat.message"
CHAT_ERROR = "chat.error"
MAX_TEXT_LENGTH = 2_000


def chat_text_error(text: object) -> str | None:
    """按 Unicode 码点计数，保留正文原有的空格和换行。"""
    if not isinstance(text, str):
        return "invalid_text"
    if not text.strip():
        return "empty_text"
    if len(text) > MAX_TEXT_LENGTH:
        return "text_too_long"
    return None


def decode_event(payload: str) -> dict[str, object] | None:
    try:
        event = json.loads(payload)
    except (ValueError, RecursionError):
        return None

    if isinstance(event, dict):
        return event

    return None

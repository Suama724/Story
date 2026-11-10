"""单大厅客户端和主机共用的 WebSocket 事件。"""

import json
import re

LOBBY_PATH = "/ws/lobby"
CHAT_SEND = "chat.send"
CHAT_ACK = "chat.ack"
CHAT_MESSAGE = "chat.message"
CHAT_ERROR = "chat.error"
CHAT_HISTORY_GET = "chat.history.get"
CHAT_HISTORY = "chat.history"
HISTORY_PAGE_SIZE = 50
PING_INTERVAL_SECONDS = 20
PONG_TIMEOUT_SECONDS = 10
MAX_TEXT_LENGTH = 2_000
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")

SEND_ERRORS = {
    "invalid_text": "消息正文必须是有效的文本。",
    "empty_text": "请输入消息，正文不能只有空白。",
    "text_too_long": f"消息最多 {MAX_TEXT_LENGTH:,} 个字符。",
    "invalid_request_id": "消息请求 ID 无效。",
    "request_id_conflict": "此请求 ID 已用于另一条消息。",
    "storage_error": "主机保存消息失败，草稿已保留，可以重试。",
}


def chat_text_error(text: object) -> str | None:
    """按 Unicode 码点计数，保留正文原有的空格和换行。"""
    if not isinstance(text, str):
        return "invalid_text"
    if not text.strip():
        return "empty_text"
    if len(text) > MAX_TEXT_LENGTH:
        return "text_too_long"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "invalid_text"
    return None


def valid_request_id(value: object) -> bool:
    return isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value) is not None


def decode_event(payload: str) -> dict[str, object] | None:
    try:
        event = json.loads(payload)
    except (ValueError, RecursionError):
        return None

    if isinstance(event, dict):
        return event

    return None

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
CHANNEL_STATE_GET = "channel.state.get"
CHANNEL_CHANGED = "channel.changed"
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
    "channel_forbidden": "你无权在这个频道发言。",
    "attributes_required": "请先在「个人属性」中导入角色属性，再发言。",
    "channel_read_forbidden": "你无权查看这个频道。",
    "dice_syntax": "掷骰格式不正确。示例：.r 2d6+3、.ra b 困难侦查、.rah 侦查 @角色名。",
    "dice_limit": "一次最多掷 100 颗骰，奖惩骰支持 1 或 2 颗。",
    "dice_engine_unavailable": "主机的骰核暂不可用，请联系组织者检查骰核运行环境。",
    "dice_secret_forbidden": "只有 KP 可以使用暗骰。",
    "dice_delegate_forbidden": "只有 KP 可以代骰或手动指定检定值。",
    "dice_role_forbidden": "只有玩家角色和 KP 可以使用骰子。",
    "dice_check_missing": "找不到用于检定的角色或属性。请核对角色名及人物卡。",
    "dice_actor_missing": "找不到指定的玩家角色，请核对角色名。",
    "dice_card_missing": "用于检定的角色还没有人物卡。KP 可用 .ra 60 @角色名 手动指定数值。",
    "dice_attribute_missing": "人物卡上没有这项属性或技能，请核对指令中的名称。",
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

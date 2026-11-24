import os
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse

from .auth import AuthError, AuthService
from .auth_routes import router as auth_router
from .lobby import Lobby
from .lobby import router as lobby_router
from .storage import open_campaign


def create_app(
    directory: Path | None = None,
    on_message: Callable[[dict[str, object]], None] | None = None,
    on_password_changed: Callable[[], None] | None = None,
    on_player_log: Callable[[str], None] | None = None,
    on_presence_changed: Callable[[list[str]], None] | None = None,
    on_attributes_changed: Callable[[], None] | None = None,
) -> FastAPI:
    configured_directory = directory or os.environ.get("STORY_CAMPAIGN_DIR")
    auth = None
    if configured_directory:
        directory = Path(configured_directory).resolve()
        with open_campaign(directory):
            pass
        auth = AuthService(directory)

    application = FastAPI(title="Story Host")
    application.state.auth = auth
    application.state.on_password_changed = on_password_changed
    application.state.on_attributes_changed = on_attributes_changed
    application.state.lobby = Lobby(auth, on_message, on_player_log=on_player_log,
                                    on_presence_changed=on_presence_changed)
    application.include_router(auth_router)
    application.include_router(lobby_router)
    application.add_api_route("/health", health)
    application.add_api_websocket_route("/ws", echo)
    application.add_exception_handler(AuthError, auth_error)
    return application


async def auth_error(request: Request, error: AuthError) -> JSONResponse:
    return JSONResponse(
        {"error": error.code, "message": str(error)}, status_code=error.status_code,
        headers={"Cache-Control": "no-store"},
    )


async def health() -> dict[str, str]:
    return {"status": "ok"}


async def echo(websocket: WebSocket) -> None:
    """把文本原样回给发送它的客户端。"""
    await websocket.accept()

    try:
        while True:
            event = await websocket.receive()
            if event["type"] == "websocket.disconnect":
                return

            text = event.get("text")
            if text is None:
                await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                return

            await websocket.send_text(text)
    except WebSocketDisconnect:
        # 客户端也可能在收到消息和发出回显之间断开。
        return


app = create_app()

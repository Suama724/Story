from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status

app = FastAPI(title="Story Host")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws")
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

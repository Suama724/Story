"""GUI 所属的监听进程，通过标准输入接收本地主持操作。"""

import asyncio
import json
import socket
import sys
import threading
from pathlib import Path
from uuid import uuid4

import uvicorn
from starlette.concurrency import run_in_threadpool

from chat_protocol import PING_INTERVAL_SECONDS, PONG_TIMEOUT_SECONDS

from .app import create_app
from .channels import archive_scene, create_scene, local_channel_state, mark_channel_read, move_members, set_visibility_mode


def emit(event: str, **values: object) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


async def run_server(directory: Path, host: str, port: int) -> int:
    application = create_app(
        directory, lambda message: emit("message", message=message),
        on_password_changed=lambda: emit("password_changed"),
        on_player_log=lambda message: emit("log", message=message),
    )
    server = uvicorn.Server(uvicorn.Config(
        application, host=host, port=port, log_level="warning",
        access_log=False, ws="websockets-sansio", ws_ping_interval=PING_INTERVAL_SECONDS,
        ws_ping_timeout=PONG_TIMEOUT_SECONDS, timeout_graceful_shutdown=2,
    ))
    listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
    except OSError as error:
        listener.close()
        emit("error", message=f"无法监听 {host}:{port}，{error}")
        return 1

    async def handle(command: dict[str, object]) -> None:
        try:
            async with application.state.lobby.lock:
                if command.get("command") == "stop":
                    server.should_exit = True
                elif command.get("command") == "say":
                    request_id = command.get("request_id", str(uuid4()))
                    message = await application.state.lobby.send_campaign_message(
                        command.get("text"), request_id, channel_id=command.get("channel_id"),
                    )
                    emit("sent", request_id=request_id, message=message)
                elif command.get("command") in ("channels", "scene.create", "scene.archive", "visibility.set", "members.move", "channel.read"):
                    operation = command["command"]
                    if operation == "scene.create":
                        await run_in_threadpool(create_scene, directory, command.get("name", ""),
                                                command.get("member_ids", []), channel_id=command.get("channel_id"))
                    elif operation == "scene.archive":
                        await run_in_threadpool(archive_scene, directory, command.get("channel_id"))
                    elif operation == "visibility.set":
                        await run_in_threadpool(set_visibility_mode, directory, command.get("mode"))
                    elif operation == "members.move":
                        await run_in_threadpool(move_members, directory, command.get("member_ids"), command.get("channel_id"),
                                                request_id=command.get("request_id"), expected_revision=command.get("expected_revision"))
                    elif operation == "channel.read":
                        await run_in_threadpool(mark_channel_read, directory, command.get("channel_id"), command.get("sequence"))
                    if operation not in ("channels", "channel.read"):
                        await application.state.lobby.channels_changed()
                    state = await run_in_threadpool(local_channel_state, directory)
                    emit("channels", request_id=command.get("request_id", ""), state=state)
        except Exception as error:
            emit("error", message=str(error), request_id=command.get("request_id"))

    loop = asyncio.get_running_loop()

    def read_commands() -> None:
        for line in sys.stdin:
            try:
                command = json.loads(line)
                if not isinstance(command, dict):
                    continue
            except ValueError:
                continue
            loop.call_soon_threadsafe(lambda value=command: asyncio.create_task(handle(value)))
        # 管理窗口退出或崩溃后，关闭它所拥有的监听进程。
        loop.call_soon_threadsafe(setattr, server, "should_exit", True)

    threading.Thread(target=read_commands, daemon=True).start()
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            if task.done():
                await task
                return 1
            await asyncio.sleep(0.01)
        emit("ready", host=host, port=listener.getsockname()[1])
        await task
        return 0
    finally:
        listener.close()

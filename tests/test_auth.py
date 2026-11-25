"""认证边界以及真实 HTTP / WebSocket 的单会话验收。"""

import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from dice_component import RuleConfig

from host import migrations
from host.app import create_app
from host.auth import AuthError, AuthService, DEFAULT_PASSWORD
from host.dice import save_local_dice_config
from host.management import add_member, edit_member, list_members
from host.messages import local_lobby_history
from host.storage import DATABASE_NAME, campaign_info, create_member, initialize_campaign, open_campaign
from tests.attribute_fixtures import add_ready_player

NEW_PASSWORD = "新的密码 🎲 123456"


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = initialize_campaign(self.root, "认证测试")
        self.member = add_member(self.directory, "调查员")
        self.auth = AuthService(self.directory)

    def test_v2_migration_preserves_changed_password_and_resolves_duplicate_names(self) -> None:
        directory = self.root / "legacy"
        directory.mkdir()
        with closing(sqlite3.connect(directory / DATABASE_NAME)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN")
            with patch.object(migrations, "SCHEMA_VERSION", 2):
                migrations.migrate(connection)
            connection.execute("INSERT INTO campaigns (id, name, ruleset, created_at) VALUES ('old', '旧团', 'coc7', 'now')")
            connection.execute("INSERT INTO play_sessions VALUES ('session', 'old', '测试场次', 'active', 'now', NULL)")
            connection.execute("INSERT INTO channels VALUES ('lobby', 'session', '大厅', 'lobby', 'active', 'now')")
            create_member(connection, "old", "旧 KP", "旧 KP", role="kp")
            existing = create_member(connection, "old", "玩家甲", "调查员")
            pending = create_member(connection, "old", "玩家乙", "调查员")
            create_member(connection, "old", "玩家丙", "调查员（2）")
            salt = b"test migration salt"
            digest = hashlib.pbkdf2_hmac("sha256", NEW_PASSWORD.encode(), salt, 2).hex()
            password_hash = f"pbkdf2_sha256$2${salt.hex()}${digest}"
            connection.execute("INSERT INTO credentials VALUES (?, ?, 0, NULL)", (existing, password_hash))
            connection.execute("INSERT INTO credentials VALUES (?, ?, 1, 0)", (pending, password_hash))
        with open_campaign(directory) as connection:
            self.assertEqual(campaign_info(connection)["description"], "")
            self.assertEqual(campaign_info(connection)["schema_version"], migrations.SCHEMA_VERSION)
            self.assertFalse(connection.execute("PRAGMA foreign_key_check").fetchall())
        members = {member["member_id"]: member for member in list_members(directory)}
        self.assertEqual(members[existing]["actor_name"], "调查员")
        self.assertIsNone(members[existing]["password"])
        self.assertEqual(members[pending]["actor_name"], "调查员（3）")
        migrated = AuthService(directory)
        self.assertEqual(migrated.login("旧团", "调查员", NEW_PASSWORD)["member_id"], existing)
        self.assertEqual(migrated.login("旧团", "调查员（3）", DEFAULT_PASSWORD)["member_id"], pending)
        with self.assertRaises(AuthError):
            migrated.login("旧团", "调查员（3）", NEW_PASSWORD)

    def test_cli_character_and_duplicate_protection(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "host", "add-character", str(self.directory), "--name", "Scout"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            capture_output=True, encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.auth.login("认证测试", "scout", DEFAULT_PASSWORD)["member_id"], json.loads(result.stdout)["member_id"])
        for name in (" SCOUT ", "KP"):
            with self.assertRaisesRegex(ValueError, "同名"):
                add_member(self.directory, name)
        with self.assertRaisesRegex(ValueError, "同名"):
            edit_member(self.directory, self.member, "Scout")
        with self.assertRaises(AuthError):
            self.auth.login("认证测试", "KP", DEFAULT_PASSWORD)

    def test_password_change_keeps_session_and_updates_visible_password(self) -> None:
        initial = self.auth.login("认证测试", "调查员", DEFAULT_PASSWORD)
        self.assertEqual(initial["campaign_name"], "认证测试")
        self.auth.change_password(initial["token"], DEFAULT_PASSWORD, NEW_PASSWORD)
        self.assertEqual(self.auth.require_session(initial["token"]).member_id, self.member)
        with self.assertRaises(AuthError):
            self.auth.login("认证测试", "调查员", DEFAULT_PASSWORD)
        self.assertEqual(list_members(self.directory)[1]["password"], NEW_PASSWORD)
        relogin = self.auth.login("认证测试", "调查员", NEW_PASSWORD)
        with self.assertRaises(AuthError):
            self.auth.require_session(initial["token"])
        database = (self.directory / DATABASE_NAME).read_bytes()
        self.assertNotIn(relogin["token"].encode(), database)

    def test_sessions_expire_but_role_password_does_not(self) -> None:
        initial = self.auth.login("认证测试", "调查员", DEFAULT_PASSWORD)
        with patch("host.auth.time.time", return_value=initial["expires_at"] + 1):
            with self.assertRaises(AuthError):
                self.auth.require_session(initial["token"])
            self.assertEqual(self.auth.login("认证测试", "调查员", DEFAULT_PASSWORD)["member_id"], self.member)

    def test_concurrent_logins_leave_one_valid_session(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.auth.login("认证测试", "调查员", DEFAULT_PASSWORD), range(2)))
        valid = 0
        for result in results:
            try:
                self.auth.require_session(result["token"])
                valid += 1
            except AuthError:
                pass
        self.assertEqual(valid, 1)

    def test_concurrent_password_changes_verify_the_current_password(self) -> None:
        initial = self.auth.login("认证测试", "调查员", DEFAULT_PASSWORD)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self.auth.change_password, initial["token"], DEFAULT_PASSWORD, NEW_PASSWORD)
                for _ in range(2)
            ]
        failures = [future.exception() for future in futures]
        self.assertEqual(failures.count(None), 1)
        self.assertIsInstance(next(error for error in failures if error is not None), AuthError)

    def test_session_write_failure_preserves_old_session(self) -> None:
        initial = self.auth.login("认证测试", "调查员", DEFAULT_PASSWORD)
        with open_campaign(self.directory) as connection:
            connection.execute(
                "CREATE TRIGGER reject_session BEFORE INSERT ON auth_sessions "
                "BEGIN SELECT RAISE(ABORT, 'test'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.auth.login("认证测试", "调查员", DEFAULT_PASSWORD)
        self.auth.require_session(initial["token"])

    def test_failed_role_creation_does_not_leave_orphan_member(self) -> None:
        with open_campaign(self.directory) as connection:
            connection.execute("CREATE TRIGGER reject_actor BEFORE INSERT ON actors BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            add_member(self.directory, "另一个角色")
        self.assertEqual(len(list_members(self.directory)), 2)


class AuthNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = initialize_campaign(Path(self.temporary.name), "联网测试")
        add_ready_player(self.directory, "调查员")
        self.password_events = []
        self.player_logs = []
        self.broadcasts = []
        self.application = create_app(
            self.directory, self.broadcasts.append, on_password_changed=lambda: self.password_events.append(True),
            on_player_log=self.player_logs.append,
        )
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        port = self.listener.getsockname()[1]
        self.http = f"http://127.0.0.1:{port}"
        self.ws = f"ws://127.0.0.1:{port}/ws/lobby"
        self.server = uvicorn.Server(uvicorn.Config(
            self.application, log_level="error", lifespan="off", ws="websockets-sansio",
            ws_ping_interval=0.5, ws_ping_timeout=1.0,
        ))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.listener]))
        async with asyncio.timeout(5):
            while not self.server.started:
                if self.server_task.done():
                    await self.server_task
                    raise RuntimeError("测试主机启动失败")
                await asyncio.sleep(0.01)

    async def asyncTearDown(self) -> None:
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, timeout=5)
        self.listener.close()
        self.temporary.cleanup()

    async def request(self, method: str, path: str, body=None, token=None):
        def send():
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            data = None if body is None else json.dumps(body).encode()
            request = Request(self.http + path, data=data, headers=headers, method=method)
            opener = build_opener(ProxyHandler({}))
            try:
                response = opener.open(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response)
        return await asyncio.to_thread(send)

    async def login(self, password=None):
        status, result = await self.request("POST", "/auth/login", {
            "campaign_name": "联网测试",
            "actor_name": "调查员",
            "password": DEFAULT_PASSWORD if password is None else password,
        })
        self.assertEqual(status, 200, result)
        return result

    async def test_lobby_removes_peer_that_does_not_answer_websocket_ping(self) -> None:
        session = await self.login()
        host, port = self.listener.getsockname()
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write((
                "GET /ws/lobby HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Connection: Upgrade\r\nUpgrade: websocket\r\n"
                "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                f"Authorization: Bearer {session['token']}\r\n\r\n"
            ).encode("ascii"))
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
            self.assertTrue(response.startswith(b"HTTP/1.1 101"))
            self.assertEqual(len(self.application.state.lobby.connections), 1)

            # 保持 TCP 打开且不发送 Pong，等待服务端超时关闭。
            frames = await asyncio.wait_for(reader.read(), timeout=5)
            self.assertEqual(frames[:1], b"\x89")
            async with asyncio.timeout(3):
                while self.application.state.lobby.connections:
                    await asyncio.sleep(0.01)
            self.assertEqual(self.player_logs, ["调查员加入了", "调查员离开了"])
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_first_login_permissions_and_bound_sender(self) -> None:
        with self.assertRaises(InvalidStatus) as refused:
            async with connect(self.ws + "?nickname=KP", proxy=None):
                pass
        self.assertEqual(refused.exception.response.status_code, 403)
        ready = await self.login()
        self.assertEqual(self.player_logs, [])
        async with connect(self.ws + "?nickname=KP", additional_headers={
            "Authorization": f"Bearer {ready['token']}"
        }, proxy=None) as websocket:
            await websocket.send(json.dumps({"type": "chat.send", "text": "中文 🎲\n第二行", "nickname": "KP"}))
            event = json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))
            self.assertEqual(event["nickname"], "调查员")
            self.assertEqual(event["text"], "中文 🎲\n第二行")
            self.assertTrue(event["own"])

    async def test_login_role_mismatch_cannot_replace_existing_session(self) -> None:
        ready = await self.login()
        body = {"campaign_name": "联网测试", "actor_name": "调查员", "password": DEFAULT_PASSWORD, "role": "ob"}
        status, response = await self.request("POST", "/auth/login", body)
        self.assertEqual(status, 400)
        self.assertEqual(response["error"], "role_mismatch")
        self.assertEqual(self.application.state.auth.require_session(ready["token"]).role, "pl")
        add_member(self.directory, "观众", role="ob")
        body["actor_name"] = "观众"
        status, response = await self.request("POST", "/auth/login", body)
        self.assertEqual(status, 200, response)
        self.assertEqual(response["role"], "ob")
        body["role"] = "pl"
        status, response = await self.request("POST", "/auth/login", body)
        self.assertEqual(status, 400)
        self.assertEqual(response["error"], "role_mismatch")
        body["role"] = "kp"
        status, _ = await self.request("POST", "/auth/login", body)
        self.assertEqual(status, 422)

    async def test_message_retry_conflict_and_order(self) -> None:
        ready = await self.login()
        async with connect(self.ws, additional_headers={"Authorization": f"Bearer {ready['token']}"},
                           proxy=None) as websocket:
            async def send(request_id, text):
                await websocket.send(json.dumps({"type": "chat.send", "request_id": request_id, "text": text}))
                return json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))

            text = "骰子 🎲\n第二行  "
            ack = await send("player-1", text)
            self.assertEqual((ack["type"], ack["request_id"]), ("chat.ack", "player-1"))
            first = ack["message"]
            self.assertEqual(first["sequence"], 1)
            self.assertEqual(first["text"], text)
            self.assertEqual(first["actor_id"], ready["actor_id"])
            self.assertEqual(json.loads(await asyncio.wait_for(websocket.recv(), timeout=3)), first)
            self.assertEqual(await send("player-1", text), ack)
            self.assertEqual(await send("player-1", "不同正文"), {
                "type": "chat.error", "request_id": "player-1", "code": "request_id_conflict",
            })
            self.assertEqual((await send(None, "无效 ID"))["code"], "invalid_request_id")
            second = (await send("player-2", "下一条"))["message"]
            self.assertEqual(second["sequence"], 2)
            self.assertNotEqual(second["message_id"], first["message_id"])
            self.assertEqual(json.loads(await asyncio.wait_for(websocket.recv(), timeout=3)), second)

        self.assertEqual(self.broadcasts, [first, second])
        with open_campaign(self.directory) as connection:
            rows = connection.execute("SELECT message_id, request_id FROM messages ORDER BY sequence").fetchall()
            self.assertEqual([tuple(row) for row in rows], [
                (first["message_id"], "player-1"), (second["message_id"], "player-2"),
            ])

    async def test_failed_message_write_rolls_back_without_broadcast(self) -> None:
        ready = await self.login()
        with open_campaign(self.directory) as connection, connection:
            connection.execute("""CREATE TRIGGER reject_message AFTER INSERT ON messages
                                  BEGIN SELECT RAISE(FAIL, '模拟写入失败'); END""")
        async with connect(self.ws, additional_headers={"Authorization": f"Bearer {ready['token']}"},
                           proxy=None) as websocket:
            payload = json.dumps({"type": "chat.send", "request_id": "retry", "text": "失败后重发"})
            await websocket.send(payload)
            self.assertEqual(json.loads(await asyncio.wait_for(websocket.recv(), timeout=3)), {
                "type": "chat.error", "request_id": "retry", "code": "storage_error",
            })
            self.assertEqual(self.broadcasts, [])
            with open_campaign(self.directory) as connection, connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
                connection.execute("DROP TRIGGER reject_message")
            await websocket.send(payload)
            ack = json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))
            self.assertEqual(ack["type"], "chat.ack")
            self.assertEqual(ack["message"]["sequence"], 1)
            self.assertEqual(json.loads(await asyncio.wait_for(websocket.recv(), timeout=3)), ack["message"])

    async def test_dice_visibility_rules_and_replay(self) -> None:
        session = await self.login()
        add_member(self.directory, "观众", role="ob")
        observer = self.application.state.auth.login("联网测试", "观众", DEFAULT_PASSWORD, "ob")
        save_local_dice_config(self.directory, RuleConfig(custom=True, critical_max=5, fumble_min=95))
        with open_campaign(self.directory) as db, db:
            db.execute("UPDATE campaigns SET dice_config = json_set(dice_config, '$.require_success', json('true'), "
                       "'$.require_failure', json('true'))")
        fixed = {"version": "test", "critical": False, "fumble": False, "total": None, "rands": [
            {"kind": "tens_d10", "value": 0}, {"kind": "tens_d10", "value": 10},
            {"kind": "normal", "value": 5},
        ]}
        app_lobby = self.application.state.lobby
        with patch("dice_component.core._worker", return_value=fixed) as engine:
            async with (
                connect(self.ws, additional_headers={"Authorization": f"Bearer {session['token']}"}, proxy=None) as pl,
                connect(self.ws, additional_headers={"Authorization": f"Bearer {observer['token']}"}, proxy=None) as ob,
            ):
                async def read(socket):
                    return json.loads(await asyncio.wait_for(socket.recv(), timeout=3))

                async def send(socket, request_id, text):
                    await socket.send(json.dumps({"type": "chat.send", "request_id": request_id, "text": text}))
                    return await read(socket)

                self.assertEqual((await send(pl, "denied", ".rah b 侦查"))["code"], "dice_secret_forbidden")
                self.assertEqual((await send(ob, "observer", ".r 1d6"))["code"], "dice_role_forbidden")
                self.assertEqual(engine.call_count, 0)
                async with app_lobby.lock:
                    command = "\n\ufeff  ．rah b 侦察 @调查员"
                    await app_lobby.send_campaign_message(command, "secret")
                    await app_lobby.send_campaign_message(command, "secret")
                self.assertEqual(engine.call_count, 1)
                ack = await send(pl, "public", ".ra b 极难诗学")
                self.assertEqual(ack["message"]["sequence"], 1)
                self.assertEqual(await read(pl), ack["message"])
                public = await read(pl)
                self.assertEqual((public["sequence"], public["nickname"], public["kind"]), (2, "骰子", "dice_result"))
                self.assertIn("5 / 2，大成功。", public["text"])
                self.assertNotIn("local_sequence", public)
                self.assertEqual(await read(ob), ack["message"])
                self.assertEqual(await read(ob), public)
                self.assertEqual((await send(pl, "public", ".ra b 极难诗学"))["result"], public)
                self.assertEqual(engine.call_count, 2)

                await ob.send(json.dumps({"type": "chat.history.get", "mode": "latest"}))
                self.assertEqual((await read(ob))["messages"], [ack["message"], public])
                local = local_lobby_history(self.directory)["messages"]
                self.assertEqual([message["sequence"] for message in local], [1, 2, 3, 4])
                self.assertEqual([message["public_sequence"] for message in local], [None, None, 1, 2])
                self.assertTrue(local[0]["text"].startswith("【暗骰】"))
                self.assertEqual(local[1]["nickname"], "骰子")
                self.assertEqual(local_lobby_history(self.directory, before=3)["messages"], local[:2])

    async def test_dice_skill_alias_after_successful_roll(self) -> None:
        session = await self.login()
        fixed = {"critical": False, "fumble": False, "total": None, "rands": [
            {"kind": "tens_d10", "value": 30}, {"kind": "normal", "value": 9},
        ]}
        with patch("dice_component.core._worker", return_value=fixed):
            async with connect(self.ws, additional_headers={
                "Authorization": f"Bearer {session['token']}",
            }, proxy=None) as player:
                async def read():
                    return json.loads(await asyncio.wait_for(player.recv(), timeout=3))

                for index, (command, expected) in enumerate((
                    (".ra 敏捷", "敏捷检定"), (".ra 侦察", "侦查检定"),
                    (".ra 查无此技能", None), (".ra 侦察", "侦查检定"),
                )):
                    await player.send(json.dumps({"type": "chat.send", "request_id": str(index), "text": command}))
                    reply = await read()
                    if expected is None:
                        self.assertEqual(reply["code"], "dice_attribute_missing")
                        continue
                    self.assertEqual(reply["type"], "chat.ack")
                    self.assertEqual(await read(), reply["message"])
                    result = await read()
                    self.assertEqual(result["kind"], "dice_result")
                    self.assertEqual(result["text"], f"调查员的{expected}：39 / 65，普通成功。")

    async def test_relogin_closes_old_socket_and_invalidates_old_token(self) -> None:
        first = await self.login()
        async with connect(self.ws, additional_headers={
            "Authorization": f"Bearer {first['token']}"
        }, proxy=None) as websocket:
            for name, password in (("调查员", DEFAULT_PASSWORD), ("不存在的角色", "wrong")):
                status, result = await self.request("POST", "/auth/login", {
                    "campaign_name": "另一个团", "actor_name": name, "password": password,
                })
                self.assertEqual((status, result["error"]), (400, "campaign_mismatch"))
            status, _ = await self.request("POST", "/auth/login", {
                "campaign_name": "联网测试", "actor_name": "调查员", "password": "wrong",
            })
            self.assertEqual(status, 401)
            await websocket.send(json.dumps({"type": "chat.send", "text": "仍可发言"}))
            await asyncio.wait_for(websocket.recv(), timeout=3)
            second = await self.login()
            with self.assertRaises(ConnectionClosed) as closed:
                await asyncio.wait_for(websocket.recv(), timeout=3)
            self.assertEqual(closed.exception.rcvd.code, 1008)
            status, _ = await self.request("GET", "/auth/session", token=first["token"])
            self.assertEqual(status, 401)
            status, _ = await self.request("GET", "/auth/session", token=second["token"])
            self.assertEqual(status, 200)
        self.assertEqual(self.player_logs, ["调查员加入了", "调查员离开了"])

    async def test_logout_closes_idle_socket(self) -> None:
        ready = await self.login()
        async with connect(self.ws, additional_headers={
            "Authorization": f"Bearer {ready['token']}"
        }, proxy=None) as websocket:
            status, _ = await self.request("POST", "/auth/logout", {}, ready["token"])
            self.assertEqual(status, 200)
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(websocket.recv(), timeout=3)
        status, _ = await self.request("GET", "/auth/session", token=ready["token"])
        self.assertEqual(status, 401)
        self.assertEqual(self.player_logs, ["调查员加入了", "调查员离开了"])

    async def test_password_change_notifies_organizer_and_keeps_socket(self) -> None:
        ready = await self.login()
        async with connect(self.ws, additional_headers={
            "Authorization": f"Bearer {ready['token']}"
        }, proxy=None) as websocket:
            status, changed = await self.request("POST", "/auth/password", {
                "current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD,
            }, ready["token"])
            self.assertEqual((status, changed), (200, {"changed": True}))
            self.assertEqual(self.password_events, [True])
            await websocket.send(json.dumps({"type": "chat.send", "text": "改密后仍可发言"}))
            self.assertEqual(json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))["text"], "改密后仍可发言")
        status, _ = await self.request("GET", "/auth/session", token=ready["token"])
        self.assertEqual(status, 200)

    async def test_session_survives_service_recreation(self) -> None:
        ready = await self.login()
        service = AuthService(self.directory)
        result = await asyncio.to_thread(service.require_session, ready["token"])
        self.assertEqual(result.member_id, ready["member_id"])
        status, _ = await self.request("GET", "/health")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()

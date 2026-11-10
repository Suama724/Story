"""角色密码与单会话身份。"""

import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .storage import campaign_info, open_campaign

DEFAULT_PASSWORD = "123456"
SESSION_SECONDS = 12 * 60 * 60


class AuthError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _password_matches(password: str, credential: sqlite3.Row) -> bool:
    try:
        supplied = password.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if credential["password_hash"] is None:
        expected = credential["password"] or DEFAULT_PASSWORD
        return hmac.compare_digest(supplied, expected.encode("utf-8"))

    # 旧版本只保留了散列，玩家改密前仍用原密码验证。
    algorithm, iterations, salt, expected = credential["password_hash"].split("$")
    if algorithm != "pbkdf2_sha256":
        return False
    actual = hashlib.pbkdf2_hmac("sha256", supplied, bytes.fromhex(salt), int(iterations)).hex()
    return hmac.compare_digest(actual, expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemberSession:
    token_hash: str
    campaign_id: str
    campaign_name: str
    member_id: str
    role: str
    actor_id: str
    actor_name: str
    expires_at: float

    def public_info(self) -> dict[str, object]:
        info = asdict(self)
        del info["token_hash"]
        return info


def session_for_token(connection: sqlite3.Connection, token: str) -> MemberSession:
    row = connection.execute(
        """SELECT s.token_hash, c.id AS campaign_id, c.name AS campaign_name,
                  m.id AS member_id, m.role, a.id AS actor_id, a.name AS actor_name, s.expires_at
           FROM auth_sessions AS s
           JOIN members AS m ON m.id = s.member_id AND m.role = 'pl'
           JOIN campaigns AS c ON c.id = m.campaign_id
           JOIN actors AS a ON a.member_id = m.id AND a.kind = 'pc'
           WHERE s.token_hash = ?""",
        (_token_hash(token),),
    ).fetchone()
    if row is None or row["expires_at"] <= time.time():
        raise AuthError("invalid_session", "会话已失效，请重新登录。")
    return MemberSession(**dict(row))


class AuthService:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def login(self, campaign_name: str, actor_name: str, password: str) -> dict[str, object]:
        with open_campaign(self.directory) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if campaign_name.strip() != campaign_info(connection)["name"]:
                    raise AuthError("campaign_mismatch", "角色信息错误", 400)
                credential = connection.execute(
                    """SELECT m.id AS member_id, c.password, c.password_hash
                       FROM actors AS a
                       JOIN members AS m ON m.id = a.member_id AND m.role = 'pl'
                       LEFT JOIN credentials AS c ON c.member_id = m.id
                       WHERE a.kind = 'pc' AND a.name = ? COLLATE NOCASE""",
                    (actor_name.strip(),),
                ).fetchone()
                if credential is None:
                    raise AuthError("actor_not_found", "角色信息错误")
                if not 1 <= len(password) <= 256 or not _password_matches(password, credential):
                    raise AuthError("invalid_credentials", "密码错误")
                member_id = credential["member_id"]
                token = secrets.token_urlsafe(32)
                connection.execute("DELETE FROM auth_sessions WHERE member_id = ?", (member_id,))
                connection.execute(
                    "INSERT INTO auth_sessions (token_hash, member_id, expires_at) VALUES (?, ?, ?)",
                    (_token_hash(token), member_id, time.time() + SESSION_SECONDS),
                )
                session = session_for_token(connection, token)
        return {"token": token, **session.public_info()}

    def require_session(self, token: str) -> MemberSession:
        with open_campaign(self.directory) as connection:
            return session_for_token(connection, token)

    def change_password(self, token: str, current_password: str, new_password: str) -> None:
        if not 1 <= len(new_password) <= 256 or not new_password.strip():
            raise AuthError("invalid_password", "新密码须为 1–256 个字符，且不能只有空白。", 400)
        if not 1 <= len(current_password) <= 256:
            raise AuthError("invalid_credentials", "当前密码错误。")
        try:
            new_password.encode("utf-8")
        except UnicodeEncodeError as error:
            raise AuthError("invalid_password", "密码包含无效的 Unicode 字符。", 400) from error
        with open_campaign(self.directory) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                session = session_for_token(connection, token)
                credential = connection.execute(
                    """SELECT c.password, c.password_hash FROM members AS m
                       LEFT JOIN credentials AS c ON c.member_id = m.id WHERE m.id = ?""",
                    (session.member_id,),
                ).fetchone()
                if not _password_matches(current_password, credential):
                    raise AuthError("invalid_credentials", "当前密码错误。")
                if current_password == new_password:
                    raise AuthError("password_unchanged", "新密码须与当前密码不同。", 400)
                connection.execute(
                    """INSERT INTO credentials (member_id, password) VALUES (?, ?)
                       ON CONFLICT(member_id) DO UPDATE SET
                           password = excluded.password, password_hash = NULL""",
                    (session.member_id, new_password),
                )

    def logout(self, token: str) -> str:
        with open_campaign(self.directory) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                session = session_for_token(connection, token)
                connection.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (session.token_hash,))
        return session.member_id

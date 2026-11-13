"""当前系统用户的角色登录记录。"""

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths

from .auth_client import ClientSession


def _password_digest(password: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password, salt, 600_000)


def offline_password_matches(record: dict, password: str) -> bool:
    if not 1 <= len(password) <= 256:
        return False
    try:
        supplied = password.encode("utf-8")
        verifier = record.get("offline_password_hash")
        if verifier is None:
            # 旧记录只有勾选记住密码时才有经主机验证的凭据。
            return hmac.compare_digest(supplied, record["password"].encode("utf-8"))
        if not isinstance(verifier, str) or len(verifier) != 97:
            return False
        salt, digest = (bytes.fromhex(part) for part in verifier.split(":"))
        return len(salt) == 16 and len(digest) == 32 and hmac.compare_digest(_password_digest(supplied, salt), digest)
    except ValueError:
        return False


def _profile_key(campaign_id: str, member_id: str) -> str:
    identity = json.dumps([campaign_id, member_id]).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


class LoginHistory:
    def __init__(self, settings: QSettings | None = None) -> None:
        if settings is not None:
            self.settings = settings
            return

        data_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
        path = data_dir / "player.ini"
        self.settings = QSettings(str(path), QSettings.Format.IniFormat)
        if not path.exists():
            self._import_legacy_history()

    def _import_legacy_history(self) -> None:
        legacy = QSettings("Story", "Player")
        legacy.setFallbacksEnabled(False)
        legacy.beginGroup("logins")
        for key in legacy.childKeys():
            target = f"logins/{key}"
            if not self.settings.contains(target):
                self.settings.setValue(target, legacy.value(key, "", type=str))
        legacy.endGroup()
        self.settings.sync()

    def entries(self) -> list[dict]:
        self.settings.sync()
        records = []
        self.settings.beginGroup("logins")
        try:
            for key in self.settings.childKeys():
                try:
                    record = json.loads(self.settings.value(key, "", type=str))
                except ValueError:
                    continue
                if (isinstance(record, dict) and isinstance(record.get("last_used"), int)
                        and all(isinstance(record.get(field), str) for field in (
                            "campaign_name", "actor_name", "address", "password",
                        ))):
                    records.append(record)
        finally:
            self.settings.endGroup()
        return sorted(records, key=lambda record: record["last_used"], reverse=True)

    def save(self, session: ClientSession, address: str, password: str, *, remember: bool) -> bool:
        key = _profile_key(session.campaign_id, session.member_id)
        salt = secrets.token_bytes(16)
        digest = _password_digest(password.encode("utf-8"), salt)
        record = {
            "campaign_id": session.campaign_id, "member_id": session.member_id,
            "campaign_name": session.campaign_name, "actor_name": session.actor_name,
            "role": session.role,
            "address": address, "password": password if remember else "", "last_used": time.time_ns(),
            "offline_password_hash": f"{salt.hex()}:{digest.hex()}",
        }
        self.settings.setValue(f"logins/{key}", json.dumps(record, ensure_ascii=False))
        self.settings.sync()
        return self.settings.status() == QSettings.Status.NoError

    def rename(self, session: ClientSession) -> None:
        # 只更新名称，保留原来的密码记忆及离线校验散列。
        key = f"logins/{_profile_key(session.campaign_id, session.member_id)}"
        self.settings.sync()
        raw = self.settings.value(key, "", type=str)
        if not raw:
            return
        record = json.loads(raw)
        record.update(actor_name=session.actor_name, role=session.role)
        self.settings.setValue(key, json.dumps(record, ensure_ascii=False))
        self.settings.sync()

    def delete(self, record: dict) -> bool:
        """只删除匹配的本机登录记录，不修改主机身份或聊天历史。"""
        self.settings.sync()
        self.settings.beginGroup("logins")
        try:
            for key in self.settings.childKeys():
                try:
                    current = json.loads(self.settings.value(key, "", type=str))
                except ValueError:
                    continue
                if current == record:
                    self.settings.remove(key)
        finally:
            self.settings.endGroup()
        self.settings.sync()
        return self.settings.status() == QSettings.Status.NoError

    def forget_rejected_password(self, record: dict, password: str) -> None:
        campaign_id, member_id = record.get("campaign_id"), record.get("member_id")
        if not isinstance(campaign_id, str) or not isinstance(member_id, str) or not offline_password_matches(record, password):
            return
        key = f"logins/{_profile_key(campaign_id, member_id)}"
        self.settings.sync()
        try:
            current = json.loads(self.settings.value(key, "", type=str))
        except ValueError:
            return
        if current != record:
            return
        record = {**record, "password": "", "offline_password_hash": ""}
        self.settings.setValue(key, json.dumps(record, ensure_ascii=False))
        self.settings.sync()

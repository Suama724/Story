"""当前系统用户的角色登录记录。"""

import hashlib
import json
import time
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths

from .auth_client import ClientSession


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

    def save(self, session: ClientSession, address: str, password: str) -> bool:
        identity = json.dumps([session.campaign_id, session.member_id]).encode("utf-8")
        key = hashlib.sha256(identity).hexdigest()
        record = {
            "campaign_name": session.campaign_name, "actor_name": session.actor_name,
            "address": address, "password": password, "last_used": time.time_ns(),
        }
        self.settings.setValue(f"logins/{key}", json.dumps(record, ensure_ascii=False))
        self.settings.sync()
        return self.settings.status() == QSettings.Status.NoError

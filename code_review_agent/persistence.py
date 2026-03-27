"""Local persistence for user sessions and computer mappings."""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class UserRecord:
    user_id: str
    token: str
    computer_id: str | None = None
    computer_name: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_active: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class UserStore:
    """JSON-file backed user store. Survives server restarts."""

    def __init__(self, path: str | Path = "data/users.json"):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._users: dict[str, UserRecord] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                for token, rec in data.items():
                    self._users[token] = UserRecord(**rec)
            except (json.JSONDecodeError, TypeError):
                pass

    def _save(self) -> None:
        self._path.write_text(json.dumps(
            {t: asdict(r) for t, r in self._users.items()},
            indent=2,
        ))

    def create_user(self) -> UserRecord:
        token = uuid.uuid4().hex
        user_id = uuid.uuid4().hex[:12]
        rec = UserRecord(user_id=user_id, token=token)
        with self._lock:
            self._users[token] = rec
            self._save()
        return rec

    def get_user(self, token: str) -> UserRecord | None:
        with self._lock:
            rec = self._users.get(token)
            if rec:
                rec.last_active = datetime.now(timezone.utc).isoformat()
                self._save()
            return rec

    def set_computer(self, token: str, computer_id: str, computer_name: str) -> None:
        with self._lock:
            rec = self._users.get(token)
            if rec:
                rec.computer_id = computer_id
                rec.computer_name = computer_name
                self._save()

    def get_by_user_id(self, user_id: str) -> UserRecord | None:
        with self._lock:
            for rec in self._users.values():
                if rec.user_id == user_id:
                    return rec
        return None

"""SQLite 会话持久化：一张表存 JSON 快照，简单可靠。"""

import json
import sqlite3
import threading
from pathlib import Path


class SessionStore:
    def __init__(self, db_path: str):
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "session_id TEXT PRIMARY KEY,"
            "payload TEXT NOT NULL,"
            "updated_at TEXT NOT NULL)"
        )
        self._conn.commit()

    def save(self, session: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (session_id, payload, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (session["session_id"], json.dumps(session, ensure_ascii=False), session["updated_at"]),
            )
            self._conn.commit()

    def load(self, session_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT payload FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT payload FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

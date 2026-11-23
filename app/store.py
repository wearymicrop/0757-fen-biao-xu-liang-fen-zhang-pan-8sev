import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.errors import InputError
from app.manifest import canonical


class Archive:
    def __init__(self, path):
        self.path = Path(path)
        with closing(self.connect()):
            pass

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("""CREATE TABLE IF NOT EXISTS runs (
            digest TEXT PRIMARY KEY, payload TEXT NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS response_orders (
            event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL
        )""")
        return connection

    def find(self, digest):
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT payload FROM runs WHERE digest = ?", (digest,)).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, digest):
        result = self.find(digest)
        if result is None:
            raise InputError("RUN_NOT_FOUND", "找不到该归档", 404)
        return result

    def save(self, record):
        with closing(self.connect()) as connection, connection:
            inserted = connection.execute(
                "INSERT INTO runs VALUES (?, ?) ON CONFLICT(digest) DO NOTHING",
                (record["id"], canonical(record)),
            ).rowcount
            payload = connection.execute("SELECT payload FROM runs WHERE digest = ?", (record["id"],)).fetchone()[0]
        return json.loads(payload), not inserted

    def save_response_order(self, event_id, digest, record):
        with closing(self.connect()) as connection:
            connection.isolation_level = None
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT digest, payload FROM response_orders WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO response_orders VALUES (?, ?, ?)",
                    (event_id, digest, canonical(record)),
                )
                connection.commit()
                return record, "created"
            connection.commit()
            status = "reused" if row[0] == digest else "conflict"
            return json.loads(row[1]), status

    def get_response_order(self, event_id):
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM response_orders WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise InputError("ORDER_NOT_FOUND", "找不到该事件的补偿单", 404)
        return json.loads(row[0])

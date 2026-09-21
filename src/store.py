"""SQLite 持久化：计划、事件（幂等台账）、变更审计。

- plans   表保存计划完整快照（JSON），状态与有效期冗余为列便于扫描；
- events  表以事件编号为主键，是幂等处理的依据，接受与拒绝均留痕；
- changes 表记录每次临时变更的批准人与理由。

重启后从库中恢复全部状态，未完成计划继续保持受控。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id     TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    status      TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    plan_id       TEXT NOT NULL,
    step_seq      INTEGER,
    outcome       TEXT,
    accepted      INTEGER NOT NULL,
    code          TEXT,
    reject_reason TEXT,
    operator      TEXT,
    post          TEXT,
    note          TEXT,
    occurred_at   TEXT,
    recorded_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_plan ON events(plan_id);
CREATE TABLE IF NOT EXISTS changes (
    change_id   TEXT PRIMARY KEY,
    plan_id     TEXT NOT NULL,
    change_type TEXT NOT NULL,
    payload     TEXT NOT NULL,
    approver    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_plan ON changes(plan_id);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ 计划
    def save_plan(self, plan_dict: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO plans (plan_id, data, status, valid_until, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (plan_dict["plan_id"], json.dumps(plan_dict, ensure_ascii=False),
                 plan_dict["status"], plan_dict["valid_until"], plan_dict["updated_at"]),
            )

    def load_plan(self, plan_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def load_all_plans(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM plans").fetchall()
        return [json.loads(r["data"]) for r in rows]

    # ------------------------------------------------------------ 事件（幂等台账）
    def get_event(self, event_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def record_event(self, rec: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events (event_id, plan_id, step_seq, outcome, accepted, code,"
                " reject_reason, operator, post, note, occurred_at, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec["event_id"], rec["plan_id"], rec.get("step_seq"), rec.get("outcome"),
                 1 if rec["accepted"] else 0, rec.get("code"), rec.get("reject_reason"),
                 rec.get("operator"), rec.get("post"), rec.get("note"),
                 rec.get("occurred_at"), rec["recorded_at"]),
            )

    def events_for_plan(self, plan_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE plan_id = ? ORDER BY recorded_at, event_id",
                (plan_id,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 变更审计
    def record_change(self, rec: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO changes (change_id, plan_id, change_type, payload, approver, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rec["change_id"], rec["plan_id"], rec["change_type"],
                 json.dumps(rec.get("payload") or {}, ensure_ascii=False),
                 rec["approver"], rec["reason"], rec["created_at"]),
            )

    def changes_for_plan(self, plan_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM changes WHERE plan_id = ? ORDER BY created_at, change_id",
                (plan_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

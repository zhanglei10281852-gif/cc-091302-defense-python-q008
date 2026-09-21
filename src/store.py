"""SQLite 持久化层：重启后未完成计划继续受控。

计划及其参与方、步骤整体重写（数据量小，简单可靠）；
事件与变更记录为只追加表，保证留痕不可篡改。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .models import (
    ChangeRecord,
    Escort,
    EventRecord,
    Plan,
    PlanStatus,
    StepRecord,
    StepStatus,
    Vehicle,
    Visitor,
    Zone,
    ZoneLevel,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    level INTEGER NOT NULL,
    status TEXT NOT NULL,
    blocking_reason TEXT,
    visit_start TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visitors (
    plan_id TEXT NOT NULL,
    visitor_id TEXT NOT NULL,
    name TEXT NOT NULL,
    credential_no TEXT NOT NULL,
    credential_expires_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, visitor_id)
);
CREATE TABLE IF NOT EXISTS escorts (
    plan_id TEXT NOT NULL,
    escort_id TEXT NOT NULL,
    name TEXT NOT NULL,
    post TEXT NOT NULL,
    PRIMARY KEY (plan_id, escort_id)
);
CREATE TABLE IF NOT EXISTS vehicles (
    plan_id TEXT NOT NULL,
    plate_no TEXT NOT NULL,
    driver_name TEXT NOT NULL,
    PRIMARY KEY (plan_id, plate_no)
);
CREATE TABLE IF NOT EXISTS zones (
    plan_id TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    name TEXT NOT NULL,
    level INTEGER NOT NULL,
    PRIMARY KEY (plan_id, zone_id)
);
CREATE TABLE IF NOT EXISTS steps (
    plan_id TEXT NOT NULL,
    step_code TEXT NOT NULL,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    post TEXT NOT NULL,
    status TEXT NOT NULL,
    operator TEXT,
    detail TEXT,
    completed_at TEXT,
    PRIMARY KEY (plan_id, step_code)
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    step_code TEXT,
    outcome TEXT,
    operator TEXT NOT NULL,
    detail TEXT,
    occurred_at TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    reject_reason TEXT,
    plan_status_after TEXT NOT NULL,
    current_step_after TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_plan ON events(plan_id);
CREATE TABLE IF NOT EXISTS changes (
    change_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    approver TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_plan ON changes(plan_id);
"""


def _dt_to_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dt_from_str(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Store:
    """薄持久化层：连接管理、模式初始化和行<->模型映射。"""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---------- 计划 ----------

    def save_plan(self, plan: Plan) -> None:
        """整体写入计划（新建或更新）：计划行 + 参与方 + 步骤。"""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO plans (plan_id, title, level, status, blocking_reason,
                                   visit_start, valid_until, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    title=excluded.title, level=excluded.level, status=excluded.status,
                    blocking_reason=excluded.blocking_reason,
                    visit_start=excluded.visit_start, valid_until=excluded.valid_until,
                    updated_at=excluded.updated_at
                """,
                (
                    plan.plan_id,
                    plan.title,
                    int(plan.level),
                    plan.status.value,
                    plan.blocking_reason,
                    _dt_to_str(plan.visit_start),
                    _dt_to_str(plan.valid_until),
                    _dt_to_str(plan.created_at),
                    _dt_to_str(plan.updated_at),
                ),
            )
            for table in ("visitors", "escorts", "vehicles", "zones", "steps"):
                self._conn.execute(f"DELETE FROM {table} WHERE plan_id = ?", (plan.plan_id,))
            self._conn.executemany(
                "INSERT INTO visitors VALUES (?, ?, ?, ?, ?)",
                [
                    (plan.plan_id, v.visitor_id, v.name, v.credential_no,
                     _dt_to_str(v.credential_expires_at))
                    for v in plan.visitors
                ],
            )
            self._conn.executemany(
                "INSERT INTO escorts VALUES (?, ?, ?, ?)",
                [(plan.plan_id, e.escort_id, e.name, e.post) for e in plan.escorts],
            )
            self._conn.executemany(
                "INSERT INTO vehicles VALUES (?, ?, ?)",
                [(plan.plan_id, v.plate_no, v.driver_name) for v in plan.vehicles],
            )
            self._conn.executemany(
                "INSERT INTO zones VALUES (?, ?, ?, ?)",
                [(plan.plan_id, z.zone_id, z.name, int(z.level)) for z in plan.zones],
            )
            self._conn.executemany(
                "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        plan.plan_id, s.step_code, s.seq, s.name, s.post,
                        s.status.value, s.operator, s.detail,
                        _dt_to_str(s.completed_at) if s.completed_at else None,
                    )
                    for s in plan.steps
                ],
            )

    def load_plan(self, plan_id: str) -> Plan | None:
        row = self._conn.execute(
            "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_plan(row)

    def load_all_plans(self) -> list[Plan]:
        rows = self._conn.execute("SELECT * FROM plans ORDER BY created_at").fetchall()
        return [self._row_to_plan(r) for r in rows]

    def _row_to_plan(self, row: sqlite3.Row) -> Plan:
        plan_id = row["plan_id"]
        visitors = [
            Visitor(
                visitor_id=r["visitor_id"],
                name=r["name"],
                credential_no=r["credential_no"],
                credential_expires_at=_dt_from_str(r["credential_expires_at"]),
            )
            for r in self._conn.execute(
                "SELECT * FROM visitors WHERE plan_id = ?", (plan_id,)
            )
        ]
        escorts = [
            Escort(escort_id=r["escort_id"], name=r["name"], post=r["post"])
            for r in self._conn.execute(
                "SELECT * FROM escorts WHERE plan_id = ?", (plan_id,)
            )
        ]
        vehicles = [
            Vehicle(plate_no=r["plate_no"], driver_name=r["driver_name"])
            for r in self._conn.execute(
                "SELECT * FROM vehicles WHERE plan_id = ?", (plan_id,)
            )
        ]
        zones = [
            Zone(zone_id=r["zone_id"], name=r["name"], level=ZoneLevel(r["level"]))
            for r in self._conn.execute(
                "SELECT * FROM zones WHERE plan_id = ?", (plan_id,)
            )
        ]
        steps = [
            StepRecord(
                step_code=r["step_code"],
                seq=r["seq"],
                name=r["name"],
                post=r["post"],
                status=StepStatus(r["status"]),
                operator=r["operator"],
                detail=r["detail"] or "",
                completed_at=_dt_from_str(r["completed_at"]) if r["completed_at"] else None,
            )
            for r in self._conn.execute(
                "SELECT * FROM steps WHERE plan_id = ? ORDER BY seq", (plan_id,)
            )
        ]
        return Plan(
            plan_id=plan_id,
            title=row["title"],
            level=ZoneLevel(row["level"]),
            status=PlanStatus(row["status"]),
            blocking_reason=row["blocking_reason"],
            visit_start=_dt_from_str(row["visit_start"]),
            valid_until=_dt_from_str(row["valid_until"]),
            created_at=_dt_from_str(row["created_at"]),
            updated_at=_dt_from_str(row["updated_at"]),
            visitors=visitors,
            escorts=escorts,
            vehicles=vehicles,
            zones=zones,
            steps=steps,
        )

    def expire_overdue(self, now: datetime) -> list[str]:
        """把超出有效窗口的未完成计划置为 EXPIRED，返回受影响的计划号。"""
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE plans SET status = ?, updated_at = ?
                WHERE status IN (?, ?) AND valid_until < ?
                RETURNING plan_id
                """,
                (
                    PlanStatus.EXPIRED.value,
                    _dt_to_str(now),
                    PlanStatus.IN_PROGRESS.value,
                    PlanStatus.BLOCKED.value,
                    _dt_to_str(now),
                ),
            )
            return [r["plan_id"] for r in cur.fetchall()]

    # ---------- 事件（幂等） ----------

    def insert_event(self, ev: EventRecord) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO events (event_id, plan_id, step_code, outcome, operator,
                                    detail, occurred_at, accepted, reject_reason,
                                    plan_status_after, current_step_after, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ev.event_id, ev.plan_id, ev.step_code, ev.outcome, ev.operator,
                    ev.detail, _dt_to_str(ev.occurred_at), int(ev.accepted),
                    ev.reject_reason, ev.plan_status_after, ev.current_step_after,
                    _dt_to_str(ev.recorded_at),
                ),
            )

    def get_event(self, event_id: str) -> EventRecord | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return self._row_to_event(row) if row else None

    def list_events(self, plan_id: str) -> list[EventRecord]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE plan_id = ? ORDER BY recorded_at", (plan_id,)
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            event_id=row["event_id"],
            plan_id=row["plan_id"],
            step_code=row["step_code"],
            outcome=row["outcome"],
            operator=row["operator"],
            detail=row["detail"] or "",
            occurred_at=_dt_from_str(row["occurred_at"]),
            accepted=bool(row["accepted"]),
            reject_reason=row["reject_reason"],
            plan_status_after=row["plan_status_after"],
            current_step_after=row["current_step_after"],
            recorded_at=_dt_from_str(row["recorded_at"]),
        )

    # ---------- 变更留痕 ----------

    def insert_change(self, change: ChangeRecord) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO changes VALUES (?, ?, ?, ?, ?, ?)",
                (
                    change.change_id, change.plan_id, change.approver,
                    change.reason, change.detail, _dt_to_str(change.created_at),
                ),
            )

    def list_changes(self, plan_id: str) -> list[ChangeRecord]:
        rows = self._conn.execute(
            "SELECT * FROM changes WHERE plan_id = ? ORDER BY created_at", (plan_id,)
        ).fetchall()
        return [
            ChangeRecord(
                change_id=r["change_id"],
                plan_id=r["plan_id"],
                approver=r["approver"],
                reason=r["reason"],
                detail=r["detail"],
                created_at=_dt_from_str(r["created_at"]),
            )
            for r in rows
        ]

"""领域模型：访问计划、核验步骤与参与方。

所有时间统一为 UTC ISO8601 字符串存储，便于排序与跨重启恢复。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """统一转换为带时区的 UTC ISO 字符串（字典序即可比较先后）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class PlanStatus(str, Enum):
    ACTIVE = "ACTIVE"          # 进行中，接受当前步骤的核验结果
    COMPLETED = "COMPLETED"    # 全部步骤通过，已放行
    BLOCKED = "BLOCKED"        # 步骤失败或证件过期，后续放行被阻断
    EXPIRED = "EXPIRED"        # 超过有效窗口仍未完成，拒绝一切旧操作
    CANCELLED = "CANCELLED"    # 已取消


class StepStatus(str, Enum):
    PENDING = "PENDING"   # 等待前序步骤
    READY = "READY"       # 当前可执行（同一计划至多一个）
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"   # 因前序失败被阻断


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


# ---------------------------------------------------------------- 参与方

@dataclass
class Visitor:
    visitor_id: str
    name: str
    credential_no: str
    credential_expires_at: str  # 证件有效期截止（ISO）

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Visitor":
        return cls(**d)


@dataclass
class Escort:
    escort_id: str
    name: str
    employee_no: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Escort":
        return cls(**d)


@dataclass
class Vehicle:
    plate_no: str
    driver_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Vehicle":
        return cls(**d)


@dataclass
class Zone:
    zone_id: str
    name: str
    level: int  # 1 普通区 / 2 受限区 / 3 核心区

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Zone":
        return cls(**d)


# ---------------------------------------------------------------- 步骤与计划

@dataclass
class Step:
    seq: int
    key: str
    name: str
    post: str                              # 责任岗位（门卫/接待室/安检岗/审批岗）
    status: StepStatus = StepStatus.PENDING
    failure_reason: Optional[str] = None
    completed_by: Optional[str] = None
    completed_at: Optional[str] = None
    event_id: Optional[str] = None         # 完成该步骤的事件编号

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Step":
        d = dict(d)
        d["status"] = StepStatus(d["status"])
        return cls(**d)


@dataclass
class VisitPlan:
    plan_id: str
    title: str
    visitors: list[Visitor]
    escorts: list[Escort]
    vehicles: list[Vehicle]
    zones: list[Zone]
    valid_from: str
    valid_until: str
    status: PlanStatus = PlanStatus.ACTIVE
    steps: list[Step] = field(default_factory=list)
    blocking_reason: Optional[str] = None
    version: int = 0                       # 每次临时变更递增
    created_at: str = ""
    updated_at: str = ""

    @property
    def required_level(self) -> int:
        return max(z.level for z in self.zones)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "title": self.title,
            "visitors": [v.to_dict() for v in self.visitors],
            "escorts": [e.to_dict() for e in self.escorts],
            "vehicles": [v.to_dict() for v in self.vehicles],
            "zones": [z.to_dict() for z in self.zones],
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
            "blocking_reason": self.blocking_reason,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VisitPlan":
        d = dict(d)
        d["visitors"] = [Visitor.from_dict(x) for x in d["visitors"]]
        d["escorts"] = [Escort.from_dict(x) for x in d["escorts"]]
        d["vehicles"] = [Vehicle.from_dict(x) for x in d["vehicles"]]
        d["zones"] = [Zone.from_dict(x) for x in d["zones"]]
        d["steps"] = [Step.from_dict(x) for x in d["steps"]]
        d["status"] = PlanStatus(d["status"])
        return cls(**d)

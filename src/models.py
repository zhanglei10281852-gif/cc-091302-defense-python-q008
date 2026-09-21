"""领域模型：访问计划、参与方、核验步骤、事件与变更记录。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum


def ensure_aware(dt: datetime, field_name: str = "时间") -> datetime:
    """要求时间必须携带时区，避免门卫终端与服务器时区不一致造成歧义。"""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{field_name}必须携带时区信息")
    return dt


class ZoneLevel(IntEnum):
    """区域等级，数值越大管控越严、核验步骤越多。"""

    GENERAL = 1  # 一般区域
    KEY = 2  # 重点区域
    CORE = 3  # 核心区域


class PlanStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"  # 核验进行中
    BLOCKED = "BLOCKED"  # 被阻断，禁止后续放行
    COMPLETED = "COMPLETED"  # 已完成放行
    EXPIRED = "EXPIRED"  # 超出有效窗口，拒绝旧操作
    CANCELLED = "CANCELLED"  # 经批准取消


class StepStatus(str, Enum):
    PENDING = "PENDING"  # 待核验
    PASSED = "PASSED"  # 已通过
    FAILED = "FAILED"  # 未通过（触发阻断）
    OBSOLETE = "OBSOLETE"  # 计划变更后不再适用，仅留痕


@dataclass(frozen=True)
class Visitor:
    visitor_id: str
    name: str
    credential_no: str  # 证件编号
    credential_expires_at: datetime  # 证件有效期截止

    def credential_valid_at(self, at: datetime) -> bool:
        return self.credential_expires_at > at


@dataclass(frozen=True)
class Escort:
    escort_id: str
    name: str
    post: str  # 陪同人岗位


@dataclass(frozen=True)
class Vehicle:
    plate_no: str
    driver_name: str


@dataclass(frozen=True)
class Zone:
    zone_id: str
    name: str
    level: ZoneLevel


@dataclass
class StepRecord:
    """计划内的一个核验步骤，seq 决定先后依赖顺序。"""

    step_code: str
    seq: int
    name: str
    post: str  # 责任岗位
    status: StepStatus = StepStatus.PENDING
    operator: str | None = None
    detail: str = ""
    completed_at: datetime | None = None


@dataclass
class Plan:
    """一次访问计划：访客、陪同人、车辆、访问区域 + 核验步骤链。"""

    plan_id: str
    title: str
    level: ZoneLevel
    status: PlanStatus
    visit_start: datetime  # 到访时间
    valid_until: datetime  # 计划有效窗口截止
    created_at: datetime
    updated_at: datetime
    blocking_reason: str | None = None
    visitors: list[Visitor] = field(default_factory=list)
    escorts: list[Escort] = field(default_factory=list)
    vehicles: list[Vehicle] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)

    def active_steps(self) -> list[StepRecord]:
        """当前有效的步骤链（不含变更后作废的），按依赖顺序排列。"""
        return sorted(
            (s for s in self.steps if s.status != StepStatus.OBSOLETE),
            key=lambda s: s.seq,
        )

    def current_step(self) -> StepRecord | None:
        """管理视角的当前步骤：卡住的失败步骤优先，否则为下一个待核验步骤。"""
        chain = self.active_steps()
        for s in chain:
            if s.status == StepStatus.FAILED:
                return s
        for s in chain:
            if s.status == StepStatus.PENDING:
                return s
        return None


@dataclass(frozen=True)
class EventRecord:
    """门卫/岗位终端提交的一次核验事件，按 event_id 幂等。"""

    event_id: str
    plan_id: str
    step_code: str | None
    outcome: str | None  # PASS / FAIL
    operator: str
    detail: str
    occurred_at: datetime  # 终端侧发生时间（断线补传时为原始时间）
    accepted: bool
    reject_reason: str | None
    plan_status_after: str
    current_step_after: str | None
    recorded_at: datetime  # 服务端落库时间


@dataclass(frozen=True)
class ChangeRecord:
    """临时变更留痕：必须登记批准人与理由。"""

    change_id: str
    plan_id: str
    approver: str  # 批准人
    reason: str  # 理由
    detail: str  # JSON，变更内容摘要
    created_at: datetime

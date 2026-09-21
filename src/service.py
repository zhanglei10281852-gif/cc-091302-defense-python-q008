"""基地访客预约联动服务。

把访客、陪同人、车辆和访问区域组成一次访问计划，按区域等级生成
有先后依赖的核验步骤；任一步骤失败或访客证件过期即阻断后续放行；
临时变更必须登记批准人和理由；门卫终端事件按事件编号幂等处理，
过期计划的旧操作一律拒绝；状态持久化，重启后未完成计划继续受控。
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Iterable

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
    ensure_aware,
)
from .store import Store
from .workflow import CREDENTIAL_GUARDED_STEPS, build_step_chain

_VALID_OUTCOMES = ("PASS", "FAIL")
_CLOSED_STATUSES = (PlanStatus.COMPLETED, PlanStatus.EXPIRED, PlanStatus.CANCELLED)


class VisitPlanError(Exception):
    """计划创建、变更等管理操作的参数或状态错误。"""


class VisitLinkageService:
    """预约联动服务入口。线程安全；所有状态经 SQLite 持久化。"""

    def __init__(self, db_path: str = ":memory:", now_fn: Callable[[], datetime] | None = None):
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._store = Store(db_path)
        self._lock = threading.RLock()
        # 重启后先清扫：超出有效窗口的未完成计划置为过期，继续受控
        self._store.expire_overdue(self._now())

    def close(self) -> None:
        self._store.close()

    # ------------------------------------------------------------------
    # 计划创建
    # ------------------------------------------------------------------

    def create_plan(
        self,
        *,
        title: str,
        visitors: Iterable[Visitor],
        zones: Iterable[Zone],
        visit_start: datetime,
        valid_until: datetime,
        escorts: Iterable[Escort] = (),
        vehicles: Iterable[Vehicle] = (),
        plan_id: str | None = None,
    ) -> str:
        """创建访问计划并按最高区域等级生成核验步骤链，返回计划号。"""
        now = self._now()
        ensure_aware(visit_start, "到访时间")
        ensure_aware(valid_until, "有效截止时间")
        visitors = list(visitors)
        zones = list(zones)
        escorts = list(escorts)
        vehicles = list(vehicles)
        if not visitors:
            raise VisitPlanError("访问计划至少包含一名访客")
        if not zones:
            raise VisitPlanError("访问计划至少包含一个访问区域")
        if valid_until <= now:
            raise VisitPlanError("计划有效截止时间必须晚于当前时间")
        if valid_until <= visit_start:
            raise VisitPlanError("计划有效截止时间必须晚于到访时间")
        expired = [v.name for v in visitors if not v.credential_valid_at(now)]
        if expired:
            raise VisitPlanError(f"访客证件已过期，不能建档：{'、'.join(expired)}")

        level = max(z.level for z in zones)
        plan = Plan(
            plan_id=plan_id or f"VP-{uuid.uuid4().hex[:12].upper()}",
            title=title,
            level=ZoneLevel(level),
            status=PlanStatus.IN_PROGRESS,
            visit_start=visit_start,
            valid_until=valid_until,
            created_at=now,
            updated_at=now,
            visitors=visitors,
            escorts=escorts,
            vehicles=vehicles,
            zones=zones,
            steps=build_step_chain(ZoneLevel(level), has_vehicles=bool(vehicles)),
        )
        with self._lock:
            if self._store.load_plan(plan.plan_id) is not None:
                raise VisitPlanError(f"计划号已存在：{plan.plan_id}")
            self._store.save_plan(plan)
        return plan.plan_id

    # ------------------------------------------------------------------
    # 门卫/岗位终端：提交核验结果（按事件编号幂等）
    # ------------------------------------------------------------------

    def submit_step_result(
        self,
        *,
        plan_id: str,
        event_id: str,
        step_code: str,
        outcome: str,
        operator: str,
        detail: str = "",
        occurred_at: datetime | None = None,
    ) -> dict:
        """处理终端上报的核验事件。

        - 同一 event_id 重复提交（含断线补传）返回首次处理结果，不重复生效；
        - 已过期/已关闭计划的旧操作一律拒绝并留痕；
        - 必须按步骤依赖顺序提交，前序未完成或计划被阻断时拒绝；
        - 证件核验与放行步骤强制检查访客证件有效期，过期即判失败并阻断。
        """
        outcome = str(outcome).upper()
        if outcome not in _VALID_OUTCOMES:
            raise ValueError(f"非法核验结果：{outcome}，仅支持 PASS/FAIL")
        with self._lock:
            now = self._now()
            occurred_at = ensure_aware(occurred_at, "事件发生时间") if occurred_at else now

            existing = self._store.get_event(event_id)
            if existing is not None:
                if existing.plan_id != plan_id:
                    raise VisitPlanError(f"事件编号 {event_id} 已属于计划 {existing.plan_id}")
                return self._event_response(existing, replayed=True)

            plan = self._store.load_plan(plan_id)
            if plan is None:
                return self._reject(
                    plan_id, event_id, step_code, outcome, operator, detail,
                    occurred_at, now, reason="计划不存在",
                    plan_status_after="UNKNOWN", current_step_after=None,
                )

            self._expire_if_overdue(plan, now)

            if plan.status in _CLOSED_STATUSES:
                return self._reject(
                    plan, event_id, step_code, outcome, operator, detail,
                    occurred_at, now, reason=f"计划已{self._status_label(plan.status)}，拒绝旧操作",
                )
            if plan.status == PlanStatus.BLOCKED:
                return self._reject(
                    plan, event_id, step_code, outcome, operator, detail,
                    occurred_at, now, reason=f"计划已被阻断：{plan.blocking_reason}",
                )

            current = plan.current_step()
            if current is None or step_code != current.step_code:
                return self._reject(
                    plan, event_id, step_code, outcome, operator, detail,
                    occurred_at, now,
                    reason=self._order_violation_reason(plan, step_code),
                )

            # 证件有效期强制检查：终端误报通过也拦截
            system_note = ""
            if outcome == "PASS" and step_code in CREDENTIAL_GUARDED_STEPS:
                expired = [v for v in plan.visitors if not v.credential_valid_at(now)]
                if expired:
                    names = "、".join(v.name for v in expired)
                    outcome = "FAIL"
                    system_note = f"系统强制拦截：访客{names}证件已过期"
                    detail = f"{detail}；{system_note}" if detail else system_note

            current.status = StepStatus.PASSED if outcome == "PASS" else StepStatus.FAILED
            current.operator = operator
            current.detail = detail
            current.completed_at = now
            if outcome == "FAIL":
                plan.status = PlanStatus.BLOCKED
                plan.blocking_reason = f"步骤[{current.name}]未通过：{detail or '未说明原因'}"
            elif current.step_code == "release":
                plan.status = PlanStatus.COMPLETED
            plan.updated_at = now
            self._store.save_plan(plan)

            event = EventRecord(
                event_id=event_id,
                plan_id=plan.plan_id,
                step_code=step_code,
                outcome=outcome,
                operator=operator,
                detail=detail,
                occurred_at=occurred_at,
                accepted=True,
                reject_reason=None,
                plan_status_after=plan.status.value,
                current_step_after=self._current_step_code(plan),
                recorded_at=now,
            )
            self._store.insert_event(event)
            return self._event_response(event, replayed=False)

    # ------------------------------------------------------------------
    # 临时变更（必须登记批准人和理由）
    # ------------------------------------------------------------------

    def apply_change(
        self,
        *,
        plan_id: str,
        approver: str,
        reason: str,
        visitors: Iterable[Visitor] | None = None,
        escorts: Iterable[Escort] | None = None,
        vehicles: Iterable[Vehicle] | None = None,
        zones: Iterable[Zone] | None = None,
        valid_until: datetime | None = None,
        cancel: bool = False,
    ) -> dict:
        """经批准的临时变更。留痕批准人、理由与变更内容。

        - 区域或车辆变化会重建步骤链：已通过的同名步骤保留，其余重新核验；
        - 被阻断的计划经批准变更后，失败步骤重置为待核验，可继续流程；
        - 已过期/已完成/已取消的计划不允许变更。
        """
        if not approver or not approver.strip():
            raise VisitPlanError("临时变更必须登记批准人")
        if not reason or not reason.strip():
            raise VisitPlanError("临时变更必须登记理由")
        with self._lock:
            now = self._now()
            plan = self._store.load_plan(plan_id)
            if plan is None:
                raise VisitPlanError(f"计划不存在：{plan_id}")
            self._expire_if_overdue(plan, now)
            if plan.status in _CLOSED_STATUSES:
                raise VisitPlanError(
                    f"计划已{self._status_label(plan.status)}，不允许变更"
                )

            summary: dict = {}
            if cancel:
                for s in plan.steps:
                    if s.status == StepStatus.PENDING:
                        s.status = StepStatus.OBSOLETE
                plan.status = PlanStatus.CANCELLED
                plan.blocking_reason = None
                summary["cancel"] = True
            else:
                if visitors is not None:
                    plan.visitors = list(visitors)
                    summary["visitors"] = [v.visitor_id for v in plan.visitors]
                if escorts is not None:
                    plan.escorts = list(escorts)
                    summary["escorts"] = [e.escort_id for e in plan.escorts]
                if vehicles is not None:
                    plan.vehicles = list(vehicles)
                    summary["vehicles"] = [v.plate_no for v in plan.vehicles]
                if zones is not None:
                    plan.zones = list(zones)
                    if not plan.zones:
                        raise VisitPlanError("访问计划至少包含一个访问区域")
                    plan.level = ZoneLevel(max(z.level for z in plan.zones))
                    summary["zones"] = [z.zone_id for z in plan.zones]
                if valid_until is not None:
                    ensure_aware(valid_until, "有效截止时间")
                    if valid_until <= now:
                        raise VisitPlanError("计划有效截止时间必须晚于当前时间")
                    plan.valid_until = valid_until
                    summary["valid_until"] = valid_until.isoformat()
                if zones is not None or vehicles is not None:
                    self._rebuild_chain(plan)
                    summary["chain_rebuilt"] = True
                if plan.status == PlanStatus.BLOCKED:
                    # 批准变更即批准整改：失败步骤重新核验，解除阻断
                    for s in plan.steps:
                        if s.status == StepStatus.FAILED:
                            s.status = StepStatus.PENDING
                            s.operator = None
                            s.detail = ""
                            s.completed_at = None
                    plan.status = PlanStatus.IN_PROGRESS
                    plan.blocking_reason = None
                    summary["unblocked"] = True
                if not summary:
                    raise VisitPlanError("变更内容为空")

            plan.updated_at = now
            self._store.save_plan(plan)
            change = ChangeRecord(
                change_id=f"CH-{uuid.uuid4().hex[:12].upper()}",
                plan_id=plan.plan_id,
                approver=approver.strip(),
                reason=reason.strip(),
                detail=json.dumps(summary, ensure_ascii=False),
                created_at=now,
            )
            self._store.insert_change(change)
            return {
                "change_id": change.change_id,
                "plan_id": plan.plan_id,
                "plan_status": plan.status.value,
                "current_step": self._current_step_code(plan),
            }

    # ------------------------------------------------------------------
    # 管理查询
    # ------------------------------------------------------------------

    def get_plan_status(self, plan_id: str) -> dict:
        """管理查询：当前步骤、责任岗位、阻塞原因与到访时间线。"""
        with self._lock:
            plan = self._store.load_plan(plan_id)
            if plan is None:
                raise VisitPlanError(f"计划不存在：{plan_id}")
            self._expire_if_overdue(plan, self._now())
            current = plan.current_step()
            now = self._now()
            return {
                "plan_id": plan.plan_id,
                "title": plan.title,
                "status": plan.status.value,
                "level": int(plan.level),
                "current_step": (
                    {"code": current.step_code, "name": current.name}
                    if current else None
                ),
                "responsible_post": current.post if current else None,
                "blocking_reason": plan.blocking_reason,
                "visit_start": plan.visit_start.isoformat(),
                "valid_until": plan.valid_until.isoformat(),
                "steps": [
                    {
                        "code": s.step_code,
                        "name": s.name,
                        "post": s.post,
                        "status": s.status.value,
                        "operator": s.operator,
                        "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                    }
                    for s in plan.active_steps()
                ],
                "visitors": [
                    {
                        "visitor_id": v.visitor_id,
                        "name": v.name,
                        "credential_no": v.credential_no,
                        "credential_expires_at": v.credential_expires_at.isoformat(),
                        "credential_valid": v.credential_valid_at(now),
                    }
                    for v in plan.visitors
                ],
                "timeline": self._build_timeline(plan),
            }

    def list_plans(self, include_closed: bool = False) -> list[dict]:
        """计划一览，默认只列未完成（仍受控）的计划。"""
        with self._lock:
            self._store.expire_overdue(self._now())
            result = []
            for plan in self._store.load_all_plans():
                if not include_closed and plan.status in _CLOSED_STATUSES:
                    continue
                current = plan.current_step()
                result.append(
                    {
                        "plan_id": plan.plan_id,
                        "title": plan.title,
                        "status": plan.status.value,
                        "current_step": current.step_code if current else None,
                        "responsible_post": current.post if current else None,
                        "blocking_reason": plan.blocking_reason,
                        "visit_start": plan.visit_start.isoformat(),
                        "valid_until": plan.valid_until.isoformat(),
                    }
                )
            return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _expire_if_overdue(self, plan: Plan, now: datetime) -> None:
        if plan.status in (PlanStatus.IN_PROGRESS, PlanStatus.BLOCKED) and now > plan.valid_until:
            plan.status = PlanStatus.EXPIRED
            plan.updated_at = now
            self._store.save_plan(plan)

    @staticmethod
    def _current_step_code(plan: Plan) -> str | None:
        current = plan.current_step()
        return current.step_code if current else None

    @staticmethod
    def _status_label(status: PlanStatus) -> str:
        return {
            PlanStatus.COMPLETED: "完成放行",
            PlanStatus.EXPIRED: "过期",
            PlanStatus.CANCELLED: "取消",
        }.get(status, status.value)

    @staticmethod
    def _order_violation_reason(plan: Plan, step_code: str) -> str:
        active = plan.active_steps()
        if any(s.step_code == step_code and s.status == StepStatus.PASSED for s in active):
            return f"步骤[{step_code}]已完成，拒绝重复核验"
        if any(s.step_code == step_code for s in active):
            return f"步骤[{step_code}]的前序步骤未完成，拒绝越序操作"
        return f"计划 {plan.plan_id} 中不存在步骤[{step_code}]"

    def _reject(
        self,
        plan_or_id,
        event_id: str,
        step_code: str | None,
        outcome: str | None,
        operator: str,
        detail: str,
        occurred_at: datetime,
        now: datetime,
        *,
        reason: str,
        plan_status_after: str | None = None,
        current_step_after: str | None = None,
    ) -> dict:
        """拒绝事件并留痕：同一事件编号再次补传时返回相同结果。"""
        if isinstance(plan_or_id, Plan):
            plan_id = plan_or_id.plan_id
            plan_status_after = plan_or_id.status.value
            current_step_after = self._current_step_code(plan_or_id)
        else:
            plan_id = plan_or_id
        event = EventRecord(
            event_id=event_id,
            plan_id=plan_id,
            step_code=step_code,
            outcome=outcome,
            operator=operator,
            detail=detail,
            occurred_at=occurred_at,
            accepted=False,
            reject_reason=reason,
            plan_status_after=plan_status_after,
            current_step_after=current_step_after,
            recorded_at=now,
        )
        self._store.insert_event(event)
        return self._event_response(event, replayed=False)

    @staticmethod
    def _event_response(ev: EventRecord, replayed: bool) -> dict:
        return {
            "event_id": ev.event_id,
            "plan_id": ev.plan_id,
            "accepted": ev.accepted,
            "reject_reason": ev.reject_reason,
            "plan_status": ev.plan_status_after,
            "current_step": ev.current_step_after,
            "replayed": replayed,
        }

    @staticmethod
    def _rebuild_chain(plan: Plan) -> None:
        """按最新区域等级/车辆情况重建步骤链，保留已通过的同名步骤。"""
        new_chain = build_step_chain(plan.level, has_vehicles=bool(plan.vehicles))
        old_by_code = {
            s.step_code: s for s in plan.steps if s.status != StepStatus.OBSOLETE
        }
        merged: list[StepRecord] = []
        for spec in new_chain:
            old = old_by_code.pop(spec.step_code, None)
            if old is not None and old.status == StepStatus.PASSED:
                old.seq = spec.seq
                merged.append(old)
            else:
                merged.append(spec)
        for obsolete in old_by_code.values():
            obsolete.status = StepStatus.OBSOLETE
            merged.append(obsolete)
        plan.steps = merged

    def _build_timeline(self, plan: Plan) -> list[dict]:
        """到访时间线：建档、每次核验事件（含被拒）与批准变更，按发生时间排序。"""
        timeline = [
            {
                "time": plan.created_at.isoformat(),
                "type": "PLAN_CREATED",
                "actor": None,
                "summary": f"计划建档，区域等级 L{int(plan.level)}，到访时间 {plan.visit_start.isoformat()}",
            }
        ]
        for ev in self._store.list_events(plan.plan_id):
            if ev.accepted:
                summary = f"步骤[{ev.step_code}]核验{ev.outcome}"
                if ev.detail:
                    summary += f"：{ev.detail}"
            else:
                summary = f"事件被拒绝：{ev.reject_reason}"
            timeline.append(
                {
                    "time": ev.occurred_at.isoformat(),
                    "type": "STEP_RESULT" if ev.accepted else "EVENT_REJECTED",
                    "actor": ev.operator,
                    "summary": summary,
                }
            )
        for ch in self._store.list_changes(plan.plan_id):
            timeline.append(
                {
                    "time": ch.created_at.isoformat(),
                    "type": "PLAN_CHANGED",
                    "actor": ch.approver,
                    "summary": f"批准变更：{ch.reason}（{ch.detail}）",
                }
            )
        timeline.sort(key=lambda item: item["time"])
        return timeline


# 兼容骨架入口
Service = VisitLinkageService

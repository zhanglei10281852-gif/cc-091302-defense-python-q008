"""基地访客预约联动服务核心。

职责：
- 将访客、陪同人、车辆、访问区域组成一次访问计划，并按区域最高等级
  生成有先后依赖的核验步骤链；
- 任一步骤失败或访客证件过期即阻断后续放行；
- 临时变更必须记录批准人与理由，全程留痕；
- 门卫/接待室等终端可能重复提交或断线补传：按事件编号幂等处理，
  过期计划的旧操作一律拒绝；
- 状态持久化，重启后未完成计划继续受控。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable, Optional

from .models import (
    Escort, Outcome, PlanStatus, Step, StepStatus, Vehicle, VisitPlan, Visitor, Zone,
    new_id, parse_dt, to_iso, utcnow,
)
from .steps import (
    CREDENTIAL_CHECK_STEP_KEYS, VALID_LEVELS, build_steps,
)
from .store import Store


class ServiceError(Exception):
    """业务校验失败（管理侧操作直接抛出，API 层映射为 4xx）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# 允许的临时变更类型
CHANGE_TYPES = {
    "ADD_VISITOR", "REMOVE_VISITOR", "REPLACE_VISITOR",
    "ADD_ESCORT", "REMOVE_ESCORT", "REPLACE_ESCORT",
    "ADD_VEHICLE", "REMOVE_VEHICLE",
    "ADD_ZONE", "REMOVE_ZONE",
    "EXTEND_VALIDITY",
}


class VisitService:
    def __init__(self, db_path: str = ":memory:", now_fn: Optional[Callable[[], datetime]] = None):
        self._store = Store(db_path)
        self._now_fn = now_fn or utcnow
        self._lock = threading.RLock()
        # 重启后先扫描：有效窗口已过且未完成的计划置为 EXPIRED，继续受控
        self.sweep_expired()

    def close(self) -> None:
        self._store.close()

    def _now(self) -> datetime:
        return self._now_fn()

    # ================================================================ 计划创建
    def create_plan(
        self,
        *,
        title: str,
        visitors: list[dict],
        escorts: list[dict] | None = None,
        vehicles: list[dict] | None = None,
        zones: list[dict],
        valid_from: str,
        valid_until: str,
        plan_id: Optional[str] = None,
    ) -> dict[str, Any]:
        visitor_objs = [Visitor.from_dict(v) for v in visitors]
        escort_objs = [Escort.from_dict(e) for e in (escorts or [])]
        vehicle_objs = [Vehicle.from_dict(v) for v in (vehicles or [])]
        zone_objs = [Zone.from_dict(z) for z in zones]

        if not visitor_objs:
            raise ServiceError("NO_VISITOR", "访问计划至少包含一名访客")
        if not zone_objs:
            raise ServiceError("NO_ZONE", "访问计划至少包含一个访问区域")
        for z in zone_objs:
            if z.level not in VALID_LEVELS:
                raise ServiceError("INVALID_ZONE_LEVEL", f"不支持的区域等级: {z.level}")
        vf, vu = parse_dt(valid_from), parse_dt(valid_until)
        if vf >= vu:
            raise ServiceError("INVALID_WINDOW", "valid_from 必须早于 valid_until")
        for v in visitor_objs:
            parse_dt(v.credential_expires_at)  # 校验证件有效期格式

        level = max(z.level for z in zone_objs)
        if level >= 2 and not escort_objs:
            raise ServiceError("ESCORT_REQUIRED", "访问受限/核心区域必须指定陪同人")

        now = to_iso(self._now())
        plan = VisitPlan(
            plan_id=plan_id or new_id("plan"),
            title=title,
            visitors=visitor_objs,
            escorts=escort_objs,
            vehicles=vehicle_objs,
            zones=zone_objs,
            valid_from=to_iso(vf),
            valid_until=to_iso(vu),
            steps=build_steps(level, has_vehicle=bool(vehicle_objs)),
            created_at=now,
            updated_at=now,
        )
        self._refresh_steps(plan)
        with self._lock:
            self._store.save_plan(plan.to_dict())
        return self.get_plan_view(plan.plan_id)

    # ================================================================ 终端结果提交（幂等）
    def submit_result(
        self,
        *,
        plan_id: str,
        step_seq: int,
        event_id: str,
        outcome: str,
        post: str,
        operator: str,
        note: str = "",
        occurred_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """终端上报核验结果。重复事件编号返回首次处理结果，不产生副作用。"""
        if not event_id:
            raise ServiceError("EVENT_ID_REQUIRED", "必须提供事件编号以保证幂等")
        try:
            outcome_enum = Outcome(outcome)
        except ValueError:
            raise ServiceError("INVALID_OUTCOME", f"非法核验结果: {outcome}")

        now = self._now()
        with self._lock:
            existing = self._store.get_event(event_id)
            if existing is not None:
                if existing["plan_id"] != plan_id:
                    raise ServiceError("EVENT_ID_CONFLICT", "事件编号已被其他计划使用")
                return self._replay(existing)

            plan = self._load_plan(plan_id)
            self._expire_if_needed(plan, now)

            base = dict(event_id=event_id, plan_id=plan_id, step_seq=step_seq,
                        operator=operator, post=post, note=note,
                        occurred_at=occurred_at, recorded_at=to_iso(now))

            if plan.status != PlanStatus.ACTIVE:
                return self._reject(plan, base, "PLAN_NOT_ACTIVE",
                                    f"计划状态为 {plan.status.value}，拒绝处理该事件")
            if now < parse_dt(plan.valid_from):
                return self._reject(plan, base, "PLAN_NOT_YET_VALID",
                                    "计划尚未生效，拒绝提前执行核验/放行")

            step = next((s for s in plan.steps if s.seq == step_seq), None)
            if step is None:
                return self._reject(plan, base, "STEP_NOT_FOUND", f"步骤不存在: seq={step_seq}")
            if step.status != StepStatus.READY:
                return self._reject(plan, base, "STEP_NOT_READY",
                                    f"步骤[{step.name}]当前状态为 {step.status.value}，不可提交")
            if step.post != post:
                return self._reject(plan, base, "WRONG_POST",
                                    f"步骤[{step.name}]责任岗位为[{step.post}]，拒绝[{post}]提交")

            # 证件过期拦截：在登记/证件核验/放行步骤上，证件过期一律判失败并阻断
            applied, auto_fail_reason = outcome_enum, None
            if outcome_enum == Outcome.PASS and step.key in CREDENTIAL_CHECK_STEP_KEYS:
                expired = self._expired_visitors(plan, now)
                if expired:
                    applied = Outcome.FAIL
                    auto_fail_reason = "访客证件过期: " + "、".join(
                        f"{v.name}({v.credential_no})" for v in expired)

            if applied == Outcome.PASS:
                self._apply_pass(plan, step, operator, now, event_id)
            else:
                self._apply_fail(plan, step, auto_fail_reason or note or "核验未通过",
                                 operator, now, event_id)

            rec = dict(base, outcome=applied.value, accepted=True,
                       code=None, reject_reason=None)
            if auto_fail_reason:
                rec["note"] = (note + " " if note else "") + f"[系统判定] {auto_fail_reason}"
            self._store.record_event(rec)
            plan.updated_at = to_iso(now)
            self._store.save_plan(plan.to_dict())
            return self._result_view(plan, step, rec)

    # ================================================================ 临时变更（需批准人与理由）
    def apply_change(
        self,
        *,
        plan_id: str,
        change_type: str,
        payload: Optional[dict] = None,
        approver: str,
        reason: str,
        reset_failed: bool = False,
    ) -> dict[str, Any]:
        if not approver or not approver.strip():
            raise ServiceError("APPROVER_REQUIRED", "临时变更必须记录批准人")
        if not reason or not reason.strip():
            raise ServiceError("REASON_REQUIRED", "临时变更必须记录变更理由")
        if change_type not in CHANGE_TYPES:
            raise ServiceError("INVALID_CHANGE_TYPE", f"不支持的变更类型: {change_type}")
        payload = payload or {}
        now = self._now()

        with self._lock:
            plan = self._load_plan(plan_id)
            self._expire_if_needed(plan, now)
            if plan.status in (PlanStatus.COMPLETED, PlanStatus.CANCELLED):
                raise ServiceError("PLAN_CLOSED", f"计划状态为 {plan.status.value}，不允许变更")
            if plan.status == PlanStatus.EXPIRED and change_type != "EXTEND_VALIDITY":
                raise ServiceError("PLAN_EXPIRED", "计划已过期，仅允许延长有效期")

            structure_changed = self._apply_change_payload(plan, change_type, payload, now)

            if structure_changed:
                level = plan.required_level
                if level >= 2 and not plan.escorts:
                    raise ServiceError("ESCORT_REQUIRED", "访问受限/核心区域必须指定陪同人")
                carried = {s.key: s for s in plan.steps if s.status == StepStatus.PASSED}
                plan.steps = build_steps(level, has_vehicle=bool(plan.vehicles), carried=carried)

            if reset_failed:
                if plan.status != PlanStatus.BLOCKED:
                    raise ServiceError("NOT_BLOCKED", "仅被阻断的计划需要重置失败步骤")
                for s in plan.steps:
                    if s.status == StepStatus.FAILED:
                        s.status, s.failure_reason = StepStatus.PENDING, None
                    elif s.status == StepStatus.BLOCKED:
                        s.status = StepStatus.PENDING
                plan.status = PlanStatus.ACTIVE
                plan.blocking_reason = None

            if plan.status == PlanStatus.ACTIVE:
                self._refresh_steps(plan)

            plan.version += 1
            plan.updated_at = to_iso(now)
            change_rec = dict(
                change_id=new_id("chg"), plan_id=plan_id, change_type=change_type,
                payload={**payload, "reset_failed": reset_failed},
                approver=approver.strip(), reason=reason.strip(),
                created_at=to_iso(now),
            )
            self._store.record_change(change_rec)
            self._store.save_plan(plan.to_dict())
        return self.get_plan_view(plan_id)

    def cancel_plan(self, *, plan_id: str, approver: str, reason: str) -> dict[str, Any]:
        if not approver or not approver.strip():
            raise ServiceError("APPROVER_REQUIRED", "取消计划必须记录批准人")
        if not reason or not reason.strip():
            raise ServiceError("REASON_REQUIRED", "取消计划必须记录理由")
        now = self._now()
        with self._lock:
            plan = self._load_plan(plan_id)
            self._expire_if_needed(plan, now)
            if plan.status in (PlanStatus.COMPLETED, PlanStatus.CANCELLED):
                raise ServiceError("PLAN_CLOSED", f"计划状态为 {plan.status.value}，不允许取消")
            plan.status = PlanStatus.CANCELLED
            plan.updated_at = to_iso(now)
            plan.version += 1
            self._store.record_change(dict(
                change_id=new_id("chg"), plan_id=plan_id, change_type="CANCEL",
                payload={}, approver=approver.strip(), reason=reason.strip(),
                created_at=to_iso(now),
            ))
            self._store.save_plan(plan.to_dict())
        return self.get_plan_view(plan_id)

    # ================================================================ 管理查询
    def get_plan_view(self, plan_id: str) -> dict[str, Any]:
        """管理视图：当前步骤、责任岗位、阻塞原因与计划全貌。"""
        with self._lock:
            plan = self._load_plan(plan_id)
            self._expire_if_needed(plan, self._now())
            current = self._current_step(plan)
            return {
                "plan_id": plan.plan_id,
                "title": plan.title,
                "status": plan.status.value,
                "version": plan.version,
                "required_level": plan.required_level,
                "valid_from": plan.valid_from,
                "valid_until": plan.valid_until,
                "visitors": [v.to_dict() for v in plan.visitors],
                "escorts": [e.to_dict() for e in plan.escorts],
                "vehicles": [v.to_dict() for v in plan.vehicles],
                "zones": [z.to_dict() for z in plan.zones],
                "steps": [s.to_dict() for s in plan.steps],
                "current_step": current,
                "responsible_post": current["post"] if current else None,
                "blocking_reason": plan.blocking_reason,
                "created_at": plan.created_at,
                "updated_at": plan.updated_at,
            }

    def get_timeline(self, plan_id: str) -> list[dict[str, Any]]:
        """到访时间线：终端事件（含被拒记录）与变更审计按时间合并。"""
        with self._lock:
            self._load_plan(plan_id)  # 不存在则抛 PLAN_NOT_FOUND
            entries: list[dict[str, Any]] = []
            for e in self._store.events_for_plan(plan_id):
                entries.append({
                    "type": "event",
                    "event_id": e["event_id"],
                    "step_seq": e["step_seq"],
                    "outcome": e["outcome"],
                    "accepted": bool(e["accepted"]),
                    "code": e["code"],
                    "reject_reason": e["reject_reason"],
                    "operator": e["operator"],
                    "post": e["post"],
                    "note": e["note"],
                    "occurred_at": e["occurred_at"],
                    "recorded_at": e["recorded_at"],
                })
            for c in self._store.changes_for_plan(plan_id):
                entries.append({
                    "type": "change",
                    "change_id": c["change_id"],
                    "change_type": c["change_type"],
                    "payload": c["payload"],
                    "approver": c["approver"],
                    "reason": c["reason"],
                    "recorded_at": c["created_at"],
                })
            entries.sort(key=lambda x: x["recorded_at"])
            return entries

    def list_plans(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            self.sweep_expired()
            out = []
            for d in self._store.load_all_plans():
                plan = VisitPlan.from_dict(d)
                if status and plan.status.value != status:
                    continue
                current = self._current_step(plan)
                out.append({
                    "plan_id": plan.plan_id,
                    "title": plan.title,
                    "status": plan.status.value,
                    "current_step": current["name"] if current else None,
                    "responsible_post": current["post"] if current else None,
                    "blocking_reason": plan.blocking_reason,
                    "valid_until": plan.valid_until,
                })
            return out

    # ================================================================ 过期扫描
    def sweep_expired(self) -> int:
        """将超过有效窗口仍未完成的计划置为 EXPIRED；返回处理数量。"""
        now = self._now()
        count = 0
        with self._lock:
            for d in self._store.load_all_plans():
                plan = VisitPlan.from_dict(d)
                if self._expire_if_needed(plan, now):
                    count += 1
        return count

    # ================================================================ 内部实现
    def _load_plan(self, plan_id: str) -> VisitPlan:
        d = self._store.load_plan(plan_id)
        if d is None:
            raise ServiceError("PLAN_NOT_FOUND", f"计划不存在: {plan_id}")
        return VisitPlan.from_dict(d)

    def _expire_if_needed(self, plan: VisitPlan, now: datetime) -> bool:
        if plan.status == PlanStatus.ACTIVE and now > parse_dt(plan.valid_until):
            plan.status = PlanStatus.EXPIRED
            plan.updated_at = to_iso(now)
            self._store.save_plan(plan.to_dict())
            return True
        return False

    @staticmethod
    def _expired_visitors(plan: VisitPlan, now: datetime) -> list[Visitor]:
        return [v for v in plan.visitors if parse_dt(v.credential_expires_at) <= now]

    @staticmethod
    def _refresh_steps(plan: VisitPlan) -> None:
        """重算 READY：首个未通过步骤置 READY，其余未通过步骤回到 PENDING。"""
        if plan.status != PlanStatus.ACTIVE:
            return
        ready_assigned = False
        for s in plan.steps:
            if s.status == StepStatus.PASSED:
                continue
            if not ready_assigned:
                s.status = StepStatus.READY
                ready_assigned = True
            elif s.status == StepStatus.READY:
                s.status = StepStatus.PENDING

    @staticmethod
    def _apply_pass(plan: VisitPlan, step: Step, operator: str, now: datetime, event_id: str) -> None:
        step.status = StepStatus.PASSED
        step.completed_by = operator
        step.completed_at = to_iso(now)
        step.event_id = event_id
        if all(s.status == StepStatus.PASSED for s in plan.steps):
            plan.status = PlanStatus.COMPLETED
        else:
            VisitService._refresh_steps(plan)

    @staticmethod
    def _apply_fail(plan: VisitPlan, step: Step, reason: str,
                    operator: str, now: datetime, event_id: str) -> None:
        step.status = StepStatus.FAILED
        step.failure_reason = reason
        step.completed_by = operator
        step.completed_at = to_iso(now)
        step.event_id = event_id
        for s in plan.steps:
            if s.seq > step.seq and s.status in (StepStatus.PENDING, StepStatus.READY):
                s.status = StepStatus.BLOCKED
        plan.status = PlanStatus.BLOCKED
        plan.blocking_reason = f"步骤[{step.name}]未通过: {reason}"

    def _reject(self, plan: VisitPlan, base: dict, code: str, reason: str) -> dict[str, Any]:
        """拒绝并留痕：拒绝记录同样按事件编号幂等。"""
        rec = dict(base, outcome=None, accepted=False, code=code, reject_reason=reason)
        self._store.record_event(rec)
        return {
            "event_id": base["event_id"],
            "accepted": False,
            "idempotent_replay": False,
            "code": code,
            "reject_reason": reason,
            "plan_id": plan.plan_id,
            "plan_status": plan.status.value,
            "blocking_reason": plan.blocking_reason,
        }

    def _replay(self, existing: dict[str, Any]) -> dict[str, Any]:
        """重复提交：返回首次处理结果，不产生任何副作用。"""
        plan = self._store.load_plan(existing["plan_id"])
        return {
            "event_id": existing["event_id"],
            "accepted": bool(existing["accepted"]),
            "idempotent_replay": True,
            "code": existing["code"],
            "reject_reason": existing["reject_reason"],
            "plan_id": existing["plan_id"],
            "plan_status": plan["status"] if plan else None,
            "step_seq": existing["step_seq"],
            "outcome": existing["outcome"],
        }

    @staticmethod
    def _result_view(plan: VisitPlan, step: Step, rec: dict) -> dict[str, Any]:
        return {
            "event_id": rec["event_id"],
            "accepted": True,
            "idempotent_replay": False,
            "code": None,
            "reject_reason": None,
            "plan_id": plan.plan_id,
            "plan_status": plan.status.value,
            "step_seq": step.seq,
            "step_key": step.key,
            "step_status": step.status.value,
            "outcome": rec["outcome"],
            "blocking_reason": plan.blocking_reason,
        }

    @staticmethod
    def _current_step(plan: VisitPlan) -> Optional[dict[str, Any]]:
        for s in plan.steps:
            if s.status == StepStatus.READY:
                return {"seq": s.seq, "key": s.key, "name": s.name, "post": s.post}
        for s in plan.steps:
            if s.status == StepStatus.FAILED:
                return {"seq": s.seq, "key": s.key, "name": s.name, "post": s.post,
                        "failure_reason": s.failure_reason}
        return None

    # ------------------------------------------------------------ 变更载荷
    def _apply_change_payload(self, plan: VisitPlan, change_type: str,
                              payload: dict, now: datetime) -> bool:
        """应用变更，返回是否影响步骤结构（区域/车辆/陪同人变化）。"""
        if change_type == "ADD_VISITOR":
            plan.visitors.append(Visitor.from_dict(payload["visitor"]))
            return False
        if change_type == "REMOVE_VISITOR":
            if len(plan.visitors) <= 1:
                raise ServiceError("LAST_VISITOR", "计划至少保留一名访客")
            plan.visitors = [v for v in plan.visitors if v.visitor_id != payload["visitor_id"]]
            return False
        if change_type == "REPLACE_VISITOR":
            vid = payload["visitor_id"]
            plan.visitors = [Visitor.from_dict(payload["visitor"]) if v.visitor_id == vid else v
                             for v in plan.visitors]
            return False
        if change_type == "ADD_ESCORT":
            plan.escorts.append(Escort.from_dict(payload["escort"]))
            return True
        if change_type == "REMOVE_ESCORT":
            plan.escorts = [e for e in plan.escorts if e.escort_id != payload["escort_id"]]
            return True
        if change_type == "REPLACE_ESCORT":
            eid = payload["escort_id"]
            plan.escorts = [Escort.from_dict(payload["escort"]) if e.escort_id == eid else e
                            for e in plan.escorts]
            return True
        if change_type == "ADD_VEHICLE":
            plan.vehicles.append(Vehicle.from_dict(payload["vehicle"]))
            return True
        if change_type == "REMOVE_VEHICLE":
            plan.vehicles = [v for v in plan.vehicles if v.plate_no != payload["plate_no"]]
            return True
        if change_type == "ADD_ZONE":
            zone = Zone.from_dict(payload["zone"])
            if zone.level not in VALID_LEVELS:
                raise ServiceError("INVALID_ZONE_LEVEL", f"不支持的区域等级: {zone.level}")
            plan.zones.append(zone)
            return True
        if change_type == "REMOVE_ZONE":
            if len(plan.zones) <= 1:
                raise ServiceError("LAST_ZONE", "计划至少保留一个访问区域")
            plan.zones = [z for z in plan.zones if z.zone_id != payload["zone_id"]]
            return True
        if change_type == "EXTEND_VALIDITY":
            new_until = parse_dt(payload["valid_until"])
            if new_until <= now:
                raise ServiceError("INVALID_WINDOW", "新的有效期截止时间必须晚于当前时间")
            plan.valid_until = to_iso(new_until)
            if plan.status == PlanStatus.EXPIRED:
                plan.status = PlanStatus.ACTIVE  # 重新激活，步骤状态保持不变
            return False
        raise ServiceError("INVALID_CHANGE_TYPE", f"不支持的变更类型: {change_type}")


# 兼容骨架中的入口命名
class Service(VisitService):
    """领域服务入口，兼容既有骨架命名。"""

    def __init__(self, db_path: str = ":memory:", now_fn=None):
        super().__init__(db_path=db_path, now_fn=now_fn)
        self.ready = True

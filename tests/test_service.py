"""预约联动服务测试：步骤依赖、阻断、幂等、过期、变更审计、重启恢复。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src.service import ServiceError, VisitService

T0 = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


class Clock:
    def __init__(self, t: datetime = T0):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> None:
        self.t += timedelta(**kwargs)


def visitor(vid="V1", name="张三", days_valid=30):
    return {
        "visitor_id": vid,
        "name": name,
        "credential_no": f"ID-{vid}",
        "credential_expires_at": iso(T0 + timedelta(days=days_valid)),
    }


def escort(eid="E1", name="李工"):
    return {"escort_id": eid, "name": name, "employee_no": f"EMP-{eid}"}


def vehicle(plate="京A12345"):
    return {"plate_no": plate, "driver_name": "王师傅"}


def zone(zid="Z1", level=2, name="装配车间"):
    return {"zone_id": zid, "name": name, "level": level}


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.svc = VisitService(db_path=":memory:", now_fn=self.clock)
        self.event_seq = 0

    def tearDown(self):
        self.svc.close()

    def eid(self) -> str:
        self.event_seq += 1
        return f"evt-{self.event_seq:04d}"

    def make_plan(self, *, zones=None, vehicles=None, escorts=None,
                  visitors=None, valid_from=None, valid_until=None, title="外协检修"):
        return self.svc.create_plan(
            title=title,
            visitors=visitors if visitors is not None else [visitor()],
            escorts=escorts if escorts is not None else [escort()],
            vehicles=vehicles if vehicles is not None else [vehicle()],
            zones=zones if zones is not None else [zone()],
            valid_from=valid_from or iso(T0 - timedelta(hours=1)),
            valid_until=valid_until or iso(T0 + timedelta(hours=8)),
        )

    def pass_step(self, plan_id, seq, post, **kw):
        return self.svc.submit_result(
            plan_id=plan_id, step_seq=seq, event_id=self.eid(),
            outcome="PASS", post=post, operator=kw.pop("operator", "op-1"), **kw)


class TestStepGeneration(ServiceTestBase):
    def test_level1_steps(self):
        view = self.make_plan(zones=[zone(level=1)], vehicles=[])
        self.assertEqual([s["key"] for s in view["steps"]],
                         ["visitor_checkin", "gate_release"])
        self.assertEqual(view["required_level"], 1)

    def test_level2_with_vehicle(self):
        view = self.make_plan()
        self.assertEqual([s["key"] for s in view["steps"]],
                         ["visitor_identity", "escort_confirm",
                          "vehicle_inspection", "gate_release"])

    def test_level2_without_vehicle_skips_inspection(self):
        view = self.make_plan(vehicles=[])
        self.assertEqual([s["key"] for s in view["steps"]],
                         ["visitor_identity", "escort_confirm", "gate_release"])

    def test_level3_steps(self):
        view = self.make_plan(zones=[zone(level=3, name="核心机房")])
        self.assertEqual([s["key"] for s in view["steps"]],
                         ["visitor_identity", "security_screening", "escort_confirm",
                          "vehicle_inspection", "zone_access_approval", "gate_release"])

    def test_highest_zone_level_wins(self):
        view = self.make_plan(zones=[zone("Z1", 1, "办公区"), zone("Z2", 3, "核心机房")])
        self.assertEqual(view["required_level"], 3)
        self.assertEqual(len(view["steps"]), 6)

    def test_level2_requires_escort(self):
        with self.assertRaises(ServiceError) as ctx:
            self.make_plan(escorts=[])
        self.assertEqual(ctx.exception.code, "ESCORT_REQUIRED")

    def test_first_step_ready_others_pending(self):
        view = self.make_plan()
        statuses = [s["status"] for s in view["steps"]]
        self.assertEqual(statuses, ["READY", "PENDING", "PENDING", "PENDING"])
        self.assertEqual(view["current_step"]["key"], "visitor_identity")
        self.assertEqual(view["responsible_post"], "接待室")


class TestStepDependency(ServiceTestBase):
    def test_out_of_order_rejected(self):
        view = self.make_plan()
        res = self.svc.submit_result(plan_id=view["plan_id"], step_seq=2,
                                     event_id=self.eid(), outcome="PASS",
                                     post="接待室", operator="op")
        self.assertFalse(res["accepted"])
        self.assertEqual(res["code"], "STEP_NOT_READY")

    def test_wrong_post_rejected(self):
        view = self.make_plan()
        res = self.svc.submit_result(plan_id=view["plan_id"], step_seq=1,
                                     event_id=self.eid(), outcome="PASS",
                                     post="门卫", operator="op")
        self.assertFalse(res["accepted"])
        self.assertEqual(res["code"], "WRONG_POST")

    def test_full_happy_path_releases(self):
        view = self.make_plan()
        pid = view["plan_id"]
        for seq, post in [(1, "接待室"), (2, "接待室"), (3, "安检岗")]:
            res = self.pass_step(pid, seq, post)
            self.assertTrue(res["accepted"])
            self.assertEqual(res["plan_status"], "ACTIVE")
        res = self.pass_step(pid, 4, "门卫")
        self.assertEqual(res["plan_status"], "COMPLETED")
        final = self.svc.get_plan_view(pid)
        self.assertIsNone(final["current_step"])

    def test_step_failure_blocks_release(self):
        view = self.make_plan()
        pid = view["plan_id"]
        self.pass_step(pid, 1, "接待室")
        self.pass_step(pid, 2, "接待室")
        res = self.svc.submit_result(plan_id=pid, step_seq=3, event_id=self.eid(),
                                     outcome="FAIL", post="安检岗", operator="op",
                                     note="车内发现违禁物品")
        self.assertEqual(res["plan_status"], "BLOCKED")
        view = self.svc.get_plan_view(pid)
        self.assertIn("车辆安全检查", view["blocking_reason"])
        self.assertEqual([s["status"] for s in view["steps"]],
                         ["PASSED", "PASSED", "FAILED", "BLOCKED"])
        # 放行步骤被拒绝
        res = self.pass_step(pid, 4, "门卫")
        self.assertFalse(res["accepted"])
        self.assertEqual(res["code"], "PLAN_NOT_ACTIVE")

    def test_early_arrival_rejected(self):
        view = self.make_plan(valid_from=iso(T0 + timedelta(hours=2)))
        res = self.pass_step(view["plan_id"], 1, "接待室")
        self.assertFalse(res["accepted"])
        self.assertEqual(res["code"], "PLAN_NOT_YET_VALID")


class TestCredentialExpiry(ServiceTestBase):
    def test_expired_credential_blocks_release(self):
        # 证件在计划执行中途过期
        v = visitor(days_valid=0)
        v["credential_expires_at"] = iso(T0 + timedelta(hours=2))
        view = self.make_plan(visitors=[v])
        pid = view["plan_id"]
        self.pass_step(pid, 1, "接待室")
        self.pass_step(pid, 2, "接待室")
        self.pass_step(pid, 3, "安检岗")
        self.clock.advance(hours=3)  # 证件已过期
        res = self.pass_step(pid, 4, "门卫")
        self.assertTrue(res["accepted"])          # 事件被受理，但系统判定失败
        self.assertEqual(res["outcome"], "FAIL")
        self.assertEqual(res["plan_status"], "BLOCKED")
        self.assertIn("证件过期", self.svc.get_plan_view(pid)["blocking_reason"])

    def test_expired_credential_fails_identity_step(self):
        v = visitor()
        v["credential_expires_at"] = iso(T0 - timedelta(days=1))  # 已过期
        view = self.make_plan(visitors=[v])
        res = self.pass_step(view["plan_id"], 1, "接待室")
        self.assertEqual(res["outcome"], "FAIL")
        self.assertEqual(res["plan_status"], "BLOCKED")


class TestIdempotency(ServiceTestBase):
    def test_duplicate_event_replays_without_side_effect(self):
        view = self.make_plan()
        pid = view["plan_id"]
        event_id = self.eid()
        r1 = self.svc.submit_result(plan_id=pid, step_seq=1, event_id=event_id,
                                    outcome="PASS", post="接待室", operator="op")
        r2 = self.svc.submit_result(plan_id=pid, step_seq=1, event_id=event_id,
                                    outcome="PASS", post="接待室", operator="op")
        self.assertTrue(r1["accepted"])
        self.assertTrue(r2["idempotent_replay"])
        events = [e for e in self.svc.get_timeline(pid) if e["type"] == "event"]
        self.assertEqual(len(events), 1)  # 只记录一次
        steps = self.svc.get_plan_view(pid)["steps"]
        self.assertEqual([s["status"] for s in steps],
                         ["PASSED", "READY", "PENDING", "PENDING"])

    def test_rejection_is_also_idempotent(self):
        view = self.make_plan()
        pid = view["plan_id"]
        event_id = self.eid()
        kw = dict(plan_id=pid, step_seq=3, event_id=event_id,
                  outcome="PASS", post="安检岗", operator="op")
        r1 = self.svc.submit_result(**kw)
        r2 = self.svc.submit_result(**kw)
        self.assertFalse(r1["accepted"])
        self.assertFalse(r2["accepted"])
        self.assertTrue(r2["idempotent_replay"])
        self.assertEqual(r1["reject_reason"], r2["reject_reason"])

    def test_offline_backfill_accepted_when_step_ready(self):
        # 断线补传：事件发生时间早于当前时间，但事件编号唯一 → 正常受理
        view = self.make_plan()
        res = self.svc.submit_result(
            plan_id=view["plan_id"], step_seq=1, event_id=self.eid(),
            outcome="PASS", post="接待室", operator="op",
            occurred_at=iso(T0 - timedelta(minutes=40)))
        self.assertTrue(res["accepted"])
        tl = self.svc.get_timeline(view["plan_id"])
        self.assertEqual(tl[0]["occurred_at"], iso(T0 - timedelta(minutes=40)))

    def test_event_id_conflict_across_plans(self):
        p1 = self.make_plan(title="计划一")
        p2 = self.make_plan(title="计划二")
        event_id = self.eid()
        self.svc.submit_result(plan_id=p1["plan_id"], step_seq=1, event_id=event_id,
                               outcome="PASS", post="接待室", operator="op")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.submit_result(plan_id=p2["plan_id"], step_seq=1, event_id=event_id,
                                   outcome="PASS", post="接待室", operator="op")
        self.assertEqual(ctx.exception.code, "EVENT_ID_CONFLICT")


class TestPlanExpiry(ServiceTestBase):
    def test_expired_plan_rejects_old_operations(self):
        view = self.make_plan(valid_until=iso(T0 + timedelta(hours=1)))
        pid = view["plan_id"]
        self.pass_step(pid, 1, "接待室")
        self.clock.advance(hours=2)  # 计划过期
        # 断线补传的旧操作（发生时间在过期前）也被拒绝
        res = self.svc.submit_result(
            plan_id=pid, step_seq=2, event_id=self.eid(), outcome="PASS",
            post="接待室", operator="op",
            occurred_at=iso(T0 + timedelta(minutes=30)))
        self.assertFalse(res["accepted"])
        self.assertEqual(res["code"], "PLAN_NOT_ACTIVE")
        self.assertEqual(self.svc.get_plan_view(pid)["status"], "EXPIRED")

    def test_sweep_marks_expired(self):
        self.make_plan(valid_until=iso(T0 + timedelta(hours=1)))
        self.clock.advance(hours=2)
        self.assertEqual(self.svc.sweep_expired(), 1)
        self.assertEqual(self.svc.list_plans(status="EXPIRED")[0]["status"], "EXPIRED")


class TestChanges(ServiceTestBase):
    def test_change_requires_approver_and_reason(self):
        view = self.make_plan()
        with self.assertRaises(ServiceError) as ctx:
            self.svc.apply_change(plan_id=view["plan_id"], change_type="ADD_VEHICLE",
                                  payload={"vehicle": vehicle("京B99999")},
                                  approver="", reason="临时加车")
        self.assertEqual(ctx.exception.code, "APPROVER_REQUIRED")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.apply_change(plan_id=view["plan_id"], change_type="ADD_VEHICLE",
                                  payload={"vehicle": vehicle("京B99999")},
                                  approver="王主任", reason="")
        self.assertEqual(ctx.exception.code, "REASON_REQUIRED")

    def test_zone_upgrade_regenerates_steps_and_keeps_passed(self):
        view = self.make_plan(zones=[zone(level=1)], vehicles=[])
        pid = view["plan_id"]
        self.pass_step(pid, 1, "接待室")  # visitor_checkin 通过
        view = self.svc.apply_change(
            plan_id=pid, change_type="ADD_ZONE",
            payload={"zone": zone("Z9", 3, "核心机房")},
            approver="王主任", reason="检修范围扩大到核心区")
        keys = [s["key"] for s in view["steps"]]
        self.assertIn("security_screening", keys)
        self.assertIn("zone_access_approval", keys)
        self.assertEqual(view["version"], 1)
        # 时间线留痕：批准人与理由
        changes = [t for t in self.svc.get_timeline(pid) if t["type"] == "change"]
        self.assertEqual(changes[0]["approver"], "王主任")
        self.assertEqual(changes[0]["reason"], "检修范围扩大到核心区")

    def test_extend_validity_reactivates_expired_plan(self):
        view = self.make_plan(valid_until=iso(T0 + timedelta(hours=1)))
        pid = view["plan_id"]
        self.clock.advance(hours=2)
        self.assertEqual(self.svc.get_plan_view(pid)["status"], "EXPIRED")
        view = self.svc.apply_change(
            plan_id=pid, change_type="EXTEND_VALIDITY",
            payload={"valid_until": iso(T0 + timedelta(hours=6))},
            approver="王主任", reason="外协队伍延误，顺延有效期")
        self.assertEqual(view["status"], "ACTIVE")
        res = self.pass_step(pid, 1, "接待室")
        self.assertTrue(res["accepted"])

    def test_reset_failed_step_after_rectification(self):
        view = self.make_plan()
        pid = view["plan_id"]
        self.pass_step(pid, 1, "接待室")
        self.pass_step(pid, 2, "接待室")
        self.svc.submit_result(plan_id=pid, step_seq=3, event_id=self.eid(),
                               outcome="FAIL", post="安检岗", operator="op", note="车辆手续不全")
        self.assertEqual(self.svc.get_plan_view(pid)["status"], "BLOCKED")
        # 更换车辆并重置失败步骤
        view = self.svc.apply_change(
            plan_id=pid, change_type="REMOVE_VEHICLE",
            payload={"plate_no": "京A12345"}, approver="王主任",
            reason="原车辆手续不全，更换备案车辆", reset_failed=True)
        self.assertEqual(view["status"], "ACTIVE")
        # 无车辆后不再生成车辆安检步骤，当前步骤变为门卫放行
        self.assertEqual(view["current_step"]["key"], "gate_release")
        res = self.pass_step(pid, view["current_step"]["seq"], "门卫")
        self.assertEqual(res["plan_status"], "COMPLETED")

    def test_completed_plan_rejects_change(self):
        view = self.make_plan(zones=[zone(level=1)], vehicles=[])
        pid = view["plan_id"]
        self.pass_step(pid, 1, "接待室")
        self.pass_step(pid, 2, "门卫")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.apply_change(plan_id=pid, change_type="ADD_VEHICLE",
                                  payload={"vehicle": vehicle()},
                                  approver="王主任", reason="补录")
        self.assertEqual(ctx.exception.code, "PLAN_CLOSED")


class TestManagementQuery(ServiceTestBase):
    def test_view_and_timeline(self):
        view = self.make_plan()
        pid = view["plan_id"]
        self.pass_step(pid, 1, "接待室")
        self.svc.apply_change(plan_id=pid, change_type="ADD_VEHICLE",
                              payload={"vehicle": vehicle("京B66666")},
                              approver="王主任", reason="增加一辆工具车")
        view = self.svc.get_plan_view(pid)
        self.assertEqual(view["current_step"]["name"], "陪同人身份确认")
        self.assertEqual(view["responsible_post"], "接待室")
        tl = self.svc.get_timeline(pid)
        self.assertEqual([t["type"] for t in tl], ["event", "change"])
        self.assertEqual(tl[1]["approver"], "王主任")


class TestRestartPersistence(unittest.TestCase):
    def test_unfinished_plan_stays_controlled_after_restart(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "visit.db")
            svc1 = VisitService(db_path=db, now_fn=clock)
            view = svc1.create_plan(
                title="外协检修", visitors=[visitor()], escorts=[escort()],
                vehicles=[vehicle()], zones=[zone()],
                valid_from=iso(T0 - timedelta(hours=1)),
                valid_until=iso(T0 + timedelta(hours=8)))
            pid = view["plan_id"]
            svc1.submit_result(plan_id=pid, step_seq=1, event_id="evt-0001",
                               outcome="PASS", post="接待室", operator="op")
            svc1.close()

            # 模拟重启：新实例加载同一数据库
            svc2 = VisitService(db_path=db, now_fn=clock)
            view = svc2.get_plan_view(pid)
            self.assertEqual(view["status"], "ACTIVE")  # 未完成计划不自动放行
            self.assertEqual(view["steps"][0]["status"], "PASSED")
            self.assertEqual(view["current_step"]["key"], "escort_confirm")
            # 已处理事件在新实例上仍然幂等
            replay = svc2.submit_result(plan_id=pid, step_seq=1, event_id="evt-0001",
                                        outcome="PASS", post="接待室", operator="op")
            self.assertTrue(replay["idempotent_replay"])
            # 流程可继续
            res = svc2.submit_result(plan_id=pid, step_seq=2, event_id="evt-0002",
                                     outcome="PASS", post="接待室", operator="op")
            self.assertTrue(res["accepted"])
            svc2.close()

    def test_restart_sweeps_expired_plans(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "visit.db")
            svc1 = VisitService(db_path=db, now_fn=clock)
            view = svc1.create_plan(
                title="短期访问", visitors=[visitor()], escorts=[escort()],
                vehicles=[], zones=[zone(level=1)],
                valid_from=iso(T0 - timedelta(hours=1)),
                valid_until=iso(T0 + timedelta(hours=1)))
            pid = view["plan_id"]
            svc1.close()

            clock.advance(hours=2)  # 重启发生在过期之后
            svc2 = VisitService(db_path=db, now_fn=clock)
            self.assertEqual(svc2.get_plan_view(pid)["status"], "EXPIRED")
            res = svc2.submit_result(plan_id=pid, step_seq=1, event_id="evt-late",
                                     outcome="PASS", post="接待室", operator="op")
            self.assertFalse(res["accepted"])
            svc2.close()


if __name__ == "__main__":
    unittest.main()

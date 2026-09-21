"""预约联动服务测试：核验链、阻断、幂等、变更留痕、持久化。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src import (
    Escort,
    Vehicle,
    VisitLinkageService,
    VisitPlanError,
    Visitor,
    Zone,
    ZoneLevel,
    build_step_chain,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = T0):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> None:
        self.t += timedelta(**kwargs)


def make_visitor(vid="V1", name="张三", days_valid=30) -> Visitor:
    return Visitor(vid, name, f"ID-{vid}", T0 + timedelta(days=days_valid))


def make_plan_kwargs(**overrides):
    kwargs = {
        "title": "外协队伍提前抵达",
        "visitors": [make_visitor()],
        "escorts": [Escort("E1", "赵六", "保障部")],
        "vehicles": [Vehicle("鲁A·0001", "王五")],
        "zones": [Zone("Z1", "一号厂房", ZoneLevel.KEY)],
        "visit_start": T0,
        "valid_until": T0 + timedelta(hours=8),
    }
    kwargs.update(overrides)
    return kwargs


class StepChainTest(unittest.TestCase):
    """按区域等级生成有先后依赖的核验步骤。"""

    def test_general_zone_without_vehicle(self):
        codes = [s.step_code for s in build_step_chain(ZoneLevel.GENERAL, False)]
        self.assertEqual(codes, ["identity_check", "reception_confirm", "release"])

    def test_vehicle_inspection_inserted_when_vehicles(self):
        codes = [s.step_code for s in build_step_chain(ZoneLevel.GENERAL, True)]
        self.assertEqual(
            codes,
            ["identity_check", "vehicle_inspection", "reception_confirm", "release"],
        )

    def test_key_zone_adds_security_screening(self):
        codes = [s.step_code for s in build_step_chain(ZoneLevel.KEY, False)]
        self.assertIn("security_screening", codes)
        self.assertNotIn("escort_briefing", codes)

    def test_core_zone_adds_escort_briefing(self):
        codes = [s.step_code for s in build_step_chain(ZoneLevel.CORE, True)]
        self.assertEqual(
            codes,
            [
                "identity_check", "vehicle_inspection", "reception_confirm",
                "security_screening", "escort_briefing", "release",
            ],
        )

    def test_seq_is_strictly_ordered(self):
        steps = build_step_chain(ZoneLevel.CORE, True)
        self.assertEqual([s.seq for s in steps], list(range(1, len(steps) + 1)))


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.svc = VisitLinkageService(now_fn=self.clock)

    def tearDown(self):
        self.svc.close()

    def create_plan(self, **overrides) -> str:
        return self.svc.create_plan(**make_plan_kwargs(**overrides))

    def submit(self, plan_id, event_id, step_code, outcome="PASS", operator="门卫-01"):
        return self.svc.submit_step_result(
            plan_id=plan_id, event_id=event_id, step_code=step_code,
            outcome=outcome, operator=operator,
        )

    def pass_steps(self, plan_id, codes, start=1):
        for i, code in enumerate(codes, start=start):
            resp = self.submit(plan_id, f"EV-{i:03d}", code)
            self.assertTrue(resp["accepted"], resp)
        return resp


class FlowTest(ServiceTestBase):
    def test_happy_path_to_completed(self):
        pid = self.create_plan()
        resp = self.pass_steps(pid, [
            "identity_check", "vehicle_inspection", "reception_confirm",
            "security_screening", "release",
        ])
        self.assertEqual(resp["plan_status"], "COMPLETED")
        status = self.svc.get_plan_status(pid)
        self.assertEqual(status["status"], "COMPLETED")
        self.assertIsNone(status["current_step"])
        self.assertIsNone(status["responsible_post"])

    def test_out_of_order_submission_rejected(self):
        pid = self.create_plan()
        resp = self.submit(pid, "EV-1", "reception_confirm")
        self.assertFalse(resp["accepted"])
        self.assertIn("前序", resp["reject_reason"])
        # 状态未被污染，仍可从头走
        self.assertEqual(self.svc.get_plan_status(pid)["current_step"]["code"], "identity_check")

    def test_unknown_step_rejected(self):
        pid = self.create_plan()
        resp = self.submit(pid, "EV-1", "no_such_step")
        self.assertFalse(resp["accepted"])
        self.assertIn("不存在", resp["reject_reason"])

    def test_failed_step_blocks_following_steps(self):
        pid = self.create_plan()
        self.submit(pid, "EV-1", "identity_check")
        resp = self.submit(pid, "EV-2", "vehicle_inspection", outcome="FAIL")
        self.assertTrue(resp["accepted"])
        self.assertEqual(resp["plan_status"], "BLOCKED")
        # 后续任何提交都被阻断
        resp2 = self.submit(pid, "EV-3", "reception_confirm")
        self.assertFalse(resp2["accepted"])
        self.assertIn("阻断", resp2["reject_reason"])
        status = self.svc.get_plan_status(pid)
        self.assertEqual(status["status"], "BLOCKED")
        self.assertIn("车辆安全检查", status["blocking_reason"])
        self.assertEqual(status["current_step"]["code"], "vehicle_inspection")
        self.assertEqual(status["responsible_post"], "车辆检查岗")

    def test_expired_credential_blocks_release_even_if_terminal_passes(self):
        # 证件在到访中途过期：终端误报通过也被系统拦截
        visitor = Visitor("V1", "张三", "ID-V1", T0 + timedelta(hours=4))
        pid = self.create_plan(
            visitors=[visitor],
            zones=[Zone("Z1", "一般区", ZoneLevel.GENERAL)],
            vehicles=[],
        )
        self.submit(pid, "EV-1", "identity_check")
        self.submit(pid, "EV-2", "reception_confirm")
        self.clock.advance(hours=5)  # 证件已过期
        resp = self.submit(pid, "EV-3", "release", outcome="PASS")
        self.assertTrue(resp["accepted"])
        self.assertEqual(resp["plan_status"], "BLOCKED")
        status = self.svc.get_plan_status(pid)
        self.assertIn("证件已过期", status["blocking_reason"])

    def test_expired_credential_rejected_at_creation(self):
        with self.assertRaises(VisitPlanError):
            self.create_plan(visitors=[make_visitor(days_valid=-1)])

    def test_invalid_window_rejected(self):
        with self.assertRaises(VisitPlanError):
            self.create_plan(valid_until=T0 - timedelta(hours=1))


class IdempotencyTest(ServiceTestBase):
    def test_duplicate_event_replays_without_side_effect(self):
        pid = self.create_plan()
        first = self.submit(pid, "EV-1", "identity_check")
        self.assertTrue(first["accepted"])
        replay = self.submit(pid, "EV-1", "identity_check")
        self.assertTrue(replay["accepted"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["current_step"], first["current_step"])
        # 只生效一次：当前步骤是车辆检查而非更后面
        self.assertEqual(
            self.svc.get_plan_status(pid)["current_step"]["code"], "vehicle_inspection"
        )

    def test_rejected_event_replay_is_stable(self):
        pid = self.create_plan()
        first = self.submit(pid, "EV-9", "release")  # 越序，被拒
        self.assertFalse(first["accepted"])
        replay = self.submit(pid, "EV-9", "release")
        self.assertFalse(replay["accepted"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["reject_reason"], first["reject_reason"])

    def test_backfill_after_reconnect_uses_original_event_ids(self):
        """断线补传：终端重连后补传已处理过的事件，不重复生效。"""
        pid = self.create_plan()
        self.submit(pid, "EV-1", "identity_check")
        self.submit(pid, "EV-2", "vehicle_inspection")
        # 终端离线期间本地缓存了 EV-1、EV-2，重连后补传
        for eid, code in (("EV-1", "identity_check"), ("EV-2", "vehicle_inspection")):
            resp = self.submit(pid, eid, code)
            self.assertTrue(resp["replayed"])
        self.assertEqual(
            self.svc.get_plan_status(pid)["current_step"]["code"], "reception_confirm"
        )

    def test_event_id_cannot_be_reused_across_plans(self):
        pid1 = self.create_plan()
        pid2 = self.create_plan(title="第二个计划")
        self.submit(pid1, "EV-1", "identity_check")
        with self.assertRaises(VisitPlanError):
            self.submit(pid2, "EV-1", "identity_check")

    def test_stale_operation_rejected_after_plan_expires(self):
        pid = self.create_plan(valid_until=T0 + timedelta(hours=2))
        self.submit(pid, "EV-1", "identity_check")
        self.clock.advance(hours=3)  # 超出有效窗口
        resp = self.submit(pid, "EV-2", "vehicle_inspection")
        self.assertFalse(resp["accepted"])
        self.assertIn("过期", resp["reject_reason"])
        self.assertEqual(self.svc.get_plan_status(pid)["status"], "EXPIRED")
        # 过期计划的旧操作补传同样被拒且幂等
        again = self.submit(pid, "EV-2", "vehicle_inspection")
        self.assertFalse(again["accepted"])
        self.assertTrue(again["replayed"])

    def test_operation_rejected_after_completion(self):
        pid = self.create_plan(zones=[Zone("Z1", "一般区", ZoneLevel.GENERAL)], vehicles=[])
        self.pass_steps(pid, ["identity_check", "reception_confirm", "release"])
        resp = self.submit(pid, "EV-99", "identity_check")
        self.assertFalse(resp["accepted"])
        self.assertIn("完成", resp["reject_reason"])


class ChangeTest(ServiceTestBase):
    def test_change_requires_approver_and_reason(self):
        pid = self.create_plan()
        with self.assertRaises(VisitPlanError):
            self.svc.apply_change(plan_id=pid, approver="", reason="x")
        with self.assertRaises(VisitPlanError):
            self.svc.apply_change(plan_id=pid, approver="王主任", reason=" ")

    def test_zone_upgrade_rebuilds_chain_and_keeps_passed_steps(self):
        pid = self.create_plan(zones=[Zone("Z1", "一般区", ZoneLevel.GENERAL)], vehicles=[])
        self.submit(pid, "EV-1", "identity_check")
        result = self.svc.apply_change(
            plan_id=pid,
            approver="王主任",
            reason="临时追加核心区域作业",
            zones=[Zone("Z3", "核心机房", ZoneLevel.CORE)],
        )
        self.assertEqual(result["plan_status"], "IN_PROGRESS")
        status = self.svc.get_plan_status(pid)
        codes = [s["code"] for s in status["steps"]]
        self.assertEqual(
            codes,
            ["identity_check", "reception_confirm", "security_screening",
             "escort_briefing", "release"],
        )
        # 已通过的证件核验保留，无需重来
        self.assertEqual(status["steps"][0]["status"], "PASSED")
        self.assertEqual(status["current_step"]["code"], "reception_confirm")
        # 留痕：批准人、理由进入时间线
        changes = [t for t in status["timeline"] if t["type"] == "PLAN_CHANGED"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["actor"], "王主任")
        self.assertIn("核心区域", changes[0]["summary"])

    def test_blocked_plan_remediated_by_approved_change(self):
        pid = self.create_plan()
        self.submit(pid, "EV-1", "identity_check", outcome="FAIL")
        self.assertEqual(self.svc.get_plan_status(pid)["status"], "BLOCKED")
        self.svc.apply_change(
            plan_id=pid,
            approver="安全主管",
            reason="访客换证后重新核验",
            visitors=[make_visitor("V2", "李四")],
        )
        status = self.svc.get_plan_status(pid)
        self.assertEqual(status["status"], "IN_PROGRESS")
        self.assertIsNone(status["blocking_reason"])
        self.assertEqual(status["current_step"]["code"], "identity_check")
        # 整改后可继续走完流程
        self.pass_steps(pid, [
            "identity_check", "vehicle_inspection", "reception_confirm",
            "security_screening", "release",
        ], start=2)
        self.assertEqual(self.svc.get_plan_status(pid)["status"], "COMPLETED")

    def test_extend_valid_until_with_approval(self):
        pid = self.create_plan(valid_until=T0 + timedelta(hours=2))
        self.svc.apply_change(
            plan_id=pid, approver="王主任", reason="工期延长",
            valid_until=T0 + timedelta(hours=10),
        )
        self.clock.advance(hours=3)
        resp = self.submit(pid, "EV-1", "identity_check")
        self.assertTrue(resp["accepted"])

    def test_expired_plan_cannot_be_changed(self):
        pid = self.create_plan(valid_until=T0 + timedelta(hours=1))
        self.clock.advance(hours=2)
        with self.assertRaises(VisitPlanError):
            self.svc.apply_change(
                plan_id=pid, approver="王主任", reason="尝试复活过期计划",
                valid_until=T0 + timedelta(hours=5),
            )

    def test_cancel_plan_with_approval(self):
        pid = self.create_plan()
        self.svc.apply_change(
            plan_id=pid, approver="王主任", reason="任务取消", cancel=True,
        )
        status = self.svc.get_plan_status(pid)
        self.assertEqual(status["status"], "CANCELLED")
        resp = self.submit(pid, "EV-1", "identity_check")
        self.assertFalse(resp["accepted"])


class QueryTest(ServiceTestBase):
    def test_status_query_shows_step_post_reason_and_timeline(self):
        pid = self.create_plan()
        self.submit(pid, "EV-1", "identity_check")
        self.submit(pid, "EV-2", "vehicle_inspection", outcome="FAIL")
        status = self.svc.get_plan_status(pid)
        self.assertEqual(status["current_step"]["code"], "vehicle_inspection")
        self.assertEqual(status["responsible_post"], "车辆检查岗")
        self.assertIsNotNone(status["blocking_reason"])
        types = [t["type"] for t in status["timeline"]]
        self.assertEqual(types[0], "PLAN_CREATED")
        self.assertIn("STEP_RESULT", types)
        # 时间线按时间升序
        times = [t["time"] for t in status["timeline"]]
        self.assertEqual(times, sorted(times))

    def test_list_plans_defaults_to_open_only(self):
        pid1 = self.create_plan()
        pid2 = self.create_plan(
            title="已完成", zones=[Zone("Z2", "一般区", ZoneLevel.GENERAL)], vehicles=[],
        )
        self.pass_steps(pid2, ["identity_check", "reception_confirm", "release"])
        open_ids = [p["plan_id"] for p in self.svc.list_plans()]
        self.assertIn(pid1, open_ids)
        self.assertNotIn(pid2, open_ids)
        all_ids = [p["plan_id"] for p in self.svc.list_plans(include_closed=True)]
        self.assertIn(pid2, all_ids)


class PersistenceTest(unittest.TestCase):
    """重启后未完成计划继续受控。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_state_survives_restart(self):
        clock = FakeClock()
        svc = VisitLinkageService(db_path=self.db_path, now_fn=clock)
        pid = svc.create_plan(**make_plan_kwargs())
        svc.submit_step_result(
            plan_id=pid, event_id="EV-1", step_code="identity_check",
            outcome="PASS", operator="门卫-01",
        )
        svc.close()

        # 模拟重启：新实例加载同一数据库
        svc2 = VisitLinkageService(db_path=self.db_path, now_fn=clock)
        status = svc2.get_plan_status(pid)
        self.assertEqual(status["status"], "IN_PROGRESS")
        self.assertEqual(status["current_step"]["code"], "vehicle_inspection")
        # 重启前的事件仍幂等
        replay = svc2.submit_step_result(
            plan_id=pid, event_id="EV-1", step_code="identity_check",
            outcome="PASS", operator="门卫-01",
        )
        self.assertTrue(replay["replayed"])
        # 流程可继续直至放行
        for i, code in enumerate(
            ["vehicle_inspection", "reception_confirm", "security_screening", "release"],
            start=2,
        ):
            svc2.submit_step_result(
                plan_id=pid, event_id=f"EV-{i}", step_code=code,
                outcome="PASS", operator="门卫-01",
            )
        self.assertEqual(svc2.get_plan_status(pid)["status"], "COMPLETED")
        svc2.close()

    def test_blocked_plan_stays_blocked_after_restart(self):
        clock = FakeClock()
        svc = VisitLinkageService(db_path=self.db_path, now_fn=clock)
        pid = svc.create_plan(**make_plan_kwargs())
        svc.submit_step_result(
            plan_id=pid, event_id="EV-1", step_code="identity_check",
            outcome="FAIL", operator="门卫-01",
        )
        svc.close()

        svc2 = VisitLinkageService(db_path=self.db_path, now_fn=clock)
        status = svc2.get_plan_status(pid)
        self.assertEqual(status["status"], "BLOCKED")
        self.assertIsNotNone(status["blocking_reason"])
        resp = svc2.submit_step_result(
            plan_id=pid, event_id="EV-2", step_code="vehicle_inspection",
            outcome="PASS", operator="门卫-01",
        )
        self.assertFalse(resp["accepted"])
        svc2.close()

    def test_overdue_plan_expired_on_restart(self):
        clock = FakeClock()
        svc = VisitLinkageService(db_path=self.db_path, now_fn=clock)
        pid = svc.create_plan(**make_plan_kwargs(valid_until=T0 + timedelta(hours=1)))
        svc.close()

        clock.advance(hours=2)
        svc2 = VisitLinkageService(db_path=self.db_path, now_fn=clock)
        self.assertEqual(svc2.get_plan_status(pid)["status"], "EXPIRED")
        svc2.close()


if __name__ == "__main__":
    unittest.main()

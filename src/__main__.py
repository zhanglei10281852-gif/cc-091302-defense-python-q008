"""演示：外协队伍提前抵达场景下的预约联动流程。

运行：python -m src
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone

from . import Vehicle, VisitLinkageService, Visitor, Zone, ZoneLevel

CST = timezone(timedelta(hours=8))


def main() -> None:
    db = tempfile.NamedTemporaryFile(prefix="visit_linkage_", suffix=".db", delete=False)
    db.close()
    svc = VisitLinkageService(db_path=db.name)
    now = datetime.now(CST)

    plan_id = svc.create_plan(
        title="外协检修队伍提前抵达",
        visitors=[
            Visitor("V001", "张三", "ID-3301", now + timedelta(days=30)),
            Visitor("V002", "李四", "ID-3302", now + timedelta(days=30)),
        ],
        escorts=[],
        vehicles=[Vehicle("鲁A·D1234", "王五")],
        zones=[Zone("Z-02", "二号装配厂房", ZoneLevel.KEY)],
        visit_start=now,
        valid_until=now + timedelta(hours=8),
    )
    print(f"计划已建档：{plan_id}（重点区域，含车辆检查与安检）\n")

    # 门卫终端按依赖顺序提交；第一次重复提交演示幂等
    steps = ["identity_check", "vehicle_inspection", "reception_confirm", "security_screening"]
    for i, code in enumerate(steps, start=1):
        resp = svc.submit_step_result(
            plan_id=plan_id, event_id=f"EV-{i:03d}", step_code=code,
            outcome="PASS", operator="门卫-01",
        )
        print(f"{code:20s} -> 受理={resp['accepted']} 当前步骤={resp['current_step']}")
    dup = svc.submit_step_result(
        plan_id=plan_id, event_id="EV-001", step_code="identity_check",
        outcome="PASS", operator="门卫-01",
    )
    print(f"\n重复提交 EV-001：受理={dup['accepted']} 重放={dup['replayed']}（不产生二次效果）")

    resp = svc.submit_step_result(
        plan_id=plan_id, event_id="EV-005", step_code="release",
        outcome="PASS", operator="门卫-01",
    )
    print(f"\n放行结果：计划状态={resp['plan_status']}")

    status = svc.get_plan_status(plan_id)
    print("\n管理查询（当前步骤/责任岗位/阻塞原因）：")
    print(json.dumps(
        {k: status[k] for k in ("status", "current_step", "responsible_post", "blocking_reason")},
        ensure_ascii=False, indent=2,
    ))
    print("\n到访时间线：")
    for item in status["timeline"]:
        print(f"  {item['time']}  {item['type']:14s}  {item['summary']}")

    svc.close()
    print(f"\n数据库文件：{db.name}（重启服务后未完成计划继续受控）")


if __name__ == "__main__":
    main()

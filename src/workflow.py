"""按区域等级生成有先后依赖的核验步骤链。

步骤链为线性依赖：只有前序步骤全部通过，下一步才允许提交。
区域等级取计划内所有区域的最高等级，等级越高步骤越多。
"""
from __future__ import annotations

from .models import StepRecord, ZoneLevel

# (step_code, 步骤名称, 责任岗位, 所需最低区域等级, 是否仅当计划含车辆)
_STEP_TEMPLATE: tuple[tuple[str, str, str, ZoneLevel, bool], ...] = (
    ("identity_check", "访客证件核验", "门卫", ZoneLevel.GENERAL, False),
    ("vehicle_inspection", "车辆安全检查", "车辆检查岗", ZoneLevel.GENERAL, True),
    ("reception_confirm", "接待登记确认", "接待室", ZoneLevel.GENERAL, False),
    ("security_screening", "人员物品安检", "安检岗", ZoneLevel.KEY, False),
    ("escort_briefing", "保密交底与陪同确认", "安全主管", ZoneLevel.CORE, False),
    ("release", "门岗放行", "门卫", ZoneLevel.GENERAL, False),
)

# 证件有效性强制检查的步骤：即使终端误报通过，系统也会拦截过期证件
CREDENTIAL_GUARDED_STEPS = frozenset({"identity_check", "release"})


def build_step_chain(level: ZoneLevel, has_vehicles: bool) -> list[StepRecord]:
    """生成步骤链，seq 从 1 递增表示先后依赖。"""
    steps: list[StepRecord] = []
    for code, name, post, min_level, needs_vehicle in _STEP_TEMPLATE:
        if level < min_level:
            continue
        if needs_vehicle and not has_vehicles:
            continue
        steps.append(
            StepRecord(step_code=code, seq=len(steps) + 1, name=name, post=post)
        )
    return steps

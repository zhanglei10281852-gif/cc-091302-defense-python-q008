"""按区域等级生成有先后依赖的核验步骤。

等级取计划内所有访问区域的最高等级：
- 1 普通区：登记核验 → 门卫放行
- 2 受限区：证件核验 → 陪同人确认 →（车辆安检）→ 门卫放行
- 3 核心区：证件核验 → 安全背景核验 → 陪同人确认 →（车辆安检）→ 区域审批 → 门卫放行

步骤按 seq 顺序串行依赖，前一步未通过，后一步不可执行。
"""
from __future__ import annotations

from .models import Step, StepStatus

# (key, 名称, 责任岗位, 条件) —— 条件 "vehicle" 表示仅当计划含车辆时生成
_STEP_DEFS: dict[int, list[tuple[str, str, str, str]]] = {
    1: [
        ("visitor_checkin", "访客登记核验", "接待室", "always"),
        ("gate_release", "门卫放行", "门卫", "always"),
    ],
    2: [
        ("visitor_identity", "访客证件核验", "接待室", "always"),
        ("escort_confirm", "陪同人身份确认", "接待室", "always"),
        ("vehicle_inspection", "车辆安全检查", "安检岗", "vehicle"),
        ("gate_release", "门卫放行", "门卫", "always"),
    ],
    3: [
        ("visitor_identity", "访客证件核验", "接待室", "always"),
        ("security_screening", "安全背景核验", "安检岗", "always"),
        ("escort_confirm", "陪同人身份确认", "接待室", "always"),
        ("vehicle_inspection", "车辆安全检查", "安检岗", "vehicle"),
        ("zone_access_approval", "访问区域审批", "审批岗", "always"),
        ("gate_release", "门卫放行", "门卫", "always"),
    ],
}

VALID_LEVELS = tuple(sorted(_STEP_DEFS))

# 最终放行步骤：通过前必须复核访客证件有效期
RELEASE_STEP_KEY = "gate_release"

# 在这些步骤上通过前需要校验访客证件未过期
CREDENTIAL_CHECK_STEP_KEYS = {"visitor_checkin", "visitor_identity", RELEASE_STEP_KEY}


def build_steps(
    level: int,
    has_vehicle: bool,
    carried: dict[str, Step] | None = None,
) -> list[Step]:
    """按等级生成有序步骤链。

    carried：以步骤 key 索引的已通过步骤，用于计划变更后保留历史核验成果，
    避免已通过的岗位重复核验。
    """
    if level not in _STEP_DEFS:
        raise ValueError(f"不支持的区域等级: {level}")
    carried = carried or {}
    steps: list[Step] = []
    for seq, (key, name, post, cond) in enumerate(_STEP_DEFS[level], start=1):
        if cond == "vehicle" and not has_vehicle:
            continue
        old = carried.get(key)
        if old is not None and old.status == StepStatus.PASSED:
            steps.append(Step(seq=len(steps) + 1, key=key, name=name, post=post,
                              status=StepStatus.PASSED,
                              completed_by=old.completed_by,
                              completed_at=old.completed_at,
                              event_id=old.event_id))
        else:
            steps.append(Step(seq=len(steps) + 1, key=key, name=name, post=post))
    return steps

# 基地访客预约联动

该项目服务于国防安全业务，负责基地访客预约联动相关信息的规范化处理与留痕。

针对“外协队伍提前抵达，门卫、接待室、车辆检查各自维护名单”的问题，本服务把
**访客、陪同人、车辆、访问区域** 组成一次访问计划，统一管控核验与放行：

- **按区域等级生成核验步骤链**：一般/重点/核心区域步骤逐级增多（证件核验 →
  车辆检查 → 接待确认 → 安检 → 保密交底 → 放行），前后步骤严格依赖，禁止越序。
- **阻断控制**：任一步骤失败即阻断后续放行；证件核验与放行环节强制检查访客
  证件有效期，过期证件即使终端误报通过也会被系统拦截。
- **临时变更留痕**：变更访客、车辆、区域、有效窗口或取消计划，必须登记批准人
  和理由；区域/车辆变化会重建步骤链并保留已通过步骤，被阻断计划经批准变更后
  可整改重验。
- **幂等与防旧**：门卫终端按事件编号幂等处理，重复提交或断线补传不产生二次
  效果；已过期/已关闭计划的旧操作一律拒绝并留痕。
- **管理查询**：实时查看当前步骤、责任岗位、阻塞原因与到访时间线。
- **持久化**：全部状态落 SQLite，服务重启后未完成计划继续受控，超窗计划自动
  置为过期。

## 运行环境

Python 3.11，仅依赖标准库。代码位于 `src` 目录，配置与数据文件应按部署环境提供。

## 使用

```python
from datetime import datetime, timedelta, timezone
from src import VisitLinkageService, Visitor, Vehicle, Zone, ZoneLevel

svc = VisitLinkageService(db_path="visit_linkage.db")  # 默认 :memory:
now = datetime.now(timezone.utc)

plan_id = svc.create_plan(
    title="外协检修队伍提前抵达",
    visitors=[Visitor("V001", "张三", "ID-3301", now + timedelta(days=30))],
    vehicles=[Vehicle("鲁A·D1234", "王五")],
    zones=[Zone("Z-02", "二号装配厂房", ZoneLevel.KEY)],
    visit_start=now,
    valid_until=now + timedelta(hours=8),
)

# 门卫/岗位终端提交核验结果（event_id 全局唯一，幂等）
resp = svc.submit_step_result(
    plan_id=plan_id, event_id="EV-001", step_code="identity_check",
    outcome="PASS", operator="门卫-01",
)

# 临时变更（必须批准人 + 理由）
svc.apply_change(plan_id=plan_id, approver="王主任",
                 reason="临时追加核心区域作业",
                 zones=[Zone("Z-03", "核心机房", ZoneLevel.CORE)])

# 管理查询：当前步骤 / 责任岗位 / 阻塞原因 / 到访时间线
status = svc.get_plan_status(plan_id)
```

演示完整流程：`python -m src`

## 测试

```bash
python -m unittest discover -s tests -v
```

# 复合技能证据通行证

为院校、赛事与招聘机构交换能力证据定义稳定契约与业务服务，保留标准版本和授权边界。

## 目录

- `contracts/domain.schema.json`：事件信封、对象类型和事件载荷约定。
- `data/sample.json`：可直接校验的中文联调样例。
- `src/skill_evidence_passport/`
  - `contracts.py`：事件信封契约校验（稳定问题报告，不改写输入）。
  - `store.py`：仅追加 JSONL 事件日志（`flock` + `fsync`，状态全部可重放）。
  - `service.py`：幂等/冻结、证据计权、签发唯一、共享到期撤权、申诉、录用快照与改版差异、企业/学生双视图。
- `tests/`：契约边界测试与业务规则测试（22 项）。
- `docs/domain.md`：领域对象、事件语义与业务规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m skill_evidence_passport.cli contracts/domain.schema.json data/sample.json
```

命令成功时输出 `valid`；校验失败时逐行输出字段、代码和中文说明，并以非零状态结束。

## 快速上手

```python
from datetime import datetime, timedelta, timezone
from skill_evidence_passport import PassportService

svc = PassportService("./data-store")

svc.publish_standard("evt-std-1", "wsi-mech", 1, [
    {"unit_id": "unit-safety", "spec_hash": "safe-v1"},
    {"unit_id": "unit-troubleshoot", "spec_hash": "tb-v1"},
])
svc.register_job_requirement(
    "evt-job-1", "job-maint", "employer-haite", "wsi-mech",
    ["unit-safety", "unit-troubleshoot"],
)
# 一次表现支撑两个能力单元（裁判授权）
svc.accept_evidence(
    "evt-ev-1", "ev-1", "wsi-mech", 1, "stu-7", "task-42",
    "coach-li", ["unit-safety", "unit-troubleshoot"],
    {"role": "judge", "authorizer_id": "ref-zhang"},
)
svc.issue_passport("evt-pp-1", "stu-7", "wsi-mech")
svc.grant_sharing(
    "evt-share-1", "passport:stu-7:wsi-mech", "employer-haite", "job-maint",
    (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
)

svc.employer_view("passport:stu-7:wsi-mech", "employer-haite", "job-maint")
# 只含岗位单元的 status / evidence_count，不含任务与裁判信息

svc.student_trace("stu-7", "wsi-mech")
# 每项能力来自哪次任务、谁确认、历次改版后为何仍有效
```

服务重启后构造同一个目录的 `PassportService` 即可重放恢复；调用 `pump()` 会继续补发已到期共享的 `ACCESS_EXPIRED` 事件。

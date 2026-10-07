# 领域约定

为院校、赛事与招聘机构交换能力证据定义稳定契约，保留标准版本和授权边界。

聚合对象包括`skill_standard`、`evidence_item`、`competency_passport`、`job_requirement`。事件类型包括`STANDARD_PUBLISHED`、`EVIDENCE_ACCEPTED`、`PASSPORT_ISSUED`、`ACCESS_REVOKED`、`APPEAL_DECIDED`、`JOB_REQUIREMENT_PUBLISHED`。所有时间都必须携带时区，版本号从 1 开始递增，校验层不会替调用方改写输入。

## 事件载荷

- `EVIDENCE_ACCEPTED`：还需包含 `standard_version`, `observer_id`。
- `PASSPORT_ISSUED`：还需包含 `recipient_scope`, `expires_at`。
- `APPEAL_DECIDED`：还需包含 `affected_units`, `decision`。
- `JOB_REQUIREMENT_PUBLISHED`：还需包含 `employer_id`, `required_units`。

同一事件标识的幂等与冲突处理属于上层业务服务职责；交换层只负责稳定报告结构、枚举、时间、版本和必需载荷问题。

## 业务服务语义

`src/skill_evidence_passport/service.py` 在交换契约之上提供：

- 幂等接收：完全相同的事件重传返回原回执；同一事件号提交不同内容会冻结并登记核对，状态不被覆盖。
- 证据计权：一次表现可支撑多个能力单元，同一证据在同一单元内只计权一次。
- 标准改版：只重算受影响能力单元；已用于录用决定的快照保持原样并追加差异说明。
- 并发签发：同一学生在同一标准版本下只存在一份有效通行证，到期后才可重新签发。
- 共享许可：按企业签发有限期许可；学生撤回后企业立即不可见，依法保留的考核事实不删除。
- 申诉处理：仅调整受影响单元，结果通过持久任务传播到快照差异说明。
- 重启恢复：到期、申诉与撤权传播任务持久化，`recover` 在服务重启后继续处理。
- 视图边界：企业只能看见岗位所需结论；学生可追溯每项能力来自哪次任务、谁确认以及为何仍然有效。

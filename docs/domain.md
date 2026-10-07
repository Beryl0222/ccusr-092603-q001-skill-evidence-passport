# 领域约定

为院校、赛事与招聘机构交换能力证据定义稳定契约，保留标准版本和授权边界。

聚合对象包括 `skill_standard`、`evidence_item`、`competency_passport`、`job_requirement`。所有时间都必须携带时区，版本号从 1 开始递增，校验层不会替调用方改写输入。

## 事件类型

| 事件 | 含义 |
| --- | --- |
| `STANDARD_PUBLISHED` | 发布某标准的新版本，载荷含全部能力单元及规格指纹 |
| `JOB_REQUIREMENT_REGISTERED` | 企业登记岗位要求（标准 + 所需单元） |
| `EVIDENCE_ACCEPTED` | 一次训练/考核表现被受理为证据，须携带标准版本与授权 |
| `PASSPORT_ISSUED` | 向学生签发某标准下唯一的有效通行证 |
| `SHARING_GRANTED` | 学生向某企业授予有限期共享许可 |
| `ACCESS_REVOKED` / `ACCESS_EXPIRED` | 学生撤权 / 许可到期传播 |
| `APPEAL_FILED` / `APPEAL_DECIDED` | 成绩申诉及裁决 |
| `SNAPSHOT_TAKEN` / `SNAPSHOT_DIFF_APPENDED` | 录用决定快照及其差异追加 |
| `RECORD_FROZEN` | 同号异内容冻结，等待人工核对 |

## 事件载荷

- `EVIDENCE_ACCEPTED`：还需包含 `standard_version`、`observer_id`、`supports_units`、`authorization`。
- `PASSPORT_ISSUED`：还需包含 `recipient_scope`、`expires_at`。
- `APPEAL_DECIDED`：还需包含 `affected_units`、`decision`（`upheld` / `rejected`）。

完整必填项以 `contracts/domain.schema.json` 的 `payload_required_by_event` 为准。

交换层（`contracts.py`）只负责稳定报告结构、枚举、时间、版本和必需载荷问题；幂等、冻结、计权、签发唯一、快照差异等属于 `service.py` 业务服务职责。

## 业务规则（service.py）

1. **一次表现支撑多个能力，证据不重复计权**：一条证据可列入多个 `supports_units`；但同一学生在同一标准版本下的同一 `task_id` 只能受理一次，换 `evidence_id` 重报会被拒绝（`evidence_already_counted`）。
2. **授权前置**：证据必须携带 `authorization.role` 为 `judge` 或 `teacher` 且有 `authorizer_id`，否则不予受理。
3. **标准版本与改版重算**：标准版本必须连续递增；事件记录发布时算出的 `changed_units`（按单元 `spec_hash` 比对）。改版后通行证版本指针前移，结论计算只把"产生于受影响改版之前、且单元被调整"的证据判为失效；未调整单元的旧证据继续有效。
4. **录用快照不可变，差异只追加**：快照保存录用时刻的岗位结论与 `basis_version`。改版或申诉成立只重算受影响单元，状态翻转时追加 `SNAPSHOT_DIFF_APPENDED`，原结论永不改写；连续多次变更与上一次差异声明的状态比较。
5. **申诉边界**：申诉成立（`upheld`）必须给出 `resolution`（受影响单元 → 更正状态），且只能覆盖申诉涉及的单元；裁决与差异事件同批原子落盘。更正锚定裁决时的标准版本，该单元以后若再被改版调整，更正自动失效、回到证据结论（快照中的历史差异仍保留）。
6. **共享许可**：许可限定到具体企业（岗位所属方）、具体岗位和 `expires_at`；到期由 `pump()` 补发 `ACCESS_EXPIRED`，事件标识确定，重复执行无副作用。
7. **撤权不删事实**：撤权或到期只改变共享状态，证据、通行证结论、快照与共享台账作为依法保留的考核事实继续存在；企业接口随之拒绝访问。
8. **企业视图最小披露**：`employer_view` 只返回岗位要求单元的 `status` 与证据条数，不含任务、教练、裁判、授权等细节；许可无效时统一拒绝，不区分原因。
9. **学生可追溯**：`student_trace` 给出每个单元的当前状态、全部证据来源（任务、确认人、授权、受理时间）以及旧证据在历次改版后仍有效的原因，并附申诉、共享与快照台账。
10. **每标准一份通行证**：签发在进程锁内串行，同一学生 + 标准重复签发返回 `passport_exists`；并发下恰有一份成功。

## 幂等与冻结

业务指纹只包含调用方提交的 `(event_type, aggregate_type, aggregate_id, payload 中的调用方字段)`，服务端生成的时间、序号、派生字段（`changed_units`、签发时版本快照、快照结论等）不参与。

- 同 `event_id`、同指纹：不重复落盘，原样返回首次回执（含首次 `recorded_at`）。
- 同 `event_id`、异指纹：追加 `RECORD_FROZEN` 事件并抛 `FrozenConflictError`，此后该号任何内容都先拦截，等待人工核对。
- 幂等判定在进程锁内、且先于一切"当前状态"校验执行，因此标准改版、通行证已签发等状态前移都不会让合法补传被误判。

## 存储与重启

全部状态派生自仅追加的 `events.jsonl`（写入带 `flock` 与 `fsync`）。新进程构造服务时重放日志即可恢复幂等索引、冻结记录、通行证、共享台账、申诉与快照；`pump()` 在重启后继续补发到期事件。

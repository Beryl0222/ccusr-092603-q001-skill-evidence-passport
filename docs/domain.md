# 领域约定

为院校、赛事与招聘机构交换能力证据定义稳定契约，保留标准版本和授权边界。

聚合对象包括`skill_standard`、`evidence_item`、`competency_passport`、`job_requirement`。事件类型包括`STANDARD_PUBLISHED`、`EVIDENCE_ACCEPTED`、`PASSPORT_ISSUED`、`ACCESS_REVOKED`、`APPEAL_DECIDED`。所有时间都必须携带时区，版本号从 1 开始递增，校验层不会替调用方改写输入。

## 事件载荷

- `EVIDENCE_ACCEPTED`：还需包含 `standard_version`, `observer_id`。
- `PASSPORT_ISSUED`：还需包含 `recipient_scope`, `expires_at`。
- `APPEAL_DECIDED`：还需包含 `affected_units`, `decision`。

同一事件标识的幂等与冲突处理属于上层业务服务职责；交换层只负责稳定报告结构、枚举、时间、版本和必需载荷问题。

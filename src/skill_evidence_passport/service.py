"""复合技能证据通行证业务服务。

在交换契约之上提供幂等接收、证据计权、通行证签发、标准改版重算、
申诉处理、共享许可与到期/撤权传播。状态保存在 SQLite 中，
服务重启后可通过 recover 继续处理到期、申诉与撤权传播任务。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import validate_event

DEFAULT_PASS_SCORE = 0.6

# 与 contracts/domain.schema.json 保持一致；仅当仓库契约文件不可用时回退。
_FALLBACK_SCHEMA: dict[str, Any] = {
    "required": [
        "event_id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "occurred_at",
        "version",
        "payload",
    ],
    "properties": {
        "event_type": {
            "enum": [
                "STANDARD_PUBLISHED",
                "EVIDENCE_ACCEPTED",
                "PASSPORT_ISSUED",
                "ACCESS_REVOKED",
                "APPEAL_DECIDED",
                "JOB_REQUIREMENT_PUBLISHED",
            ]
        },
        "aggregate_type": {
            "enum": ["skill_standard", "evidence_item", "competency_passport", "job_requirement"]
        },
    },
    "payload_required_by_event": {
        "EVIDENCE_ACCEPTED": ["standard_version", "observer_id"],
        "PASSPORT_ISSUED": ["recipient_scope", "expires_at"],
        "APPEAL_DECIDED": ["affected_units", "decision"],
        "JOB_REQUIREMENT_PUBLISHED": ["employer_id", "required_units"],
    },
}

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS receipts (
    event_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    receipt_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conflicts (
    event_id TEXT NOT NULL,
    incoming_hash TEXT NOT NULL,
    incoming_json TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (event_id, incoming_hash)
);
CREATE TABLE IF NOT EXISTS standards (
    standard_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    units_json TEXT NOT NULL,
    observers_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (standard_id, version)
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    standard_id TEXT NOT NULL,
    standard_version INTEGER NOT NULL,
    observer_id TEXT NOT NULL,
    unit_ids_json TEXT NOT NULL,
    score REAL NOT NULL,
    weight REAL NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passports (
    passport_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    standard_id TEXT NOT NULL,
    standard_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_valid_passport_per_version
    ON passports (student_id, standard_id, standard_version) WHERE status = 'valid';
CREATE TABLE IF NOT EXISTS passport_units (
    passport_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    status TEXT NOT NULL,
    score REAL,
    computed_under_version INTEGER NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (passport_id, unit_id)
);
CREATE TABLE IF NOT EXISTS unit_recomputations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    passport_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT,
    old_score REAL,
    new_score REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permits (
    permit_id TEXT PRIMARY KEY,
    passport_id TEXT NOT NULL,
    employer_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE (passport_id, employer_id)
);
CREATE TABLE IF NOT EXISTS requirements (
    requirement_id TEXT PRIMARY KEY,
    employer_id TEXT NOT NULL,
    standard_id TEXT,
    required_units_json TEXT NOT NULL,
    title TEXT,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    passport_id TEXT NOT NULL,
    employer_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS appeals (
    appeal_id TEXT PRIMARY KEY,
    passport_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    affected_units_json TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    run_after TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS propagation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# 事件类型与聚合对象的对应关系，用于接收时的交叉校验。
_EXPECTED_AGGREGATE = {
    "STANDARD_PUBLISHED": "skill_standard",
    "EVIDENCE_ACCEPTED": "evidence_item",
    "PASSPORT_ISSUED": "competency_passport",
    "ACCESS_REVOKED": "competency_passport",
    "APPEAL_DECIDED": "competency_passport",
    "JOB_REQUIREMENT_PUBLISHED": "job_requirement",
}


class ServiceError(Exception):
    """业务服务错误，code 供调用方程序化处理。"""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class AccessDeniedError(ServiceError):
    """企业访问被许可边界拒绝。"""

    def __init__(self, detail: str):
        super().__init__("access_denied", detail)


class _Rejection(Exception):
    """事件内容不合法，接收方拒绝且不应用。"""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _Conflict(Exception):
    """事件与已生效状态冲突，接收方登记冲突且不应用。"""

    def __init__(self, code: str, detail: str, extra: dict[str, Any] | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra or {}


def _load_default_schema() -> dict[str, Any]:
    candidate = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except OSError:
        return _FALLBACK_SCHEMA


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须携带时区")
    return parsed


def _compute_unit(pass_score: float, unit_id: str, evidence_rows: list[sqlite3.Row]) -> tuple[str, float | None, list[str]]:
    """按证据标识去重后加权计分，同一证据在同一单元内只计权一次。"""
    seen: set[str] = set()
    weighted = 0.0
    weight_sum = 0.0
    for row in evidence_rows:
        if row["evidence_id"] in seen or unit_id not in json.loads(row["unit_ids_json"]):
            continue
        seen.add(row["evidence_id"])
        weighted += row["score"] * row["weight"]
        weight_sum += row["weight"]
    if not seen:
        return "no_evidence", None, []
    score = weighted / weight_sum
    return ("met" if score >= pass_score else "not_met"), score, sorted(seen)


def _fmt_score(score: float | None) -> str:
    return "无" if score is None else f"{score:.2f}"


class PassportService:
    """复合技能证据通行证业务服务。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        schema: Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA_DDL)
        self._lock = threading.RLock()
        self._schema = dict(schema) if schema is not None else _load_default_schema()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PassportService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 接收与幂等
    # ------------------------------------------------------------------

    def ingest(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """接收一个领域事件并返回回执。

        完全相同的事件重传返回原回执；同一事件号提交不同内容会冻结核对。
        """
        if not isinstance(event, Mapping):
            raise ServiceError("not_an_object", "事件必须是 JSON 对象")
        try:
            content_hash = self._content_hash(event)
        except (TypeError, ValueError):
            raise ServiceError("not_json", "事件必须可以序列化为 JSON")
        event_id = event.get("event_id")
        with self._lock:
            if isinstance(event_id, str) and event_id.strip():
                existing = self._conn.execute(
                    "SELECT content_hash, receipt_json FROM receipts WHERE event_id = ?", (event_id,)
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] == content_hash:
                        return json.loads(existing["receipt_json"])
                    return self._freeze(event_id, content_hash, event)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                receipt = self._accept_or_reject(event)
                if isinstance(event_id, str) and event_id.strip():
                    self._conn.execute(
                        "INSERT INTO receipts (event_id, content_hash, receipt_json) VALUES (?, ?, ?)",
                        (event_id, content_hash, json.dumps(receipt, ensure_ascii=False)),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return receipt

    def list_conflicts(self) -> list[dict[str, Any]]:
        """列出因同号异内容被冻结、等待核对的记录。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, incoming_hash, incoming_json, created_at, status FROM conflicts ORDER BY created_at"
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "incoming_hash": row["incoming_hash"],
                "incoming_event": json.loads(row["incoming_json"]),
                "created_at": row["created_at"],
                "status": row["status"],
            }
            for row in rows
        ]

    @staticmethod
    def _content_hash(event: Mapping[str, Any]) -> str:
        canonical = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _freeze(self, event_id: str, content_hash: str, event: Mapping[str, Any]) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT receipt_json FROM conflicts WHERE event_id = ? AND incoming_hash = ?",
            (event_id, content_hash),
        ).fetchone()
        if row is not None:
            return json.loads(row["receipt_json"])
        receipt = self._receipt(event_id, "frozen", "同一事件号提交了不同内容，记录已冻结等待核对")
        self._conn.execute(
            "INSERT INTO conflicts (event_id, incoming_hash, incoming_json, receipt_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                event_id,
                content_hash,
                json.dumps(event, ensure_ascii=False),
                json.dumps(receipt, ensure_ascii=False),
                self._now_iso(),
            ),
        )
        return receipt

    def _accept_or_reject(self, event: Mapping[str, Any]) -> dict[str, Any]:
        event_id = event.get("event_id")
        issues = validate_event(event, self._schema)
        if issues:
            return self._receipt(
                event_id,
                "rejected",
                "事件未通过契约校验",
                issues=[{"field": i.field, "code": i.code, "message": i.message} for i in issues],
            )
        expected = _EXPECTED_AGGREGATE.get(event["event_type"])
        if expected is not None and event["aggregate_type"] != expected:
            return self._receipt(event_id, "rejected", "事件类型与聚合对象不匹配", code="aggregate_mismatch")
        try:
            extra = self._apply(event)
        except _Rejection as rejection:
            return self._receipt(event_id, "rejected", rejection.detail, code=rejection.code)
        except _Conflict as conflict:
            return self._receipt(event_id, "conflict", conflict.detail, code=conflict.code, **conflict.extra)
        return self._receipt(event_id, "accepted", "事件已接收并应用", **extra)

    def _receipt(self, event_id: Any, status: str, detail: str, **extra: Any) -> dict[str, Any]:
        receipt = {
            "receipt_id": uuid.uuid4().hex,
            "event_id": event_id,
            "status": status,
            "detail": detail,
            "received_at": self._now_iso(),
        }
        receipt.update(extra)
        return receipt

    def _apply(self, event: Mapping[str, Any]) -> dict[str, Any]:
        handlers = {
            "STANDARD_PUBLISHED": self._apply_standard_published,
            "EVIDENCE_ACCEPTED": self._apply_evidence_accepted,
            "PASSPORT_ISSUED": self._apply_passport_issued,
            "ACCESS_REVOKED": self._apply_access_revoked,
            "APPEAL_DECIDED": self._apply_appeal_decided,
            "JOB_REQUIREMENT_PUBLISHED": self._apply_job_requirement_published,
        }
        return handlers[event["event_type"]](event)

    # ------------------------------------------------------------------
    # 各类事件的应用逻辑
    # ------------------------------------------------------------------

    def _apply_standard_published(self, event: Mapping[str, Any]) -> dict[str, Any]:
        standard_id = event["aggregate_id"]
        payload = event["payload"]
        version = payload.get("standard_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise _Rejection("invalid_field", "载荷字段 standard_version 必须是正整数")
        units = self._parse_units(payload.get("units"))
        observers = self._parse_observers(payload.get("observers"))
        if self._standard(standard_id, version) is not None:
            raise _Conflict("standard_version_exists", f"赛项标准 {standard_id} 版本 {version} 已发布")
        previous = self._conn.execute(
            "SELECT version, units_json FROM standards WHERE standard_id = ? AND version < ?"
            " ORDER BY version DESC LIMIT 1",
            (standard_id, version),
        ).fetchone()
        self._conn.execute(
            "INSERT INTO standards (standard_id, version, units_json, observers_json, published_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                standard_id,
                version,
                json.dumps(units, ensure_ascii=False),
                json.dumps(observers, ensure_ascii=False),
                event["occurred_at"],
            ),
        )
        if previous is not None:
            self._recompute_after_revision(
                standard_id,
                previous_version=previous["version"],
                new_version=version,
                previous_units=json.loads(previous["units_json"]),
                new_units=units,
            )
        return {"standard_id": standard_id, "standard_version": version}

    def _apply_evidence_accepted(self, event: Mapping[str, Any]) -> dict[str, Any]:
        evidence_id = event["aggregate_id"]
        payload = event["payload"]
        student_id = self._require_str(payload, "student_id")
        task_id = self._require_str(payload, "task_id")
        standard_id = self._require_str(payload, "standard_id")
        standard_version = payload.get("standard_version")
        if isinstance(standard_version, bool) or not isinstance(standard_version, int) or standard_version < 1:
            raise _Rejection("invalid_field", "载荷字段 standard_version 必须是正整数")
        observer_id = self._require_str(payload, "observer_id")
        unit_ids = self._require_str_list(payload, "unit_ids")
        score = payload.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
            raise _Rejection("invalid_field", "载荷字段 score 必须是 0 到 1 之间的数值")
        weight = payload.get("weight", 1.0)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
            raise _Rejection("invalid_field", "载荷字段 weight 必须是正数")
        standard = self._standard(standard_id, standard_version)
        if standard is None:
            raise _Rejection("unknown_standard", f"未找到赛项标准 {standard_id} 版本 {standard_version}")
        units = json.loads(standard["units_json"])
        observers = json.loads(standard["observers_json"])
        if observer_id not in observers:
            raise _Rejection("observer_not_authorized", f"观察人 {observer_id} 未获得该标准版本的裁判或教师授权")
        unknown = [unit_id for unit_id in unit_ids if unit_id not in units]
        if unknown:
            raise _Rejection("unknown_unit", f"能力单元未在标准版本中登记: {'、'.join(unknown)}")
        if self._conn.execute("SELECT 1 FROM evidence WHERE evidence_id = ?", (evidence_id,)).fetchone():
            raise _Conflict("evidence_exists", f"证据 {evidence_id} 已存在，内容不一致需核对")
        self._conn.execute(
            "INSERT INTO evidence (evidence_id, student_id, task_id, standard_id, standard_version,"
            " observer_id, unit_ids_json, score, weight, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evidence_id,
                student_id,
                task_id,
                standard_id,
                standard_version,
                observer_id,
                json.dumps(sorted(set(unit_ids)), ensure_ascii=False),
                float(score),
                float(weight),
                event["occurred_at"],
            ),
        )
        return {"evidence_id": evidence_id}

    def _apply_passport_issued(self, event: Mapping[str, Any]) -> dict[str, Any]:
        passport_id = event["aggregate_id"]
        payload = event["payload"]
        student_id = self._require_str(payload, "student_id")
        standard_id = self._require_str(payload, "standard_id")
        standard_version = payload.get("standard_version")
        if isinstance(standard_version, bool) or not isinstance(standard_version, int) or standard_version < 1:
            raise _Rejection("invalid_field", "载荷字段 standard_version 必须是正整数")
        recipients = self._require_str_list(payload, "recipient_scope")
        expires_at = self._require_time(payload, "expires_at")
        now = self._now()
        if expires_at <= now:
            raise _Rejection("invalid_expiry", "通行证有效期必须晚于当前时间")
        standard = self._standard(standard_id, standard_version)
        if standard is None:
            raise _Rejection("unknown_standard", f"未找到赛项标准 {standard_id} 版本 {standard_version}")
        # 已到期的旧通行证不再占用有效名额。
        for row in self._conn.execute(
            "SELECT passport_id, expires_at FROM passports"
            " WHERE student_id = ? AND standard_id = ? AND standard_version = ? AND status = 'valid'",
            (student_id, standard_id, standard_version),
        ).fetchall():
            if _parse_time(row["expires_at"]) <= now:
                self._conn.execute(
                    "UPDATE passports SET status = 'expired' WHERE passport_id = ?", (row["passport_id"],)
                )
        existing = self._conn.execute(
            "SELECT passport_id FROM passports"
            " WHERE student_id = ? AND standard_id = ? AND standard_version = ? AND status = 'valid'",
            (student_id, standard_id, standard_version),
        ).fetchone()
        if existing is not None:
            raise _Conflict(
                "passport_exists",
                "该学生在此标准版本下已存在有效通行证",
                {"passport_id": existing["passport_id"]},
            )
        units = json.loads(standard["units_json"])
        pool = self._evidence_pool(student_id, standard_id, standard_version)
        try:
            self._conn.execute(
                "INSERT INTO passports (passport_id, student_id, standard_id, standard_version,"
                " status, issued_at, expires_at) VALUES (?, ?, ?, ?, 'valid', ?, ?)",
                (
                    passport_id,
                    student_id,
                    standard_id,
                    standard_version,
                    event["occurred_at"],
                    expires_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            # 并发签发兜底：唯一索引保证每个标准版本只有一份有效通行证。
            raise _Conflict("passport_exists", "该学生在此标准版本下已存在有效通行证")
        now_iso = self._now_iso()
        for unit_id in sorted(units):
            status, score, evidence_ids = _compute_unit(units[unit_id]["pass_score"], unit_id, pool)
            self._conn.execute(
                "INSERT INTO passport_units (passport_id, unit_id, status, score,"
                " computed_under_version, evidence_ids_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    passport_id,
                    unit_id,
                    status,
                    score,
                    standard_version,
                    json.dumps(evidence_ids, ensure_ascii=False),
                    now_iso,
                ),
            )
        for employer_id in sorted(set(recipients)):
            permit_id = f"{passport_id}:{employer_id}"
            self._conn.execute(
                "INSERT INTO permits (permit_id, passport_id, employer_id, student_id, status, expires_at)"
                " VALUES (?, ?, ?, ?, 'valid', ?)",
                (permit_id, passport_id, employer_id, student_id, expires_at.isoformat()),
            )
            self._enqueue(
                f"permit-expiry:{permit_id}",
                "permit_expiry",
                {"permit_id": permit_id, "passport_id": passport_id},
                run_after=expires_at.isoformat(),
            )
        return {"passport_id": passport_id}

    def _apply_access_revoked(self, event: Mapping[str, Any]) -> dict[str, Any]:
        passport_id = event["aggregate_id"]
        payload = event["payload"]
        employer_id = self._require_str(payload, "employer_id")
        student_id = payload.get("student_id")
        permit = self._conn.execute(
            "SELECT * FROM permits WHERE passport_id = ? AND employer_id = ?",
            (passport_id, employer_id),
        ).fetchone()
        if permit is None:
            raise _Rejection("permit_not_found", "未找到对应的共享许可")
        if student_id is not None and student_id != permit["student_id"]:
            raise _Rejection("student_mismatch", "撤回方与许可记录的学生不一致")
        if permit["status"] == "valid":
            # 只关闭访问通道，依法保留的考核事实（证据、成绩、快照）不删除。
            self._conn.execute(
                "UPDATE permits SET status = 'revoked', revoked_at = ? WHERE permit_id = ?",
                (self._now_iso(), permit["permit_id"]),
            )
        self._enqueue(
            f"revocation:{event['event_id']}",
            "revocation_propagation",
            {
                "permit_id": permit["permit_id"],
                "passport_id": passport_id,
                "employer_id": employer_id,
            },
            run_after=self._now_iso(),
        )
        return {"permit_id": permit["permit_id"]}

    def _apply_appeal_decided(self, event: Mapping[str, Any]) -> dict[str, Any]:
        passport_id = event["aggregate_id"]
        payload = event["payload"]
        affected_units = self._require_str_list(payload, "affected_units")
        decision = payload.get("decision")
        if decision not in ("upheld", "rejected"):
            raise _Rejection("invalid_field", "载荷字段 decision 必须是 upheld 或 rejected")
        passport = self._conn.execute(
            "SELECT * FROM passports WHERE passport_id = ?", (passport_id,)
        ).fetchone()
        if passport is None:
            raise _Rejection("unknown_passport", f"未找到通行证 {passport_id}")
        changes: list[dict[str, Any]] = []
        if decision == "upheld":
            adjusted = payload.get("adjusted_scores", {})
            if not isinstance(adjusted, Mapping):
                raise _Rejection("invalid_field", "载荷字段 adjusted_scores 必须是对象")
            for unit_id in affected_units:
                row = self._conn.execute(
                    "SELECT * FROM passport_units WHERE passport_id = ? AND unit_id = ?",
                    (passport_id, unit_id),
                ).fetchone()
                if row is None:
                    raise _Rejection("unknown_unit", f"通行证不包含能力单元 {unit_id}")
                if unit_id not in adjusted:
                    continue
                new_score = adjusted[unit_id]
                if isinstance(new_score, bool) or not isinstance(new_score, (int, float)) or not 0 <= new_score <= 1:
                    raise _Rejection("invalid_field", f"调整后的成绩必须在 0 到 1 之间: {unit_id}")
                standard = self._standard(passport["standard_id"], row["computed_under_version"])
                units = json.loads(standard["units_json"]) if standard is not None else {}
                if unit_id in units:
                    new_status = "met" if new_score >= units[unit_id]["pass_score"] else "not_met"
                else:
                    new_status = row["status"]
                self._conn.execute(
                    "UPDATE passport_units SET status = ?, score = ?, updated_at = ?"
                    " WHERE passport_id = ? AND unit_id = ?",
                    (new_status, float(new_score), self._now_iso(), passport_id, unit_id),
                )
                self._record_recomputation(
                    passport_id, unit_id, "appeal", row["status"], new_status, row["score"], float(new_score)
                )
                changes.append(
                    {
                        "unit_id": unit_id,
                        "old_status": row["status"],
                        "new_status": new_status,
                        "old_score": row["score"],
                        "new_score": float(new_score),
                    }
                )
        self._conn.execute(
            "INSERT INTO appeals (appeal_id, passport_id, decision, affected_units_json, decided_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                event["event_id"],
                passport_id,
                decision,
                json.dumps(affected_units, ensure_ascii=False),
                self._now_iso(),
            ),
        )
        self._enqueue(
            f"appeal:{event['event_id']}",
            "appeal_propagation",
            {
                "passport_id": passport_id,
                "decision": decision,
                "affected_units": affected_units,
                "changes": changes,
            },
            run_after=self._now_iso(),
        )
        return {"passport_id": passport_id}

    def _apply_job_requirement_published(self, event: Mapping[str, Any]) -> dict[str, Any]:
        requirement_id = event["aggregate_id"]
        payload = event["payload"]
        employer_id = self._require_str(payload, "employer_id")
        required_units = self._require_str_list(payload, "required_units")
        standard_id = payload.get("standard_id")
        if standard_id is not None and not isinstance(standard_id, str):
            raise _Rejection("invalid_field", "载荷字段 standard_id 必须是字符串")
        title = payload.get("title")
        if title is not None and not isinstance(title, str):
            raise _Rejection("invalid_field", "载荷字段 title 必须是字符串")
        if self._conn.execute(
            "SELECT 1 FROM requirements WHERE requirement_id = ?", (requirement_id,)
        ).fetchone():
            raise _Conflict("requirement_exists", f"岗位要求 {requirement_id} 已发布")
        self._conn.execute(
            "INSERT INTO requirements (requirement_id, employer_id, standard_id, required_units_json,"
            " title, published_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                requirement_id,
                employer_id,
                standard_id,
                json.dumps(required_units, ensure_ascii=False),
                title,
                event["occurred_at"],
            ),
        )
        return {"requirement_id": requirement_id}

    # ------------------------------------------------------------------
    # 标准改版重算
    # ------------------------------------------------------------------

    def _recompute_after_revision(
        self,
        standard_id: str,
        *,
        previous_version: int,
        new_version: int,
        previous_units: dict[str, Any],
        new_units: dict[str, Any],
    ) -> None:
        affected = sorted(
            unit_id
            for unit_id in set(previous_units) | set(new_units)
            if previous_units.get(unit_id) != new_units.get(unit_id)
        )
        if not affected:
            return
        passports = self._conn.execute(
            "SELECT * FROM passports WHERE standard_id = ? AND standard_version = ? AND status = 'valid'",
            (standard_id, previous_version),
        ).fetchall()
        for passport in passports:
            pool = self._evidence_pool(passport["student_id"], standard_id, passport["standard_version"])
            changes: list[tuple[str, str, str]] = []
            for unit_id in affected:
                current = self._conn.execute(
                    "SELECT * FROM passport_units WHERE passport_id = ? AND unit_id = ?",
                    (passport["passport_id"], unit_id),
                ).fetchone()
                if current is None:
                    continue
                if unit_id not in new_units:
                    new_status, new_score, evidence_ids = "invalidated", None, json.loads(current["evidence_ids_json"])
                else:
                    new_status, new_score, evidence_ids = _compute_unit(
                        new_units[unit_id]["pass_score"], unit_id, pool
                    )
                self._conn.execute(
                    "UPDATE passport_units SET status = ?, score = ?, computed_under_version = ?,"
                    " evidence_ids_json = ?, updated_at = ? WHERE passport_id = ? AND unit_id = ?",
                    (
                        new_status,
                        new_score,
                        new_version,
                        json.dumps(evidence_ids, ensure_ascii=False),
                        self._now_iso(),
                        passport["passport_id"],
                        unit_id,
                    ),
                )
                self._record_recomputation(
                    passport["passport_id"],
                    unit_id,
                    "standard_revision",
                    current["status"],
                    new_status,
                    current["score"],
                    new_score,
                )
                changes.append((unit_id, current["status"], new_status))
            if changes:
                change_text = "；".join(f"{unit}: {old}→{new}" for unit, old, new in changes)
                self._note_snapshots(
                    passport["passport_id"],
                    f"赛项标准版本 v{previous_version}→v{new_version}，受影响单元：{'、'.join(affected)}；"
                    f"结论变化：{change_text}；快照内容保持原样",
                )

    def _record_recomputation(
        self,
        passport_id: str,
        unit_id: str,
        reason: str,
        old_status: str | None,
        new_status: str | None,
        old_score: float | None,
        new_score: float | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO unit_recomputations (passport_id, unit_id, reason, old_status, new_status,"
            " old_score, new_score, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (passport_id, unit_id, reason, old_status, new_status, old_score, new_score, self._now_iso()),
        )

    def _note_snapshots(self, passport_id: str, note: str) -> None:
        rows = self._conn.execute(
            "SELECT snapshot_id FROM snapshots WHERE passport_id = ?", (passport_id,)
        ).fetchall()
        for row in rows:
            self._conn.execute(
                "INSERT INTO snapshot_notes (snapshot_id, note, created_at) VALUES (?, ?, ?)",
                (row["snapshot_id"], note, self._now_iso()),
            )

    # ------------------------------------------------------------------
    # 持久任务：到期、申诉与撤权传播
    # ------------------------------------------------------------------

    def run_due_jobs(self, now: datetime | None = None) -> int:
        """执行到期的持久任务，返回处理数量。"""
        moment = now or self._now()
        processed = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status = 'pending' ORDER BY run_after, job_id"
            ).fetchall()
            for row in rows:
                if _parse_time(row["run_after"]) > moment:
                    continue
                self._conn.execute(
                    "UPDATE jobs SET attempts = attempts + 1, updated_at = ? WHERE job_id = ?",
                    (self._now_iso(), row["job_id"]),
                )
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._run_job(row["kind"], json.loads(row["payload_json"]), moment)
                    self._conn.execute(
                        "UPDATE jobs SET status = 'done', updated_at = ? WHERE job_id = ?",
                        (self._now_iso(), row["job_id"]),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                processed += 1
        return processed

    def recover(self, now: datetime | None = None) -> dict[str, int]:
        """服务重启后继续处理到期、申诉与撤权传播任务。"""
        return {"processed": self.run_due_jobs(now)}

    def _enqueue(self, job_id: str, kind: str, payload: Mapping[str, Any], *, run_after: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO jobs (job_id, kind, payload_json, run_after, status, attempts, updated_at)"
            " VALUES (?, ?, ?, ?, 'pending', 0, ?)",
            (job_id, kind, json.dumps(payload, ensure_ascii=False), run_after, self._now_iso()),
        )

    def _run_job(self, kind: str, payload: dict[str, Any], now: datetime) -> None:
        if kind == "permit_expiry":
            permit = self._conn.execute(
                "SELECT * FROM permits WHERE permit_id = ?", (payload["permit_id"],)
            ).fetchone()
            if permit is not None and permit["status"] == "valid" and _parse_time(permit["expires_at"]) <= now:
                self._conn.execute(
                    "UPDATE permits SET status = 'expired' WHERE permit_id = ?", (permit["permit_id"],)
                )
            passport = self._conn.execute(
                "SELECT * FROM passports WHERE passport_id = ?", (payload["passport_id"],)
            ).fetchone()
            remaining = self._conn.execute(
                "SELECT COUNT(*) AS n FROM permits WHERE passport_id = ? AND status = 'valid'",
                (payload["passport_id"],),
            ).fetchone()["n"]
            if (
                passport is not None
                and passport["status"] == "valid"
                and remaining == 0
                and _parse_time(passport["expires_at"]) <= now
            ):
                self._conn.execute(
                    "UPDATE passports SET status = 'expired' WHERE passport_id = ?", (passport["passport_id"],)
                )
        elif kind == "revocation_propagation":
            self._conn.execute(
                "INSERT INTO propagation_log (kind, detail_json, created_at) VALUES (?, ?, ?)",
                ("revocation_propagated", json.dumps(payload, ensure_ascii=False), self._now_iso()),
            )
        elif kind == "appeal_propagation":
            decision_text = "申诉成立" if payload["decision"] == "upheld" else "申诉驳回"
            changes = payload.get("changes") or []
            if changes:
                change_text = "；".join(
                    f"{c['unit_id']}: {c['old_status']}→{c['new_status']}"
                    f"（{_fmt_score(c['old_score'])}→{_fmt_score(c['new_score'])}）"
                    for c in changes
                )
            else:
                change_text = "结论不变"
            self._note_snapshots(
                payload["passport_id"],
                f"{decision_text}，受影响单元：{'、'.join(payload['affected_units'])}；"
                f"{change_text}；快照内容保持原样",
            )
            self._conn.execute(
                "INSERT INTO propagation_log (kind, detail_json, created_at) VALUES (?, ?, ?)",
                ("appeal_propagated", json.dumps(payload, ensure_ascii=False), self._now_iso()),
            )
        else:
            raise ServiceError("unknown_job", f"未知任务类型 {kind}")

    def list_propagation(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, detail_json, created_at FROM propagation_log ORDER BY id"
            ).fetchall()
        return [
            {"kind": row["kind"], "detail": json.loads(row["detail_json"]), "created_at": row["created_at"]}
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 企业与学生视图
    # ------------------------------------------------------------------

    def employer_view(
        self,
        employer_id: str,
        passport_id: str,
        requirement_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """企业视图：只返回岗位所需能力单元的结论，不暴露原始证据。"""
        moment = now or self._now()
        with self._lock:
            passport = self._conn.execute(
                "SELECT * FROM passports WHERE passport_id = ?", (passport_id,)
            ).fetchone()
            if passport is None:
                raise ServiceError("unknown_passport", f"未找到通行证 {passport_id}")
            permit = self._conn.execute(
                "SELECT * FROM permits WHERE passport_id = ? AND employer_id = ?",
                (passport_id, employer_id),
            ).fetchone()
            if permit is None:
                raise AccessDeniedError("该企业未获得此通行证的共享许可")
            if permit["status"] == "revoked":
                raise AccessDeniedError("学生已撤回该企业的访问许可")
            if permit["status"] == "expired" or _parse_time(permit["expires_at"]) <= moment:
                raise AccessDeniedError("共享许可已到期")
            requirement = self._conn.execute(
                "SELECT * FROM requirements WHERE requirement_id = ?", (requirement_id,)
            ).fetchone()
            if requirement is None:
                raise ServiceError("unknown_requirement", f"未找到岗位要求 {requirement_id}")
            if requirement["employer_id"] != employer_id:
                raise AccessDeniedError("岗位要求不属于该企业")
            conclusions = []
            for unit_id in json.loads(requirement["required_units_json"]):
                row = self._conn.execute(
                    "SELECT * FROM passport_units WHERE passport_id = ? AND unit_id = ?",
                    (passport_id, unit_id),
                ).fetchone()
                if row is None:
                    conclusions.append(
                        {
                            "unit_id": unit_id,
                            "status": "not_assessed",
                            "score": None,
                            "standard_version": passport["standard_version"],
                        }
                    )
                else:
                    conclusions.append(
                        {
                            "unit_id": unit_id,
                            "status": row["status"],
                            "score": row["score"],
                            "standard_version": row["computed_under_version"],
                        }
                    )
            return {
                "passport_id": passport_id,
                "student_id": passport["student_id"],
                "standard_id": passport["standard_id"],
                "standard_version": passport["standard_version"],
                "requirement_id": requirement_id,
                "permit_expires_at": permit["expires_at"],
                "conclusions": conclusions,
            }

    def record_hiring_snapshot(self, employer_id: str, passport_id: str, requirement_id: str) -> str:
        """把当前企业视图固化为录用决定快照；此后内容不再修改，只追加差异说明。"""
        with self._lock:
            view = self.employer_view(employer_id, passport_id, requirement_id)
            snapshot_id = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO snapshots (snapshot_id, passport_id, employer_id, requirement_id,"
                " content_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    passport_id,
                    employer_id,
                    requirement_id,
                    json.dumps(view, ensure_ascii=False),
                    self._now_iso(),
                ),
            )
            return snapshot_id

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise ServiceError("unknown_snapshot", f"未找到快照 {snapshot_id}")
            notes = self._conn.execute(
                "SELECT note FROM snapshot_notes WHERE snapshot_id = ? ORDER BY id", (snapshot_id,)
            ).fetchall()
            return {
                "snapshot_id": snapshot_id,
                "passport_id": row["passport_id"],
                "employer_id": row["employer_id"],
                "requirement_id": row["requirement_id"],
                "created_at": row["created_at"],
                "content": json.loads(row["content_json"]),
                "notes": [note["note"] for note in notes],
            }

    def student_view(self, student_id: str) -> dict[str, Any]:
        """学生视图：每项能力可追溯来源任务、确认人以及仍然有效的原因。"""
        with self._lock:
            passports = self._conn.execute(
                "SELECT * FROM passports WHERE student_id = ? ORDER BY issued_at, passport_id", (student_id,)
            ).fetchall()
            result = []
            for passport in passports:
                units = self._conn.execute(
                    "SELECT * FROM passport_units WHERE passport_id = ? ORDER BY unit_id",
                    (passport["passport_id"],),
                ).fetchall()
                entries = []
                for unit in units:
                    evidence_ids = json.loads(unit["evidence_ids_json"])
                    sources = []
                    for evidence_id in evidence_ids:
                        evidence = self._conn.execute(
                            "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
                        ).fetchone()
                        if evidence is not None:
                            sources.append(
                                {
                                    "evidence_id": evidence["evidence_id"],
                                    "task_id": evidence["task_id"],
                                    "observer_id": evidence["observer_id"],
                                    "occurred_at": evidence["occurred_at"],
                                }
                            )
                    appeal_count = self._conn.execute(
                        "SELECT COUNT(*) AS n FROM unit_recomputations"
                        " WHERE passport_id = ? AND unit_id = ? AND reason = 'appeal'",
                        (passport["passport_id"], unit["unit_id"]),
                    ).fetchone()["n"]
                    entries.append(
                        {
                            "unit_id": unit["unit_id"],
                            "status": unit["status"],
                            "score": unit["score"],
                            "standard_version": unit["computed_under_version"],
                            "sources": sources,
                            "validity": self._validity_text(passport, unit, sources, appeal_count),
                        }
                    )
                result.append(
                    {
                        "passport_id": passport["passport_id"],
                        "standard_id": passport["standard_id"],
                        "standard_version": passport["standard_version"],
                        "status": passport["status"],
                        "expires_at": passport["expires_at"],
                        "units": entries,
                    }
                )
            return {"student_id": student_id, "passports": result}

    @staticmethod
    def _validity_text(
        passport: sqlite3.Row,
        unit: sqlite3.Row,
        sources: list[dict[str, Any]],
        appeal_count: int,
    ) -> str:
        status = unit["status"]
        if status == "invalidated":
            return f"该能力单元在标准版本 v{unit['computed_under_version']} 中已失效，需按现行标准重新训练"
        if status == "no_evidence":
            return "暂无已确认的训练证据，结论待补充"
        observers = "、".join(sorted({source["observer_id"] for source in sources}))
        tasks = "、".join(sorted({source["task_id"] for source in sources}))
        parts = [
            f"证据由 {observers} 在任务 {tasks} 中确认",
            f"按标准版本 v{unit['computed_under_version']} 计权且同一证据只计一次",
        ]
        if appeal_count:
            parts.append("申诉已改判")
        if passport["status"] == "expired":
            parts.append("通行证已到期，需重新签发")
        else:
            parts.append(f"通行证有效期至 {passport['expires_at']}")
        return "；".join(parts)

    def get_passport(self, passport_id: str) -> dict[str, Any]:
        with self._lock:
            passport = self._conn.execute(
                "SELECT * FROM passports WHERE passport_id = ?", (passport_id,)
            ).fetchone()
            if passport is None:
                raise ServiceError("unknown_passport", f"未找到通行证 {passport_id}")
            units = self._conn.execute(
                "SELECT * FROM passport_units WHERE passport_id = ? ORDER BY unit_id", (passport_id,)
            ).fetchall()
            return {
                "passport_id": passport["passport_id"],
                "student_id": passport["student_id"],
                "standard_id": passport["standard_id"],
                "standard_version": passport["standard_version"],
                "status": passport["status"],
                "issued_at": passport["issued_at"],
                "expires_at": passport["expires_at"],
                "units": [
                    {
                        "unit_id": unit["unit_id"],
                        "status": unit["status"],
                        "score": unit["score"],
                        "computed_under_version": unit["computed_under_version"],
                        "evidence_ids": json.loads(unit["evidence_ids_json"]),
                        "updated_at": unit["updated_at"],
                    }
                    for unit in units
                ],
            }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _standard(self, standard_id: str, version: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM standards WHERE standard_id = ? AND version = ?", (standard_id, version)
        ).fetchone()

    def _evidence_pool(self, student_id: str, standard_id: str, standard_version: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM evidence WHERE student_id = ? AND standard_id = ? AND standard_version = ?"
            " ORDER BY evidence_id",
            (student_id, standard_id, standard_version),
        ).fetchall()

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("时钟必须返回带时区的时间")
        return moment

    def _now_iso(self) -> str:
        return self._now().isoformat()

    @staticmethod
    def _require_str(payload: Mapping[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise _Rejection("invalid_field", f"载荷字段 {field} 必须是非空字符串")
        return value

    @staticmethod
    def _require_str_list(payload: Mapping[str, Any], field: str) -> list[str]:
        value = payload.get(field)
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            raise _Rejection("invalid_field", f"载荷字段 {field} 必须是非空字符串列表")
        return list(value)

    @staticmethod
    def _require_time(payload: Mapping[str, Any], field: str) -> datetime:
        value = payload.get(field)
        if not isinstance(value, str):
            raise _Rejection("invalid_field", f"载荷字段 {field} 必须是携带时区的时间字符串")
        try:
            return _parse_time(value)
        except ValueError:
            raise _Rejection("invalid_field", f"载荷字段 {field} 必须是携带时区的时间字符串") from None

    @staticmethod
    def _parse_units(raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise _Rejection("invalid_field", "载荷字段 units 必须是非空列表")
        units: dict[str, dict[str, Any]] = {}
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise _Rejection("invalid_field", "能力单元必须是对象")
            unit_id = entry.get("unit_id")
            if not isinstance(unit_id, str) or not unit_id.strip():
                raise _Rejection("invalid_field", "能力单元缺少 unit_id")
            pass_score = entry.get("pass_score", DEFAULT_PASS_SCORE)
            if isinstance(pass_score, bool) or not isinstance(pass_score, (int, float)) or not 0 <= pass_score <= 1:
                raise _Rejection("invalid_field", f"能力单元 {unit_id} 的 pass_score 必须在 0 到 1 之间")
            title = entry.get("title", unit_id)
            units[unit_id] = {"title": str(title), "pass_score": float(pass_score)}
        return units

    @staticmethod
    def _parse_observers(raw: Any) -> dict[str, str]:
        if not isinstance(raw, list) or not raw:
            raise _Rejection("invalid_field", "标准发布必须登记裁判或教师授权")
        observers: dict[str, str] = {}
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise _Rejection("invalid_field", "授权条目必须是对象")
            observer_id = entry.get("observer_id")
            role = entry.get("role")
            if not isinstance(observer_id, str) or not observer_id.strip():
                raise _Rejection("invalid_field", "授权条目缺少 observer_id")
            if not isinstance(role, str) or not role.strip():
                raise _Rejection("invalid_field", f"观察人 {observer_id} 缺少授权角色")
            observers[observer_id] = role
        return observers

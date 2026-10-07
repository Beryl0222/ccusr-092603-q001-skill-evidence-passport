"""复合技能证据通行证业务服务。

在交换契约之上实现领域规则：

* 同号同内容返回原回执；同号异内容冻结核对；
* 一次表现可支撑多个能力单元，同一证据不重复计权；
* 证据必须携带裁判或教师授权；
* 每个学生、每个标准只存在一份有效通行证（签发在锁内串行）；
* 共享许可带有效期，到期通过 pump 传播，撤权即时生效，
  依法保留的考核事实不删除；
* 标准改版只重算受影响单元，录用快照保持原样并追加差异说明；
* 企业接口只暴露岗位所需结论，学生可追溯每项能力的来源与有效性。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import validate_event
from .store import content_hash


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"

AUTHORIZED_ROLES = ("judge", "teacher")
APPEAL_DECISIONS = ("upheld", "rejected")

# 指纹中需要剔除的服务端派生字段：重放与补传时这些字段由状态推出，
# 不能参与"内容是否相同"的判定，否则合法补传会被误冻。
_DERIVED_PAYLOAD_FIELDS = {
    "STANDARD_PUBLISHED": {"changed_units"},
    "PASSPORT_ISSUED": {"standard_version", "recipient_scope", "expires_at"},
    "SNAPSHOT_TAKEN": {"basis_version", "conclusions"},
}
# 其余事件指纹就是 (类型, 聚合, 载荷)；时间与序号永不参与。


class DomainError(Exception):
    """上层可直接据 code 处理的业务冲突。"""

    def __init__(self, code: str, message: str, field: str = "$") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


class FrozenConflictError(DomainError):
    """同号异内容：记录已冻结，等待人工核对。"""

    def __init__(self, event_id: str, original_hash: str, duplicate_hash: str) -> None:
        super().__init__(
            "record_frozen",
            f"事件 {event_id} 曾以不同内容提交，已冻结核对",
            "event_id",
        )
        self.event_id = event_id
        self.original_hash = original_hash
        self.duplicate_hash = duplicate_hash


class AccessDeniedError(DomainError):
    def __init__(self, message: str = "共享许可无效或已终止") -> None:
        super().__init__("access_denied", message, "recipient_id")


def _fingerprint(event: Mapping[str, Any]) -> str:
    """业务指纹：只标识调用方提交的内容，不含服务端派生数据。"""

    drop = _DERIVED_PAYLOAD_FIELDS.get(event["event_type"], set())
    payload = {k: v for k, v in event["payload"].items() if k not in drop}
    return content_hash(
        {
            "event_type": event["event_type"],
            "aggregate_type": event["aggregate_type"],
            "aggregate_id": event["aggregate_id"],
            "payload": payload,
        }
    )


def _parse_dt(value: str | datetime, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise DomainError("bad_datetime", f"{field} 不是合法时间", field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DomainError("timezone_required", f"{field} 必须携带时区", field)
    return parsed


def _require(condition: bool, code: str, message: str, field: str = "$") -> None:
    if not condition:
        raise DomainError(code, message, field)


@dataclass(frozen=True)
class Receipt:
    """补传同一记录时原样返回的回执。"""

    event_id: str
    aggregate_type: str
    aggregate_id: str
    recorded_at: str
    hash: str
    refs: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "recorded_at": self.recorded_at,
            "hash": self.hash,
            "refs": dict(self.refs),
        }


class PassportService:
    def __init__(
        self,
        directory: str | Path,
        clock: Callable[[], datetime] | None = None,
        schema: Mapping[str, Any] | None = None,
    ) -> None:
        from .store import EventStore

        self.store = EventStore(directory)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.schema = schema or json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._reset_state()
        for event in self.store.load():
            self._apply(event)

    # ------------------------------------------------------------------ 状态

    def _reset_state(self) -> None:
        self.events_by_id: dict[str, dict[str, Any]] = {}
        self.fingerprints: dict[str, str] = {}
        self.frozen: dict[str, dict[str, str]] = {}
        self.standards: dict[str, dict[str, Any]] = {}
        # (standard_id, from_version, to_version) -> 受影响单元
        self.transitions: dict[tuple[str, int, int], set[str]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.evidence: dict[str, dict[str, Any]] = {}
        self.passports: dict[str, dict[str, Any]] = {}
        self.passport_index: dict[tuple[str, str], str] = {}
        self.appeals: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, Receipt] = {}

    # ------------------------------------------------------------- 事件装配

    def _envelope(
        self,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self.clock().isoformat(),
            "version": len(self.events_by_id) + len(self.frozen) + 1,
            "payload": dict(payload),
        }

    @staticmethod
    def _refs_for(event: Mapping[str, Any]) -> dict[str, Any]:
        payload = event["payload"]
        refs: dict[str, Any] = {}
        for key in ("passport_id", "evidence_id", "snapshot_id", "appeal_id", "job_requirement_id", "standard_id"):
            if key in payload:
                refs[key] = payload[key]
        if event["aggregate_type"] == "competency_passport":
            refs["passport_id"] = event["aggregate_id"]
        return refs

    def _duplicate_locked(self, event: Mapping[str, Any]) -> Receipt | None:
        """锁内幂等判定。同内容返回原回执；异内容冻结并抛错；新事件返回 None。

        必须在任何"当前状态"校验之前调用：补传旧命令时状态早已前移，
        只有指纹比对能认出它是同一记录。
        """

        event_id = event["event_id"]
        if event_id in self.frozen:
            record = self.frozen[event_id]
            raise FrozenConflictError(event_id, record["original_hash"], record["duplicate_hash"])
        if event_id in self.events_by_id:
            new_fp = _fingerprint(event)
            old_fp = self.fingerprints[event_id]
            if new_fp == old_fp:
                return self.receipts[event_id]
            freeze = self._envelope(
                f"freeze:{event_id}",
                "RECORD_FROZEN",
                "evidence_item",
                f"freeze:{event_id}",
                {
                    "original_hash": old_fp,
                    "duplicate_hash": new_fp,
                    "reason": f"事件 {event_id} 同号异内容",
                },
            )
            issues = validate_event(freeze, self.schema)
            assert not issues, issues
            self.store.append(freeze)
            self._apply(freeze, override_id=event_id)
            raise FrozenConflictError(event_id, old_fp, new_fp)
        return None

    def _commit_locked(self, events: list[dict[str, Any]]) -> Receipt:
        """锁内：契约校验 + 批量原子落盘 + 投影。"""

        for event in events:
            issues = validate_event(event, self.schema)
            if issues:
                raise DomainError(
                    "contract_violation",
                    "; ".join(f"{i.field}:{i.code}" for i in issues),
                )
        self.store.append_many(events)
        for event in events:
            self._apply(event)
        return self.receipts[events[0]["event_id"]]

    # ------------------------------------------------------------- 状态迁移

    def _apply(self, event: Mapping[str, Any], override_id: str | None = None) -> None:
        etype = event["event_type"]
        payload = event["payload"]

        if etype == "RECORD_FROZEN":
            frozen_key = (override_id or event["event_id"]).removeprefix("freeze:")
            self.frozen[frozen_key] = {
                "original_hash": payload["original_hash"],
                "duplicate_hash": payload["duplicate_hash"],
                "frozen_at": event["occurred_at"],
            }
            return

        eid = override_id or event["event_id"]
        self.events_by_id[eid] = dict(event)
        self.fingerprints[eid] = _fingerprint(event)
        self.receipts[eid] = Receipt(
            event_id=eid,
            aggregate_type=event["aggregate_type"],
            aggregate_id=event["aggregate_id"],
            recorded_at=event["occurred_at"],
            hash=self.fingerprints[eid],
            refs=self._refs_for(event),
        )

        if etype == "STANDARD_PUBLISHED":
            std = self.standards.setdefault(payload["standard_id"], {"latest": 0, "versions": {}})
            ver = payload["standard_version"]
            std["versions"][ver] = {
                u["unit_id"]: u.get("spec_hash", u["unit_id"]) for u in payload["units"]
            }
            if ver > std["latest"]:
                if std["latest"]:
                    self.transitions[(payload["standard_id"], std["latest"], ver)] = set(
                        payload.get("changed_units", [])
                    )
                std["latest"] = ver
            # 通行证跟随新版本；具体单元是否有效由改版单元集合决定
            for passport in self.passports.values():
                if passport["standard_id"] == payload["standard_id"]:
                    passport["current_version"] = ver

        elif etype == "JOB_REQUIREMENT_REGISTERED":
            self.jobs[payload["job_requirement_id"]] = {
                "employer_id": payload["employer_id"],
                "standard_id": payload["standard_id"],
                "required_units": list(payload["required_units"]),
                "registered_at": event["occurred_at"],
            }

        elif etype == "EVIDENCE_ACCEPTED":
            self.evidence[payload["evidence_id"]] = {**payload, "accepted_at": event["occurred_at"]}

        elif etype == "PASSPORT_ISSUED":
            passport = {
                "passport_id": event["aggregate_id"],
                "student_id": payload["student_id"],
                "standard_id": payload["standard_id"],
                "current_version": payload["standard_version"],
                "issued_at": event["occurred_at"],
                "sharings": {},
                "snapshots": [],
                "appeals": [],
                "appeal_overrides": {},
            }
            self.passports[event["aggregate_id"]] = passport
            self.passport_index[(payload["student_id"], payload["standard_id"])] = event["aggregate_id"]

        elif etype == "SHARING_GRANTED":
            passport = self.passports[payload["passport_id"]]
            prior_history = passport["sharings"].get(payload["recipient_id"], {}).get("history", [])
            passport["sharings"][payload["recipient_id"]] = {
                "recipient_id": payload["recipient_id"],
                "job_requirement_id": payload["job_requirement_id"],
                "status": "active",
                "granted_at": event["occurred_at"],
                "expires_at": payload["expires_at"],
                "revoked_at": None,
                "history": [
                    *prior_history,
                    {"at": event["occurred_at"], "expires_at": payload["expires_at"], "action": "granted"},
                ],
            }

        elif etype in ("ACCESS_REVOKED", "ACCESS_EXPIRED"):
            sharing = self.passports[payload["passport_id"]]["sharings"][payload["recipient_id"]]
            sharing["status"] = "revoked" if etype == "ACCESS_REVOKED" else "expired"
            sharing["revoked_at"] = event["occurred_at"]
            sharing["history"].append(
                {"at": event["occurred_at"], "action": sharing["status"], "reason": payload.get("reason", "")}
            )

        elif etype == "APPEAL_FILED":
            appeal = {
                "appeal_id": payload["appeal_id"],
                "passport_id": payload["passport_id"],
                "affected_units": list(payload["affected_units"]),
                "reason": payload["reason"],
                "filed_at": event["occurred_at"],
                "status": "open",
                "decided_at": None,
                "decision": None,
                "resolution": {},
                "note": "",
            }
            self.appeals[payload["appeal_id"]] = appeal
            self.passports[payload["passport_id"]]["appeals"].append(appeal)

        elif etype == "APPEAL_DECIDED":
            appeal = self.appeals[payload["appeal_id"]]
            appeal["status"] = payload["decision"]
            appeal["decided_at"] = event["occurred_at"]
            appeal["decision"] = payload["decision"]
            appeal["resolution"] = dict(payload.get("resolution", {}))
            appeal["note"] = payload.get("note", "")
            if payload["decision"] == "upheld":
                # 更正锚定裁决时的标准版本；该单元以后再被改版调整则更正失效
                basis = self.passports[appeal["passport_id"]]["current_version"]
                for unit, status in appeal["resolution"].items():
                    self.passports[appeal["passport_id"]]["appeal_overrides"][unit] = {
                        "status": status,
                        "basis_version": basis,
                    }

        elif etype == "SNAPSHOT_TAKEN":
            self.passports[payload["passport_id"]]["snapshots"].append(
                {
                    "snapshot_id": payload["snapshot_id"],
                    "recipient_id": payload["recipient_id"],
                    "job_requirement_id": payload["job_requirement_id"],
                    "taken_at": event["occurred_at"],
                    "basis_version": payload["basis_version"],
                    "conclusions": json.loads(json.dumps(payload["conclusions"])),
                    "diffs": [],
                }
            )

        elif etype == "SNAPSHOT_DIFF_APPENDED":
            for snapshot in self.passports[payload["passport_id"]]["snapshots"]:
                if snapshot["snapshot_id"] == payload["snapshot_id"]:
                    snapshot["diffs"].append(
                        {
                            "at": event["occurred_at"],
                            "cause": payload["cause"],
                            "changes": json.loads(json.dumps(payload["changes"])),
                        }
                    )
                    break

    # ------------------------------------------------------------- 结论计算

    def _evaluate(
        self,
        passport: Mapping[str, Any],
        unit_id: str,
        overrides: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        standard_id = passport["standard_id"]
        current = passport["current_version"]
        hits: list[dict[str, Any]] = []
        for evidence in self.evidence.values():
            if evidence["student_id"] != passport["student_id"]:
                continue
            if evidence["standard_id"] != standard_id:
                continue
            if unit_id not in evidence["supports_units"]:
                continue
            ev_ver = evidence["standard_version"]
            if ev_ver > current:
                continue
            # 证据版本之后，若任一改版调整过此单元，旧证据对当前结论失效
            invalidated = any(
                sid == standard_id and ev_ver < to <= current and unit_id in changed
                for (sid, _frm, to), changed in self.transitions.items()
            )
            if not invalidated:
                hits.append(evidence)
        resolution = overrides if overrides is not None else passport.get("appeal_overrides", {})
        overridden = resolution.get(unit_id)
        if isinstance(overridden, dict):
            basis = overridden["basis_version"]
            superseded = any(
                sid == standard_id and basis < to <= current and unit_id in changed
                for (sid, _frm, to), changed in self.transitions.items()
            )
            if not superseded:
                status = overridden["status"]
            else:
                status = "met" if hits else "not_met"
        else:
            status = "met" if hits else "not_met"
        return {
            "status": status,
            "basis_version": current,
            "evidence": [
                {
                    "evidence_id": e["evidence_id"],
                    "task_id": e["task_id"],
                    "observer_id": e["observer_id"],
                    "standard_version": e["standard_version"],
                    "accepted_at": e["accepted_at"],
                    "authorization": e["authorization"],
                }
                for e in hits
            ],
        }

    def _job_conclusions(
        self, passport: Mapping[str, Any], job: Mapping[str, Any], overrides: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        return {
            unit: self._evaluate(passport, unit, overrides) for unit in job["required_units"]
        }

    def _snapshot_diff_events(
        self,
        passport: Mapping[str, Any],
        changed_units: set[str],
        cause: str,
        overrides: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """只比较受影响单元；快照原结论不动，只生成追加差异事件。"""

        events: list[dict[str, Any]] = []
        for snapshot in passport["snapshots"]:
            # 连续变更时与上一次差异声明的状态比较，原始结论永不改写
            declared: dict[str, str] = {}
            for prior in snapshot["diffs"]:
                for change in prior["changes"]:
                    declared[change["unit_id"]] = change["after"]["status"]
            changes = []
            for unit in changed_units:
                if unit not in snapshot["conclusions"]:
                    continue
                before_status = declared.get(unit, snapshot["conclusions"][unit]["status"])
                after = self._evaluate(passport, unit, overrides)
                if after["status"] != before_status:
                    changes.append(
                        {
                            "unit_id": unit,
                            "before": {"status": before_status},
                            "after": {"status": after["status"], "basis_version": after["basis_version"]},
                        }
                    )
            if changes:
                events.append(
                    self._envelope(
                        f"diff:{snapshot['snapshot_id']}:{len(snapshot['diffs']) + 1}",
                        "SNAPSHOT_DIFF_APPENDED",
                        "competency_passport",
                        passport["passport_id"],
                        {
                            "snapshot_id": snapshot["snapshot_id"],
                            "passport_id": passport["passport_id"],
                            "cause": cause,
                            "changes": changes,
                        },
                    )
                )
        return events

    # --------------------------------------------------------------- 命令

    def publish_standard(
        self,
        event_id: str,
        standard_id: str,
        version: int,
        units: list[Mapping[str, Any]],
    ) -> Receipt:
        with self.store.lock:
            # 先用调用方原始输入做幂等探测（剔除 changed_units 等派生字段）
            probe = self._envelope(
                event_id,
                "STANDARD_PUBLISHED",
                "skill_standard",
                standard_id,
                {
                    "standard_id": standard_id,
                    "standard_version": version,
                    "units": [dict(u) for u in units],
                },
            )
            if (duplicate := self._duplicate_locked(probe)) is not None:
                return duplicate

            _require(
                isinstance(version, int) and not isinstance(version, bool) and version >= 1,
                "positive_integer",
                "标准版本必须从 1 递增",
                "standard_version",
            )
            _require(units, "required", "标准至少包含一个能力单元", "units")
            ids = [u["unit_id"] for u in units]
            _require(len(ids) == len(set(ids)), "duplicate_unit", "能力单元标识重复", "units")

            prev = self.standards.get(standard_id)
            if prev is None:
                _require(version == 1, "version_skip", "新标准首个版本必须为 1", "standard_version")
                changed_units: list[str] = []
            else:
                _require(
                    version == prev["latest"] + 1,
                    "version_conflict",
                    "标准版本必须连续递增",
                    "standard_version",
                )
                old = prev["versions"][prev["latest"]]
                new = {u["unit_id"]: u.get("spec_hash", u["unit_id"]) for u in units}
                changed_units = [
                    uid
                    for uid in set(old) | set(new)
                    if uid not in old or uid not in new or old[uid] != new[uid]
                ]

            primary = self._envelope(
                event_id,
                "STANDARD_PUBLISHED",
                "skill_standard",
                standard_id,
                {
                    "standard_id": standard_id,
                    "standard_version": version,
                    "units": [dict(u) for u in units],
                    "changed_units": changed_units,
                },
            )
            self._commit_locked([primary])
            # 标准已推进：仅对受影响单元重算既有录用快照
            diffs: list[dict[str, Any]] = []
            if changed_units:
                for passport in self.passports.values():
                    if passport["standard_id"] == standard_id:
                        diffs.extend(
                            self._snapshot_diff_events(
                                passport, set(changed_units), f"standard_v{version}_adjustment"
                            )
                        )
            if diffs:
                self._commit_locked(diffs)
            return self.receipts[event_id]

    def register_job_requirement(
        self,
        event_id: str,
        job_requirement_id: str,
        employer_id: str,
        standard_id: str,
        required_units: list[str],
    ) -> Receipt:
        with self.store.lock:
            event = self._envelope(
                event_id,
                "JOB_REQUIREMENT_REGISTERED",
                "job_requirement",
                job_requirement_id,
                {
                    "job_requirement_id": job_requirement_id,
                    "employer_id": employer_id,
                    "standard_id": standard_id,
                    "required_units": list(required_units),
                },
            )
            if (duplicate := self._duplicate_locked(event)) is not None:
                return duplicate

            std = self.standards.get(standard_id)
            _require(std is not None, "unknown_standard", "标准尚未发布", "standard_id")
            known = set(std["versions"][std["latest"]])
            _require(required_units, "required", "岗位要求至少包含一个能力单元", "required_units")
            unknown = [u for u in required_units if u not in known]
            _require(not unknown, "unknown_unit", f"岗位引用了未知能力单元: {unknown}", "required_units")
            _require(job_requirement_id not in self.jobs, "job_exists", "岗位要求标识已存在", "job_requirement_id")
            return self._commit_locked([event])

    def accept_evidence(
        self,
        event_id: str,
        evidence_id: str,
        standard_id: str,
        standard_version: int,
        student_id: str,
        task_id: str,
        observer_id: str,
        supports_units: list[str],
        authorization: Mapping[str, Any],
    ) -> Receipt:
        with self.store.lock:
            event = self._envelope(
                event_id,
                "EVIDENCE_ACCEPTED",
                "evidence_item",
                evidence_id,
                {
                    "evidence_id": evidence_id,
                    "standard_id": standard_id,
                    "standard_version": standard_version,
                    "student_id": student_id,
                    "task_id": task_id,
                    "observer_id": observer_id,
                    "supports_units": list(supports_units),
                    "authorization": dict(authorization),
                },
            )
            if (duplicate := self._duplicate_locked(event)) is not None:
                return duplicate

            std = self.standards.get(standard_id)
            _require(std is not None, "unknown_standard", "标准尚未发布", "standard_id")
            _require(
                standard_version in std["versions"],
                "unknown_version",
                "证据引用的标准版本不存在",
                "standard_version",
            )
            _require(supports_units, "required", "证据至少支撑一个能力单元", "supports_units")
            bad = [u for u in supports_units if u not in set(std["versions"][standard_version])]
            _require(not bad, "unknown_unit", f"证据支撑了该版本不存在的单元: {bad}", "supports_units")
            _require(
                authorization.get("role") in AUTHORIZED_ROLES and bool(authorization.get("authorizer_id")),
                "authorization_required",
                "证据必须携带裁判或教师授权（role 与 authorizer_id）",
                "authorization",
            )
            _require(
                evidence_id not in self.evidence,
                "evidence_exists",
                "证据标识已存在，同一证据不得重复计权",
                "evidence_id",
            )
            for existing in self.evidence.values():
                if (
                    existing["student_id"] == student_id
                    and existing["task_id"] == task_id
                    and existing["standard_id"] == standard_id
                    and existing["standard_version"] == standard_version
                ):
                    raise DomainError(
                        "evidence_already_counted",
                        f"任务 {task_id} 的表现已作为证据 {existing['evidence_id']} 计权，不得重复",
                        "task_id",
                    )
            return self._commit_locked([event])

    def issue_passport(self, event_id: str, student_id: str, standard_id: str) -> Receipt:
        with self.store.lock:
            passport_id = f"passport:{student_id}:{standard_id}"
            # 指纹剔除签发时的版本快照，保证未来重放仍能识别同一签发命令
            probe = self._envelope(
                event_id,
                "PASSPORT_ISSUED",
                "competency_passport",
                passport_id,
                {"student_id": student_id, "standard_id": standard_id},
            )
            if (duplicate := self._duplicate_locked(probe)) is not None:
                return duplicate

            std = self.standards.get(standard_id)
            _require(std is not None, "unknown_standard", "标准尚未发布", "standard_id")
            _require(
                (student_id, standard_id) not in self.passport_index,
                "passport_exists",
                "该学生在此标准上已存在有效通行证，每个标准只能签发一份",
                "standard_id",
            )
            event = self._envelope(
                event_id,
                "PASSPORT_ISSUED",
                "competency_passport",
                passport_id,
                {
                    "student_id": student_id,
                    "standard_id": standard_id,
                    "standard_version": std["latest"],
                    "recipient_scope": "issuing_school",
                    "expires_at": None,
                },
            )
            return self._commit_locked([event])

    def grant_sharing(
        self,
        event_id: str,
        passport_id: str,
        recipient_id: str,
        job_requirement_id: str,
        expires_at: str | datetime,
    ) -> Receipt:
        with self.store.lock:
            event = self._envelope(
                event_id,
                "SHARING_GRANTED",
                "competency_passport",
                passport_id,
                {
                    "passport_id": passport_id,
                    "recipient_id": recipient_id,
                    "job_requirement_id": job_requirement_id,
                    "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at,
                },
            )
            if (duplicate := self._duplicate_locked(event)) is not None:
                return duplicate

            passport = self.passports.get(passport_id)
            _require(passport is not None, "unknown_passport", "通行证不存在", "passport_id")
            job = self.jobs.get(job_requirement_id)
            _require(job is not None, "unknown_job", "岗位要求尚未登记", "job_requirement_id")
            _require(
                job["employer_id"] == recipient_id,
                "recipient_mismatch",
                "共享许可只能签给岗位所属企业",
                "recipient_id",
            )
            _require(
                job["standard_id"] == passport["standard_id"],
                "standard_mismatch",
                "岗位要求与通行证标准不一致",
                "job_requirement_id",
            )
            expiry = _parse_dt(expires_at, "expires_at")
            _require(expiry > self.clock(), "expiry_in_past", "共享许可到期时间必须晚于当前时间", "expires_at")
            sharing = passport["sharings"].get(recipient_id)
            _require(
                sharing is None or sharing["status"] != "active",
                "sharing_active",
                "该企业的共享许可仍在有效期内，如需延期请先撤回再重签",
                "recipient_id",
            )
            return self._commit_locked([event])

    def revoke_access(
        self, event_id: str, passport_id: str, recipient_id: str, reason: str = ""
    ) -> Receipt:
        with self.store.lock:
            event = self._envelope(
                event_id,
                "ACCESS_REVOKED",
                "competency_passport",
                passport_id,
                {"passport_id": passport_id, "recipient_id": recipient_id, "reason": reason},
            )
            if (duplicate := self._duplicate_locked(event)) is not None:
                return duplicate

            passport = self.passports.get(passport_id)
            _require(passport is not None, "unknown_passport", "通行证不存在", "passport_id")
            sharing = passport["sharings"].get(recipient_id)
            _require(sharing is not None, "no_sharing", "不存在该企业的共享许可", "recipient_id")
            _require(sharing["status"] == "active", "sharing_closed", "共享许可已终止，不可重复撤权", "recipient_id")
            return self._commit_locked([event])

    def file_appeal(
        self,
        event_id: str,
        appeal_id: str,
        passport_id: str,
        affected_units: list[str],
        reason: str,
    ) -> Receipt:
        with self.store.lock:
            event = self._envelope(
                event_id,
                "APPEAL_FILED",
                "competency_passport",
                passport_id,
                {
                    "appeal_id": appeal_id,
                    "passport_id": passport_id,
                    "affected_units": list(affected_units),
                    "reason": reason,
                },
            )
            if (duplicate := self._duplicate_locked(event)) is not None:
                return duplicate

            passport = self.passports.get(passport_id)
            _require(passport is not None, "unknown_passport", "通行证不存在", "passport_id")
            known = set(self.standards[passport["standard_id"]]["versions"][passport["current_version"]])
            _require(affected_units, "required", "申诉须指明受影响单元", "affected_units")
            _require(all(u in known for u in affected_units), "unknown_unit", "申诉包含未知能力单元", "affected_units")
            _require(appeal_id not in self.appeals, "appeal_exists", "申诉标识已存在", "appeal_id")
            return self._commit_locked([event])

    def decide_appeal(
        self,
        event_id: str,
        appeal_id: str,
        decision: str,
        resolution: Mapping[str, str] | None = None,
        note: str = "",
    ) -> Receipt:
        with self.store.lock:
            appeal = self.appeals.get(appeal_id)
            _require(appeal is not None, "unknown_appeal", "申诉不存在", "appeal_id")
            passport = self.passports[appeal["passport_id"]]
            event = self._envelope(
                event_id,
                "APPEAL_DECIDED",
                "competency_passport",
                passport["passport_id"],
                {
                    "appeal_id": appeal_id,
                    "passport_id": passport["passport_id"],
                    "affected_units": appeal["affected_units"],
                    "decision": decision,
                    "resolution": dict(resolution or {}),
                    "note": note,
                },
            )
            if (duplicate := self._duplicate_locked(event)) is not None:
                return duplicate

            _require(decision in APPEAL_DECISIONS, "bad_decision", "裁决结果只能是 upheld 或 rejected", "decision")
            _require(appeal["status"] == "open", "appeal_closed", "申诉已裁决", "appeal_id")
            if decision == "upheld":
                known = set(self.standards[passport["standard_id"]]["versions"][passport["current_version"]])
                resolution = resolution or {}
                _require(resolution, "resolution_required", "申诉成立须给出受影响单元的更正结论", "resolution")
                _require(
                    set(resolution) <= set(appeal["affected_units"]),
                    "resolution_scope",
                    "更正结论只能覆盖申诉涉及的单元",
                    "resolution",
                )
                _require(
                    all(unit in known and status in ("met", "not_met") for unit, status in resolution.items()),
                    "bad_resolution",
                    "更正结论只能指向当前版本存在的单元且状态合法",
                    "resolution",
                )
            # 差异事件与裁决同批落盘：用"假设更正已生效"的覆盖表只重算受影响单元
            basis = passport["current_version"]
            anchored = {u: {"status": s, "basis_version": basis} for u, s in (resolution or {}).items()}
            overrides = {**passport["appeal_overrides"], **anchored}
            diffs = (
                self._snapshot_diff_events(
                    passport, set(appeal["affected_units"]), f"appeal_{appeal_id}_upheld", overrides
                )
                if decision == "upheld"
                else []
            )
            return self._commit_locked([event, *diffs])

    def take_hiring_snapshot(
        self,
        event_id: str,
        snapshot_id: str,
        passport_id: str,
        recipient_id: str,
        job_requirement_id: str,
    ) -> Receipt:
        with self.store.lock:
            # 指纹只含调用方输入：同一命令在标准改版后补传仍取得原回执
            probe = self._envelope(
                event_id,
                "SNAPSHOT_TAKEN",
                "competency_passport",
                passport_id,
                {
                    "snapshot_id": snapshot_id,
                    "passport_id": passport_id,
                    "recipient_id": recipient_id,
                    "job_requirement_id": job_requirement_id,
                },
            )
            if (duplicate := self._duplicate_locked(probe)) is not None:
                return duplicate

            passport = self.passports.get(passport_id)
            _require(passport is not None, "unknown_passport", "通行证不存在", "passport_id")
            job = self.jobs.get(job_requirement_id)
            _require(job is not None, "unknown_job", "岗位要求尚未登记", "job_requirement_id")
            sharing = passport["sharings"].get(recipient_id)
            _require(sharing is not None, "access_denied", "企业未获得共享许可", "recipient_id")
            _require(
                sharing["job_requirement_id"] == job_requirement_id,
                "job_mismatch",
                "共享许可与岗位要求不一致",
                "job_requirement_id",
            )
            _require(sharing["status"] == "active", "access_denied", "共享许可已终止", "recipient_id")
            _require(
                _parse_dt(sharing["expires_at"], "expires_at") > self.clock(),
                "access_denied",
                "共享许可已到期",
                "recipient_id",
            )
            _require(
                not any(s["snapshot_id"] == snapshot_id for s in passport["snapshots"]),
                "snapshot_exists",
                "录用快照标识已存在",
                "snapshot_id",
            )
            conclusions = self._job_conclusions(passport, job)
            event = self._envelope(
                event_id,
                "SNAPSHOT_TAKEN",
                "competency_passport",
                passport_id,
                {
                    "snapshot_id": snapshot_id,
                    "passport_id": passport_id,
                    "recipient_id": recipient_id,
                    "job_requirement_id": job_requirement_id,
                    "basis_version": passport["current_version"],
                    "conclusions": conclusions,
                },
            )
            return self._commit_locked([event])

    # --------------------------------------------------------------- 查询

    def employer_view(
        self, passport_id: str, recipient_id: str, job_requirement_id: str
    ) -> dict[str, Any]:
        """企业只读接口：只返回岗位所需结论，不含证据、任务、授权人细节。"""

        with self.store.lock:
            passport = self.passports.get(passport_id)
            _require(passport is not None, "unknown_passport", "通行证不存在", "passport_id")
            sharing = passport["sharings"].get(recipient_id)
            # 对无权方不区分"从未许可/已撤/到期/岗位不符"，统一拒绝
            if (
                sharing is None
                or sharing["job_requirement_id"] != job_requirement_id
                or sharing["status"] != "active"
                or _parse_dt(sharing["expires_at"], "expires_at") <= self.clock()
            ):
                raise AccessDeniedError()
            job = self.jobs[job_requirement_id]
            full = self._job_conclusions(passport, job)
            return {
                "passport_id": passport_id,
                "job_requirement_id": job_requirement_id,
                "evaluated_at": self.clock().isoformat(),
                "standard_version": passport["current_version"],
                "sharing_expires_at": sharing["expires_at"],
                "conclusions": {
                    unit: {"status": item["status"], "evidence_count": len(item["evidence"])}
                    for unit, item in full.items()
                },
            }

    def student_trace(self, student_id: str, standard_id: str) -> dict[str, Any]:
        """学生视角：每项能力来自哪次任务、谁确认、为何仍然有效。"""

        with self.store.lock:
            passport_id = self.passport_index.get((student_id, standard_id))
            _require(passport_id is not None, "unknown_passport", "通行证不存在", "passport_id")
            passport = self.passports[passport_id]
            current = passport["current_version"]
            units = []
            for unit_id in self.standards[standard_id]["versions"][current]:
                evaluation = self._evaluate(passport, unit_id)
                provenance = []
                for ref in evaluation["evidence"]:
                    survived = [
                        f"v{to}"
                        for (sid, _frm, to), changed in sorted(self.transitions.items())
                        if sid == standard_id
                        and ref["standard_version"] < to <= current
                        and unit_id not in changed
                    ]
                    provenance.append(
                        {
                            **ref,
                            "validity_reason": (
                                "证据版本即为当前版本"
                                if ref["standard_version"] == current
                                else f"改版 {'、'.join(survived)} 未调整该单元，证据继续有效"
                            ),
                        }
                    )
                units.append(
                    {
                        "unit_id": unit_id,
                        "status": evaluation["status"],
                        "basis_version": current,
                        "provenance": provenance,
                    }
                )
            return {
                "passport_id": passport_id,
                "student_id": student_id,
                "standard_id": standard_id,
                "current_version": current,
                "issued_at": passport["issued_at"],
                "units": units,
                "appeals": json.loads(json.dumps(passport["appeals"])),
                "sharing_ledger": [
                    {
                        "recipient_id": s["recipient_id"],
                        "job_requirement_id": s["job_requirement_id"],
                        "status": s["status"],
                        "granted_at": s["granted_at"],
                        "expires_at": s["expires_at"],
                        "ended_at": s["revoked_at"],
                        "history": s["history"],
                    }
                    for s in passport["sharings"].values()
                ],
                "snapshot_ledger": json.loads(json.dumps(passport["snapshots"])),
            }

    def hiring_snapshot_record(self, snapshot_id: str) -> dict[str, Any]:
        with self.store.lock:
            for passport in self.passports.values():
                for snapshot in passport["snapshots"]:
                    if snapshot["snapshot_id"] == snapshot_id:
                        return json.loads(json.dumps(snapshot))
        raise DomainError("unknown_snapshot", "快照不存在", "snapshot_id")

    def frozen_records(self) -> dict[str, dict[str, str]]:
        with self.store.lock:
            return json.loads(json.dumps(self.frozen))

    # ------------------------------------------------- 到期 / 申诉 / 撤权传播

    def pump(self) -> list[str]:
        """推进到期传播；重启后调用即可继续，重复执行无副作用。

        撤权在落库时即生效，申诉裁决与快照差异同批写入；重放自动续上。
        这里只补发到期事件（ACCESS_EXPIRED），事件标识确定，天然幂等。
        """

        emitted: list[str] = []
        now = self.clock()
        with self.store.lock:
            for passport in list(self.passports.values()):
                for recipient_id, sharing in list(passport["sharings"].items()):
                    if sharing["status"] != "active":
                        continue
                    if _parse_dt(sharing["expires_at"], "expires_at") <= now:
                        event = self._envelope(
                            f"expiry:{passport['passport_id']}:{recipient_id}:{sharing['granted_at']}",
                            "ACCESS_EXPIRED",
                            "competency_passport",
                            passport["passport_id"],
                            {
                                "passport_id": passport["passport_id"],
                                "recipient_id": recipient_id,
                                "reason": "sharing_expired",
                            },
                        )
                        issues = validate_event(event, self.schema)
                        assert not issues, issues
                        self.store.append(event)
                        self._apply(event)
                        emitted.append(event["event_id"])
        return emitted

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from skill_evidence_passport.service import AccessDeniedError, PassportService, ServiceError

CST = timezone(timedelta(hours=8))
STD = "WS-NEW-01"
T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=CST)


class FakeClock:
    def __init__(self, moment: datetime):
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def make_event(event_id, event_type, aggregate_type, aggregate_id, payload, occurred="2026-10-01T09:00:00+08:00"):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred,
        "version": 1,
        "payload": payload,
    }


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "service.db"
        self.clock = FakeClock(T0)
        self.service = PassportService(self.db, clock=self.clock)
        self.addCleanup(self.service.close)

    # -- 事件构造 -------------------------------------------------------

    def standard_event(self, event_id="EVT-STD-1", version=1, units=None, observers=None):
        units = units if units is not None else [
            {"unit_id": "U1", "title": "设备排障", "pass_score": 0.6},
            {"unit_id": "U2", "title": "英文资料使用", "pass_score": 0.6},
        ]
        observers = observers if observers is not None else [
            {"observer_id": "T001", "role": "教师"},
            {"observer_id": "J009", "role": "裁判"},
        ]
        return make_event(
            event_id,
            "STANDARD_PUBLISHED",
            "skill_standard",
            STD,
            {"standard_version": version, "units": units, "observers": observers},
        )

    def evidence_event(
        self,
        event_id,
        evidence_id,
        student="S1",
        task="TASK-1",
        units=("U1",),
        score=0.8,
        observer="T001",
        version=1,
        weight=None,
    ):
        payload = {
            "student_id": student,
            "task_id": task,
            "standard_id": STD,
            "standard_version": version,
            "observer_id": observer,
            "unit_ids": list(units),
            "score": score,
        }
        if weight is not None:
            payload["weight"] = weight
        return make_event(event_id, "EVIDENCE_ACCEPTED", "evidence_item", evidence_id, payload)

    def passport_event(
        self,
        event_id,
        passport_id,
        student="S1",
        version=1,
        recipients=("EMP-1",),
        expires="2027-06-30T00:00:00+08:00",
    ):
        return make_event(
            event_id,
            "PASSPORT_ISSUED",
            "competency_passport",
            passport_id,
            {
                "student_id": student,
                "standard_id": STD,
                "standard_version": version,
                "recipient_scope": list(recipients),
                "expires_at": expires,
            },
        )

    def requirement_event(self, event_id, requirement_id, employer="EMP-1", units=("U1",)):
        return make_event(
            event_id,
            "JOB_REQUIREMENT_PUBLISHED",
            "job_requirement",
            requirement_id,
            {
                "employer_id": employer,
                "standard_id": STD,
                "required_units": list(units),
                "title": "运维技师",
            },
        )

    def appeal_event(self, event_id, passport_id, units, decision, adjusted=None):
        payload = {"student_id": "S1", "affected_units": list(units), "decision": decision}
        if adjusted is not None:
            payload["adjusted_scores"] = adjusted
        return make_event(event_id, "APPEAL_DECIDED", "competency_passport", passport_id, payload)

    def revoke_event(self, event_id, passport_id, employer="EMP-1"):
        return make_event(
            event_id,
            "ACCESS_REVOKED",
            "competency_passport",
            passport_id,
            {"student_id": "S1", "employer_id": employer, "reason": "学生撤回"},
        )

    def publish_standard(self, **kwargs):
        receipt = self.service.ingest(self.standard_event(**kwargs))
        self.assertEqual("accepted", receipt["status"], receipt)
        return receipt

    def issue_basic_passport(self, passport_id="P1", **kwargs):
        receipt = self.service.ingest(self.passport_event("EVT-ISSUE-1", passport_id, **kwargs))
        self.assertEqual("accepted", receipt["status"], receipt)
        return passport_id


class IngestTests(ServiceTestCase):
    def test_identical_resubmission_returns_original_receipt(self) -> None:
        self.publish_standard()
        event = self.evidence_event("EVT-E1", "E1", score=0.9)
        first = self.service.ingest(event)
        second = self.service.ingest(dict(event))
        self.assertEqual("accepted", first["status"])
        self.assertEqual(first, second)

        self.issue_basic_passport()
        passport = self.service.get_passport("P1")
        unit_u1 = next(unit for unit in passport["units"] if unit["unit_id"] == "U1")
        self.assertEqual(["E1"], unit_u1["evidence_ids"])

    def test_same_id_different_content_is_frozen(self) -> None:
        self.publish_standard()
        event = self.evidence_event("EVT-E1", "E1", score=0.9)
        first = self.service.ingest(event)
        tampered = self.evidence_event("EVT-E1", "E1", score=0.1)
        frozen = self.service.ingest(tampered)
        self.assertEqual("accepted", first["status"])
        self.assertEqual("frozen", frozen["status"])

        # 再次提交同一份异内容，得到同一张冻结回执。
        self.assertEqual(frozen, self.service.ingest(self.evidence_event("EVT-E1", "E1", score=0.1)))
        # 原始内容重传仍取得原回执。
        self.assertEqual(first, self.service.ingest(self.evidence_event("EVT-E1", "E1", score=0.9)))

        conflicts = self.service.list_conflicts()
        self.assertEqual(1, len(conflicts))
        self.assertEqual("EVT-E1", conflicts[0]["event_id"])

        # 冻结期间状态不被覆盖：仍按首次内容计权。
        self.issue_basic_passport()
        passport = self.service.get_passport("P1")
        unit_u1 = next(unit for unit in passport["units"] if unit["unit_id"] == "U1")
        self.assertAlmostEqual(0.9, unit_u1["score"])

    def test_contract_violation_is_rejected_and_not_applied(self) -> None:
        self.publish_standard()
        event = self.evidence_event("EVT-E2", "E2")
        del event["payload"]["observer_id"]
        receipt = self.service.ingest(event)
        self.assertEqual("rejected", receipt["status"])
        self.assertTrue(any(issue["field"] == "payload.observer_id" for issue in receipt["issues"]))

    def test_unauthorized_observer_and_unknown_unit_are_rejected(self) -> None:
        self.publish_standard()
        receipt = self.service.ingest(self.evidence_event("EVT-E3", "E3", observer="STRANGER"))
        self.assertEqual("rejected", receipt["status"])
        self.assertEqual("observer_not_authorized", receipt["code"])

        receipt = self.service.ingest(self.evidence_event("EVT-E4", "E4", units=("U9",)))
        self.assertEqual("rejected", receipt["status"])
        self.assertEqual("unknown_unit", receipt["code"])

    def test_aggregate_type_mismatch_is_rejected(self) -> None:
        event = self.standard_event()
        event["aggregate_type"] = "evidence_item"
        receipt = self.service.ingest(event)
        self.assertEqual("rejected", receipt["status"])
        self.assertEqual("aggregate_mismatch", receipt["code"])


class WeightingTests(ServiceTestCase):
    def test_one_performance_supports_multiple_units_counted_once(self) -> None:
        self.publish_standard()
        # 一次表现同时支撑两个能力单元。
        self.assertEqual(
            "accepted",
            self.service.ingest(self.evidence_event("EVT-E1", "E1", units=("U1", "U2"), score=0.8))["status"],
        )
        # 第二条证据只支撑 U1，拉低均分；若 E1 被重复计权，U1 均分将高于 0.6。
        self.assertEqual(
            "accepted",
            self.service.ingest(self.evidence_event("EVT-E2", "E2", units=("U1",), score=0.4))["status"],
        )
        self.issue_basic_passport()
        passport = self.service.get_passport("P1")
        units = {unit["unit_id"]: unit for unit in passport["units"]}
        self.assertEqual(["E1", "E2"], units["U1"]["evidence_ids"])
        self.assertAlmostEqual(0.6, units["U1"]["score"])
        self.assertEqual("met", units["U1"]["status"])
        self.assertEqual(["E1"], units["U2"]["evidence_ids"])
        self.assertAlmostEqual(0.8, units["U2"]["score"])
        self.assertEqual("met", units["U2"]["status"])


class IssuanceTests(ServiceTestCase):
    def test_concurrent_issuance_yields_single_valid_passport(self) -> None:
        self.publish_standard()
        self.service.ingest(self.evidence_event("EVT-E1", "E1", score=0.9))
        receipts = []
        threads = [
            threading.Thread(
                target=lambda index=index: receipts.append(
                    self.service.ingest(self.passport_event(f"EVT-ISSUE-{index}", f"P{index}"))
                )
            )
            for index in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        accepted = [receipt for receipt in receipts if receipt["status"] == "accepted"]
        conflicts = [receipt for receipt in receipts if receipt["status"] == "conflict"]
        self.assertEqual(1, len(accepted))
        self.assertEqual(3, len(conflicts))
        winner = accepted[0]["passport_id"]
        for receipt in conflicts:
            self.assertEqual(winner, receipt["passport_id"])
        self.assertEqual("valid", self.service.get_passport(winner)["status"])

    def test_duplicate_issuance_conflicts_until_previous_expires(self) -> None:
        self.publish_standard()
        self.service.ingest(self.evidence_event("EVT-E1", "E1", score=0.9))
        self.issue_basic_passport(expires="2026-10-01T10:00:00+08:00")

        duplicate = self.service.ingest(self.passport_event("EVT-ISSUE-2", "P2"))
        self.assertEqual("conflict", duplicate["status"])
        self.assertEqual("P1", duplicate["passport_id"])

        # 旧通行证到期后允许重新签发同一标准版本。
        self.clock.moment = datetime(2026, 10, 1, 11, 0, 0, tzinfo=CST)
        self.assertEqual(1, self.service.run_due_jobs())
        self.assertEqual("expired", self.service.get_passport("P1")["status"])
        reissue = self.service.ingest(self.passport_event("EVT-ISSUE-3", "P3"))
        self.assertEqual("accepted", reissue["status"])
        self.assertEqual("valid", self.service.get_passport("P3")["status"])


class RevisionAndAppealTests(ServiceTestCase):
    def _prepare_passport_with_snapshot(self):
        self.publish_standard()
        self.service.ingest(self.evidence_event("EVT-E1", "E1", units=("U1",), score=0.9))
        self.service.ingest(self.evidence_event("EVT-E2", "E2", units=("U2",), score=0.9))
        self.issue_basic_passport()
        self.service.ingest(self.requirement_event("EVT-REQ-1", "REQ-1", units=("U1", "U2")))
        snapshot_id = self.service.record_hiring_snapshot("EMP-1", "P1", "REQ-1")
        return snapshot_id

    def test_standard_revision_recomputes_only_affected_units(self) -> None:
        self._prepare_passport_with_snapshot()
        before = {unit["unit_id"]: unit for unit in self.service.get_passport("P1")["units"]}

        self.clock.moment = datetime(2026, 10, 2, 9, 0, 0, tzinfo=CST)
        receipt = self.service.ingest(
            self.standard_event(
                event_id="EVT-STD-2",
                version=2,
                units=[
                    {"unit_id": "U1", "title": "设备排障", "pass_score": 0.6},
                    {"unit_id": "U2", "title": "英文资料使用", "pass_score": 0.95},
                ],
            )
        )
        self.assertEqual("accepted", receipt["status"], receipt)

        after = {unit["unit_id"]: unit for unit in self.service.get_passport("P1")["units"]}
        # 未受影响的 U1 完全保持原样。
        self.assertEqual(before["U1"], after["U1"])
        # 受影响的 U2 按新版本阈值重算。
        self.assertEqual("not_met", after["U2"]["status"])
        self.assertEqual(2, after["U2"]["computed_under_version"])
        self.assertNotEqual(before["U2"]["updated_at"], after["U2"]["updated_at"])

    def test_revision_removing_unit_invalidates_it(self) -> None:
        self._prepare_passport_with_snapshot()
        self.clock.moment = datetime(2026, 10, 2, 9, 0, 0, tzinfo=CST)
        self.service.ingest(
            self.standard_event(
                event_id="EVT-STD-2",
                version=2,
                units=[{"unit_id": "U1", "title": "设备排障", "pass_score": 0.6}],
            )
        )
        units = {unit["unit_id"]: unit for unit in self.service.get_passport("P1")["units"]}
        self.assertEqual("invalidated", units["U2"]["status"])
        self.assertIsNone(units["U2"]["score"])
        # 证据标识保留，学生仍可追溯来源。
        self.assertEqual(["E2"], units["U2"]["evidence_ids"])

    def test_hiring_snapshot_kept_with_diff_notes(self) -> None:
        snapshot_id = self._prepare_passport_with_snapshot()
        original = self.service.get_snapshot(snapshot_id)["content"]

        # 标准改版：快照内容不变，追加差异说明。
        self.clock.moment = datetime(2026, 10, 2, 9, 0, 0, tzinfo=CST)
        self.service.ingest(
            self.standard_event(
                event_id="EVT-STD-2",
                version=2,
                units=[
                    {"unit_id": "U1", "title": "设备排障", "pass_score": 0.6},
                    {"unit_id": "U2", "title": "英文资料使用", "pass_score": 0.95},
                ],
            )
        )
        snapshot = self.service.get_snapshot(snapshot_id)
        self.assertEqual(original, snapshot["content"])
        self.assertEqual(1, len(snapshot["notes"]))
        self.assertIn("v1→v2", snapshot["notes"][0])
        self.assertIn("U2", snapshot["notes"][0])

        # 申诉改判：同样只追加说明，并通过持久任务传播。
        self.service.ingest(self.appeal_event("EVT-APPEAL-1", "P1", ("U2",), "upheld", {"U2": 0.97}))
        self.assertEqual(1, self.service.run_due_jobs())
        snapshot = self.service.get_snapshot(snapshot_id)
        self.assertEqual(original, snapshot["content"])
        self.assertEqual(2, len(snapshot["notes"]))
        self.assertIn("申诉成立", snapshot["notes"][1])
        self.assertIn("U2", snapshot["notes"][1])

        unit_u2 = next(unit for unit in self.service.get_passport("P1")["units"] if unit["unit_id"] == "U2")
        self.assertEqual("met", unit_u2["status"])
        self.assertAlmostEqual(0.97, unit_u2["score"])

    def test_appeal_rejected_keeps_conclusions(self) -> None:
        snapshot_id = self._prepare_passport_with_snapshot()
        receipt = self.service.ingest(self.appeal_event("EVT-APPEAL-1", "P1", ("U2",), "rejected"))
        self.assertEqual("accepted", receipt["status"])
        self.assertEqual(1, self.service.run_due_jobs())
        snapshot = self.service.get_snapshot(snapshot_id)
        self.assertIn("申诉驳回", snapshot["notes"][0])
        self.assertIn("结论不变", snapshot["notes"][0])
        unit_u2 = next(unit for unit in self.service.get_passport("P1")["units"] if unit["unit_id"] == "U2")
        self.assertEqual("met", unit_u2["status"])

    def test_appeal_on_unknown_passport_is_rejected(self) -> None:
        receipt = self.service.ingest(self.appeal_event("EVT-APPEAL-9", "P9", ("U1",), "upheld", {"U1": 0.9}))
        self.assertEqual("rejected", receipt["status"])
        self.assertEqual("unknown_passport", receipt["code"])


class RevocationTests(ServiceTestCase):
    def test_revocation_blocks_employer_but_retains_facts(self) -> None:
        self.publish_standard()
        self.service.ingest(self.evidence_event("EVT-E1", "E1", units=("U1", "U2"), score=0.9))
        self.issue_basic_passport()
        self.service.ingest(self.requirement_event("EVT-REQ-1", "REQ-1", units=("U1",)))
        snapshot_id = self.service.record_hiring_snapshot("EMP-1", "P1", "REQ-1")

        receipt = self.service.ingest(self.revoke_event("EVT-REVOKE-1", "P1"))
        self.assertEqual("accepted", receipt["status"])

        # 企业立即不可见。
        with self.assertRaises(AccessDeniedError):
            self.service.employer_view("EMP-1", "P1", "REQ-1")
        # 依法保留的考核事实不删除：证据、成绩、快照均可查。
        passport = self.service.get_passport("P1")
        self.assertEqual("valid", passport["status"])
        self.assertEqual(["E1"], passport["units"][0]["evidence_ids"])
        self.assertEqual([], self.service.get_snapshot(snapshot_id)["notes"])
        student = self.service.student_view("S1")
        self.assertEqual(1, len(student["passports"]))

        # 撤权传播通过持久任务完成。
        self.assertEqual(1, self.service.run_due_jobs())
        log = self.service.list_propagation()
        self.assertEqual("revocation_propagated", log[0]["kind"])
        self.assertEqual("EMP-1", log[0]["detail"]["employer_id"])

    def test_revoking_unknown_permit_is_rejected(self) -> None:
        self.publish_standard()
        self.issue_basic_passport()
        receipt = self.service.ingest(self.revoke_event("EVT-REVOKE-9", "P1", employer="EMP-9"))
        self.assertEqual("rejected", receipt["status"])
        self.assertEqual("permit_not_found", receipt["code"])


class ExpiryAndRecoveryTests(ServiceTestCase):
    def test_expired_permit_denies_employer_view(self) -> None:
        self.publish_standard()
        self.service.ingest(self.evidence_event("EVT-E1", "E1", score=0.9))
        self.issue_basic_passport(expires="2026-10-01T10:00:00+08:00")
        self.service.ingest(self.requirement_event("EVT-REQ-1", "REQ-1"))

        self.clock.moment = datetime(2026, 10, 1, 11, 0, 0, tzinfo=CST)
        with self.assertRaises(AccessDeniedError):
            self.service.employer_view("EMP-1", "P1", "REQ-1")
        self.assertEqual(1, self.service.run_due_jobs())
        self.assertEqual("expired", self.service.get_passport("P1")["status"])

    def test_restart_recovery_processes_expiry_appeal_and_revocation(self) -> None:
        self.publish_standard()
        self.service.ingest(self.evidence_event("EVT-E1", "E1", units=("U1", "U2"), score=0.9))
        self.issue_basic_passport(expires="2026-10-01T10:00:00+08:00")
        self.service.ingest(self.requirement_event("EVT-REQ-1", "REQ-1", units=("U1", "U2")))
        snapshot_id = self.service.record_hiring_snapshot("EMP-1", "P1", "REQ-1")
        self.service.ingest(self.appeal_event("EVT-APPEAL-1", "P1", ("U2",), "upheld", {"U2": 0.95}))
        self.service.ingest(self.revoke_event("EVT-REVOKE-1", "P1"))
        self.service.close()

        # 服务重启：新实例接管同一数据库，继续处理到期、申诉与撤权传播。
        later = FakeClock(datetime(2026, 10, 1, 11, 0, 0, tzinfo=CST))
        reopened = PassportService(self.db, clock=later)
        self.addCleanup(reopened.close)
        self.assertEqual({"processed": 3}, reopened.recover())

        self.assertEqual("expired", reopened.get_passport("P1")["status"])
        notes = reopened.get_snapshot(snapshot_id)["notes"]
        self.assertEqual(1, len(notes))
        self.assertIn("申诉成立", notes[0])
        kinds = [entry["kind"] for entry in reopened.list_propagation()]
        self.assertIn("revocation_propagated", kinds)
        self.assertIn("appeal_propagated", kinds)
        with self.assertRaises(AccessDeniedError):
            reopened.employer_view("EMP-1", "P1", "REQ-1")
        # 考核事实仍在。
        self.assertEqual(1, len(reopened.student_view("S1")["passports"]))


class ViewTests(ServiceTestCase):
    def _prepare(self):
        self.publish_standard()
        self.service.ingest(self.evidence_event("EVT-E1", "E1", task="TASK-A", units=("U1", "U2"), score=0.9))
        self.issue_basic_passport(recipients=("EMP-1", "EMP-2"))
        self.service.ingest(self.requirement_event("EVT-REQ-1", "REQ-1", employer="EMP-1", units=("U1",)))

    def test_employer_view_returns_only_job_required_conclusions(self) -> None:
        self._prepare()
        view = self.service.employer_view("EMP-1", "P1", "REQ-1")
        self.assertEqual(["U1"], [item["unit_id"] for item in view["conclusions"]])
        conclusion = view["conclusions"][0]
        self.assertEqual({"unit_id", "status", "score", "standard_version"}, set(conclusion))
        self.assertEqual("met", conclusion["status"])

    def test_employer_without_permit_or_foreign_requirement_is_denied(self) -> None:
        self._prepare()
        # EMP-3 不在共享范围内。
        with self.assertRaises(AccessDeniedError):
            self.service.employer_view("EMP-3", "P1", "REQ-1")
        # REQ-1 不属于 EMP-2，即使 EMP-2 持有许可也不能越权使用。
        with self.assertRaises(AccessDeniedError):
            self.service.employer_view("EMP-2", "P1", "REQ-1")

    def test_employer_view_rejects_unknown_passport(self) -> None:
        self._prepare()
        with self.assertRaises(ServiceError):
            self.service.employer_view("EMP-1", "P9", "REQ-1")

    def test_student_view_traces_lineage_and_validity(self) -> None:
        self._prepare()
        view = self.service.student_view("S1")
        self.assertEqual("S1", view["student_id"])
        passport = view["passports"][0]
        self.assertEqual("P1", passport["passport_id"])
        units = {unit["unit_id"]: unit for unit in passport["units"]}
        u1 = units["U1"]
        self.assertEqual([{"evidence_id": "E1", "task_id": "TASK-A", "observer_id": "T001",
                           "occurred_at": "2026-10-01T09:00:00+08:00"}], u1["sources"])
        self.assertIn("T001", u1["validity"])
        self.assertIn("TASK-A", u1["validity"])
        self.assertIn("标准版本 v1", u1["validity"])
        self.assertIn("只计一次", u1["validity"])

        # 申诉改判后，学生能看到改判原因。
        self.service.ingest(self.appeal_event("EVT-APPEAL-1", "P1", ("U2",), "upheld", {"U2": 0.99}))
        view = self.service.student_view("S1")
        units = {unit["unit_id"]: unit for unit in view["passports"][0]["units"]}
        self.assertIn("申诉已改判", units["U2"]["validity"])
        self.assertNotIn("申诉已改判", units["U1"]["validity"])


if __name__ == "__main__":
    unittest.main()

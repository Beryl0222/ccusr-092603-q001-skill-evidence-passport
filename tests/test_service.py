from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from skill_evidence_passport import (
    AccessDeniedError,
    DomainError,
    FrozenConflictError,
    PassportService,
)

CST = timezone(timedelta(hours= 8))


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


UNITS_V1 = [
    {"unit_id": "unit-test", "spec_hash": "spec-test-v1"},
    {"unit_id": "unit-troubleshoot", "spec_hash": "spec-tb-v1"},
    {"unit_id": "unit-english-docs", "spec_hash": "spec-en-v1"},
    {"unit_id": "unit-safety", "spec_hash": "spec-safe-v1"},
]


class ServiceFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(datetime(2026, 10, 1, 9, 0, tzinfo=CST))
        self.svc = PassportService(self.tmp.name, clock=self.clock)
        self.svc.publish_standard("evt-std-v1", "wsi-mech", 1, UNITS_V1)
        self.svc.register_job_requirement(
            "evt-job-1", "job-maintenance", "employer-haite", "wsi-mech",
            ["unit-troubleshoot", "unit-safety"],
        )

    def close(self) -> None:
        self.tmp.cleanup()


class PassportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = ServiceFixture()
        self.svc = self.fx.svc
        self.clock = self.fx.clock

    def tearDown(self) -> None:
        self.fx.close()

    # ------------------------------------------------------------ 证据计权

    def test_one_performance_supports_many_units_but_counts_once(self) -> None:
        self.svc.accept_evidence(
            "evt-ev-1", "ev-task-42", "wsi-mech", 1, "stu-7", "task-42",
            "coach-li", ["unit-troubleshoot", "unit-english-docs"],
            {"role": "judge", "authorizer_id": "ref-zhang"},
        )
        self.svc.issue_passport("evt-pp-1", "stu-7", "wsi-mech")
        trace = self.svc.student_trace("stu-7", "wsi-mech")
        by_unit = {u["unit_id"]: u for u in trace["units"]}
        self.assertEqual("met", by_unit["unit-troubleshoot"]["status"])
        self.assertEqual("met", by_unit["unit-english-docs"]["status"])
        # 同一证据出现在两个单元，但每项能力下只有一条来源，未重复计权
        self.assertEqual(1, len(by_unit["unit-troubleshoot"]["provenance"]))
        self.assertEqual(1, len(by_unit["unit-english-docs"]["provenance"]))

    def test_same_task_performance_cannot_be_counted_twice(self) -> None:
        kwargs = dict(
            standard_id="wsi-mech", standard_version=1, student_id="stu-7",
            task_id="task-42", observer_id="coach-li",
            supports_units=["unit-troubleshoot"],
            authorization={"role": "judge", "authorizer_id": "ref-zhang"},
        )
        self.svc.accept_evidence("evt-ev-1", "ev-a", **kwargs)
        with self.assertRaises(DomainError) as cm:
            self.svc.accept_evidence("evt-ev-2", "ev-b", **kwargs)
        self.assertEqual("evidence_already_counted", cm.exception.code)

    def test_evidence_requires_judge_or_teacher_authorization(self) -> None:
        with self.assertRaises(DomainError) as cm:
            self.svc.accept_evidence(
                "evt-ev-x", "ev-x", "wsi-mech", 1, "stu-7", "task-x",
                "coach-li", ["unit-safety"], {"role": "coach", "authorizer_id": "coach-li"},
            )
        self.assertEqual("authorization_required", cm.exception.code)

    # ------------------------------------------------------------ 签发唯一

    def test_only_one_valid_passport_per_standard(self) -> None:
        self.svc.issue_passport("evt-pp-1", "stu-7", "wsi-mech")
        with self.assertRaises(DomainError) as cm:
            self.svc.issue_passport("evt-pp-2", "stu-7", "wsi-mech")
        self.assertEqual("passport_exists", cm.exception.code)

    def test_concurrent_issuance_serializes_to_single_passport(self) -> None:
        errors: list[DomainError] = []

        def issue(eid: str) -> None:
            try:
                self.svc.issue_passport(eid, "stu-9", "wsi-mech")
            except DomainError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=issue, args=(f"evt-pp-c{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        passports = [p for p in self.svc.passports.values() if p["student_id"] == "stu-9"]
        self.assertEqual(1, len(passports))
        self.assertEqual(7, len(errors))
        self.assertTrue(all(e.code == "passport_exists" for e in errors))

    # ------------------------------------------------------------ 幂等/冻结

    def test_identical_resubmission_returns_original_receipt(self) -> None:
        first = self.svc.accept_evidence(
            "evt-ev-same", "ev-same", "wsi-mech", 1, "stu-7", "task-same",
            "coach-li", ["unit-safety"],
            {"role": "teacher", "authorizer_id": "teacher-wang"},
        )
        self.clock.advance(hours=2)  # 服务端时间变化不影响业务指纹
        second = self.svc.accept_evidence(
            "evt-ev-same", "ev-same", "wsi-mech", 1, "stu-7", "task-same",
            "coach-li", ["unit-safety"],
            {"role": "teacher", "authorizer_id": "teacher-wang"},
        )
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(first.recorded_at, second.recorded_at)

    def test_same_id_different_content_freezes_for_manual_check(self) -> None:
        self.svc.accept_evidence(
            "evt-ev-clash", "ev-clash", "wsi-mech", 1, "stu-7", "task-a",
            "coach-li", ["unit-safety"],
            {"role": "teacher", "authorizer_id": "teacher-wang"},
        )
        with self.assertRaises(FrozenConflictError) as cm:
            self.svc.accept_evidence(
                "evt-ev-clash", "ev-clash", "wsi-mech", 1, "stu-7", "task-b",
                "coach-li", ["unit-safety"],
                {"role": "teacher", "authorizer_id": "teacher-wang"},
            )
        self.assertEqual("record_frozen", cm.exception.code)
        frozen = self.svc.frozen_records()
        self.assertIn("evt-ev-clash", frozen)
        # 冻结后任何同号提交都继续被拦下
        with self.assertRaises(FrozenConflictError):
            self.svc.accept_evidence(
                "evt-ev-clash", "ev-clash", "wsi-mech", 1, "stu-7", "task-a",
                "coach-li", ["unit-safety"],
                {"role": "teacher", "authorizer_id": "teacher-wang"},
            )

    # ------------------------------------------------------------ 共享与视图

    def _passport_with_sharing(self, expires: timedelta = timedelta(days=30)) -> str:
        self.svc.accept_evidence(
            "evt-ev-1", "ev-task-42", "wsi-mech", 1, "stu-7", "task-42",
            "coach-li", ["unit-troubleshoot", "unit-safety"],
            {"role": "judge", "authorizer_id": "ref-zhang"},
        )
        self.svc.issue_passport("evt-pp-1", "stu-7", "wsi-mech")
        pid = "passport:stu-7:wsi-mech"
        self.svc.grant_sharing(
            "evt-share-1", pid, "employer-haite", "job-maintenance",
            (self.clock.now + expires).isoformat(),
        )
        return pid

    def test_employer_sees_only_job_conclusions(self) -> None:
        pid = self._passport_with_sharing()
        view = self.svc.employer_view(pid, "employer-haite", "job-maintenance")
        self.assertEqual(
            {"unit-troubleshoot", "unit-safety"}, set(view["conclusions"])
        )  # 不含岗位外的单元
        entry = view["conclusions"]["unit-safety"]
        self.assertEqual({"status", "evidence_count"}, set(entry))  # 不含任务/裁判等细节
        serialized = repr(view)
        self.assertNotIn("coach-li", serialized)
        self.assertNotIn("task-42", serialized)

    def test_employer_without_sharing_is_denied(self) -> None:
        pid = self._passport_with_sharing()
        with self.assertRaises(AccessDeniedError):
            self.svc.employer_view(pid, "employer-other", "job-maintenance")

    def test_revocation_propagates_but_keeps_assessment_facts(self) -> None:
        pid = self._passport_with_sharing()
        self.svc.revoke_access("evt-revoke-1", pid, "employer-haite", "学生撤回授权")
        with self.assertRaises(AccessDeniedError):
            self.svc.employer_view(pid, "employer-haite", "job-maintenance")
        # 考核事实仍依法保留：学生追溯、事件日志、证据都还在
        trace = self.svc.student_trace("stu-7", "wsi-mech")
        self.assertTrue(any(u["provenance"] for u in trace["units"]))
        ledger = {s["recipient_id"]: s for s in trace["sharing_ledger"]}
        self.assertEqual("revoked", ledger["employer-haite"]["status"])
        self.assertIn("ev-task-42", self.svc.evidence)

    def test_expiry_propagates_via_pump_and_is_idempotent(self) -> None:
        pid = self._passport_with_sharing(expires=timedelta(days=1))
        self.assertEqual([], self.svc.pump())
        self.clock.advance(days=2)
        emitted = self.svc.pump()
        self.assertEqual(1, len(emitted))
        with self.assertRaises(AccessDeniedError):
            self.svc.employer_view(pid, "employer-haite", "job-maintenance")
        # 再跑一次不补发
        self.assertEqual([], self.svc.pump())

    # ------------------------------------------------------------ 改版与快照

    def test_standard_revision_only_recomputes_affected_units(self) -> None:
        pid = self._passport_with_sharing()
        snap_receipt = self.svc.take_hiring_snapshot(
            "evt-snap-1", "snap-1", pid, "employer-haite", "job-maintenance"
        )
        self.assertEqual("snap-1", snap_receipt.refs["snapshot_id"])
        before = self.svc.hiring_snapshot_record("snap-1")
        self.assertEqual("met", before["conclusions"]["unit-safety"]["status"])

        # v2：只改安全操作单元；排障单元规格不变
        units_v2 = [
            {"unit_id": "unit-test", "spec_hash": "spec-test-v1"},
            {"unit_id": "unit-troubleshoot", "spec_hash": "spec-tb-v1"},
            {"unit_id": "unit-english-docs", "spec_hash": "spec-en-v1"},
            {"unit_id": "unit-safety", "spec_hash": "spec-safe-v2"},
        ]
        self.svc.publish_standard("evt-std-v2", "wsi-mech", 2, units_v2)

        after = self.svc.hiring_snapshot_record("snap-1")
        # 原结论保持原样
        self.assertEqual("met", after["conclusions"]["unit-safety"]["status"])
        self.assertEqual(1, len(after["diffs"]))
        change = {c["unit_id"]: c for c in after["diffs"][0]["changes"]}
        self.assertEqual(["unit-safety"], list(change))  # 只重算受影响单元
        self.assertEqual("met", change["unit-safety"]["before"]["status"])
        self.assertEqual("not_met", change["unit-safety"]["after"]["status"])

        # 未改版的排障单元结论与证据仍然有效，且学生能看到有效性说明
        trace = self.svc.student_trace("stu-7", "wsi-mech")
        by_unit = {u["unit_id"]: u for u in trace["units"]}
        self.assertEqual(2, trace["current_version"])
        self.assertEqual("met", by_unit["unit-troubleshoot"]["status"])
        self.assertIn("继续有效", by_unit["unit-troubleshoot"]["provenance"][0]["validity_reason"])
        self.assertEqual("not_met", by_unit["unit-safety"]["status"])

        # 快照之后企业当前接口看到的是新结论；快照本身可用于解释录用决定
        view = self.svc.employer_view(pid, "employer-haite", "job-maintenance")
        self.assertEqual("not_met", view["conclusions"]["unit-safety"]["status"])
        self.assertEqual("met", view["conclusions"]["unit-troubleshoot"]["status"])

    def test_snapshot_resubmission_after_revision_still_returns_original_receipt(self) -> None:
        pid = self._passport_with_sharing()
        self.svc.take_hiring_snapshot(
            "evt-snap-r", "snap-r", pid, "employer-haite", "job-maintenance"
        )
        units_v2 = [dict(u) for u in UNITS_V1 if u["unit_id"] != "unit-test"]
        self.svc.publish_standard("evt-std-v2b", "wsi-mech", 2, units_v2)
        # 学校用原事件号补传完全相同的快照命令：仍取原回执，不产生第二份快照
        again = self.svc.take_hiring_snapshot(
            "evt-snap-r", "snap-r", pid, "employer-haite", "job-maintenance"
        )
        self.assertEqual("evt-snap-r", again.event_id)
        trace = self.svc.student_trace("stu-7", "wsi-mech")
        self.assertEqual(1, len(trace["snapshot_ledger"]))

    # ------------------------------------------------------------ 申诉

    def test_appeal_upheld_recomputes_affected_units_and_appends_diff(self) -> None:
        # stu-8 只有排障证据，岗位还要求安全操作 → 安全 initially not_met
        self.svc.accept_evidence(
            "evt-ev-8", "ev-8", "wsi-mech", 1, "stu-8", "task-8",
            "coach-li", ["unit-troubleshoot"],
            {"role": "judge", "authorizer_id": "ref-zhang"},
        )
        self.svc.issue_passport("evt-pp-8", "stu-8", "wsi-mech")
        pid8 = "passport:stu-8:wsi-mech"
        self.svc.grant_sharing(
            "evt-share-8", pid8, "employer-haite", "job-maintenance",
            (self.clock.now + timedelta(days=30)).isoformat(),
        )
        self.svc.take_hiring_snapshot("evt-snap-8", "snap-8", pid8, "employer-haite", "job-maintenance")
        self.assertEqual(
            "not_met",
            self.svc.hiring_snapshot_record("snap-8")["conclusions"]["unit-safety"]["status"],
        )
        self.svc.file_appeal(
            "evt-appeal-8", "appeal-8", pid8, ["unit-safety"], "安全操作记录漏登"
        )
        self.svc.decide_appeal(
            "evt-appeal-decide-8", "appeal-8", "upheld",
            resolution={"unit-safety": "met"}, note="补核监控记录后确认达标",
        )
        snap = self.svc.hiring_snapshot_record("snap-8")
        self.assertEqual("not_met", snap["conclusions"]["unit-safety"]["status"])  # 原结论不变
        self.assertEqual("met", snap["diffs"][0]["changes"][0]["after"]["status"])
        view = self.svc.employer_view(pid8, "employer-haite", "job-maintenance")
        self.assertEqual("met", view["conclusions"]["unit-safety"]["status"])
        # 申诉成立不能越权重判未申诉单元
        self.svc.file_appeal("evt-appeal-9", "appeal-9", pid8, ["unit-safety"], "再次申诉")
        with self.assertRaises(DomainError) as cm:
            self.svc.decide_appeal(
                "evt-appeal-decide-9", "appeal-9", "upheld",
                resolution={"unit-troubleshoot": "not_met"},
            )
        self.assertEqual("resolution_scope", cm.exception.code)

    def test_appeal_correction_is_superseded_by_later_unit_revision(self) -> None:
        self.svc.accept_evidence(
            "evt-ev-8", "ev-8", "wsi-mech", 1, "stu-8", "task-8",
            "coach-li", ["unit-troubleshoot"],
            {"role": "judge", "authorizer_id": "ref-zhang"},
        )
        self.svc.issue_passport("evt-pp-8", "stu-8", "wsi-mech")
        pid8 = "passport:stu-8:wsi-mech"
        self.svc.grant_sharing(
            "evt-share-8", pid8, "employer-haite", "job-maintenance",
            (self.clock.now + timedelta(days=300)).isoformat(),
        )
        self.svc.file_appeal("evt-ap", "appeal", pid8, ["unit-safety"], "漏登")
        self.svc.decide_appeal(
            "evt-ap-dec", "appeal", "upheld", resolution={"unit-safety": "met"}
        )
        self.assertEqual("met", self.svc.employer_view(pid8, "employer-haite", "job-maintenance")
                         ["conclusions"]["unit-safety"]["status"])
        # v2 再次调整安全单元：锚定在 v1 的更正失效，回到证据结论
        units_v2 = [
            {"unit_id": "unit-test", "spec_hash": "spec-test-v1"},
            {"unit_id": "unit-troubleshoot", "spec_hash": "spec-tb-v1"},
            {"unit_id": "unit-english-docs", "spec_hash": "spec-en-v1"},
            {"unit_id": "unit-safety", "spec_hash": "spec-safe-v3"},
        ]
        self.svc.publish_standard("evt-std-v2c", "wsi-mech", 2, units_v2)
        self.assertEqual("not_met", self.svc.employer_view(pid8, "employer-haite", "job-maintenance")
                         ["conclusions"]["unit-safety"]["status"])

    # ------------------------------------------------------------ 重启恢复

    def test_restart_resumes_state_and_pending_propagation(self) -> None:
        pid = self._passport_with_sharing(expires=timedelta(days=1))
        self.svc.take_hiring_snapshot(
            "evt-snap-1", "snap-1", pid, "employer-haite", "job-maintenance"
        )
        self.clock.advance(days=2)

        # 模拟服务重启：新实例重放事件日志，时钟继续
        restarted = PassportService(self.fx.tmp.name, clock=self.clock)
        with self.assertRaises(AccessDeniedError):
            restarted.employer_view(pid, "employer-haite", "job-maintenance")
        emitted = restarted.pump()
        self.assertEqual(1, len(emitted))
        self.assertEqual([], restarted.pump())

        # 重放后状态完整：快照、证据、共享台账都在
        snap = restarted.hiring_snapshot_record("snap-1")
        self.assertEqual("met", snap["conclusions"]["unit-troubleshoot"]["status"])
        trace = restarted.student_trace("stu-7", "wsi-mech")
        self.assertEqual("expired", trace["sharing_ledger"][0]["status"])

    def test_restart_preserves_frozen_and_receipts(self) -> None:
        self.svc.accept_evidence(
            "evt-ev-frz", "ev-frz", "wsi-mech", 1, "stu-7", "task-f",
            "coach-li", ["unit-safety"],
            {"role": "teacher", "authorizer_id": "teacher-wang"},
        )
        with self.assertRaises(FrozenConflictError):
            self.svc.accept_evidence(
                "evt-ev-frz", "ev-frz", "wsi-mech", 1, "stu-7", "task-g",
                "coach-li", ["unit-safety"],
                {"role": "teacher", "authorizer_id": "teacher-wang"},
            )
        restarted = PassportService(self.fx.tmp.name, clock=self.clock)
        self.assertIn("evt-ev-frz", restarted.frozen_records())
        with self.assertRaises(FrozenConflictError):
            restarted.accept_evidence(
                "evt-ev-frz", "ev-frz", "wsi-mech", 1, "stu-7", "task-f",
                "coach-li", ["unit-safety"],
                {"role": "teacher", "authorizer_id": "teacher-wang"},
            )


if __name__ == "__main__":
    unittest.main()

import json
import unittest

from pcb_dev.reports import CheckReport, Issue, Severity, Waiver, gate_reports


HASH = "a" * 64


class ReportTests(unittest.TestCase):
    def test_findings_are_deterministic_and_input_details_are_copied(self):
        details = {"nested": [1, {"x": 2}]}
        issue = Issue("voltage.test", Severity.ERROR, "Out of tolerance", "board.mcu", details)
        fingerprint = issue.fingerprint
        details["nested"][1]["x"] = 100
        self.assertEqual(issue.fingerprint, fingerprint)
        other = Issue("voltage.test", "error", "Out of tolerance", "board.mcu", {"nested": [1, {"x": 2}]})
        self.assertEqual(issue.fingerprint, other.fingerprint)
        report = CheckReport("preflight", [issue], {"completed": True, "design_hash": HASH})
        self.assertTrue(report.blocking)
        self.assertFalse(report.passed)
        self.assertEqual(json.loads(report.to_json()), report.to_dict())

    def test_warning_does_not_block_but_incomplete_report_does(self):
        warning = Issue("optional", "warning", "Review this")
        self.assertTrue(CheckReport("planning", [warning]).passed)
        self.assertTrue(CheckReport("erc", metadata={"completed": False}).blocking)

    def test_waiver_requires_explicit_human_approval_and_exact_design(self):
        issue = Issue("constraint.failed", "error", "Over budget")
        report = CheckReport("preflight", [issue], {"completed": True, "design_hash": HASH})
        common = dict(issue_fingerprint=issue.fingerprint, design_hash=HASH, actor="Engineer Alice", reason="Approved prototype deviation")
        self.assertTrue(report.apply_waivers([Waiver(**common)]).blocking)
        approved = Waiver(**common, human_approved=True)
        audited = report.apply_waivers([approved])
        self.assertFalse(audited.blocking)
        self.assertTrue(audited.to_dict()["issues"][0]["waived"])
        self.assertEqual(audited.to_dict()["waivers"][0]["actor"], "Engineer Alice")
        self.assertTrue(report.blocking, "applying a waiver must not mutate its source report")
        stale = Waiver(**{**common, "design_hash": "b" * 64}, human_approved=True)
        self.assertTrue(report.apply_waivers([stale]).blocking)
        modified = Issue("constraint.failed", "error", "Over budget", details={"changed": True})
        self.assertTrue(CheckReport("preflight", [modified], {"design_hash": HASH}).apply_waivers([approved]).blocking)

    def test_report_cannot_be_rebound_to_a_different_design(self):
        report = CheckReport("preflight", metadata={"design_hash": HASH})
        with self.assertRaises(ValueError):
            report.apply_waivers([], "c" * 64)

    def test_invalid_waivers_and_nonfinite_payloads_are_rejected(self):
        base = dict(issue_fingerprint=HASH, design_hash=HASH, actor="Alice", reason="Review")
        for changed in ({"actor": ""}, {"reason": " "}, {"issue_fingerprint": "*"},
                        {"human_approved": 1}, {"created_at": "2026-10-15T12:00:00"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                Waiver(**{**base, **changed})
        with self.assertRaises(ValueError):
            Waiver.from_dict({**base, "wildcard": True})
        with self.assertRaises(ValueError):
            Issue("nan", "error", "Invalid", details={"nan": float("nan")})

    def test_missing_native_checks_never_pass_and_cannot_be_waived(self):
        preflight = CheckReport("preflight", metadata={"completed": True, "design_hash": HASH})
        gate = gate_reports([preflight], design_hash=HASH)
        self.assertTrue(gate.blocking)
        self.assertEqual(sum(issue.code == "gate.missing_stage" for issue in gate.issues), 2)
        waivers = [Waiver(issue.fingerprint, HASH, "Alice", "Prototype only", human_approved=True) for issue in gate.issues]
        self.assertTrue(gate.apply_waivers(waivers).blocking)

    def test_native_gate_requires_completion_and_matching_hash(self):
        def clean(stage):
            return CheckReport(stage, metadata={"completed": True, "native": True, "design_hash": HASH})
        reports = [clean(stage) for stage in ("preflight", "erc", "drc")]
        self.assertTrue(gate_reports(reports, design_hash=HASH).passed)
        reports[1].metadata["native"] = False
        reports[2].metadata["completed"] = False
        reports[0].metadata["design_hash"] = "b" * 64
        gate = gate_reports(reports, design_hash=HASH)
        self.assertTrue(gate.blocking)
        self.assertTrue({"gate.not_native", "gate.incomplete_stage", "gate.stale_report"}.issubset({issue.code for issue in gate.issues}))

    def test_duplicate_reports_and_failed_optional_checks_block(self):
        good = CheckReport("preflight", metadata={"completed": True})
        gate = gate_reports([good, good], required_stages=("preflight",))
        self.assertIn("gate.duplicate_stage", [issue.code for issue in gate.issues])
        failed = CheckReport("manufacturing", [Issue("missing", "error", "Missing footprints")])
        self.assertTrue(gate_reports([good, failed], required_stages=("preflight",)).blocking)

    def test_markdown_preserves_audit_and_escapes_table_breaks(self):
        issue = Issue("test", "error", "pipe|and\nnewline", "board")
        report = CheckReport("preflight", [issue], {"design_hash": HASH})
        waiver = Waiver(issue.fingerprint, HASH, "Alice", "Reason|yes", human_approved=True)
        rendered = report.apply_waivers([waiver]).to_markdown()
        self.assertIn("# preflight: PASS", rendered)
        self.assertIn("pipe\\|and<br>newline", rendered)
        self.assertIn("Waiver audit", rendered)
        self.assertIn("Reason\\|yes", rendered)
        self.assertIn(issue.fingerprint, rendered)

    def test_markdown_escapes_raw_html_in_finding_text(self):
        rendered = CheckReport("preflight", [Issue("test", "warning", '<img src="https://untrusted.example/image">')]).to_markdown()
        self.assertNotIn("<img", rendered)
        self.assertIn("&lt;img", rendered)


if __name__ == "__main__":
    unittest.main()

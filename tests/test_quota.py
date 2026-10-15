from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from pcb_dev.quota import QuotaError, TokenLedger


class TokenLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = TokenLedger(self.root / "project")

    def tearDown(self):
        self.temporary.cleanup()

    def credit(self, tokens=900):
        self.ledger.record_qwen(tokens, 0, usage_reported=True)

    def test_empty_project_has_no_claude_credit(self):
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot["qwen_tokens"], 0)
        self.assertEqual(snapshot["claude_available_tokens"], 0)
        with self.assertRaises(QuotaError):
            self.ledger.reserve_claude(1, human_authorized=True)

    def test_missing_qwen_usage_counts_unknown_call_but_never_credits_estimates(self):
        self.ledger.record_qwen(1_000_000, 50_000, usage_reported=False)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot["unknown_qwen_calls"], 1)
        self.assertEqual(snapshot["qwen_calls"], 1)
        self.assertEqual(snapshot["qwen_tokens"], 0)

    def test_reported_qwen_usage_requires_real_nonnegative_integers(self):
        for value in (True, -1, 1.5, "9", None, 10**19):
            with self.subTest(value=value), self.assertRaises(QuotaError):
                self.ledger.record_qwen(value, 0, usage_reported=True)
        self.assertEqual(self.ledger.snapshot()["qwen_tokens"], 0)

    def test_ratio_is_tokens_not_calls_and_floor_is_q_divided_by_nine(self):
        self.credit(908)
        self.assertEqual(self.ledger.snapshot()["claude_available_tokens"], 100)
        reservation = self.ledger.reserve_claude(100, human_authorized=True)
        snapshot = self.ledger.settle_claude(reservation, input_tokens=10, output_tokens=20,
            cache_read_input_tokens=30, cache_creation_input_tokens=40)
        self.assertEqual(snapshot["claude_tokens"], 100)
        self.assertLessEqual(snapshot["claude_share"], 0.1)
        self.assertEqual(snapshot["claude_available_tokens"], 0)
        self.assertEqual(snapshot["claude_reserved_tokens"], 0)
        self.assertTrue(snapshot["quota_verified"])

    def test_no_authorization_is_reused_by_the_ledger(self):
        self.credit()
        for authority in (False, None, 1, "yes"):
            with self.subTest(authority=authority), self.assertRaises(QuotaError):
                self.ledger.reserve_claude(10, human_authorized=authority)
        self.assertEqual(self.ledger.snapshot()["claude_reserved_tokens"], 0)

    def test_success_releases_only_the_known_unused_reservation(self):
        self.credit()
        reservation = self.ledger.reserve_claude(80, human_authorized=True)
        snapshot = self.ledger.settle_claude(reservation, input_tokens=10, output_tokens=5)
        self.assertEqual(snapshot["claude_tokens"], 15)
        self.assertEqual(snapshot["claude_available_tokens"], 85)
        self.assertFalse(snapshot["claude_quarantined"])

    def test_settlement_is_idempotent_and_different_usage_cannot_overwrite_it(self):
        self.credit()
        reservation = self.ledger.reserve_claude(80, human_authorized=True)
        first = self.ledger.settle_claude(reservation, input_tokens=10, output_tokens=5)
        self.assertEqual(first, self.ledger.settle_claude(reservation, input_tokens=10, output_tokens=5))
        with self.assertRaises(QuotaError):
            self.ledger.settle_claude(reservation, input_tokens=0, output_tokens=0)
        self.assertEqual(self.ledger.snapshot()["claude_tokens"], 15)

    def test_oversized_reported_usage_is_recorded_and_quarantines_without_refund(self):
        self.credit()
        reservation = self.ledger.reserve_claude(80, human_authorized=True)
        snapshot = self.ledger.settle_claude(reservation, input_tokens=101, output_tokens=5)
        self.assertEqual(snapshot["claude_tokens"], 106)
        self.assertEqual(snapshot["claude_reserved_tokens"], 80)
        self.assertGreater(snapshot["claude_share"], 0.1)
        self.assertFalse(snapshot["quota_verified"])
        with self.assertRaises(QuotaError):
            TokenLedger(self.root / "project").reserve_claude(1, human_authorized=True)

    def test_unknown_or_malformed_usage_preserves_charge_and_quarantines(self):
        for reported, value in ((False, 0), (True, -1), (True, True), ("yes", 1)):
            ledger = TokenLedger(self.root / str(reported) / str(value))
            ledger.record_qwen(900, 0, True)
            reservation = ledger.reserve_claude(80, human_authorized=True)
            snapshot = ledger.settle_claude(reservation, input_tokens=value, output_tokens=0, usage_reported=reported)
            self.assertEqual(snapshot["claude_tokens"], 0)
            self.assertEqual(snapshot["claude_reserved_tokens"], 80)
            self.assertTrue(snapshot["claude_quarantined"])

    def test_timeout_does_not_recredit_and_reason_cannot_echo_secrets(self):
        self.credit()
        reservation = self.ledger.reserve_claude(80, human_authorized=True)
        snapshot = self.ledger.fail_claude(reservation, reason="SECRET_THAT_MUST_NOT_BE_STORED")
        self.assertEqual(snapshot["claude_reserved_tokens"], 80)
        self.assertEqual(snapshot["claude_tokens"], 0)
        self.assertEqual(snapshot["claude_available_tokens"], 0)
        self.assertNotIn("SECRET_THAT_MUST_NOT_BE_STORED", self.ledger.path.read_text())

    def test_restart_cannot_drop_an_inflight_reservation_or_add_a_second_review(self):
        self.credit()
        self.ledger.reserve_claude(50, human_authorized=True)
        restarted = TokenLedger(self.root / "project")
        self.assertEqual(restarted.snapshot()["claude_reserved_tokens"], 50)
        self.assertFalse(restarted.snapshot()["quota_verified"])
        with self.assertRaises(QuotaError):
            restarted.reserve_claude(20, human_authorized=True)

    def test_distinct_instances_reread_actual_disk_state(self):
        other = TokenLedger(self.root / "project")
        self.credit(900)
        self.assertEqual(other.snapshot()["qwen_tokens"], 900)
        other.record_qwen(90, 10, True)
        self.assertEqual(self.ledger.snapshot()["qwen_tokens"], 1000)
        identifier = other.reserve_claude(80, human_authorized=True)
        self.assertEqual(self.ledger.snapshot()["claude_reserved_tokens"], 80)
        self.ledger.settle_claude(identifier, input_tokens=10, output_tokens=5)
        self.assertEqual(other.snapshot()["claude_tokens"], 15)

    def test_concurrent_instances_do_not_lose_reported_usage(self):
        def record(_):
            TokenLedger(self.root / "project").record_qwen(9, 1, True)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(record, range(36)))
        self.assertEqual(self.ledger.snapshot()["qwen_tokens"], 360)
        self.assertEqual(self.ledger.snapshot()["qwen_calls"], 36)

    def test_concurrent_admission_cannot_reserve_twice_from_a_stale_snapshot(self):
        self.credit()
        def reserve(_):
            try:
                return TokenLedger(self.root / "project").reserve_claude(20, human_authorized=True)
            except QuotaError:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            identifiers = list(pool.map(reserve, range(12)))
        self.assertEqual(len([identifier for identifier in identifiers if identifier]), 1)
        self.assertEqual(self.ledger.snapshot()["claude_reserved_tokens"], 20)

    def test_os_lock_serializes_real_subprocess_writers(self):
        command = [sys.executable, "-c", "from pathlib import Path; from pcb_dev.quota import TokenLedger; import sys; ledger=TokenLedger(Path(sys.argv[1])); [ledger.record_qwen(9,1,True) for _ in range(12)]", str(self.root / "project")]
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        processes = [subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        for process in processes:
            output, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, (output + error).decode())
        self.assertEqual(self.ledger.snapshot()["qwen_tokens"], 360)

    def test_corruption_and_contradictory_totals_are_not_silently_reset(self):
        for text in ('{"schema_version":1,"schema_version":1}', "not json"):
            self.ledger.path.write_text(text)
            with self.assertRaises(QuotaError):
                TokenLedger(self.root / "project")
            self.assertEqual(self.ledger.path.read_text(), text)

    def test_usage_totals_cannot_disagree_with_persistent_settlement_records(self):
        self.credit()
        reservation = self.ledger.reserve_claude(80, human_authorized=True)
        self.ledger.settle_claude(reservation, input_tokens=10, output_tokens=5)
        data = json.loads(self.ledger.path.read_text())
        data["claude_input_tokens"] = 0
        self.ledger.path.write_text(json.dumps(data))
        with self.assertRaises(QuotaError):
            TokenLedger(self.root / "project")

    def test_projects_do_not_share_credit(self):
        self.credit()
        other = TokenLedger(self.root / "different-project")
        self.assertEqual(other.snapshot()["claude_available_tokens"], 0)


if __name__ == "__main__":
    unittest.main()

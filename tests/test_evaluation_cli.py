"""Explicit inference and test-only approval boundaries of the evaluation CLI."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.cli import main
from pcb_dev.providers import ModelProfile, ProviderConfig


class EvaluationCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = ProviderConfig(ModelProfile("served-qwen"), endpoint="https://pod.example/v1",
                                offline=False, allow_network=True)
        self.config = self.root / "qwen.json"
        self.config.write_text(json.dumps(config.to_dict()), encoding="utf-8")
        self.args = ["model-eval", "--config", str(self.config), "--output", str(self.root / "results")]

    def invoke(self, *options):
        out, errors = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(errors):
            code = main([*self.args, *options])
        return code, out.getvalue(), errors.getvalue()

    def test_test_brief_approval_requires_explicit_flag(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            main(self.args)
        self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.root / "results").exists())

    def test_remote_permission_precedes_evaluation_and_any_output(self):
        with patch("pcb_dev.evaluation.run_evaluation") as run:
            code, _, error = self.invoke("--approve-test-briefs")
        self.assertEqual(code, 2)
        self.assertIn("allow-network", error)
        run.assert_not_called()
        self.assertFalse((self.root / "results").exists())

    def test_selected_case_and_blocked_result_are_preserved(self):
        result = {"passed": False, "fabrication_ready": False}
        with patch("pcb_dev.evaluation.run_evaluation", return_value=result) as run:
            code, output, _ = self.invoke("--approve-test-briefs", "--allow-network", "--case", "ideal-divider")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), result)
        self.assertEqual(run.call_args.kwargs["case_ids"], ["ideal-divider"])
        self.assertEqual(run.call_args.kwargs["budget"].max_steps, 12)


if __name__ == "__main__":
    unittest.main()

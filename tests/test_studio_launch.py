"""Host configuration and live-request boundaries for the studio CLI."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.cli import _parser, main
from pcb_dev.providers import ModelProfile, ProviderConfig
from pcb_dev.studio_launch import configured_manager


class StudioLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def args(self, *extra):
        return _parser().parse_args(["studio", "--project", str(self.folder / "project"), *extra])

    def invoke(self, *args):
        out, errors = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(errors):
            code = main(list(args))
        return code, out.getvalue(), errors.getvalue()

    def config(self, remote=False):
        value = ProviderConfig(ModelProfile("served-qwen-model"),
            endpoint="https://selfhosted.example/v1" if remote else "http://127.0.0.1:8000/v1",
            offline=not remote, allow_network=remote)
        target = self.folder / "model.json"
        target.write_text(json.dumps(value.to_dict()), encoding="utf-8")
        return target

    def test_constructing_local_manager_does_not_invoke_inference(self):
        with patch("pcb_dev.studio_launch.ChatCompletionsProvider") as provider, patch("pcb_dev.project.ProjectManager") as manager:
            provider.return_value.network_enabled = False
            configured_manager(self.args("--model", "served-qwen-model"))
            provider.return_value.complete.assert_not_called()
            manager.assert_called_once()
            config = provider.call_args.args[0]
            self.assertTrue(config.offline)
            self.assertEqual(config.profile.model, "served-qwen-model")

    def test_remote_config_needs_host_permission_before_manager_start(self):
        with patch("pcb_dev.project.ProjectManager") as manager:
            with self.assertRaisesRegex(ValueError, "allow-network"):
                configured_manager(self.args("--model-config", str(self.config(remote=True))))
            manager.assert_not_called()

    def test_demo_cannot_enable_live_claude_or_model_credentials(self):
        for options in (("--claude-config", "claude.json"), ("--model-key-env", "SECRET_ENV")):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "demo"):
                configured_manager(self.args("--demo", *options))

    def test_template_model_refused_without_any_inference(self):
        with patch("pcb_dev.studio_launch.ChatCompletionsProvider") as provider:
            with self.assertRaisesRegex(ValueError, "template"):
                configured_manager(self.args("--model", "REPLACE_MODEL"))
            provider.assert_not_called()

    def test_missing_studio_access_token_never_echoes_secret_values(self):
        with patch.dict(os.environ, {}, clear=True):
            code, _, error = self.invoke("studio", "--project", str(self.folder / "p"), "--demo", "--auth-env", "STUDIO_KEY")
        self.assertEqual(code, 2)
        self.assertIn("access-token", error)
        self.assertFalse((self.folder / "p").exists())

    def test_model_check_is_an_explicit_challenge_and_needs_remote_permission(self):
        with patch("pcb_dev.providers.ChatCompletionsProvider") as provider:
            provider.return_value.network_enabled = True
            config = self.config(remote=True)
            code, _, _ = self.invoke("model-check", "--config", str(config))
            self.assertEqual(code, 2)
            provider.return_value.self_test.assert_not_called()
            provider.return_value.self_test.return_value = {"passed": True, "check": "tool-protocol-only", "fabrication_ready": False}
            code, output, _ = self.invoke("model-check", "--config", str(config), "--allow-network")
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(output)["fabrication_ready"])
            provider.return_value.self_test.assert_called_once()


if __name__ == "__main__":
    unittest.main()

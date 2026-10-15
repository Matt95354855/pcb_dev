"""HTTP security and user journeys with an explicitly labelled manager double."""

from contextlib import redirect_stderr
from copy import deepcopy
import http.client
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

from pcb_dev.studio import MAX_REQUEST_BYTES, make_server


class LabelledManagerDouble:
    """No provider or network: the HTTP contract is the subject under test."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.calls = []
        self.wait = None
        self.failure = False
        self.manifest = {"outputs/brief.txt"}
        self.state = {"schema_version": 1, "revision": 0, "status": "new", "demo": True,
            "provider": "demo", "brief": None, "plan": None, "conversation": [], "design": None,
            "preflight": None, "artifacts": [{"name": "Brief", "relative_path": "outputs/brief.txt"}],
            "usage": {"qwen_tokens": 0, "claude_tokens": 0, "claude_share": 0,
                      "claude_available_tokens": 0, "unknown_qwen_calls": 0},
            "claude_available": False, "fabrication_ready": False}
        (self.directory / "outputs").mkdir()
        (self.directory / "outputs/brief.txt").write_text("Brief du projet", encoding="utf-8")

    def snapshot(self):
        return deepcopy(self.state)

    def artifact_path(self, relative_path):
        if relative_path not in self.manifest:
            raise ValueError("Not in persisted manifest")
        return self.directory / relative_path

    def send_message(self, text):
        self.calls.append(("message", text))
        if self.wait:
            self.wait.wait(2)
        if self.failure:
            raise RuntimeError("provider key sk-private-secret must never reach HTTP")
        self.state.update(revision=self.state["revision"] + 1, status="brief_proposed",
            brief={"title": "Capteur USB", "summary": text, "requirements": ["USB"],
                   "assumptions": [], "questions": [], "suggestions": [], "hash": "a" * 64, "approved": False})
        self.state["conversation"].extend([{"role": "user", "content": text, "provider": "human"},
            {"role": "assistant", "content": "Un brief de démonstration.", "provider": "demo"}])
        return self.snapshot()

    def approve_brief(self, *, expected_hash):
        if self.state["brief"]["hash"] != expected_hash:
            raise ValueError("Displayed brief changed")
        self.calls.append(("approve", expected_hash))
        self.state["brief"]["approved"] = True
        self.state["revision"] += 1
        return self.snapshot()

    def request_claude_review(self, question):
        self.calls.append(("review", question))
        self.state["conversation"].append({"role": "assistant", "content": "Avis simulé explicitement.", "provider": "claude-double"})
        self.state["revision"] += 1
        return self.snapshot()


class StudioHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = LabelledManagerDouble(self.temporary.name)
        self.server = make_server(self.manager, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.worker.start()
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.origin = "http://" + self.host

    def tearDown(self):
        if self.manager.wait:
            self.manager.wait.set()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        self.temporary.cleanup()

    def request(self, method="GET", path="/api/state", *, data=None, body=None, headers=None):
        values = {"Host": self.host}
        if method == "POST":
            values.update(Origin=self.origin)
            values["Content-Type"] = "application/json"
        if data is not None:
            body = json.dumps(data).encode()
        values.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(method, path, body=body, headers=values)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def state(self):
        status, _, raw = self.request()
        self.assertEqual(status, 200)
        return json.loads(raw)

    def finished(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = self.state()
            if not state["studio"]["busy"]:
                return state
            time.sleep(.005)
        self.fail("labelled double did not finish")

    def test_startup_and_page_reads_never_call_any_model_and_deliver_csp(self):
        for path, media in [("/", "text/html"), ("/studio.js", "text/javascript"), ("/studio.css", "text/css")]:
            status, headers, raw = self.request(path=path)
            self.assertEqual(status, 200)
            self.assertIn(media, headers["Content-Type"])
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertGreater(len(raw), 100)
        self.assertEqual(self.manager.calls, [])
        self.assertTrue(self.state()["demo"])
        self.assertFalse(self.state()["fabrication_ready"])

    def test_human_chat_approval_and_explicit_review_are_separate_actions(self):
        self.assertEqual(self.request("POST", "/api/message", data={"text": "Un capteur USB"})[0], 202)
        state = self.finished()
        self.assertEqual(state["brief"]["title"], "Capteur USB")
        self.assertEqual(self.manager.calls, [("message", "Un capteur USB")])
        self.assertEqual(self.request("POST", "/api/approve", data={"brief_hash": self.state()["brief"]["hash"]})[0], 202)
        self.assertTrue(self.finished()["brief"]["approved"])
        self.assertEqual(self.request("POST", "/api/claude-review", data={"question": "Le courant USB est-il adapté ?"})[0], 202)
        state = self.finished()
        self.assertEqual(state["conversation"][-1]["provider"], "claude-double")
        self.assertEqual(self.manager.calls[-1], ("review", "Le courant USB est-il adapté ?"))

    def test_slow_provider_does_not_block_state_and_duplicate_post_is_not_billed(self):
        self.manager.wait = threading.Event()
        started = time.monotonic()
        self.assertEqual(self.request("POST", "/api/message", data={"text": "Projet"})[0], 202)
        self.assertTrue(self.state()["studio"]["busy"])
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.request("POST", "/api/message", data={"text": "Doublon"})[0], 409)
        self.manager.wait.set()
        self.finished()
        self.assertEqual(self.manager.calls, [("message", "Projet")])

    def test_provider_failure_remains_error_without_secret_or_demo_fallback(self):
        self.manager.state.update(demo=False, provider="qwen")
        # New server captures the explicitly configured mode before any call.
        with self.server._state_lock:
            self.server._snapshot = self.manager.snapshot()
        self.manager.failure = True
        self.request("POST", "/api/message", data={"text": "Projet"})
        state = self.finished()
        self.assertEqual(state["status"], "error")
        self.assertFalse(state["demo"])
        self.assertEqual(state["provider"], "qwen")
        self.assertTrue(state["studio"]["error"])
        self.assertNotIn("sk-private-secret", json.dumps(state))
        self.assertEqual(len(self.manager.calls), 1)

    def test_dns_rebinding_foreign_origin_and_form_csrf_never_reach_manager(self):
        attempts = [({"Host": "attacker.example"}, "application/json"),
                    ({"Origin": "https://attacker.example"}, "application/json"),
                    ({"Origin": "null"}, "application/json"),
                    ({"Sec-Fetch-Site": "cross-site"}, "application/json"),
                    ({}, "application/x-www-form-urlencoded")]
        for extra, content_type in attempts:
            with self.subTest(extra=extra, content_type=content_type):
                status = self.request("POST", "/api/message", data={"text": "CSRF"}, headers={**extra, "Content-Type": content_type})[0]
                self.assertIn(status, (403, 415))
        self.assertEqual(self.request(headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.manager.calls, [])

    def test_mutations_require_an_origin_even_when_content_type_is_json(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("POST", "/api/message", body=b'{"text":"CSRF"}', headers={"Host": self.host, "Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()
        self.assertEqual(self.manager.calls, [])

    def test_strict_json_and_action_arguments_fail_before_any_provider_call(self):
        bodies = [b'{"text":"first","text":"second"}', b'{"text":NaN}', b'{"text":1}', b'{"text":""}',
                  b'{"text":"ok","tool":"shell"}', b'[]', b'{"text":"truncated"', b'\xff']
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/message", body=body)[0], 400)
        self.assertEqual(self.request("POST", "/api/approve", data={"approved": True})[0], 400)
        self.assertEqual(self.request("POST", "/api/message", body=b"x" * (MAX_REQUEST_BYTES + 1))[0], 413)
        self.assertEqual(self.manager.calls, [])

    def test_artifacts_require_both_visible_allowlist_and_persisted_manifest(self):
        status, headers, raw = self.request(path="/api/artifact?path=outputs%2Fbrief.txt")
        self.assertEqual((status, raw.decode()), (200, "Brief du projet"))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.manager.manifest.clear()
        self.assertEqual(self.request(path="/api/artifact?path=outputs%2Fbrief.txt")[0], 404)
        (self.manager.directory / "private-key.txt").write_text("secret")
        self.assertEqual(self.request(path="/api/artifact?path=private-key.txt")[0], 404)

    def test_linked_and_traversal_artifacts_are_rejected_even_if_listed(self):
        outside = self.manager.directory / "private-key.txt"
        outside.write_text("secret")
        target = self.manager.directory / "outputs/brief.txt"
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            self.skipTest("platform disallows symbolic links")
        self.assertEqual(self.request(path="/api/artifact?path=outputs%2Fbrief.txt")[0], 404)
        for value in ("../private-key.txt", "/etc/passwd", "outputs/../private-key.txt", "outputs\\brief.txt"):
            self.assertEqual(self.request(path="/api/artifact?path=" + quote(value, safe=""))[0], 404)

    def test_tokens_protect_every_api_and_artifact_but_never_use_url_or_logs(self):
        self.server.auth_token = "test-private-access-token-32-chars"
        for method, path, data in [("GET", "/api/state", None), ("GET", "/api/artifact?path=outputs%2Fbrief.txt", None), ("POST", "/api/message", {"text": "Hello"}), ("POST", "/api/claude-review", {"question": "Avis"}), ("POST", "/api/approve", {"brief_hash": "a" * 64})]:
            self.assertEqual(self.request(method, path, data=data)[0], 401)
        token = self.server.auth_token
        logs = StringIO()
        with redirect_stderr(logs):
            self.assertEqual(self.request(path="/api/state?token=" + token)[0], 401)
            self.assertEqual(self.request(headers={"Authorization": "Bearer " + token})[0], 200)
            self.assertEqual(self.request(headers={"Authorization": "Bearer é"})[0], 401)
        self.assertNotIn(token, logs.getvalue())
        self.assertEqual(self.manager.calls, [])

    def test_unexpected_http_and_path_variants_cannot_invoke_actions(self):
        self.assertEqual(self.request("GET", "/api/message")[0], 404)
        self.assertEqual(self.request("POST", "/api/message?token=secret", data={"text": "Hello"})[0], 404)
        self.assertEqual(self.request("OPTIONS", "/api/message")[0], 405)
        for path in ("/../../studio.py", "/studio.js?token=secret", "/api/artifact?path=outputs%2Fbrief.txt&token=x"):
            self.assertEqual(self.request(path=path)[0], 404)
        self.assertEqual(self.manager.calls, [])

    def test_nonloopback_requires_strong_token_and_exact_https_proxy_origin(self):
        for config in ({"host": "0.0.0.0"}, {"host": "0.0.0.0", "auth_token": "a" * 32},
                       {"host": "0.0.0.0", "auth_token": "a" * 32, "public_origin": "http://studio.example"},
                       {"auth_token": "short"}, {"auth_token": "a" * 24 + "\x00"},
                       {"public_origin": "https://key:secret@studio.example"},
                       {"public_origin": "https://studio.example/path"}, {"host": "untrusted.example"}):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    make_server(self.manager, port=0, **config)
        self.server.public_origin = "https://studio.example"
        self.server.allowed_hosts = {"studio.example"}
        self.server.allowed_origins = {"https://studio.example"}
        self.server.auth_token = "a" * 32
        headers = {"Host": "studio.example", "Origin": "https://studio.example", "Authorization": "Bearer " + "a" * 32}
        self.assertEqual(self.request(headers=headers)[0], 200)
        self.assertEqual(self.request(headers={**headers, "Origin": "https://another.example"})[0], 403)

    def test_duplicate_security_headers_and_transfer_encoding_are_rejected(self):
        attempts = [
            [("Host", self.host), ("Host", "attacker.example"), ("Origin", self.origin)],
            [("Host", self.host), ("Origin", self.origin), ("Origin", "https://attacker.example")],
            [("Host", self.host), ("Origin", self.origin), ("Transfer-Encoding", "chunked")],
        ]
        for extra in attempts:
            with self.subTest(extra=extra):
                connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
                connection.putrequest("POST", "/api/message", skip_host=True)
                for key, value in extra:
                    connection.putheader(key, value)
                body = b'{"text":"must not run"}'
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(body)))
                connection.endheaders(body)
                response = connection.getresponse()
                self.assertIn(response.status, (403, 411))
                response.read()
                connection.close()
        self.assertEqual(self.manager.calls, [])

    def test_stale_browser_approval_never_approves_a_revised_brief(self):
        self.request("POST", "/api/message", data={"text": "Première idée"})
        shown = self.finished()["brief"]["hash"]
        # A different trusted controller revises the brief after this browser's
        # last state response. The server must forward the displayed hash.
        self.manager.state["brief"]["hash"] = "b" * 64
        self.manager.state["brief"]["summary"] = "Nouvelle alimentation à examiner"
        self.assertEqual(self.request("POST", "/api/approve", data={"brief_hash": shown})[0], 202)
        rejected = self.finished()
        self.assertFalse(self.manager.state["brief"]["approved"])
        self.assertEqual(rejected["brief"]["hash"], "b" * 64)
        self.assertTrue(rejected["studio"]["error"])
        self.assertFalse(any(name == "approve" for name, _ in self.manager.calls))
        for data in ({}, {"brief_hash": "not-a-hash"}, {"brief_hash": "a" * 64, "approved": True}):
            self.assertEqual(self.request("POST", "/api/approve", data=data)[0], 400)

    def test_real_explicit_demo_build_roundtrip_and_manifest_download(self):
        from pcb_dev.project import DemoManagerProvider, ProjectManager

        directory = self.manager.directory / "real-project"
        manager = ProjectManager(directory, DemoManagerProvider())
        self.server.manager = manager
        with self.server._state_lock:
            self.server._snapshot = manager.snapshot()
        self.assertEqual(self.state()["status"], "new")
        self.assertEqual(self.request("POST", "/api/message", data={"text": "Un exemple de carte"})[0], 202)
        state = self.finished()
        self.assertEqual(state["status"], "brief_proposed")
        self.assertFalse(state["brief"]["approved"])
        self.assertEqual(self.request("POST", "/api/approve", data={"brief_hash": self.state()["brief"]["hash"]})[0], 202)
        state = self.finished()
        self.assertEqual(state["status"], "review_ready")
        self.assertTrue(state["demo"])
        self.assertFalse(state["fabrication_ready"])
        self.assertGreater(len(state["artifacts"]), 5)
        artifact = next(item for item in state["artifacts"] if item["relative_path"].endswith("design.json"))
        status, _, raw = self.request(path="/api/artifact?path=" + quote(artifact["relative_path"], safe=""))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), state["design"])
        self.assertEqual(self.request(path="/api/artifact?path=project.json")[0], 404)

    def test_browser_assets_render_model_text_as_text_and_keep_credentials_in_memory(self):
        root = Path(__file__).resolve().parents[1] / "src/pcb_dev/web"
        script = (root / "studio.js").read_text()
        html = (root / "studio.html").read_text()
        self.assertIn("textContent", script)
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "localStorage", "sessionStorage", "document.write"):
            self.assertNotIn(unsafe, script)
        self.assertIn("Démonstration — aucun modèle connecté", html)
        self.assertIn('src="/studio.js"', html)
        self.assertNotIn("onclick=", html)
        self.assertNotIn("<style", html)


if __name__ == "__main__":
    unittest.main()

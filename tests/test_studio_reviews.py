"""Real manager/HTTP decision integration without any live model or network."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from pcb_dev.agent import ModelReply
from pcb_dev.project import ProjectManager
from pcb_dev.studio import make_server
from test_project import Provider, brief, call, design


class StudioReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.provider = Provider([call("brief", "propose_brief", brief()), ModelReply("Approve"),
                                  call("design", "create_design", {"design": design()}), ModelReply("Draft")])
        self.manager = ProjectManager(Path(self.temporary.name) / "project", self.provider, review_patches=True)
        self.manager.send_message("Prototype")
        self.manager.approve_brief()
        self.before = self.manager.snapshot()["design_versions"][0]["design_hash"]
        self.provider.replies = [call("patch", "apply_design_patch", {"reason": "Ajouter la valeur demandée", "operations": [{"op": "set_parameter", "block": "board", "name": "voltage", "value": 3.3}]})]
        self.proposal = self.manager.send_message("Préciser la tension")["patch_proposals"][-1]
        self.server = make_server(self.manager, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.worker.start()
        self.host = f"127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        self.temporary.cleanup()

    def request(self, path, data=None, *, headers=None):
        values = {"Host": self.host, "Origin": "http://" + self.host, "Content-Type": "application/json"}
        values.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET" if data is None else "POST", path, body=None if data is None else json.dumps(data), headers=values)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def finished(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            _, state = self.request("/api/state")
            if not state["studio"]["busy"]:
                return state
            time.sleep(.005)
        self.fail("Local decision did not finish")

    def test_human_acceptance_and_read_only_comparison_never_trigger_inference(self):
        calls = len(self.provider.calls)
        self.assertEqual(self.request("/api/patch/accept", {"proposal_hash": self.proposal["proposal_hash"]})[0], 202)
        state = self.finished()
        self.assertEqual(state["patch_proposals"][-1]["status"], "accepted")
        after = state["design_versions"][-1]["design_hash"]
        status, comparison = self.request(f"/api/versions/compare?before={self.before}&after={after}")
        self.assertEqual(status, 200)
        self.assertTrue(comparison["read_only"])
        self.assertEqual(comparison["changes"][0]["path"], "/root/parameters/voltage")
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(self.manager.snapshot(), {key: value for key, value in state.items() if key != "studio"})

    def test_rejection_requires_exact_hash_and_human_reason(self):
        for data in ({"proposal_hash": self.proposal["proposal_hash"]}, {"proposal_hash": self.proposal["proposal_hash"], "reason": ""},
                     {"proposal_hash": "not-a-hash", "reason": "No"}, {"proposal_hash": self.proposal["proposal_hash"], "reason": "No", "tool": "shell"}):
            self.assertEqual(self.request("/api/patch/reject", data)[0], 400)
        before = self.manager.snapshot()
        self.assertEqual(self.request("/api/patch/reject", {"proposal_hash": self.proposal["proposal_hash"], "reason": "Spécification à préciser"})[0], 202)
        state = self.finished()
        self.assertEqual(state["patch_proposals"][-1]["status"], "rejected")
        for key in ("design", "preflight", "usage", "design_versions"):
            self.assertEqual(state[key], before[key])

    def test_stale_or_repeated_hash_is_visible_error_without_implicit_approval(self):
        before = self.manager.snapshot()
        self.assertEqual(self.request("/api/patch/accept", {"proposal_hash": "0" * 64})[0], 202)
        failed = self.finished()
        self.assertTrue(failed["studio"]["error"])
        self.assertNotIn("journal", failed["studio"]["error"])
        self.assertEqual(self.manager.snapshot(), before)
        self.request("/api/patch/accept", {"proposal_hash": self.proposal["proposal_hash"]})
        first = self.finished()
        self.request("/api/patch/accept", {"proposal_hash": self.proposal["proposal_hash"]})
        second = self.finished()
        self.assertTrue(second["studio"]["error"])
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(len(first["design_versions"]), len(second["design_versions"]))

    def test_new_routes_require_auth_and_reject_file_paths_or_unknown_hashes(self):
        self.server.auth_token = "private-test-session-token-32-chars"
        for path, data in [("/api/patch/accept", {"proposal_hash": self.proposal["proposal_hash"]}),
                           ("/api/patch/reject", {"proposal_hash": self.proposal["proposal_hash"], "reason": "No"}),
                           (f"/api/versions/compare?before={self.before}&after={self.before}", None)]:
            self.assertEqual(self.request(path, data)[0], 401)
        auth = {"Authorization": "Bearer " + self.server.auth_token}
        for query in ("before=../../private-key&after=x", f"before={self.before}&after={'0' * 64}",
                      f"before={self.before}&after={self.before}&path=project.json"):
            self.assertEqual(self.request("/api/versions/compare?" + query, headers=auth)[0], 409)
        self.assertEqual(self.manager.snapshot()["patch_proposals"][-1]["status"], "pending")

    def test_modified_historical_snapshot_is_never_rendered_as_a_valid_comparison(self):
        version = self.manager.snapshot()["design_versions"][0]
        self.manager.artifact_path(version["relative_path"]).write_text("{}")
        status, result = self.request(f"/api/versions/compare?before={self.before}&after={self.before}")
        self.assertEqual(status, 409)
        self.assertIn("intégrité", result["error"])
        self.assertNotIn("changes", result)


if __name__ == "__main__":
    unittest.main()

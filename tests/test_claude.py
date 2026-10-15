import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.agent import ModelReply, ToolSchema
from pcb_dev.claude import ClaudeConfig, ClaudeError, ClaudeProvider
from pcb_dev.providers import ProviderError, _NoRedirect
from pcb_dev.quota import TokenLedger


URL = "https://api.anthropic.com/v1/messages"
COUNT_URL = URL + "/count_tokens"
MESSAGES = ({"role": "system", "content": "Review only; do not authorize fabrication."},
            {"role": "user", "content": "Review the declared electrical requirements."})
TOOL = ToolSchema("inspect", "Inspect a declared value", {"type": "object", "properties": {
    "value": {"type": "integer", "minimum": 0}}, "required": ["value"], "additionalProperties": False})


def message(*, usage=None, content=None, stop="end_turn"):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": "explicit-test-model",
            "content": content if content is not None else [{"type": "text", "text": "Review remains advisory."}],
            "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5} if usage is None else usage}


class Response:
    def __init__(self, data, *, url=URL, status=200, headers=None):
        self.content = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.url, self.status = url, status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.read_limits = []

    def geturl(self):
        return self.url

    def read(self, limit):
        self.read_limits.append(limit)
        return self.content[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Opener:
    def __init__(self, response=None):
        self.responses = [Response({"input_tokens": 12}, url=COUNT_URL), response or Response(message())]
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = TokenLedger(self.root / "project")
        self.ledger.record_qwen(900_000, 0, True)
        self.environment = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "SECRET_TEST_VALUE"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def provider(self, opener, **configuration):
        with patch("pcb_dev.claude.urllib.request.build_opener", return_value=opener) as builder:
            provider = ClaudeProvider(ClaudeConfig("explicit-test-model", allow_network=True, **configuration), ledger=self.ledger)
        handlers = builder.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], _NoRedirect)
        return provider

    def review(self, provider):
        return provider.review(MESSAGES, max_tokens=128, human_authorized=True)

    def test_constructor_never_requests_or_reads_a_key(self):
        opener = Opener()
        with patch.dict(os.environ, {}, clear=True):
            provider = self.provider(opener)
        self.assertEqual(opener.calls, [])
        self.assertTrue(provider.network_enabled)

    def test_configuration_roundtrips_without_credentials_and_has_no_model_default(self):
        config = ClaudeConfig("explicit-test-model")
        self.assertEqual(ClaudeConfig.from_dict(config.to_dict()), config)
        self.assertNotIn("SECRET_TEST_VALUE", str(config))
        for data in ({}, {"model": "x", "key": "secret"}, {"model": "x", "api_key_env": "sk-ant-secret"}):
            with self.subTest(data=data), self.assertRaises(ClaudeError):
                ClaudeConfig.from_dict(data)

    def test_tls_allowlist_and_plain_endpoint_path_are_required(self):
        for endpoint in ("http://api.anthropic.com", "https://attacker.example", "https://api.anthropic.com@attacker.example",
                         "https://api.anthropic.com?key=secret", "https://api.anthropic.com/v1/messages/other",
                         "https://api.anthropic.com:8443", "https://api.anthropic.com/#fragment"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ClaudeError):
                ClaudeConfig("model", endpoint=endpoint)
        config = ClaudeConfig("model", endpoint="https://review.example/v1", allowed_hosts=("review.example",))
        self.assertEqual(config.allowed_hosts, ("review.example",))

    def test_explicit_human_authority_is_required_for_each_call(self):
        opener = Opener()
        provider = self.provider(opener)
        for authority in (False, None, 1, "yes"):
            with self.subTest(authority=authority), self.assertRaises(ClaudeError):
                provider.review(MESSAGES, max_tokens=128, human_authorized=authority)
        with self.assertRaises(ClaudeError):
            provider.complete(MESSAGES, (), max_tokens=128)
        self.assertEqual(opener.calls, [])
        self.review(provider)
        with self.assertRaises(ClaudeError):
            provider.review(MESSAGES, max_tokens=128)
        self.assertEqual(len(opener.calls), 2)

    def test_disabled_network_and_zero_project_credit_prevent_even_count_request(self):
        opener = Opener()
        provider = ClaudeProvider(ClaudeConfig("model"), ledger=self.ledger)
        with self.assertRaises(ClaudeError):
            self.review(provider)
        empty = TokenLedger(self.root / "empty-project")
        provider = self.provider(opener)
        provider.ledger = empty
        with self.assertRaises(ClaudeError):
            self.review(provider)
        self.assertEqual(opener.calls, [])

    def test_native_headers_payload_and_usage_are_accounted(self):
        opener = Opener()
        provider = self.provider(opener)
        reply = self.review(provider)
        self.assertIsInstance(reply, ModelReply)
        self.assertTrue(reply.usage_reported)
        self.assertEqual((reply.input_tokens, reply.output_tokens), (10, 5))
        count, inference = [json.loads(call[0].data) for call in opener.calls]
        self.assertNotIn("max_tokens", count)
        self.assertEqual(count["messages"][0]["role"], "user")
        self.assertEqual(count["system"], MESSAGES[0]["content"])
        self.assertEqual(inference["max_tokens"], 128)
        self.assertFalse(inference["stream"])
        headers = {name.lower(): value for name, value in opener.calls[1][0].header_items()}
        self.assertEqual(headers["x-api-key"], "SECRET_TEST_VALUE")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(self.ledger.snapshot()["claude_tokens"], 15)
        self.assertEqual(self.ledger.snapshot()["qwen_tokens"], 900_000)
        self.assertFalse(provider.last_accounting["estimate_is_guarantee"])
        self.assertGreater(provider.last_accounting["reserved_tokens"], 12 + 128)

    def test_cache_aggregate_and_read_tokens_count_once_without_double_breakdown(self):
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 20,
                 "cache_creation_input_tokens": 30,
                 "cache_creation": {"ephemeral_5m_input_tokens": 11, "ephemeral_1h_input_tokens": 19}}
        reply = self.review(self.provider(Opener(Response(message(usage=usage)))))
        self.assertEqual(reply.input_tokens, 60)
        self.assertEqual(self.ledger.snapshot()["claude_tokens"], 65)
        self.assertEqual(self.ledger.snapshot()["claude_cache_creation_input_tokens"], 30)

    def test_timeout_keeps_reservation_and_blocks_restart_without_exposing_key(self):
        opener = Opener(TimeoutError("SECRET_TEST_VALUE echoed upstream"))
        provider = self.provider(opener)
        with self.assertRaises(ClaudeError) as caught:
            self.review(provider)
        self.assertNotIn("SECRET_TEST_VALUE", str(caught.exception))
        snapshot = TokenLedger(self.root / "project").snapshot()
        self.assertTrue(snapshot["claude_quarantined"])
        self.assertGreater(snapshot["claude_reserved_tokens"], 0)
        self.assertEqual(snapshot["claude_tokens"], 0)
        self.assertFalse(snapshot["quota_verified"])
        self.assertNotIn("SECRET_TEST_VALUE", self.ledger.path.read_text())
        with self.assertRaises(ClaudeError):
            self.review(self.provider(Opener()))

    def test_absent_usage_quarantines_instead_of_returning_a_false_compliant_reply(self):
        data = message()
        del data["usage"]
        with self.assertRaises(ClaudeError):
            self.review(self.provider(Opener(Response(data))))
        self.assertTrue(self.ledger.snapshot()["claude_quarantined"])
        self.assertGreater(self.ledger.snapshot()["claude_reserved_tokens"], 0)

    def test_server_usage_over_reservation_is_recorded_even_if_it_exceeds_ten_percent(self):
        usage = {"input_tokens": 200_000, "output_tokens": 5}
        with self.assertRaises(ClaudeError) as caught:
            self.review(self.provider(Opener(Response(message(usage=usage)))))
        self.assertIn("quarantined", str(caught.exception))
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot["claude_tokens"], 200_005)
        self.assertGreater(snapshot["claude_share"], 0.1)
        self.assertFalse(snapshot["quota_verified"])

    def test_response_bytes_json_duplicates_and_nonfinite_values_fail_closed(self):
        for content in (b'{"usage":{},"usage":{}}', b'{"value":NaN}', b"not-json"):
            with self.subTest(content=content):
                ledger = TokenLedger(self.root / str(len(content)))
                ledger.record_qwen(900_000, 0, True)
                provider = self.provider(Opener(Response(content)))
                provider.ledger = ledger
                with self.assertRaises(ProviderError):
                    self.review(provider)
                self.assertFalse(ledger.snapshot()["quota_verified"])

    def test_malformed_usage_and_cache_breakdown_are_not_zeroed(self):
        for usage in ({"input_tokens": True, "output_tokens": 1}, {"input_tokens": -1, "output_tokens": 1},
                      {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": "100"},
                      {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 20,
                       "cache_creation": {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 20}}):
            with self.subTest(usage=usage):
                ledger = TokenLedger(self.root / str(len(str(usage))))
                ledger.record_qwen(900_000, 0, True)
                provider = self.provider(Opener(Response(message(usage=usage))))
                provider.ledger = ledger
                with self.assertRaises(ClaudeError):
                    self.review(provider)
                self.assertTrue(ledger.snapshot()["claude_quarantined"])

    def test_tools_require_explicit_human_request_and_valid_typed_arguments(self):
        block = {"type": "tool_use", "id": "tool-1", "name": "inspect", "input": {"value": 2}}
        opener = Opener(Response(message(content=[block], stop="tool_use")))
        provider = self.provider(opener)
        reply = provider.complete(MESSAGES, (TOOL,), max_tokens=128, human_authorized=True)
        self.assertEqual(reply.tool_calls[0].name, "inspect")
        self.assertEqual(reply.tool_calls[0].arguments, {"value": 2})
        payload = json.loads(opener.calls[-1][0].data)
        self.assertEqual(payload["tools"][0]["input_schema"], TOOL.parameters)
        self.assertTrue(payload["tool_choice"]["disable_parallel_tool_use"])

    def test_manager_review_exposes_no_tools_and_rejects_unsolicited_tool_use(self):
        block = {"type": "tool_use", "id": "tool-1", "name": "inspect", "input": {"value": 2}}
        opener = Opener(Response(message(content=[block], stop="tool_use")))
        with self.assertRaises(ProviderError):
            self.review(self.provider(opener))
        self.assertNotIn("tools", json.loads(opener.calls[-1][0].data))
        self.assertEqual(self.ledger.snapshot()["claude_tokens"], 15)

    def test_unknown_tools_bad_arguments_duplicates_and_wrong_stop_are_rejected(self):
        variants = [({"type": "tool_use", "id": "tool-1", "name": "shell", "input": {}}, "tool_use"),
                    ({"type": "tool_use", "id": "tool-1", "name": "inspect", "input": {"value": -1}}, "tool_use"),
                    ({"type": "tool_use", "id": "tool-1", "name": "inspect", "input": {"value": 1}}, "end_turn"),
                    ({"type": "thinking", "thinking": "unsupported"}, "end_turn")]
        for block, stop in variants:
            with self.subTest(block=block, stop=stop):
                provider = self.provider(Opener(Response(message(content=[block], stop=stop))))
                with self.assertRaises(ProviderError):
                    provider.complete(MESSAGES, (TOOL,), max_tokens=128, human_authorized=True)

    def test_truncation_and_output_budget_overrun_are_not_successes(self):
        with self.assertRaises(ClaudeError):
            self.review(self.provider(Opener(Response(message(stop="max_tokens")))))
        provider = self.provider(Opener(Response(message(usage={"input_tokens": 1, "output_tokens": 129}))))
        with self.assertRaises(ClaudeError):
            self.review(provider)
        self.assertTrue(self.ledger.snapshot()["claude_quarantined"])

    def test_count_is_only_an_estimate_and_too_large_reservation_prevents_inference(self):
        opener = Opener()
        opener.responses[0] = Response({"input_tokens": 500_000}, url=COUNT_URL)
        with self.assertRaises(ClaudeError):
            self.review(self.provider(opener))
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.ledger.snapshot()["claude_reserved_tokens"], 0)

    def test_transport_rejects_redirect_destinations_and_does_not_log_error_bodies(self):
        opener = Opener(Response(message(), url="https://attacker.example/messages"))
        with self.assertRaises(ClaudeError):
            self.review(self.provider(opener))
        self.assertTrue(self.ledger.snapshot()["claude_quarantined"])

    def test_bad_count_response_does_not_claim_an_inference_was_charged(self):
        opener = Opener()
        opener.responses[0] = Response({"input_tokens": True}, url=COUNT_URL)
        with self.assertRaises(ClaudeError):
            self.review(self.provider(opener))
        self.assertEqual(self.ledger.snapshot()["claude_reserved_tokens"], 0)
        self.assertTrue(self.ledger.snapshot()["quota_verified"])

    def test_request_limit_never_leaves_an_unnecessary_reservation(self):
        opener = Opener()
        provider = self.provider(opener, max_requests=1)
        with self.assertRaises(ClaudeError):
            self.review(provider)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.ledger.snapshot()["claude_reserved_tokens"], 0)

    def test_missing_key_does_not_open_socket(self):
        opener = Opener()
        provider = self.provider(opener)
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ClaudeError):
            self.review(provider)
        self.assertEqual(opener.calls, [])


if __name__ == "__main__":
    unittest.main()
